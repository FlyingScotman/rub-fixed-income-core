#!/usr/bin/env python3
# ruff: noqa: BLE001, I001
"""Standalone, evidence-preserving QR/Calypso bond reconciliation probe.

Copy this file to a Windows desk machine and run it with the proprietary stack's
Python.  The module deliberately has no third-party imports at import time so its
data handling and archive helpers can also be tested away from the desk.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import zipfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

INPUT_ROWS: tuple[tuple[str, str, float, float | None, float | None], ...] = (
    ("Атомэнпр07", "RU000A10C6L5", 97.46, 16.04, 124),
    ("Атомэнпр12", "RU000A10F7L0", 96.73, 15.95, 142),
    ("МТС 2P-11", "RU000A10BP79", 100.75, 13.75, -106),
    ("ПСБ 3P-09", "RU000A109TJ6", 100.11, None, 147),
    ("Сбер Sb51R", "RU000A10DS74", 101.00, 15.09, 68),
    ("ВЭБ2Р-58", "RU000A10F504", 99.83, None, 219),
    ("ГТЛК 1P-03", "RU000A0JXE06", 97.82, None, 538),
    ("РусГид2Р07", "RU000A10CC24", 99.27, 15.19, 94),
    ("Сбер Sb52R", "RU000A10EF52", 100.18, 15.03, 65),
    ("Роснфт2P3", "RU000A0ZYLG5", 101.00, 14.50, 24),
    ("ГазКап3P29", "RU000A10FZ32", 99.85, None, 191),
    ("iСелктл1Р8", "RU000A10FMY2", 102.13, 16.47, 192),
    ("ЧеркизБ2P5", "RU000A10FUU3", 99.90, None, 175),
    ("РостелP15R", "RU000A10B214", 102.39, 14.41, -20),
    ("Систем1P29", "RU000A108GL1", 89.29, None, 1180),
    ("ЭталонФин2", "RU000A109SP5", 90.01, None, 1699),
    ("ПочтаР3P03", "RU000A10EA16", 101.00, 18.67, 414),
    ("ДОМ 2P5", "RU000A109U97", 100.12, None, 69),
    ("ОДК1Р03", "RU000A10FG76", 99.90, 17.86, 333),
    ("РусГид2Р08", "RU000A10D3C0", 101.91, 15.32, 102),
    ("ЯНДЕКС1Р2", "RU000A10CMT9", 99.38, 14.85, 59),
    ("ИКС5Фи3P17", "RU000A10E6V2", 100.36, 15.14, 66),
    ("Атомэнпр11", "RU000A10EJQ7", 99.27, 15.82, 121),
    ("ФосАгро2П6", "RU000A10ER66", 98.41, 15.40, 97),
    ("БалтЛизП14", "RU000A10A3W1", 95.47, None, 2967),
    ("iФабрик1Р1", "RU000A10FX18", 99.49, 19.07, 471),
    ("Брус 2Р09", "RU000A10FWA1", 99.99, 24.06, 952),
    ("СТМ 1P4", "RU000A1082Y8", 101.34, 18.42, 400),
    ("РСХБ2Р3", "RU000A107S10", 100.19, None, 112),
    ("ПГК3Р04", "RU000A10EYV6", 98.82, 17.14, 256),
)

DEFAULT_CURVE_DATE = "2026-09-03"
DEFAULT_CALCULATION_DATES = ("2026-09-03", "2026-09-04", "2026-09-07")
DEFAULT_PUBLICATION = "CALYPSO LIVE DATA"
DEFAULT_OUTPUT_DIR = "qr_reconciliation_20260903"
ISIN_RE = re.compile(r"^RU[A-Z0-9]{10}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_bytes(value: Any) -> bytes:
    """Accept a direct payload or the desk API's singleton-list wrapper."""
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise ValueError("expected exactly one serialized product payload")
        value = value[0]
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if not isinstance(value, bytes):
        raise TypeError("serialized product must be bytes or singleton [bytes]")
    if not value:
        raise ValueError("serialized product is empty")
    return value


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def parse_isin_filters(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    result: list[str] = []
    for value in values:
        for item in value.split(","):
            isin = item.strip().upper()
            if not ISIN_RE.fullmatch(isin):
                raise argparse.ArgumentTypeError(f"invalid ISIN filter: {item!r}")
            if isin not in result:
                result.append(isin)
    known = {row[1] for row in INPUT_ROWS}
    unknown = [isin for isin in result if isin not in known]
    if unknown:
        raise argparse.ArgumentTypeError(
            "ISIN not present in the 30-row input: " + ", ".join(unknown)
        )
    return tuple(result)


def json_safe(value: Any, depth: int = 0) -> Any:
    """Convert proprietary values without leaking arbitrary object repr strings."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"non_finite": str(value)}
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if depth >= 6:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v, depth + 1) for v in value]
    if isinstance(value, set):
        return sorted((json_safe(v, depth + 1) for v in value), key=lambda x: str(x))
    # Preserve useful NumPy/proprietary array diagnostics without exposing reprs.
    try:
        tolist = getattr(value, "tolist", None)
    except Exception:
        tolist = None
    if callable(tolist):
        try:
            listed = tolist()
        except Exception:
            listed_ok = False
        else:
            listed_ok = True
        if listed_ok:
            return json_safe(listed, depth + 1)
    # Permit numeric wrappers while deliberately avoiding repr(value).
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return {"type": type(value).__name__}
    return number if math.isfinite(number) else {"non_finite": str(number)}


def error_record(exc: BaseException) -> dict[str, str]:
    # Exception text can contain server/user information.  The class is sufficient
    # for the runtime log; a short scrubbed message is useful for overload diagnosis.
    message = str(exc).replace(os.environ.get("CALYPSO_LOGIN", "\0"), "<redacted>")
    password = os.environ.get("CALYPSO_PASSWORD")
    if password:
        message = message.replace(password, "<redacted>")
    message = re.sub(r"(?i)(password|login|user(name)?)\s*[=:]\s*\S+", r"\1=<redacted>", message)
    return {"status": "error", "error_type": type(exc).__name__, "message": message[:500]}


def safe_module_location(module: Any) -> str | None:
    """Report a useful location while avoiding workstation account names."""
    filename = getattr(module, "__file__", None)
    if not filename:
        return None
    path = Path(filename).resolve()
    home = Path.home().resolve()
    try:
        return "<home>/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def attempt(label: str, function: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"label": label, "status": "ok", "value": json_safe(function())}
    except Exception as exc:  # proprietary bindings raise many non-standard subclasses
        return {"label": label, **error_record(exc)}


def numeric_attempt(
    vtb_qr: Any, label: str, function: Callable[[], Any]
) -> tuple[dict[str, Any], float | None]:
    """Execute a QR numeric call and unwrap its untouched proprietary result."""
    try:
        raw = function()
        number = qr_scalar(vtb_qr, raw, label=label)
        return {"label": label, "status": "ok", "value": number}, number
    except Exception as exc:  # proprietary bindings raise many non-standard subclasses
        return {"label": label, **error_record(exc)}, None


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig"
    )
    path.chmod(0o600)


def write_jsonl(path: Path, records: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")
    path.chmod(0o600)


CSV_FIELDS = (
    "name",
    "isin",
    "calculation_date",
    "wap",
    "server_yield",
    "server_z_ruonia",
    "direct_yield_live",
    "direct_z_ruonia_live",
    "direct_z_key_rate_live",
    "calculator_yield_live",
    "calculator_z_ruonia_live",
    "calculator_clean_price_live",
    "calculator_accrual_live",
    "direct_yield_roundtrip",
    "direct_z_ruonia_roundtrip",
    "yield_direct_minus_server",
    "z_ruonia_direct_minus_server",
    "yield_calculator_minus_direct",
    "z_calculator_minus_direct",
    "status",
)


def write_comparison_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in CSV_FIELDS})
    path.chmod(0o600)


def build_manifest(output_dir: Path, counts: dict[str, int], status: str) -> dict[str, Any]:
    files = []
    for path in sorted(output_dir.rglob("*"), key=lambda p: p.relative_to(output_dir).as_posix()):
        if path.is_file() and path.name != "manifest.json":
            files.append(
                {
                    "path": path.relative_to(output_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {"schema_version": 1, "status": status, "counts": counts, "files": files}


def manifest_matches_files(output_dir: Path, manifest: dict[str, Any]) -> bool:
    """Verify the manifest describes the exact pre-archive payload."""
    expected = {
        path.relative_to(output_dir).as_posix(): (path.stat().st_size, sha256_file(path))
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    actual = {
        item["path"]: (item["size"], item["sha256"])
        for item in manifest.get("files", [])
    }
    return actual == expected


def create_deterministic_zip(output_dir: Path, archive: Path) -> None:
    """Zip files in stable order, never including the output archive itself."""
    archive_resolved = archive.resolve()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(
            output_dir.rglob("*"), key=lambda p: p.relative_to(output_dir).as_posix()
        ):
            if not path.is_file() or path.resolve() == archive_resolved:
                continue
            relative = Path(output_dir.name) / path.relative_to(output_dir)
            info = zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            bundle.writestr(info, path.read_bytes())


def probe_properties(vtb_qr: Any, obj: Any, names: Sequence[str]) -> list[dict[str, Any]]:
    """Inspect each property independently through both observed QR APIs."""
    records: list[dict[str, Any]] = []
    for name in names:
        records.append(attempt(f"PyObjectInfo:{name}", lambda name=name: vtb_qr.PyObjectInfo(obj, name)))
        records.append(attempt(f"ObjectInfo:{name}", lambda name=name: vtb_qr.ObjectInfo(obj, name)))
    return records


def save_object(vtb_qr: Any, obj: Any, path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = vtb_qr.SaveObject(obj, str(path))
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("SaveObject returned without creating a non-empty target")
    path.chmod(0o600)
    loaded = vtb_qr.LoadObject(str(path))
    return {"save_return": json_safe(result), "sha256": sha256_file(path), "roundtrip": loaded}


def load_curve(
    rbk: Any,
    vtb_qr: Any,
    curve_date: dt.date,
    publication: str,
    curve_id: str,
    out: Path,
) -> tuple[Any, Any, dict[str, Any]]:
    record: dict[str, Any] = {
        "curve_id": curve_id,
        "publication": publication,
        "date": curve_date.isoformat(),
    }
    loaded = rbk.RBK_ObjectsLoad(curve_date, publication, "RATESCURVE", curve_id)
    try:
        curve = loaded[0][0]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("RBK_ObjectsLoad did not return a populated [0][0] shape") from exc
    if curve is None:
        raise ValueError("RBK_ObjectsLoad returned an empty curve at [0][0]")
    saved = save_object(vtb_qr, curve, out / "curves" / f"{curve_id}.xml.zip")
    roundtrip = saved.pop("roundtrip")
    record.update(saved)
    names = (
        "details",
        "Identifier",
        "CurvePillars",
        "Dates",
        "Rates",
        "CalibrationSet",
        "DiscountFactors",
    )
    record["probes_live"] = probe_properties(vtb_qr, curve, names)
    record["probes_roundtrip"] = probe_properties(vtb_qr, roundtrip, names)
    return curve, roundtrip, record


def get_calypso_payload(session: Any, isin: str) -> bytes:
    """Make the single request supported by the installed Calypso contract."""
    return normalize_bytes(session.get_QR_Bond_Product(isin))


def load_serialized_product(vtb_qr: Any, payload: bytes) -> Any:
    return vtb_qr.LoadObjectFromString(payload.decode("UTF-8-sig"))


def direct_metrics(
    vtb_qr: Any, product: Any, price: float, date: dt.date, curves: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, float | None]]:
    variant_price = vtb_qr.toVariant(price)
    variant_date = vtb_qr.toVariant(date)
    true_ann = vtb_qr.Glue("h2", "YieldCalculationMethod", "TrueAnnualized")
    annual = vtb_qr.Glue("h2", "CompoundingStyle", "AnnuallyCompounded")
    calls: list[tuple[str, Callable[[], Any]]] = [
        (
            "BondPriceToYield",
            lambda: vtb_qr.BondPriceToYield(product, variant_price, variant_date, true_ann),
        ),
        (
            "BondPriceToZSpread_RUONIA",
            lambda: vtb_qr.BondPriceToZSpread(
                product, variant_price, curves["RUONIA"], annual
            ),
        ),
    ]
    if "RUB_KEY_RATE" in curves:
        calls.append(
            (
                "BondPriceToZSpread_RUB_KEY_RATE",
                lambda: vtb_qr.BondPriceToZSpread(
                    product, variant_price, curves["RUB_KEY_RATE"], annual
                ),
            )
        )
    for curve_name, curve in curves.items():
        floater_options = vtb_qr.Glue(
            "h2",
            "CompoundingStyle",
            "AnnuallyCompounded",
            "forwardcurves",
            curve,
        )
        calls.append(
            (
                f"BondPriceToZSpread_{curve_name}_floater_forwardcurve",
                lambda curve=curve, options=floater_options: vtb_qr.BondPriceToZSpread(
                    product, variant_price, curve, options
                ),
            )
        )
    records: dict[str, Any] = {}
    numbers: dict[str, float | None] = {}
    for label, function in calls:
        item, number = numeric_attempt(vtb_qr, label, function)
        records[label] = item
        numbers[label] = number
    return records, numbers


def calculator_metrics(
    vtb_qr: Any,
    product: Any,
    price: float,
    date: dt.date,
    ruonia: Any,
    key_curve: Any | None,
) -> tuple[dict[str, Any], dict[str, float | None]]:
    """Attempt the canonical context/model/calculator path without hiding failures."""
    evidence: dict[str, Any] = {}
    values: dict[str, float | None] = {}
    try:
        context_args: list[Any] = [
            None,
            "marketdatacontext",
            None,
            "marketdataobject",
            ruonia,
        ]
        if key_curve is not None:
            context_args.extend(("marketdataobject", key_curve))
        context_args.extend(("valuedate", vtb_qr.toVariant(date)))
        mdc = vtb_qr.CreateObject(*context_args)
        evidence["marketdatacontext"] = {"status": "ok"}
        model = vtb_qr.CreateObject(
            None,
            "model",
            None,
            "marketdatacontextobject",
            mdc,
            "valuedate",
            vtb_qr.toVariant(date),
            "financingcurveobject",
            ruonia,
        )
        evidence["model"] = {"status": "ok"}
        calc = vtb_qr.CreateObject(
            None,
            "calculator",
            None,
            "modelobject",
            model,
            "productobject",
            product,
            "price",
            vtb_qr.toVariant(price),
            "priceexpression",
            "clean",
            "effectivematurity",
            "first",
            "CompoundingStyle",
            "AnnuallyCompounded",
            "YieldCalculationMethod",
            "TrueAnnualized",
        )
        evidence["calculator"] = {"status": "ok"}
        # These locals deliberately remain referenced throughout all property reads.
        keep_alive = (mdc, model, calc)
        for key in (
            "TrueannualizedYield",
            "AnnuallyCompoundedZspread",
            "CleanPrice",
            "accrual",
        ):
            item, number = numeric_attempt(
                vtb_qr,
                key,
                lambda key=key: vtb_qr.PyObjectInfo(calc, key),
            )
            evidence[key] = item
            values[key] = number
        del keep_alive
    except Exception as exc:
        evidence["setup"] = error_record(exc)
    return evidence, values


def qr_scalar(vtb_qr: Any, value: Any, *, label: str) -> float:
    current = vtb_qr.toNumpy(value)
    for _ in range(8):
        if hasattr(current, "shape") and getattr(current, "size", 0) == 1:
            current = current.reshape(-1)[0]
            continue
        if isinstance(current, (list, tuple)) and len(current) == 1:
            current = current[0]
            continue
        break
    result = float(current)
    if not math.isfinite(result):
        raise ValueError(f"{label} returned a non-finite value")
    return result


PRODUCT_PROBES = (
    "details",
    "Identifier",
    "Identifiers",
    "ISIN",
    "IssueDate",
    "MaturityDate",
    "SettlementDate",
    "Notional",
    "Coupon",
    "CouponRate",
    "Index",
    "CouponPeriod",
    "DayCount",
    "Calendar",
    "Rolling",
    "TermStructure",
)


def delta(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def process_bond(
    rbk: Any,
    vtb_qr: Any,
    session: Any,
    row: tuple[str, str, float, float | None, float | None],
    dates: Sequence[dt.date],
    curves: dict[str, Any],
    out: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name, isin, wap, server_yield, server_z = row
    record: dict[str, Any] = {
        "name": name,
        "isin": isin,
        "wap": wap,
        "server_yield": server_yield,
        "server_z_ruonia": server_z,
        "stages": {},
    }
    flat: list[dict[str, Any]] = []
    try:
        payload = get_calypso_payload(session, isin)
        raw_path = out / "products" / "raw" / f"{isin}.bin"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(payload)
        raw_path.chmod(0o600)
        record["stages"]["calypso_raw"] = {
            "status": "ok",
            "length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    except Exception as exc:
        record["stages"]["calypso_raw"] = error_record(exc)
        record["status"] = "error"
        return record, [
            {
                "name": name,
                "isin": isin,
                "wap": wap,
                "server_yield": server_yield,
                "server_z_ruonia": server_z,
                "status": "product_load_error",
            }
        ]
    try:
        product = load_serialized_product(vtb_qr, payload)
        saved = save_object(vtb_qr, product, out / "products" / "objects" / f"{isin}.xml.zip")
        roundtrip = saved.pop("roundtrip")
        record["stages"]["qr_product"] = {"status": "ok", **saved}
    except Exception as exc:
        record["stages"]["qr_product"] = error_record(exc)
        record["status"] = "error"
        return record, [
            {
                "name": name,
                "isin": isin,
                "wap": wap,
                "server_yield": server_yield,
                "server_z_ruonia": server_z,
                "status": "qr_load_error",
            }
        ]
    record["product_probes_live"] = probe_properties(vtb_qr, product, PRODUCT_PROBES)
    record["product_probes_roundtrip"] = probe_properties(vtb_qr, roundtrip, PRODUCT_PROBES)
    cashflow_date = dates[0]
    record["cashflow_signature_probes"] = [
        attempt(
            "vtb_qr.BondCashflowsTable(product,toVariant(date))",
            lambda: vtb_qr.BondCashflowsTable(product, vtb_qr.toVariant(cashflow_date)),
        ),
    ]
    record["calculations"] = []
    for date in dates:
        calculation: dict[str, Any] = {"date": date.isoformat()}
        try:
            live_evidence, live = direct_metrics(vtb_qr, product, wap, date, curves)
            calculation["direct_live"] = live_evidence
        except Exception as exc:
            calculation["direct_live_setup"] = error_record(exc)
            live = {}
        try:
            rt_evidence, rt = direct_metrics(vtb_qr, roundtrip, wap, date, curves)
            calculation["direct_roundtrip"] = rt_evidence
        except Exception as exc:
            calculation["direct_roundtrip_setup"] = error_record(exc)
            rt = {}
        if "RUONIA" in curves:
            calc_evidence, calc = calculator_metrics(
                vtb_qr, product, wap, date, curves["RUONIA"], curves.get("RUB_KEY_RATE")
            )
        else:
            calc_evidence = {
                "setup": {
                    "status": "error",
                    "error_type": "MissingCurve",
                    "message": "RUONIA curve unavailable",
                }
            }
            calc = {}
        calculation["calculator_live"] = calc_evidence
        y = live.get("BondPriceToYield")
        z = live.get("BondPriceToZSpread_RUONIA")
        calculation["deltas"] = {
            "yield_direct_minus_server": delta(y, server_yield),
            "z_ruonia_direct_minus_server": delta(z, server_z),
            "yield_calculator_minus_direct": delta(calc.get("TrueannualizedYield"), y),
            "z_calculator_minus_direct": delta(calc.get("AnnuallyCompoundedZspread"), z),
            "yield_roundtrip_minus_live": delta(rt.get("BondPriceToYield"), y),
            "z_roundtrip_minus_live": delta(rt.get("BondPriceToZSpread_RUONIA"), z),
        }
        record["calculations"].append(calculation)
        flat.append(
            {
                "name": name,
                "isin": isin,
                "calculation_date": date.isoformat(),
                "wap": wap,
                "server_yield": server_yield,
                "server_z_ruonia": server_z,
                "direct_yield_live": y,
                "direct_z_ruonia_live": z,
                "direct_z_key_rate_live": live.get("BondPriceToZSpread_RUB_KEY_RATE"),
                "calculator_yield_live": calc.get("TrueannualizedYield"),
                "calculator_z_ruonia_live": calc.get("AnnuallyCompoundedZspread"),
                "calculator_clean_price_live": calc.get("CleanPrice"),
                "calculator_accrual_live": calc.get("accrual"),
                "direct_yield_roundtrip": rt.get("BondPriceToYield"),
                "direct_z_ruonia_roundtrip": rt.get("BondPriceToZSpread_RUONIA"),
                **calculation["deltas"],
                "status": (
                    "complete"
                    if y is not None and z is not None
                    else "partial"
                    if y is not None or z is not None
                    else "error"
                ),
            }
        )
    statuses = [row["status"] for row in flat]
    record["status"] = (
        "complete"
        if statuses and all(item == "complete" for item in statuses)
        else "partial"
        if any(item in {"complete", "partial"} for item in statuses)
        else "error"
    )
    return record, flat


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--curve-date", type=parse_date, default=parse_date(DEFAULT_CURVE_DATE))
    parser.add_argument(
        "--calculation-dates",
        default=",".join(DEFAULT_CALCULATION_DATES),
        help="comma-separated ISO dates",
    )
    parser.add_argument("--publication", default=DEFAULT_PUBLICATION)
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--isin", action="append", help="repeat or use comma-separated ISINs")
    return parser


def parse_calculation_dates(value: str) -> tuple[dt.date, ...]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("at least one calculation date is required")
    return tuple(parse_date(part) for part in parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        dates = parse_calculation_dates(args.calculation_dates)
        selected = parse_isin_filters(args.isin)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    rows = [row for row in INPUT_ROWS if not selected or row[1] in selected]
    out = args.output_dir.resolve()
    archive = out.with_suffix(".zip")
    out.mkdir(parents=True, exist_ok=True)
    runtime: dict[str, Any] = {
        "started_utc": dt.datetime.now(dt.UTC).isoformat(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "arguments": {
            "curve_date": args.curve_date.isoformat(),
            "calculation_dates": [d.isoformat() for d in dates],
            "publication": args.publication,
            "output_dir": out.name,
            "isins": list(selected),
        },
        "stages": {},
    }
    records: list[dict[str, Any]] = []
    flat_rows: list[dict[str, Any]] = []
    status = "error"
    session = None
    try:
        write_json(
            out / "input_rows.json",
            [
                dict(zip(("name", "isin", "wap", "server_yield", "server_z_ruonia"), row))
                for row in rows
            ],
        )
        import vtb_qr
        import rubik_api as rbk
        from calypso_framework import get_calypso_session

        runtime["modules"] = {
            "vtb_qr": safe_module_location(vtb_qr),
            "rubik_api": safe_module_location(rbk),
        }
        vtb_qr.load_xll_and_static()
        runtime["stages"]["vtb_qr.load_xll_and_static"] = {"status": "ok"}
        rbk.load()
        runtime["stages"]["rbk.load"] = {"status": "ok"}
        login = os.environ.get("CALYPSO_LOGIN")
        password = os.environ.get("CALYPSO_PASSWORD")
        if not login or not password:
            raise RuntimeError(
                "CALYPSO_LOGIN and CALYPSO_PASSWORD environment variables are required"
            )
        session = get_calypso_session(login, password)
        runtime["stages"]["calypso_session"] = {"status": "ok"}
        curves: dict[str, Any] = {}
        curve_records: dict[str, Any] = {}
        for curve_id in ("RUONIA", "RUB_KEY_RATE"):
            try:
                curve, _roundtrip, curve_record = load_curve(
                    rbk, vtb_qr, args.curve_date, args.publication, curve_id, out
                )
                curves[curve_id] = curve
                curve_records[curve_id] = {"status": "ok", **curve_record}
            except Exception as exc:
                curve_records[curve_id] = error_record(exc)
        write_json(out / "curves" / "diagnostics.json", curve_records)
        for row in rows:
            try:
                record, flat = process_bond(rbk, vtb_qr, session, row, dates, curves, out)
            except Exception as exc:
                record = {
                    "name": row[0],
                    "isin": row[1],
                    "status": "error",
                    "fatal_bond_error": error_record(exc),
                }
                flat = [
                    {
                        "name": row[0],
                        "isin": row[1],
                        "wap": row[2],
                        "status": "unexpected_bond_error",
                    }
                ]
            records.append(record)
            flat_rows.extend(flat)
            write_jsonl(out / "per_bond.jsonl", records)
            write_comparison_csv(out / "comparison.csv", flat_rows)
        bond_statuses = [record.get("status", "error") for record in records]
        status = (
            "complete"
            if bond_statuses and all(item == "complete" for item in bond_statuses)
            else "partial"
            if any(item in {"complete", "partial"} for item in bond_statuses)
            else "error"
        )
    except Exception as exc:
        runtime["stages"]["fatal"] = error_record(exc)
    finally:
        if session is not None:
            close = getattr(session, "close", None) or getattr(session, "Close", None)
            if callable(close):
                runtime["stages"]["session_close"] = attempt("session.close", close)
        runtime["finished_utc"] = dt.datetime.now(dt.UTC).isoformat()
        runtime["status"] = status
        write_json(out / "runtime_diagnostic.json", runtime)
        if not (out / "per_bond.jsonl").exists():
            write_jsonl(out / "per_bond.jsonl", records)
        if not (out / "comparison.csv").exists():
            write_comparison_csv(out / "comparison.csv", flat_rows)
        counts = {
            "input_rows": len(rows),
            "bond_records": len(records),
            "comparison_rows": len(flat_rows),
            "complete_bonds": sum(r.get("status") == "complete" for r in records),
            "partial_bonds": sum(r.get("status") == "partial" for r in records),
            "error_bonds": sum(r.get("status") not in {"complete", "partial"} for r in records),
        }
        runtime["stages"]["archive"] = {"status": "will_create_after_manifest"}
        write_json(out / "runtime_diagnostic.json", runtime)
        manifest = build_manifest(out, counts, status)
        write_json(out / "manifest.json", manifest)
        try:
            if not manifest_matches_files(out, manifest):
                raise RuntimeError("manifest does not match pre-archive files")
            create_deterministic_zip(out, archive)
        except Exception as exc:
            runtime["stages"]["archive"] = error_record(exc)
            write_json(out / "runtime_diagnostic.json", runtime)
            write_json(out / "manifest.json", build_manifest(out, counts, "partial"))
            return 2
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
