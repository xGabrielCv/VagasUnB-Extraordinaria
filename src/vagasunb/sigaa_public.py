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
    soup = BeautifulSoup(html, "html.parser")
    errors = [normalize(x.get_text(" ", strip=True)) for x in soup.select("ul.erros li")]
    if errors:
        if all("nao foram encontrados resultados" in x for x in errors):
            return []
        raise SourceError("search_rejected")
    table = soup.select_one("table.listagem")
    if not table:
        raise SourceError("missing_table")
    headers = normalize(" ".join(th.get_text(" ", strip=True) for th in table.select("th")))
    if not ("turma" in headers and "vagas" in headers and "ocupadas" in headers):
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
            if offered is not None and occupied is not None and occupied > offered:
                raise SourceError("inconsistent_counts")
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
    def __init__(self, transport=None):
        self.transport = transport
        self.serial = asyncio.Lock()
        self.failures = 0
        self.next_attempt = 0.0
        self.last_request = 0.0
        self.min_interval = 0 if transport else 1.0
        self.requests = 0

    async def collect(self, unit, period):
        if time.monotonic() < self.next_attempt:
            raise SourceError("circuit_open")
        if not re.fullmatch(r"\d{4}\.\d", period):
            raise SourceError("invalid_period")
        async with self.serial:
            # An isolated anonymous session for each scope prevents ViewState cross-talk.
            async with httpx.AsyncClient(
                base_url=BASE,
                transport=self.transport,
                timeout=20,
                follow_redirects=False,
                trust_env=False,
            ) as http:
                for attempt in range(3):
                    try:
                        result = await self._attempt(http, unit, period)
                        self.failures = 0
                        self.next_attempt = 0
                        return result
                    except SourceError as exc:
                        if exc.code in {"rate_limit", "access_restricted"}:
                            raise
                        http.cookies.clear()
                        if exc.code != "session_expired":
                            break
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt < 2:
                            await asyncio.sleep(2**attempt + random.random())
                self.failures += 1
                self.next_attempt = time.monotonic() + min(300, 2**self.failures)
                raise SourceError("unavailable_or_changed")

    async def request(self, http, method, path, **kwargs):
        await asyncio.sleep(max(0, self.min_interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.requests += 1
        response = await http.request(method, path, **kwargs)
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "60")
            delay = min(86400, max(1, int(retry))) if retry.isdigit() else 300
            self.next_attempt = time.monotonic() + delay
            raise SourceError("rate_limit")
        if response.status_code in {401, 403}:
            self.next_attempt = time.monotonic() + 3600
            raise SourceError("access_restricted")
        if response.is_redirect:
            location = urljoin(BASE, response.headers.get("location", ""))
            if location != BASE + HOME:
                raise SourceError("unexpected_redirect")
            raise SourceError("session_expired")
        if response.status_code != 200:
            raise SourceError("http_unavailable")
        return response

    async def _attempt(self, http, unit, period):
        await self.request(http, "GET", HOME)
        page = await self.request(http, "GET", PATH)
        soup = BeautifulSoup(page.text, "html.parser")
        form = soup.find("form", id="formTurma")
        if not form:
            raise SourceError("changed_form")
        names = {
            "formTurma:inputNivel",
            "formTurma:inputDepto",
            "formTurma:inputAno",
            "formTurma:inputPeriodo",
            "javax.faces.ViewState",
        }
        payload = {}
        for item in form.select("input[name], select[name]"):
            if item.name == "select":
                chosen = item.select_one("option[selected]") or item.select_one("option")
                payload[item["name"]] = chosen.get("value", "") if chosen else ""
            elif item.get("type") != "submit":
                payload[item["name"]] = item.get("value", "")
        button = form.find("input", attrs={"type": "submit", "value": "Buscar"})
        if (
            not names.issubset(payload)
            or not payload["javax.faces.ViewState"]
            or not button
            or not button.get("name")
        ):
            raise SourceError("changed_form")
        unit_select = form.find("select", attrs={"name": "formTurma:inputDepto"})
        if not unit_select.find("option", attrs={"value": unit}):
            raise SourceError("unknown_unit")
        year, term = period.split(".")
        payload.update(
            {
                "formTurma:inputNivel": "G",
                "formTurma:inputDepto": unit,
                "formTurma:inputAno": year,
                "formTurma:inputPeriodo": term,
                button["name"]: "Buscar",
            }
        )
        action = urljoin(BASE, form.get("action", ""))
        if action != BASE + PATH:
            raise SourceError("unsafe_action")
        response = await self.request(http, "POST", action, data=payload)
        return parse_results(response.text, unit, period)
