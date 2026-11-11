"""Public-only JSF flow adapted from Followw UnB (MIT); see THIRD_PARTY_NOTICES.

No authenticated client, academic credentials or persistent cookies are supported.
"""

import asyncio
import random
import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from vagasunb.catalog import normalize

BASE = "https://sigaa.unb.br"
HOME = "/sigaa/public/home.jsf"
PATH = "/sigaa/public/turmas/listar.jsf"


class SourceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_results(html, unit, period):
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
    errors = [normalize(x.get_text(" ", strip=True)) for x in soup.select("ul.erros li")]
    if errors:
        if all("nao foram encontrados resultados" in x for x in errors):
            return []
        raise SourceError("search_rejected")
    table = soup.select_one("table.listagem")
    if not table:
        raise SourceError("missing_table")
    headers = normalize(" ".join(th.get_text(" ", strip=True) for th in table.select("th")))
    if not (
        ("turma" in headers or "codigo" in headers) and "vagas" in headers and "ocupadas" in headers
    ):
        raise SourceError("changed_header")
    code = name = None
    found = {}
    for row in table.select("tr"):
        classes = row.get("class", [])
        if "agrupador" in classes:
            title = row.select_one(".tituloDisciplina")
            if not title or " - " not in title.get_text():
                raise SourceError("invalid_subject")
            code, name = title.get_text(" ", strip=True).split(" - ", 1)
            if not re.fullmatch(r"[A-Za-z0-9]+", code):
                raise SourceError("invalid_code")
        elif set(classes) & {"linhaPar", "linhaImpar"}:
            cells = row.find_all("td", recursive=False)
            if not code or len(cells) != 8:
                raise SourceError("malformed_row")
            fields = [c.get_text(" ", strip=True) for c in cells]
            if fields[1] != period or not fields[0]:
                raise SourceError("period_mismatch")
            offered = int(fields[5]) if fields[5].isdigit() else None
            occupied = int(fields[6]) if fields[6].isdigit() else None
            # Public SIGAA can report over-enrolment. Preserve the actual counts;
            # the detector treats this row as UNKNOWN, never a negative/zero vacancy.
            data = dict(
                source_id=row.get("data-id-turma"),
                code=code,
                name=name,
                number=fields[0],
                period=period,
                units=[unit],
                teachers=re.sub(r"\s*\(\d+h\)", "", fields[2]) or None,
                schedule=fields[3] or None,
                location=fields[7] or None,
                offered=offered,
                occupied=occupied,
                campus=None,
            )
            key = (code, fields[0], period)
            if key in found and found[key] != data:
                raise SourceError("identity_conflict")
            found[key] = data
    if not found:
        raise SourceError("unconfirmed_empty")
    return list(found.values())


