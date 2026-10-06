# natural-earth

A reproducible copy of every Natural Earth vector layer at 1:10m, 1:50m and
1:110m, cultural and physical, as the publisher distributes them (one zip per
layer from `naciscdn.org`), plus one GeoParquet file per layer.

- Publisher: Natural Earth, <https://www.naturalearthdata.com/>
- Distribution: `https://naciscdn.org/naturalearth/<scale>/<category>/<layer>.zip`
- Layer list: the tree of the latest release tag of
  <https://github.com/nvkelso/natural-earth-vector>, read through the public
  GitHub API (v5.1.2, commit f1890d9f152c, when this was written)
- Dataset on the Hub: <https://huggingface.co/datasets/yuiseki/natural-earth>

## Licence

Natural Earth data is in the public domain: "All versions of Natural Earth
raster + vector map data found on this website are in the public domain."
(<https://www.naturalearthdata.com/about/terms-of-use/>). No attribution is
required; the same page credits third-party sources (The Washington Post,
EC JRC IES, XNR Productions, International Mapping Associates, Wikidata).
The code in this repository is MIT licensed (`LICENSE`).

## What is fetched

The CDN has no directory listing, so the layer list is the repository tree:
every `.shp` under `10m_cultural/`, `10m_physical/`, `50m_cultural/`,
`50m_physical/`, `110m_cultural/` and `110m_physical/` (215 layers at
v5.1.2; `updates/`, `packages/` and `tools/` are not data). Each layer is
fetched from its own zip on the CDN. For the few layers the CDN does not
serve on their own (it answers 403), the zip is the bundle the download
page of that scale and category links instead: the longest linked bundle
whose name begins the layer name, else the theme package
`<scale>_<category>.zip`. The manifest records which zip each layer came
from.

The six vector download pages on naturalearthdata.com are read too, to
record what they state next to each link (label, size, "version x.y.z"), the
links they carry that were not needed (theme packages, `_all` bundles,
building blocks, broken links), and the repository layers they do not link.

Versions: the directory is named after the release tag (`data/raw/v5.1.2/`).
The URLs carry no version; each zip declares its own in
`<layer>.VERSION.txt`, which the manifest records per zip. These differ from
layer to layer and from the release tag.

Not fetched: rasters (`01_download.py --list-rasters` records the 10m and
50m raster zips with their sizes, about 6.7 GB in 52 zips, without downloading them),
the theme packages and `_all` bundles whose layers are fetched on their own,
and the GeoPackage and other packages.

## How to run

```sh
uv run python scripts/01_download.py --dry-run        # plan: requests and bytes, nothing fetched
uv run python scripts/01_download.py --list-rasters   # full run: every layer, plus the raster listing
uv run python scripts/03_export_parquet.py            # zips -> data/parquet/<version>/
uv run python scripts/02_verify.py                    # zips against MANIFEST.json, Parquet against shapefiles
uv run python scripts/04_card.py                      # regenerate the layer tables in the card and provenance
uv run python -m pytest                               # unit tests
uv run python src/publish.py                          # dry run: the files that would go to the Hub
uv run python src/publish.py --push                   # upload to huggingface.co/datasets/yuiseki/natural-earth
```

A small slice: `--only` takes layer names, `<scale>_<category>` groups or a
scale, `--limit N` the first N layers, for example
`uv run python scripts/01_download.py --only 110m_physical ne_10m_land`.
The same `--only` works for `03_export_parquet.py` and `02_verify.py`.

Resuming: run the same command again. `MANIFEST.json` is rewritten after
every zip; a zip already recorded whose sha256 still matches is neither
fetched nor HEADed again, and a `.part` file left by an interrupted download
is overwritten. A full run from scratch makes about 230 HEAD and 206 GET
requests to the CDN (830 MB; 215 layers in 206 zips), 3 to the GitHub API
and 6 to naturalearthdata.com, one at a time with at least one second
between them; it took about 20 minutes on 2026-10-06. `--list-rasters` adds
about 20 page requests and 52 HEADs. A second run fetches nothing.

`01_download.py` uses the Python standard library only, sends
`User-Agent: natural-earth/1 (+https://github.com/yuiseki/natural-earth)`,
writes each download to `.part`, checks it against Content-Length and
renames it, and backs off on 429 and 5xx, honouring Retry-After.

`03_export_parquet.py` reads each layer with DuckDB's spatial extension
(ST_Read) and writes one GeoParquet file per layer: all `.dbf` fields in
order under their names, typed from the `.dbf` field definitions (read in
Python, not inferred), and the geometry as `GEOMETRY('EPSG:4326')` with
GeoParquet 1.0 metadata. No reprojection and no geometry repair. Text is
decoded in the encoding the layer's `.cpg` names; see the script's
docstring for the rules and for a layer without a `.cpg`.

`02_verify.py` re-hashes and test-reads every zip, then for every layer
compares feature counts (`.dbf`, `.shx`, Parquet), column names and types,
every cell against its own Python `.dbf` reader, and every geometry's WKB
against ST_Read; it counts invalid, null and empty geometries and reports
them without fixing them. It writes the counts to `data/work/<version>/VERIFY.json`.

`04_card.py` writes the `configs` list and the per-layer table of the
dataset card and the per-layer section of `data/provenance.yaml` from
`LAYERS.json`, `VERIFY.json` and the manifest.

`publish.py` refuses to run if the card does not declare every Parquet file
as a subset, or a raw zip's sha256 differs from its manifest.

## Layout

```
scripts/01_download.py         layer list from GitHub, zips from the CDN, MANIFEST.json
scripts/02_verify.py           zips against MANIFEST.json, Parquet against the shapefiles
scripts/03_export_parquet.py   one GeoParquet per layer, typed from the .dbf
scripts/04_card.py             generated parts of data/README.md and data/provenance.yaml
src/publish.py                 upload to the Hugging Face Hub (dry run by default)
tests/                         unit tests; tests/fixtures/ holds trimmed real responses and one small layer
data/README.md                 the dataset card (tracked)
data/LICENSE                   terms of use and what the Parquet changes (tracked)
data/provenance.yaml           source, versions, counts, checks (tracked)
data/raw/<version>/            zips as served, under <scale>/<category>/, plus MANIFEST.json (not in git)
data/parquet/<version>/        <scale>_<category>/<layer>.parquet and LAYERS.json (not in git)
data/work/                     shapefiles extracted while exporting and verifying, VERIFY.json (not in git)
```
