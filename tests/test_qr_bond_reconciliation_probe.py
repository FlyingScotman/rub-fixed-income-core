from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "tools" / "qr_bond_reconciliation_probe.py"
SPEC = importlib.util.spec_from_file_location("qr_bond_reconciliation_probe", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_exact_dataset() -> None:
    assert len(probe.INPUT_ROWS) == 30
    assert len({row[1] for row in probe.INPUT_ROWS}) == 30
    assert probe.INPUT_ROWS[0] == ("Атомэнпр07", "RU000A10C6L5", 97.46, 16.04, 124)
    assert probe.INPUT_ROWS[2] == ("МТС 2P-11", "RU000A10BP79", 100.75, 13.75, -106)
    assert probe.INPUT_ROWS[14] == ("Систем1P29", "RU000A108GL1", 89.29, None, 1180)
    assert probe.INPUT_ROWS[24] == ("БалтЛизП14", "RU000A10A3W1", 95.47, None, 2967)
    assert probe.INPUT_ROWS[-1] == ("ПГК3Р04", "RU000A10EYV6", 98.82, 17.14, 256)


def test_bytes_normalization() -> None:
    assert probe.normalize_bytes(b"abc") == b"abc"
    assert probe.normalize_bytes([b"abc"]) == b"abc"
    assert probe.normalize_bytes(bytearray(b"abc")) == b"abc"
    with pytest.raises(ValueError):
        probe.normalize_bytes([])
    with pytest.raises(ValueError):
        probe.normalize_bytes([b"a", b"b"])
    with pytest.raises(TypeError):
        probe.normalize_bytes("secret")


def test_date_and_filter_validation() -> None:
    assert str(probe.parse_date("2026-09-03")) == "2026-09-03"
    assert probe.parse_isin_filters(["RU000A10C6L5,RU000A10F7L0", "RU000A10C6L5"]) == (
        "RU000A10C6L5",
        "RU000A10F7L0",
    )
    with pytest.raises(argparse.ArgumentTypeError):
        probe.parse_date("03.09.2026")
    with pytest.raises(argparse.ArgumentTypeError):
        probe.parse_isin_filters(["RU000A000000"])


def test_json_safe_no_object_repr_and_nonfinite() -> None:
    class SecretObject:
        def __repr__(self) -> str:
            return "PASSWORD=do-not-leak"

    value = probe.json_safe({"x": math.inf, "payload": b"abc", "object": SecretObject()})
    encoded = json.dumps(value)
    assert "do-not-leak" not in encoded
    assert value["payload"]["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert value["x"] == {"non_finite": "inf"}


def test_json_safe_preserves_nested_array_diagnostics() -> None:
    class FakeArray:
        def tolist(self):
            return [[1, 2], [3, math.nan]]

    assert probe.json_safe(FakeArray()) == [[1, 2], [3, {"non_finite": "nan"}]]


def test_manifest_and_deterministic_zip_exclude_archive(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "b.txt").write_text("b", encoding="utf-8")
    (output / "a.txt").write_text("a", encoding="utf-8")
    manifest = probe.build_manifest(output, {"input_rows": 2}, "complete")
    assert [item["path"] for item in manifest["files"]] == ["a.txt", "b.txt"]
    probe.write_json(output / "manifest.json", manifest)
    assert probe.manifest_matches_files(output, manifest)
    archive = tmp_path / "evidence.zip"
    probe.create_deterministic_zip(output, archive)
    first = archive.read_bytes()
    probe.create_deterministic_zip(output, archive)
    assert archive.read_bytes() == first
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == [
            "evidence/a.txt",
            "evidence/b.txt",
            "evidence/manifest.json",
        ]
        assert not any(name.endswith("evidence.zip") for name in bundle.namelist())


def test_csv_has_utf8_bom_and_expected_columns(tmp_path: Path) -> None:
    target = tmp_path / "comparison.csv"
    probe.write_comparison_csv(target, [{"name": "Атомэнпр07", "isin": "RU000A10C6L5"}])
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    text = target.read_text(encoding="utf-8-sig")
    assert "direct_z_ruonia_live" in text
    assert "Атомэнпр07" in text


class FakeQR:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def toVariant(self, value):
        result = ("variant", value)
        self.calls.append(("toVariant", value, result))
        return result

    def Glue(self, *args):
        result = ("glue", *args)
        self.calls.append(("Glue", *args))
        return result

    def toNumpy(self, value):
        self.calls.append(("toNumpy", value))
        return [value]

    def BondPriceToYield(self, *args):
        self.calls.append(("BondPriceToYield", *args))
        return 15.25

    def BondPriceToZSpread(self, *args):
        self.calls.append(("BondPriceToZSpread", *args))
        return 123.0

    def CreateObject(self, *args):
        result = {"kind": args[1], "sequence": len(self.calls)}
        self.calls.append(("CreateObject", *args, result))
        return result

    def PyObjectInfo(self, obj, key):
        self.calls.append(("PyObjectInfo", obj, key))
        return 7.5


def test_calypso_contract_is_one_exact_request_and_utf8_sig_decode() -> None:
    class Session:
        def __init__(self) -> None:
            self.calls = []

        def get_QR_Bond_Product(self, isin):
            self.calls.append(("get_QR_Bond_Product", isin))
            return [b"\xef\xbb\xbf<Product/>"]

    class Loader:
        def __init__(self) -> None:
            self.calls = []

        def LoadObjectFromString(self, text):
            self.calls.append(("LoadObjectFromString", text))
            return "product"

    session = Session()
    loader = Loader()
    payload = probe.get_calypso_payload(session, "RU000A10C6L5")
    assert probe.load_serialized_product(loader, payload) == "product"
    assert session.calls == [("get_QR_Bond_Product", "RU000A10C6L5")]
    assert loader.calls == [("LoadObjectFromString", "<Product/>")]


def test_direct_metrics_exact_vtb_qr_calls_and_variants() -> None:
    qr = FakeQR()
    date = dt.date(2026, 9, 3)
    product, ruonia = object(), object()
    _evidence, values = probe.direct_metrics(qr, product, 97.46, date, {"RUONIA": ruonia})

    assert (
        "BondPriceToYield", product, ("variant", 97.46), ("variant", date),
        ("glue", "h2", "YieldCalculationMethod", "TrueAnnualized"),
    ) in qr.calls
    assert (
        "BondPriceToZSpread", product, ("variant", 97.46), ruonia,
        ("glue", "h2", "CompoundingStyle", "AnnuallyCompounded"),
    ) in qr.calls
    assert (
        "Glue", "h2", "CompoundingStyle", "AnnuallyCompounded", "forwardcurves", ruonia,
    ) in qr.calls
    yield_calls = [call for call in qr.calls if call[0] == "BondPriceToYield"]
    assert len(yield_calls) == 1
    assert len(yield_calls[0]) == 5
    assert values["BondPriceToYield"] == 15.25
    assert values["BondPriceToZSpread_RUONIA"] == 123.0


def test_numeric_metrics_pass_raw_proprietary_value_to_to_numpy() -> None:
    class RawNumber:
        def tolist(self):
            return [999.0]

    raw = RawNumber()

    class RawQR(FakeQR):
        def BondPriceToYield(self, *args):
            self.calls.append(("BondPriceToYield", *args))
            return raw

        def toNumpy(self, value):
            assert value is raw
            self.calls.append(("toNumpy", value))
            return [12.5]

    _evidence, values = probe.direct_metrics(
        RawQR(), object(), 97.46, dt.date(2026, 9, 3), {"RUONIA": object()}
    )
    assert values["BondPriceToYield"] == 12.5


def test_save_object_rejects_empty_target(tmp_path: Path) -> None:
    class QR:
        def SaveObject(self, obj, path):
            Path(path).touch()

    with pytest.raises(RuntimeError, match="non-empty"):
        probe.save_object(QR(), object(), tmp_path / "empty.xml.zip")


def test_calculator_exact_construction_and_property_api() -> None:
    qr = FakeQR()
    date = dt.date(2026, 9, 3)
    product, ruonia, key_curve = object(), object(), object()
    evidence, values = probe.calculator_metrics(qr, product, 100.25, date, ruonia, key_curve)
    creates = [call for call in qr.calls if call[0] == "CreateObject"]
    assert creates[0][1:-1] == (
        None, "marketdatacontext", None, "marketdataobject", ruonia,
        "marketdataobject", key_curve, "valuedate", ("variant", date),
    )
    mdc = creates[0][-1]
    assert creates[1][1:-1] == (
        None, "model", None, "marketdatacontextobject", mdc,
        "valuedate", ("variant", date), "financingcurveobject", ruonia,
    )
    model = creates[1][-1]
    assert creates[2][1:-1] == (
        None, "calculator", None, "modelobject", model, "productobject", product,
        "price", ("variant", 100.25), "priceexpression", "clean",
        "effectivematurity", "first", "CompoundingStyle", "AnnuallyCompounded",
        "YieldCalculationMethod", "TrueAnnualized",
    )
    calc = creates[2][-1]
    assert [call[2] for call in qr.calls if call[0] == "PyObjectInfo"] == [
        "TrueannualizedYield", "AnnuallyCompoundedZspread", "CleanPrice", "accrual"
    ]
    assert all(call[1] is calc for call in qr.calls if call[0] == "PyObjectInfo")
    assert evidence["calculator"]["status"] == "ok"
    assert values["TrueannualizedYield"] == 7.5


def test_calculator_metrics_pass_raw_property_value_to_to_numpy() -> None:
    class RawNumber:
        def tolist(self):
            return [999.0]

    raw = RawNumber()

    class RawQR(FakeQR):
        def PyObjectInfo(self, obj, key):
            self.calls.append(("PyObjectInfo", obj, key))
            return raw

        def toNumpy(self, value):
            assert value is raw
            self.calls.append(("toNumpy", value))
            return [6.25]

    _evidence, values = probe.calculator_metrics(
        RawQR(), object(), 100.25, dt.date(2026, 9, 3), object(), None
    )
    assert values["TrueannualizedYield"] == 6.25


def test_curve_loader_accepts_proprietary_indexable_range(tmp_path: Path) -> None:
    curve = object()

    class Row:
        def __getitem__(self, index):
            if index == 0:
                return curve
            raise IndexError(index)

    class ProprietaryRange:
        def __getitem__(self, index):
            if index == 0:
                return Row()
            raise IndexError(index)

        def __bool__(self):
            raise AssertionError("range truthiness must not be inspected")

    class RBK:
        def __init__(self):
            self.calls = []

        def RBK_ObjectsLoad(self, *args):
            self.calls.append(("RBK_ObjectsLoad", *args))
            return ProprietaryRange()

    class QR(FakeQR):
        def SaveObject(self, obj, path):
            self.calls.append(("SaveObject", obj, path))
            Path(path).write_bytes(b"saved")

        def LoadObject(self, path):
            self.calls.append(("LoadObject", path))
            return curve

        def PyObjectInfo(self, obj, key):
            return key

        def ObjectInfo(self, obj, key):
            return key

    rbk, qr = RBK(), QR()
    probe.load_curve(rbk, qr, dt.date(2026, 9, 3), "CALYPSO LIVE DATA", "RUONIA", tmp_path)
    assert [call[0] for call in rbk.calls] == ["RBK_ObjectsLoad"]
    assert any(call[0] == "SaveObject" for call in qr.calls)
    assert any(call[0] == "LoadObject" for call in qr.calls)
