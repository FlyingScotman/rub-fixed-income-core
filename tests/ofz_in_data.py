"""Loaders for the OFZ-IN golden fixtures (``tests/fixtures/ofz_in/README.md``).

Instruments are built from the raw ISS responses as a backend adapter would, and the
CPI series is the Bank of Russia's month-on-month CPI for Russia chained into levels.
The xlsx is read with the standard library (zip + XML) so the tests need no extra
dependency.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from rub_fixed_income import (
    CpiIndexation,
    CpiObservation,
    CpiSeries,
    DayCount,
    IndexedCouponLeg,
    IndexedCouponPeriod,
    IndexedPrincipalLeg,
    Instrument,
    ProductType,
)
from rub_fixed_income.inflation import add_months

FIXTURES = Path(__file__).parent / "fixtures" / "ofz_in"
LINKERS = ("SU52002RMFS1", "SU52003RMFS9", "SU52004RMFS7", "SU52005RMFS4")
CPI_ID = "RU_CPI_CBR_CHAINED"
# Last-Modified of reg_cpd.xlsx as served by www.cbr.ru (README): the series' as-of date.
CPI_AS_OF = date(2026, 10, 14)
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(block["columns"], row, strict=True)) for row in block["data"]]


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref)
    assert letters is not None
    index = 0
    for char in letters.group(0):
        index = index * 26 + ord(char) - 64
    return index - 1


def read_xlsx_sheet(path: Path, sheet_name: str) -> list[list[object]]:
    """Cell values of one worksheet (numbers as float, shared strings as str)."""
    with zipfile.ZipFile(path) as book:
        strings = [
            "".join(t.text or "" for t in item.iter(f"{{{_NS['m']}}}t"))
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).findall("m:si", _NS)
        ]
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        rel_id = next(
            s.attrib[_REL]
            for s in workbook.iterfind("m:sheets/m:sheet", _NS)
            if s.attrib["name"] == sheet_name
        )
        rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        target = next(r.attrib["Target"] for r in rels if r.attrib["Id"] == rel_id)
        sheet = ET.fromstring(book.read("xl/" + target.lstrip("/").removeprefix("xl/")))
    table: list[list[object]] = []
    for row in sheet.iter(f"{{{_NS['m']}}}row"):
        values: list[object] = []
        for cell in row.findall("m:c", _NS):
            column = _column(cell.attrib["r"])
            values.extend([None] * (column - len(values)))
            raw = cell.find("m:v", _NS)
            text = None if raw is None else raw.text
            if text is None:
                values.append(None)
            elif cell.attrib.get("t") == "s":
                values.append(strings[int(text)])
            elif cell.attrib.get("t") == "str":
                values.append(text)
            else:
                values.append(float(text))
        table.append(values)
    return table


@cache
def cbr_month_on_month() -> dict[date, float]:
    """Russia's CPI month on month in percent, sheet ``мм`` of ``reg_cpd.xlsx``."""
    table = read_xlsx_sheet(FIXTURES / "cbr" / "reg_cpd.xlsx", "мм")
    header = table[0]
    russia = next(r for r in table if r and r[0] == "Россия")
    out: dict[date, float] = {}
    for serial, value in zip(header[1:], russia[1:], strict=False):
        if serial is None or value is None:
            continue
        assert isinstance(serial, float) and isinstance(value, float)
        day = date(1899, 12, 30) + timedelta(days=int(serial))
        out[day.replace(day=1)] = value
    return out


@cache
def cbr_cpi_series() -> CpiSeries:
    """Levels chained from the 2-decimal m/m figures, 100 at December 2015."""
    month = date(2015, 12, 1)
    observations = [CpiObservation(month, 100.0, CPI_AS_OF)]
    level = 100.0
    for when, change in sorted(cbr_month_on_month().items()):
        month = add_months(month, 1)
        assert when == month
        level *= 1.0 + change / 100.0
        observations.append(CpiObservation(month, level, CPI_AS_OF))
    return CpiSeries(
        CPI_ID, "cbr.ru reg_cpd.xlsx (Rosstat m/m), chained", CPI_AS_OF, tuple(observations)
    )


def _json(*parts: str) -> dict[str, Any]:
    return dict(json.loads(FIXTURES.joinpath(*parts).read_text("utf-8")))


def description(secid: str) -> dict[str, str]:
    return {r[0]: r[2] for r in _json("securities", f"{secid}.json")["description"]["data"]}


def bondization(secid: str) -> dict[str, Any]:
    return _json("bondization", f"{secid}.json")


@cache
def history(secid: str) -> tuple[dict[str, Any], ...]:
    """Every EOD row of the bond on TQOB, by trade date (paged ISS responses joined)."""
    by_date: dict[str, dict[str, Any]] = {}
    for path in sorted((FIXTURES / "history").glob(f"{secid}_*.json")):
        for row in rows(json.loads(path.read_text("utf-8"))["history"]):
            by_date[row["TRADEDATE"]] = row
    return tuple(by_date[k] for k in sorted(by_date))


def board(trade_date: str = "2026-10-16") -> dict[str, dict[str, Any]]:
    return {r["SECID"]: r for r in rows(_json("board", f"TQOB_{trade_date}.json")["history"])}


def linker(secid: str, *, lag_months: int = 3) -> Instrument:
    """An OFZ-IN from ISS: base = placement date (ISSUEDATE), 2.5 % on the indexed face."""
    info = description(secid)
    raw = bondization(secid)
    periods = tuple(
        IndexedCouponPeriod(
            date.fromisoformat(c["startdate"]),
            date.fromisoformat(c["coupondate"]),
            date.fromisoformat(c["coupondate"]),
            c["valueprc"] / 100.0,
        )
        for c in rows(raw["coupons"])
    )
    indexation = CpiIndexation(CPI_ID, date.fromisoformat(info["ISSUEDATE"]), lag_months)
    return Instrument(
        secid,
        ProductType.INFLATION_LINKED_BOND,
        "RUB",
        float(info["INITIALFACEVALUE"]),
        IndexedCouponLeg(periods, DayCount.ACT_365_FIXED),
        IndexedPrincipalLeg(date.fromisoformat(info["MATDATE"]), indexation, True),
        "MOEX_STOCK",
        isin=info["ISIN"],
    )


__all__ = [
    "CPI_AS_OF",
    "CPI_ID",
    "FIXTURES",
    "LINKERS",
    "board",
    "bondization",
    "cbr_cpi_series",
    "cbr_month_on_month",
    "description",
    "history",
    "linker",
    "read_xlsx_sheet",
    "rows",
]
