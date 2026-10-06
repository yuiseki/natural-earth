#!/usr/bin/env python3
"""Check the raw zips against their manifest and every GeoParquet file against its shapefile.

Raw, for every data/raw/<version>/MANIFEST.json:
  - every zip exists, has the manifest's size and sha256, and reads to the
    end with zipfile.testzip (every member's CRC checked)
  - every layer the manifest assigns to a zip is in that zip

Parquet, for every layer (the shapefile is extracted again into data/work/):
  - feature counts: .dbf record count (and how many records are flagged
    deleted), .shx record count, Parquet rows; all must agree
  - columns: the .dbf field names in order, then geometry
  - types: each column has the type its .dbf field definition gives
    (03_export_parquet.duckdb_type)
  - every cell equals the value this script decodes from the .dbf itself
    with the Python reader in 03_export_parquet (strict decoding in the
    layer's encoding), compared one by one; floats must be equal exactly
  - every geometry's WKB equals what ST_Read returns for the same record
  - the 'geo' metadata names EPSG:4326 (a layer whose shapes are all null
    has no 'geo' metadata; its column must then be GEOMETRY('EPSG:4326'))
  - geometry validity is counted (ST_IsValid), with null and empty
    geometries and the geometry types. Invalid geometries are reported, not
    fixed, and are not a failure: Natural Earth ships them that way.

Writes data/work/<version>/VERIFY.json with the counts per layer, prints one
line per check, and exits non-zero on any failure.

    uv run python scripts/02_verify.py
    uv run python scripts/02_verify.py --only 110m_physical    # Parquet checks for a subset
"""

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
PQ = ROOT / "data" / "parquet"
WORK = ROOT / "data" / "work"

