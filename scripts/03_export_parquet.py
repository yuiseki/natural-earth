#!/usr/bin/env python3
"""Write one GeoParquet file per Natural Earth layer, from the zips 01_download.py fetched.

    data/parquet/<version>/<scale>_<category>/<layer>.parquet

For every layer in data/raw/<version>/MANIFEST.json the shapefile members
(.shp .shx .dbf .prj .cpg) are extracted from its zip into data/work/ and read
with DuckDB's spatial extension (ST_Read, which uses GDAL). Then:

  - Columns are the .dbf fields, in their order and under their names, every
    one kept, then `geometry`. Nothing is added, renamed or dropped.
  - Each column's type comes from its .dbf field definition, which this
    script reads itself (read_dbf_header), never from inference:
        C            VARCHAR
        N, 0 decimals, width <= 9    INTEGER
        N, 0 decimals, width 10-18   BIGINT
        N with decimals, or F        DOUBLE
        L            BOOLEAN
        D            DATE
    Any other field type stops the run. The value GDAL reads is cast to that
    type; a value that does not fit stops the run.
  - Text is decoded in the encoding the layer's .cpg names. When a layer has
    no .cpg, its LDID byte must be 0 and every text value must decode as
    UTF-8; it is then read as UTF-8 and the manifest of the export says so.
    Anything else stops the run.
  - Text is read as GDAL reads it: a value ends at its first NUL byte
    (several layers pad text with NULs instead of spaces), and leading and
    trailing spaces are dropped (shapelib's TRIM_DBF_WHITESPACE; a handful
    of values carry a leading space in the source, which 02_verify counts).
  - NULL follows the dBASE and GDAL conventions: a text value of only spaces,
    a number that is empty or all '*', a date that is empty or 00000000, a
    logical that is '?' or empty.
  - Geometry is kept as it is in the .shp: no reprojection, no repair, no
    simplification. The .prj must describe geographic WGS 84 (GCS_WGS_1984),
    which is EPSG:4326. A layer whose zip has no .prj (ne_10m_admin_0_names,
    which has none in the repository either) is given EPSG:4326 only if all
    its coordinates lie within lon/lat bounds, or if it has no coordinates at
    all (that layer's 505 shapes are all null shapes), and LAYERS.json says so. The column is GEOMETRY('EPSG:4326') and DuckDB writes
    GeoParquet 1.0 'geo' metadata with that CRS as PROJJSON. Coordinates are
    longitude, latitude, as in the shapefile.
  - Parquet key-value metadata adds the layer name, the version the zip
    declares in its VERSION.txt, the source URL and the zip's sha256.
  - zstd, no bloom filters.

A summary per layer goes to data/parquet/<version>/LAYERS.json.

    uv run python scripts/03_export_parquet.py
    uv run python scripts/03_export_parquet.py --only ne_110m_land 50m_cultural
"""

import argparse
import datetime
import json
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "parquet"
WORK = ROOT / "data" / "work"
SHAPE_EXT = (".shp", ".shx", ".dbf", ".prj", ".cpg")
CPG = {"UTF-8": "utf-8", "UTF8": "utf-8", "65001": "utf-8"}
GDAL_ENCODING = {"utf-8": "UTF-8"}
COPY = "format parquet, compression zstd, write_bloom_filter false"


# ---------------------------------------------------------------- dBASE


def read_dbf_header(path: Path) -> dict:
    """Record count, LDID and field definitions of a dBASE III file."""
    with open(path, "rb") as f:
        head = f.read(32)
        version = head[0]
        if version not in (0x03, 0x83, 0x8B, 0x30, 0xF5):
            raise ValueError(f"{path.name}: not a dBASE III/IV file (version byte {version:#x})")
        records, header_len, record_len = struct.unpack("<IHH", head[4:12])
        rest = f.read(header_len - 32)
    fields = []
    for i in range(0, len(rest), 32):
        d = rest[i : i + 32]
        if d[:1] == b"\r" or len(d) < 32:
            break
        fields.append(
            {
                "name": d[:11].split(b"\0")[0].decode("ascii"),
                "type": chr(d[11]),
                "length": d[16],
                "decimals": d[17],
            }
        )
    return {
        "version": version,
        "records": records,
        "header_len": header_len,
        "record_len": record_len,
        "ldid": head[29],
        "fields": fields,
    }


