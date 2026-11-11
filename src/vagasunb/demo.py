from sqlalchemy import select

from vagasunb.db import Classroom, Setting
from vagasunb.monitor import observe
from vagasunb.services import change_options


def fixtures(period="DEMO.1"):
    names = [
        "Cálculo experimental",
        "Computação sintética",
        "Física de demonstração",
        "Álgebra simulada",
        "Estatística fictícia",
        "Química de teste",
        "Lógica demonstrativa",
        "Redes sintéticas",
        "Sistemas de teste",
        "Projeto fictício",
        "Ética demonstrativa",
        "Pesquisa sintética",
    ]
    return [
        dict(
            code=f"DEMO{i + 1:04d}",
            name=name,
            number=f"{number:02d}",
            period=period,
            units=["Unidade sintética A" if i % 2 == 0 else "Unidade sintética B"],
            campus="Campus demonstração" if i % 3 else None,
            provenance="fixture sintética v1",
            teachers="Docente sintético",
            schedule="Segunda, 08h–10h (simulado)",
            location="Sala fictícia",
            offered=30,
            occupied=30,
        )
        for i, name in enumerate(names)
        for number in (1, 2)
    ]


async def seed(s, mode):
    if mode != "demo":
        return
    if not await s.scalar(select(Classroom.id).limit(1)):
        await observe(s, fixtures(), "demo")
        s.add(Setting(key="demo_source", value={"rows": fixtures()}))
        await change_options(
            s, {"period": "DEMO.1", "scopes": ["Unidade sintética A", "Unidade sintética B"]}
        )


class DemoSource:
    complete = True
    kind = "demo"

    def __init__(self, db):
        self.db = db

    async def discover(self):
        async with self.db.transaction() as s:
            row = await s.get(Setting, "demo_source")
            units = sorted({u for r in row.value["rows"] for u in r["units"]}) if row else []
            return {
                "units": [{"id": u, "name": u} for u in units],
                "levels": [{"id": "G", "name": "Sintético"}],
            }

    async def collect(self, scope, period, level="G"):
        async with self.db.transaction() as s:
            cfg = await s.get(Setting, "demo_source")
            return [r for r in cfg.value["rows"] if scope in r["units"] and r["period"] == period]