_spec = importlib.util.spec_from_file_location("export", ROOT / "scripts" / "03_export_parquet.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
    if bad is not None:
        raise zipfile.BadZipFile(f"bad member {bad}")


def shapefiles_in(z: zipfile.ZipFile) -> list[str]:
    return sorted(n[:-4] for n in z.namelist() if n.lower().endswith(".shp"))


def values_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return float(a) == float(b) or (math.isnan(float(a)) and math.isnan(float(b)))
    return a == b and type(a) is type(b)


def compare_layer(shp: Path, parquet: Path) -> dict:
    """Everything 02_verify checks for one layer; 'problems' lists the failures."""
    import duckdb

    problems = []
    dbf = shp.with_suffix(".dbf")
    header = export.read_dbf_header(dbf)
    cpg = shp.with_suffix(".cpg")
    cpg_text = cpg.read_text(encoding="ascii").strip() if cpg.exists() else None
    texts = [] if cpg_text else export.text_values(dbf, header)
    enc, _ = export.layer_encoding(cpg_text, header["ldid"], texts)
    records = list(export.read_records(dbf, header, enc))
    live = [r["values"] for r in records if not r["deleted"]]
    shx = export.shx_count(shp.with_suffix(".shx"))

    con = duckdb.connect()
    con.execute("load spatial")
    con.execute("set memory_limit = '8GB'")
    rel = con.sql(f"select * from '{parquet}'")
    names = [f["name"] for f in header["fields"]]
    rows = rel.fetchall()
    text_idx = [i for i, f in enumerate(header["fields"]) if f["type"] == "C"]
    nul_padded = leading = 0
    for _, vals in export.raw_records(dbf, header):
        for i in text_idx:
            raw = vals[i]
            cut = raw.split(b"\0", 1)[0]
            nul_padded += len(cut) < len(raw)
            leading += cut.strip(b" ") != b"" and cut.startswith(b" ")
    out = {
        "text_nul_padded": nul_padded,
        "text_leading_space_trimmed": leading,
        "dbf_records": header["records"],
        "dbf_deleted": header["records"] - len(live),
        "shx_records": shx,
        "parquet_rows": len(rows),
        "encoding": enc,
    }
    if shx != header["records"]:
        problems.append(f".shx has {shx} records, .dbf {header['records']}")
    if len(rows) != len(live):
        problems.append(f"Parquet has {len(rows)} rows, the .dbf {len(live)} live records")
    if rel.columns != names + ["geometry"]:
        problems.append(f"columns differ from the .dbf fields: {rel.columns}")
    else:
        want = [export.duckdb_type(f) for f in header["fields"]] + ["GEOMETRY('EPSG:4326')"]
        got = [str(t) for t in rel.types]
        wrong = [(n, g, w) for n, g, w in zip(rel.columns, got, want, strict=True) if g != w]
        if wrong:
            problems.append(f"types differ from the .dbf definitions: {wrong[:5]}")
        diffs, cells = [], 0
        for i, (a, b) in enumerate(zip(live, rows, strict=False)):
            for n, x, y in zip(names, a, b, strict=False):
                cells += 1
                if not values_equal(x, y):
                    diffs.append((i, n, x, y))
        out["cells_compared"] = cells
        if diffs:
            problems.append(f"{len(diffs)} cells differ from the .dbf, e.g. {diffs[:3]}")

    geo = dict(
        con.sql(
            f"select decode(key), decode(value) from parquet_kv_metadata('{parquet}')"
        ).fetchall()
    ).get("geo")
    crs = json.loads(geo)["columns"]["geometry"].get("crs", {}).get("id") if geo else None
    nonnull = con.sql(f"select count(*) from '{parquet}' where geometry is not null").fetchone()[0]
    if geo is None and nonnull == 0:
        # an attribute-only layer: no 'geo' metadata, the logical type carries the CRS
        coltype = str(dict(zip(rel.columns, rel.types, strict=True)).get("geometry"))
        if coltype != "GEOMETRY('EPSG:4326')":
            problems.append(f"all geometries null and the column is {coltype}")
    elif crs != {"authority": "EPSG", "code": 4326}:
        problems.append(f"geo metadata CRS is {crs}")

    g = con.sql(
        f"""select count(*) filter (where geometry is null),
                   count(*) filter (where geometry is not null and ST_IsEmpty(geometry)),
                   count(*) filter (where geometry is not null and not ST_IsValid(geometry))
            from '{parquet}'"""
    ).fetchone()
    types = dict(
        con.sql(
            f"""select ST_GeometryType(geometry)::varchar, count(*) from '{parquet}'
                where geometry is not null group by 1 order by 1"""
        ).fetchall()
    )
    wkb = con.sql(
        f"""with p as (select file_row_number as i, ST_AsWKB(geometry) as w
                       from read_parquet('{parquet}', file_row_number = true)),
                 s as (select row_number() over () - 1 as i, ST_AsWKB(geom) as w
                       from ST_Read('{shp}'))
            select count(*) from p full join s using (i)
            where p.w is distinct from s.w"""
    ).fetchone()[0]
    if wkb:
        problems.append(f"{wkb} geometries differ from ST_Read")
    out["geometry"] = {
        "null": g[0],
        "empty": g[1],
        "invalid": g[2],
        "types": types,
        "wkb_differs": wkb,
    }
    out["problems"] = problems
    return out


def verify_raw(d: Path, man: dict) -> int:
    failures = 0
    for key, e in man["files"].items():
        p = d / key
        tag = f"{d.name}/{key}"
        if not p.exists() or p.stat().st_size != e["bytes"] or sha256(p) != e["sha256"]:
            print(f"FAIL {tag}: missing, or size or sha256 differs from the manifest")
            failures += 1
            continue
        try:
            check_zip(p)
        except Exception as ex:  # zlib.error and friends as well as BadZipFile
            print(f"FAIL {tag}: {ex}")
            failures += 1
            continue
        print(f"ok   {tag}: sha256, zip read to the end")
    for name, x in man["layers"].items():
        if x["zip"] not in man["files"]:
            continue
        with zipfile.ZipFile(d / x["zip"]) as z:
            if x.get("member") not in shapefiles_in(z):
                print(f"FAIL {d.name}/{x['zip']}: no {x.get('member')}.shp for {name}")
                failures += 1
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", help="layer names, <scale>_<category> groups or scales")
    a = ap.parse_args()

    manifests = sorted(RAW.glob("*/MANIFEST.json"))
    if not manifests:
        print("FAIL no MANIFEST.json under data/raw/")
        return 1
    failures = 0
    for mpath in manifests:
        d = mpath.parent
        version = d.name
        man = json.loads(mpath.read_text())
        failures += verify_raw(d, man)
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
        rpath = WORK / version / "VERIFY.json"
        report = json.loads(rpath.read_text()) if rpath.exists() else {}
        for x in layers:
            name = x["layer"]
            pq = export.output_path(version, x)
            if not pq.exists():
                print(f"FAIL {name}: no {pq.relative_to(ROOT)}")
                failures += 1
                continue
            work = WORK / version / "verify" / name
            shp = export.extract(d / x["zip"], x["member"], work)
            r = compare_layer(shp, pq)
            shutil.rmtree(work)
            report[name] = r
            gm = r["geometry"]
            line = (
                f"{name}: {r['parquet_rows']:,} features (dbf {r['dbf_records']:,}, "
                f"shx {r['shx_records']:,}), {r.get('cells_compared', 0):,} cells; "
                f"geometry invalid {gm['invalid']}, null {gm['null']}, empty {gm['empty']}"
            )
            if r["problems"]:
                print(f"FAIL {line}: {'; '.join(r['problems'])}")
                failures += 1
            else:
                print(f"ok   {line}")
        missing = sorted(set(man["layers"]) - {x["layer"] for x in layers})
        if not a.only and missing:
            print(f"FAIL {version}: layers without a zip: {missing}")
            failures += 1
        rpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.write_text(json.dumps(dict(sorted(report.items())), indent=1) + "\n")
        print(
            f"{version}: unavailable layers (no zip on the CDN): {sorted(man.get('unavailable', {}))}"
        )
    print("all ok" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