def duckdb_type(field: dict) -> str:
    t, n, d = field["type"], field["length"], field["decimals"]
    if t == "C":
        return "VARCHAR"
    if t == "N" and d == 0:
        if n <= 9:
            return "INTEGER"
        if n <= 18:
            return "BIGINT"
        return "DOUBLE"
    if t in ("N", "F"):
        return "DOUBLE"
    if t == "L":
        return "BOOLEAN"
    if t == "D":
        return "DATE"
    raise ValueError(f"field {field['name']}: dBASE type {t} is not handled")


_INT = re.compile(r"[+-]?[0-9]+")


def parse_value(field: dict, raw: bytes, encoding: str):
    """One stored value as the Parquet should hold it."""
    t = field["type"]
    if t == "C":
        s = raw.split(b"\0", 1)[0].decode(encoding, errors="strict").strip(" ")
        return s or None
    s = raw.decode("ascii", errors="strict").strip()
    if t in ("N", "F"):
        if not s or set(s) == {"*"}:
            return None
        if duckdb_type(field) in ("INTEGER", "BIGINT"):
            if not _INT.fullmatch(s):
                raise ValueError(f"{field['name']}: {s!r} is not an integer")
            return int(s)
        return float(s)
    if t == "L":
        if s in ("", "?"):
            return None
        if s in "TtYy":
            return True
        if s in "FfNn":
            return False
        raise ValueError(f"{field['name']}: {s!r} is not a logical")
    if t == "D":
        if not s or s == "00000000":
            return None
        return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"field {field['name']}: dBASE type {t} is not handled")


def raw_records(path: Path, header: dict):
    """(deleted, [raw bytes per field]) for every record."""
    widths = [f["length"] for f in header["fields"]]
    with open(path, "rb") as f:
        f.seek(header["header_len"])
        for _ in range(header["records"]):
            rec = f.read(header["record_len"])
            if len(rec) < header["record_len"]:
                raise ValueError(f"{path.name}: file ends inside a record")
            pos, vals = 1, []
            for w in widths:
                vals.append(rec[pos : pos + w])
                pos += w
            yield rec[:1] == b"*", vals


def read_records(path: Path, header: dict, encoding: str):
    for deleted, vals in raw_records(path, header):
        yield {
            "deleted": deleted,
            "values": [
                parse_value(fd, v, encoding) for fd, v in zip(header["fields"], vals, strict=True)
            ],
        }


def shx_count(path: Path) -> int:
    """Records in a shapefile, from its index: (file length - 100) / 8."""
    with open(path, "rb") as f:
        head = f.read(100)
    words = struct.unpack(">i", head[24:28])[0]
    return (words * 2 - 100) // 8


def text_values(path: Path, header: dict) -> list[bytes]:
    idx = [i for i, f in enumerate(header["fields"]) if f["type"] == "C"]
    return [vals[i] for _, vals in raw_records(path, header) for i in idx]


def layer_encoding(cpg: str | None, ldid: int, texts: list[bytes]) -> tuple[str, str]:
    """(Python codec, how it was decided)."""
    if cpg is not None:
        key = cpg.strip()
        enc = CPG.get(key.upper())
        if enc is None:
            raise SystemExit(f".cpg says {key!r}, which this script does not handle")
        return enc, f"from .cpg {key!r}"
    if ldid != 0:
        raise SystemExit(f"no .cpg and LDID {ldid:#x}; decide the encoding by hand")
    non_ascii = 0
    for t in texts:
        try:
            t.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            raise SystemExit(f"no .cpg, LDID 0, and a text value is not UTF-8: {t!r}") from e
        non_ascii += any(b > 0x7F for b in t)
    return "utf-8", f"no .cpg, LDID 0; every text value is valid UTF-8 ({non_ascii} non-ASCII)"


