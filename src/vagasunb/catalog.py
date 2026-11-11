import difflib
import re
import unicodedata

from sqlalchemy import select

from vagasunb.db import Classroom


def normalize(value):
    return " ".join(
        "".join(
            c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c)
        ).split()
    )


def code_key(value):
    return re.sub(r"([a-z]+)0+(\d)", r"\1\2", normalize(value))


async def search(session, query, period, page=0, campus=None, unit=None, vacancies=False):
    if len(query) > 120 or page < 0 or page > 10000:
        raise ValueError("Busca deve ter até 120 caracteres")
    stmt = select(Classroom).where(Classroom.period == period)
    if campus:
        stmt = (
            stmt.where(Classroom.campus == campus)
            if campus != "?"
            else stmt.where(Classroom.campus.is_(None))
        )
    if vacancies:
        stmt = stmt.where(Classroom.available > 0)
    # Bounded catalog; ranking is accent-insensitive and code-preserving. No remote queries.
    rows = list((await session.scalars(stmt.order_by(Classroom.code, Classroom.number))).all())
    if unit:
        rows = [c for c in rows if unit in c.units]
    q = normalize(query)
    parts = q.split()
    ranked = []
    for c in rows:
        ck = code_key(c.code)
        rank = None
        if not q:
            rank = 4
        elif q == normalize(c.name) or code_key(q) == ck:
            rank = 0
        elif (
            len(parts) == 2
            and code_key(parts[0]) == ck
            and parts[1].isdigit()
            and c.number.isdigit()
            and int(parts[1]) == int(c.number)
        ):
            rank = 1
        elif ck.startswith(code_key(q)):
            rank = 2
        elif all(p in c.search for p in parts):
            rank = 3
        if rank is not None:
            ranked.append((rank, c.code, c.number, c))
    approximate = False
    if not ranked and q:
        choices = difflib.get_close_matches(
            q, sorted({normalize(c.name) for c in rows}), n=3, cutoff=0.65
        )
        ranked = [(5, c.code, c.number, c) for c in rows if normalize(c.name) in choices]
        approximate = bool(ranked)
    ranked.sort(key=lambda x: x[:3])
    return [r[3] for r in ranked[page * 6 : (page + 1) * 6]], len(ranked), approximate
