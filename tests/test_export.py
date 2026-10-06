import datetime
import importlib.util
import json
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIX = Path(__file__).parent / "fixtures" / "ne_110m_geography_regions_points"
BASE = FIX / "ne_110m_geography_regions_points"
_spec = importlib.util.spec_from_file_location("export", ROOT / "scripts" / "03_export_parquet.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def test_read_dbf_header_of_a_real_file():
    h = m.read_dbf_header(BASE.with_suffix(".dbf"))
    assert h["records"] == 3
    assert h["ldid"] == 0
    assert len(h["fields"]) == 39
    assert h["fields"][0] == {"name": "scalerank", "type": "N", "length": 10, "decimals": 0}
    assert h["fields"][5] == {"name": "lat_y", "type": "N", "length": 19, "decimals": 9}


def test_duckdb_type_follows_the_field_definition():
    def f(t, n, d=0):
        return {"name": "x", "type": t, "length": n, "decimals": d}

    assert m.duckdb_type(f("C", 254)) == "VARCHAR"
    assert m.duckdb_type(f("N", 9)) == "INTEGER"
    assert m.duckdb_type(f("N", 10)) == "BIGINT"
    assert m.duckdb_type(f("N", 18)) == "BIGINT"
    assert m.duckdb_type(f("N", 4, 1)) == "DOUBLE"
    assert m.duckdb_type(f("F", 19, 11)) == "DOUBLE"
    assert m.duckdb_type(f("L", 1)) == "BOOLEAN"
    assert m.duckdb_type(f("D", 8)) == "DATE"
    with pytest.raises(ValueError, match="M"):
        m.duckdb_type(f("M", 10))


def test_parse_value_mirrors_the_dbf_conventions():
    def f(t, n, d=0):
        return {"name": "x", "type": t, "length": n, "decimals": d}

    p = m.parse_value
    assert p(f("N", 10), b"        12", "utf-8") == 12
    assert p(f("N", 10), b"       -99", "utf-8") == -99
    assert p(f("N", 10), b"          ", "utf-8") is None
    assert p(f("N", 10), b"**********", "utf-8") is None
    assert p(f("N", 4, 1), b" 3.5", "utf-8") == 3.5
    assert p(f("C", 8), "Côte   ".encode(), "utf-8") == "Côte"
    # GDAL (shapelib built with TRIM_DBF_WHITESPACE) trims leading spaces too
    assert p(f("C", 8), b" Cote   ", "utf-8") == "Cote"
    assert p(f("C", 8), b"        ", "utf-8") is None
    # GDAL ends a text value at the first NUL; some Natural Earth files pad with NULs
    assert p(f("C", 8), b"Indo\x00\x00\x00\x00", "utf-8") == "Indo"
    assert p(f("C", 8), b"\x00\x00\x00\x00\x00\x00\x00\x00", "utf-8") is None
    assert p(f("L", 1), b"T", "utf-8") is True
    assert p(f("L", 1), b"n", "utf-8") is False
    assert p(f("L", 1), b"?", "utf-8") is None
    assert p(f("D", 8), b"20210904", "utf-8") == datetime.date(2021, 9, 4)
    assert p(f("D", 8), b"00000000", "utf-8") is None
    with pytest.raises(ValueError):
        p(f("N", 10), b"       1.5", "utf-8")
    with pytest.raises(UnicodeDecodeError):
        p(f("C", 2), b"\xff\xfe", "utf-8")


def test_read_records_decodes_every_value():
    h = m.read_dbf_header(BASE.with_suffix(".dbf"))
    rows = list(m.read_records(BASE.with_suffix(".dbf"), h, "utf-8"))
    assert len(rows) == 3
    assert [r["deleted"] for r in rows] == [False, False, False]
    first = rows[0]["values"]
    assert first[0] == 2
    assert first[2] == "Niagara Falls"
    names = [f["name"] for f in h["fields"]]
    assert first[names.index("name_ja")] == "ナイアガラの滝"


def test_shx_count():
    assert m.shx_count(BASE.with_suffix(".shx")) == 3


def test_encoding_policy():
    assert m.layer_encoding("UTF-8", 0, []) == ("utf-8", "from .cpg 'UTF-8'")
    assert m.layer_encoding("utf8\n", 0, []) == ("utf-8", "from .cpg 'utf8'")
    py, how = m.layer_encoding(None, 0, [b"abc", "é".encode()])
    assert py == "utf-8" and "no .cpg" in how and "1 non-ASCII" in how
    with pytest.raises(SystemExit, match="no .cpg"):
        m.layer_encoding(None, 0, [b"\xe9t\xe9"])
    with pytest.raises(SystemExit, match="LDID"):
        m.layer_encoding(None, 0x57, [b"x"])
    with pytest.raises(SystemExit, match="cpg"):
        m.layer_encoding("KOI8-R", 0, [])


def test_prj_must_be_geographic_wgs84():
    assert m.prj_is_wgs84(BASE.with_suffix(".prj").read_text())
    assert not m.prj_is_wgs84('PROJCS["WGS_1984_Web_Mercator",GEOGCS["GCS_WGS_1984"]]')
    assert not m.prj_is_wgs84('GEOGCS["GCS_North_American_1983"]')


def test_export_layer_writes_typed_geoparquet(tmp_path):
    import duckdb

    out = tmp_path / "x.parquet"
    info = m.export_layer(BASE.with_suffix(".shp"), out, {"natural_earth_version": "5.0.0"})
    assert info["features"] == 3
    assert info["encoding"] == "utf-8"
    con = duckdb.connect()
    con.execute("load spatial")
    rel = con.sql(f"select * from '{out}'")
    h = m.read_dbf_header(BASE.with_suffix(".dbf"))
    assert rel.columns == [f["name"] for f in h["fields"]] + ["geometry"]
    types = dict(zip(rel.columns, map(str, rel.types), strict=True))
    assert types["scalerank"] == "BIGINT"
    assert types["lat_y"] == "DOUBLE"
    assert types["name"] == "VARCHAR"
    assert types["geometry"].startswith("GEOMETRY")
    kv = dict(
        con.sql(f"select decode(key), decode(value) from parquet_kv_metadata('{out}')").fetchall()
    )
    crs = json.loads(kv["geo"])["columns"]["geometry"]["crs"]["id"]
    assert crs == {"authority": "EPSG", "code": 4326}
    assert kv["natural_earth_version"] == "5.0.0"
    wkt = con.sql(f"select ST_AsText(geometry) from '{out}' order by scalerank limit 1").fetchone()
    assert wkt[0].startswith("POINT")


def test_dbf_header_rejects_a_non_dbf(tmp_path):
    p = tmp_path / "x.dbf"
    p.write_bytes(b"\x99" + struct.pack("<3xIHH", 0, 33, 1) + b"\x00" * 20 + b"\r")
    with pytest.raises(ValueError, match="dBASE"):
        m.read_dbf_header(p)


def test_a_layer_without_prj_gets_epsg4326_only_with_lon_lat_bounds_and_says_so(tmp_path):
    import shutil

    d = tmp_path / "shp"
    shutil.copytree(FIX, d)
    (d / "ne_110m_geography_regions_points.prj").unlink()
    info = m.export_layer(d / "ne_110m_geography_regions_points.shp", tmp_path / "x.parquet", {})
    assert info["crs"] == "EPSG:4326"
    assert "no .prj" in info["crs_how"]


def test_crs_from_prj_is_recorded(tmp_path):
    info = m.export_layer(BASE.with_suffix(".shp"), tmp_path / "x.parquet", {})
    assert info["crs"] == "EPSG:4326"
    assert info["crs_how"].startswith("from .prj")