def prj_is_wgs84(prj: str) -> bool:
    s = prj.strip()
    return (
        s.startswith("GEOGCS[")
        and "PROJCS" not in s
        and ("GCS_WGS_1984" in s or 'GEOGCS["WGS 84"' in s)
    )


# ---------------------------------------------------------------- export


def export_layer(shp: Path, out: Path, kv: dict[str, str]) -> dict:
    """Write one layer; returns what LAYERS.json records for it."""
    import duckdb

    dbf = shp.with_suffix(".dbf")
    header = read_dbf_header(dbf)
    cpg_path = shp.with_suffix(".cpg")
    cpg = cpg_path.read_text(encoding="ascii").strip() if cpg_path.exists() else None
    texts = [] if cpg else text_values(dbf, header)
    enc, how = layer_encoding(cpg, header["ldid"], texts)
    prj_path = shp.with_suffix(".prj")
    prj = prj_path.read_text(encoding="ascii") if prj_path.exists() else None
    if prj is not None and not prj_is_wgs84(prj):
        raise SystemExit(f"{shp.name}: .prj is not geographic WGS 84: {prj[:80]}")

    con = duckdb.connect()
    con.execute("load spatial")
    con.execute("set memory_limit = '8GB'")
    src = f"ST_Read('{shp}', open_options = ['ENCODING={GDAL_ENCODING[enc]}'])"
    rel = con.sql(f"select * from {src} limit 0")
    gdal_cols = [c for c in rel.columns if c not in ("OGC_FID", "geom")]
    geom_type = dict(zip(rel.columns, map(str, rel.types), strict=True)).get("geom")
    if len(gdal_cols) != len(header["fields"]):
        raise SystemExit(
            f"{shp.name}: GDAL sees {len(gdal_cols)} fields, the .dbf has {len(header['fields'])}"
        )
    if prj is not None:
        if geom_type != "GEOMETRY('EPSG:4326')":
            raise SystemExit(f"{shp.name}: GDAL reads the CRS as {geom_type}")
        crs_how = "from .prj (" + prj.strip()[7 : prj.strip().index(",")] + "), as GDAL reads it"
    else:
        x0, y0, x1, y1 = con.sql(
            f"select min(ST_XMin(geom)), min(ST_YMin(geom)), max(ST_XMax(geom)), "
            f"max(ST_YMax(geom)) from {src}"
        ).fetchone()
        if x0 is None:
            crs_how = (
                "no .prj in the zip and every shape is a null shape (an attribute-only "
                "table); the all-NULL geometry column is typed EPSG:4326 like the others"
            )
        elif x0 < -180 or x1 > 180 or y0 < -90 or y1 > 90:
            raise SystemExit(f"{shp.name}: no .prj and coordinates outside lon/lat bounds")
        else:
            crs_how = (
                "no .prj in the zip; coordinates lie within lon/lat bounds "
                f"({x0:.4f}, {y0:.4f}, {x1:.4f}, {y1:.4f}); assigned EPSG:4326"
            )
    names = [f["name"] for f in header["fields"]]
    lower = [n.lower() for n in names] + ["geometry"]
    if len(set(lower)) != len(lower):
        raise SystemExit(f"{shp.name}: field names collide when case is ignored: {names}")
    cols = [
        f'cast("{g}" as {duckdb_type(fd)}) as "{fd["name"]}"'
        for g, fd in zip(gdal_cols, header["fields"], strict=True)
    ]
    cols.append("geom::GEOMETRY('EPSG:4326') as geometry")
    meta = ", ".join(f"{k}: '{v}'" for k, v in kv.items())
    kv_clause = f", kv_metadata {{{meta}}}" if kv else ""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.part")
    con.execute(f"copy (select {', '.join(cols)} from {src}) to '{tmp}' ({COPY}{kv_clause})")
    if (
        con.sql(
            f"select count(*) from read_parquet('{tmp}') where geometry is not null"
        ).fetchone()[0]
        == 0
    ):
        # With no geometry at all DuckDB writes no 'geo' metadata and the column would read
        # back as BLOB; the Parquet GEOMETRY logical type keeps its type and CRS instead.
        con.execute(
            f"copy (select {', '.join(cols)} from {src}) to '{tmp}' "
            f"({COPY}, geoparquet_version 'BOTH'{kv_clause})"
        )
        crs_how += "; no 'geo' metadata (nothing to describe), Parquet GEOMETRY logical type"
    tmp.replace(out)
    n = con.sql(f"select count(*) from '{out}'").fetchone()[0]
    return {
        "features": n,
        "dbf_records": header["records"],
        "fields": len(header["fields"]),
        "encoding": enc,
        "encoding_how": how,
        "crs": "EPSG:4326",
        "crs_how": crs_how,
        "parquet_bytes": out.stat().st_size,
    }