class PublicSource:
    complete = True
    kind = "public"

    def __init__(self, transport=None):
        self.serial = asyncio.Lock()
        self.http = httpx.AsyncClient(
            base_url=BASE, transport=transport, timeout=20, follow_redirects=False, trust_env=False
        )
        self.failures = self.requests = self.bytes = self.retries = self.limited = 0
        self.sessions = 0
        self.next_attempt = self.last_request = self.parse_ms = 0.0
        self.min_interval = 0 if transport else 1.0
        self.form = None
        self.warm = False
        self.metadata = None
        self.metadata_at = 0.0

    async def aclose(self):
        await self.http.aclose()

    async def request(self, method, path, **kwargs):
        if time.monotonic() < self.next_attempt:
            raise SourceError("circuit_open")
        await asyncio.sleep(max(0, self.min_interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.requests += 1
        response = await self.http.request(method, path, **kwargs)
        self.bytes += response.num_bytes_downloaded + len(response.request.content)
        if response.status_code == 429:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone

            retry = response.headers.get("Retry-After", "60")
            try:
                delay = (
                    int(retry)
                    if retry.isdigit()
                    else (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                )
            except (ValueError, TypeError):
                delay = 300
            self.limited += 1
            self.next_attempt = time.monotonic() + min(86400, max(1, delay))
            raise SourceError("rate_limit")
        if response.status_code in {401, 403} or any(
            word in response.text.lower() for word in ("captcha", "access denied")
        ):
            self.next_attempt = time.monotonic() + 3600
            raise SourceError("access_restricted")
        if response.is_redirect:
            if urljoin(BASE, response.headers.get("location", "")) != BASE + HOME:
                raise SourceError("unexpected_redirect")
            raise SourceError("session_expired")
        if response.status_code != 200:
            raise SourceError("http_unavailable")
        return response

    def read_form(self, html):
        soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
        form = soup.find("form", id="formTurma")
        if not form:
            raise SourceError("changed_form")
        payload = {}
        for item in form.select("input[name],select[name]"):
            if item.name == "select":
                selected = item.select_one("option[selected]") or item.select_one("option")
                payload[item["name"]] = selected.get("value", "") if selected else ""
            elif item.get("type") != "submit":
                payload[item["name"]] = item.get("value", "")
        required = {
            "formTurma:inputNivel",
            "formTurma:inputDepto",
            "formTurma:inputAno",
            "formTurma:inputPeriodo",
            "javax.faces.ViewState",
        }
        button = form.find("input", attrs={"type": "submit", "value": "Buscar"})
        action = urljoin(BASE, form.get("action", ""))
        if (
            not required.issubset(payload)
            or not payload["javax.faces.ViewState"]
            or not button
            or not button.get("name")
            or action != BASE + PATH
        ):
            raise SourceError("changed_form")

        def choices(field):
            select = form.find("select", attrs={"name": field})
            if not select:
                raise SourceError("changed_form")
            return [
                {"id": str(o.get("value", "")), "name": o.get_text(" ", strip=True)}
                for o in select.select("option")
            ]

        units = [
            v for v in choices("formTurma:inputDepto") if v["id"].isdigit() and int(v["id"]) > 0
        ]
        levels = [
            v
            for v in choices("formTurma:inputNivel")
            if v["id"] and "selecione" not in normalize(v["name"])
        ]
        everyone = [v for v in choices("formTurma:inputNivel") if "todos" in normalize(v["name"])]
        if everyone:
            levels = everyone[:1]
        if not units or not levels:
            raise SourceError("empty_discovery")
        metadata = {
            "units": units,
            "levels": levels,
            "period": payload["formTurma:inputAno"] + "." + payload["formTurma:inputPeriodo"],
        }
        return (action, payload, str(button["name"])), metadata

    async def ensure_form(self):
        if not self.warm:
            await self.request("GET", HOME)
            self.sessions += 1
            self.warm = True
        if self.form is None:
            response = await self.request("GET", PATH)
            self.form, self.metadata = self.read_form(response.text)
            self.metadata_at = time.monotonic()

    async def discover(self):
        async with self.serial:
            if not self.metadata or time.monotonic() - self.metadata_at > 21600:
                self.form = None
                await self.ensure_form()
            return self.metadata

    async def collect(self, unit, period, level="G"):
        if not re.fullmatch(r"\d{4}\.\d", period):
            raise SourceError("invalid_period")
        async with self.serial:
            for attempt in range(3):
                try:
                    await self.ensure_form()
                    if unit not in {u["id"] for u in self.metadata["units"]} or level not in {
                        v["id"] for v in self.metadata["levels"]
                    }:
                        raise SourceError("unknown_scope")
                    action, defaults, button = self.form
                    payload = dict(defaults)
                    year, term = period.split(".")
                    payload.update(
                        {
                            "formTurma:inputNivel": level,
                            "formTurma:inputDepto": unit,
                            "formTurma:inputAno": year,
                            "formTurma:inputPeriodo": term,
                            button: "Buscar",
                        }
                    )
                    response = await self.request("POST", action, data=payload)
                    start = time.perf_counter()
                    soup = BeautifulSoup(response.text, "html.parser")
                    rows = parse_results(soup, unit, period)
                    unit_name = next(u["name"] for u in self.metadata["units"] if u["id"] == unit)
                    for row in rows:
                        row["provenance"] = "SIGAA público · " + unit_name
                    try:
                        self.form, _ = self.read_form(soup)
                    except SourceError:
                        self.form = None  # No proven reusable form: reopen it, keep warm session.
                    self.parse_ms += (time.perf_counter() - start) * 1000
                    self.failures = 0
                    return rows
                except SourceError as exc:
                    if exc.code not in {"session_expired", "http_unavailable"}:
                        raise
                    self.warm = False
                    self.form = None
                    self.http.cookies.clear()
                    if attempt == 2:
                        self.failures += 1
                        self.next_attempt = time.monotonic() + min(300, 2**self.failures)
                        raise
                except (httpx.TimeoutException, httpx.NetworkError):
                    self.form = None
                    if attempt == 2:
                        self.failures += 1
                        self.next_attempt = time.monotonic() + min(300, 2**self.failures)
                        raise SourceError("network_unavailable") from None
                self.retries += 1
                await asyncio.sleep(0 if self.min_interval == 0 else 2**attempt + random.random())
