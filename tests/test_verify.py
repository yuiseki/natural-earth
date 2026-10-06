import importlib.util
import io
import shutil
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIX = Path(__file__).parent / "fixtures" / "ne_110m_geography_regions_points"
LAYER = "ne_110m_geography_regions_points"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load("verify", "02_verify.py")
export = _load("export", "03_export_parquet.py")


@pytest.fixture
def layer(tmp_path):
    d = tmp_path / "shp"
    shutil.copytree(FIX, d)
    out = tmp_path / "x.parquet"
    export.export_layer(d / f"{LAYER}.shp", out, {"natural_earth_version": "5.0.0"})
    return d / f"{LAYER}.shp", out


def test_compare_layer_passes_on_a_faithful_export(layer):
    shp, out = layer
    r = v.compare_layer(shp, out)
    assert r["problems"] == []
    assert r["dbf_records"] == r["shx_records"] == r["parquet_rows"] == 3
    assert r["cells_compared"] == 3 * 39
    assert r["geometry"] == {
        "null": 0,
        "empty": 0,
        "invalid": 0,
        "types": {"POINT": 3},
        "wkb_differs": 0,
    }


def test_compare_layer_reports_a_missing_row(layer, tmp_path):
    import duckdb

    shp, out = layer
    short = tmp_path / "short.parquet"
    con = duckdb.connect()
    con.execute("load spatial")
    con.execute(f"copy (select * from '{out}' limit 2) to '{short}' (format parquet)")
    r = v.compare_layer(shp, short)
    assert any("2 rows" in p for p in r["problems"])


def test_compare_layer_reports_a_changed_value(layer, tmp_path):
    import duckdb

    shp, out = layer
    bad = tmp_path / "bad.parquet"
    con = duckdb.connect()
    con.execute("load spatial")
    con.execute(
        f"copy (select * replace (upper(name) as name) from '{out}') to '{bad}' (format parquet)"
    )
    r = v.compare_layer(shp, bad)
    assert any("name" in p for p in r["problems"])


def test_check_zip_finds_a_corrupt_member(tmp_path):
    good = tmp_path / "a.zip"
    with zipfile.ZipFile(good, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", "hello" * 100)
    v.check_zip(good)
    data = bytearray(good.read_bytes())
    i = data.index(b"a.txt") + 5 + 2  # inside the compressed data of the only member
    data[i] ^= 0xFF
    bad = tmp_path / "b.zip"
    bad.write_bytes(bytes(data))
    with pytest.raises((zipfile.BadZipFile, zipfile.LargeZipFile, OSError, Exception)):
        v.check_zip(bad)


def test_values_equal_treats_floats_exactly_and_none_as_none():
    assert v.values_equal(1.5, 1.5)
    assert not v.values_equal(1.5, 1.5000001)
    assert v.values_equal(None, None)
    assert not v.values_equal(0, None)
    assert v.values_equal(12, 12)
    assert not v.values_equal("a", "a ")


def test_zip_member_count_ignores_directories():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("d/", "")
        z.writestr("d/x.shp", "")
    assert v.shapefiles_in(zipfile.ZipFile(b)) == ["d/x"]