def extract(zpath: Path, member: str, dest: Path) -> Path:
    """The shapefile members of one layer, flat, into dest; returns the .shp path."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        for ext in SHAPE_EXT:
            n = member + ext
            if n in names:
                with z.open(n) as s, open(dest / Path(n).name, "wb") as d:
                    shutil.copyfileobj(s, d)
            elif ext in (".shp", ".shx", ".dbf"):
                raise SystemExit(f"{zpath.name}: no {n}")
    return dest / (Path(member).name + ".shp")


def declared_version(entry: dict, layer: str) -> str | None:
    v = entry.get("declared_versions", {})
    for k, text in v.items():
        if Path(k).name == f"{layer}.VERSION.txt":
            return text
    return None


def output_path(version: str, layer: dict) -> Path:
    return OUT / version / f"{layer['scale']}_{layer['category']}" / f"{layer['layer']}.parquet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", help="layer names, <scale>_<category> groups or scales")
    a = ap.parse_args()

    manifests = sorted(RAW.glob("*/MANIFEST.json"))
    if not manifests:
        raise SystemExit("no data/raw/<version>/MANIFEST.json; run scripts/01_download.py")
    for mpath in manifests:
        d = mpath.parent
        version = d.name
        man = json.loads(mpath.read_text())
        summary_path = OUT / version / "LAYERS.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        layers = [x for x in man["layers"].values() if x["zip"] in man["files"]]
        if a.only:
            want = set(a.only)
            layers = [
                x
                for x in layers
                if x["layer"] in want
                or f"{x['scale']}_{x['category']}" in want
                or x["scale"] in want
            ]
        for i, x in enumerate(layers, 1):
            entry = man["files"][x["zip"]]
            work = WORK / version / x["layer"]
            shp = extract(d / x["zip"], x["member"], work)
            ver = declared_version(entry, x["layer"])
            kv = {
                "natural_earth_layer": x["layer"],
                "natural_earth_version": ver or "",
                "source_url": entry["url"],
                "source_sha256": entry["sha256"],
            }
            out = output_path(version, x)
            info = export_layer(shp, out, kv)
            shutil.rmtree(work)
            summary[x["layer"]] = {
                "scale": x["scale"],
                "category": x["category"],
                "file": out.relative_to(OUT).as_posix(),
                "zip": x["zip"],
                "source": x["source"],
                "declared_version": ver,
                **info,
            }
            print(
                f"[{i}/{len(layers)}] {x['layer']}: {info['features']:,} features, "
                f"{info['fields']} fields, {info['parquet_bytes']:,} bytes",
                flush=True,
            )
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(dict(sorted(summary.items())), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
