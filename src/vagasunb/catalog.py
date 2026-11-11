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
    from sqlalchemy import and_, case, func, or_, cast
    from sqlalchemy.dialects.postgresql import JSONB

    if len(query) > 120 or not 0 <= page <= 10000:
        raise ValueError("Busca deve ter até 120 caracteres")
    filters = [Classroom.period == period]
    if campus:
        filters.append(Classroom.campus == campus if campus != "?" else Classroom.campus.is_(None))
    if vacancies:
        filters.extend([Classroom.available > 0, Classroom.missing == 0])
    if unit:
        filters.append(cast(Classroom.units, JSONB).contains([unit]))
    q = normalize(query)
    code = func.regexp_replace(func.lower(Classroom.code), r"([a-z]+)0+([0-9])", r"\1\2", "g")
    rank = 4
    if q:
        exact = or_(code == code_key(q), Classroom.search == q)
        parts = q.split()
        pair = (
            and_(
                code == code_key(parts[0]),
                func.ltrim(func.lower(Classroom.number), "0") == parts[1].lstrip("0"),
            )
            if len(parts) == 2
            else False
        )
        prefix = code.startswith(code_key(q), autoescape=True)
        terms = and_(*(Classroom.search.contains(p, autoescape=True) for p in parts))
        filters.append(or_(exact, pair, prefix, terms))
        rank = case((exact, 0), (pair, 1), (prefix, 2), else_=3)
    stmt = select(Classroom).where(*filters)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    approximate = False
    if not total and q:
        filters = filters[:-1] + [func.similarity(Classroom.search, q) >= 0.3]
        stmt = select(Classroom).where(*filters)
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rank = -func.similarity(Classroom.search, q)
        approximate = bool(total)
    order = [rank] if not isinstance(rank, int) else []
    rows = (
        await session.scalars(
            stmt.order_by(*order, Classroom.code, Classroom.number, Classroom.id)
            .offset(page * 6)
            .limit(6)
        )
    ).all()
    return list(rows), total, approximate
