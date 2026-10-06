#!/usr/bin/env python3
"""Fetch every Natural Earth vector layer, one zip per layer, as the publisher serves it.

Where the layer list comes from
    Natural Earth publishes no machine-readable index: the CDN directory
    https://naciscdn.org/naturalearth/10m/cultural/ answers 200 with an empty
    body. The list is therefore taken from the build repository the
    distribution is made from, github.com/nvkelso/natural-earth-vector, through
    the public GitHub API: the tree of its latest release tag
    (GET /repos/nvkelso/natural-earth-vector/releases/latest, then
    /git/trees/<tag>?recursive=1). Every .shp under the six data directories
    10m_cultural, 10m_physical, 50m_cultural, 50m_physical, 110m_cultural and
    110m_physical is one layer (updates/, packages/ and tools/ are not data).
    A few layers sit one directory deeper (10m_physical/ne_10m_bathymetry_all/,
    */ne_*_graticules_all/); they are still one layer each.

Where each layer is fetched from
    https://naciscdn.org/naturalearth/<scale>/<category>/<layer>.zip, the
    publisher's distribution, which the download buttons on
    naturalearthdata.com point to. Some layers of the repository have no zip
    of their own there (the CDN answers 403). For those, and only for those,
    the zip is a bundle linked from the download page of the same scale and
    category: first the longest linked bundle whose name begins the layer's
    name (ne_10m_parks_and_protected_lands.zip holds the four parks layers),
    else the theme package <scale>_<category>.zip. A layer that is in neither
    is recorded as unavailable, never fetched from a guessed URL.

    The six download pages (https://www.naturalearthdata.com/downloads/
    <scale>-<category>-vectors/) are also read to record what the site states
    next to each link: its label, size and "version x.y.z".

Versions
    The directory is named after the release tag (data/raw/v5.1.2/). The CDN
    URLs carry no version; each zip declares its own in <layer>.VERSION.txt,
    which is recorded per zip in the manifest as found.

Rasters
    --list-rasters reads the 10m and 50m raster download pages and their
    sub-pages, HEADs every raster zip, and records URL, size and Last-Modified
    under "rasters" in the manifest. Rasters are never downloaded by this
    script (6.7 GB in 52 zips on 2026-10-06).

Politeness
    Standard library HTTP, sequential, at least 1 second between requests,
    exponential back-off on 429/500/502/503/504 honouring Retry-After.
    Downloads go to <name>.part, are checked against Content-Length, then
    renamed. The manifest is rewritten after every file, so an interrupted run
    resumes where it stopped; a zip already in the manifest whose sha256 still
    matches is neither fetched nor HEADed again.

    uv run python scripts/01_download.py                   # everything
    uv run python scripts/01_download.py --dry-run         # only the estimate
    uv run python scripts/01_download.py --only 110m_physical ne_10m_land
    uv run python scripts/01_download.py --limit 5
    uv run python scripts/01_download.py --list-rasters
"""

import argparse
import hashlib
import html as htmllib
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
UA = "natural-earth/1 (+https://github.com/yuiseki/natural-earth)"
GITHUB_REPO = "nvkelso/natural-earth-vector"
API = f"https://api.github.com/repos/{GITHUB_REPO}"
CDN = "https://naciscdn.org/naturalearth"
SITE = "https://www.naturalearthdata.com"
SCALES = ("10m", "50m", "110m")
CATEGORIES = ("cultural", "physical")
DATA_DIR = re.compile(r"^(10m|50m|110m)_(cultural|physical)$")
TERMS_URL = f"{SITE}/about/terms-of-use/"
DELAY = 1.0
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_TRIES = 6

_last_request = 0.0


# ---------------------------------------------------------------- planning


def layers_from_tree(tree: dict) -> list[dict]:
    """One entry per .shp under the six data directories of the repository tree."""
    if tree.get("truncated"):
        raise SystemExit("the GitHub tree is truncated; the layer list would be incomplete")
    out = []
    for e in tree["tree"]:
        parts = e["path"].split("/")
        if e.get("type") != "blob" or not parts[-1].endswith(".shp"):
            continue
        mt = DATA_DIR.match(parts[0])
        if not mt or len(parts) not in (2, 3):
            continue
        out.append(
            {
                "layer": parts[-1][: -len(".shp")],
                "scale": mt.group(1),
                "category": mt.group(2),
                "github_path": e["path"],
                "github_dir": parts[1] if len(parts) == 3 else None,
            }
        )
    order = {(s, c): i for i, (s, c) in enumerate((s, c) for s in SCALES for c in CATEGORIES)}
    out.sort(key=lambda x: (order[(x["scale"], x["category"])], x["layer"].lower()))
    return out


def cdn_url(scale: str, category: str, stem: str) -> str:
    return f"{CDN}/{scale}/{category}/{stem}.zip"


_LINK = re.compile(
    r'href="[^"]*/download/(10m|50m|110m)/(cultural|physical)/([^"/]+)\.zip">'
    r"(.*?)</a>\s*(?:<span[^>]*>\s*\(([^)]*)\)\s*version\s*([0-9][0-9.]*)\s*</span>)?",
    re.S,
)


def parse_download_page(html: str) -> list[dict]:
    """Every zip link of a vector download page, with the size and version the page states."""
    out, seen = [], set()
    for scale, cat, stem, label, size, version in _LINK.findall(html):
        if (scale, cat, stem) in seen:
            continue
        seen.add((scale, cat, stem))
        out.append(
            {
                "scale": scale,
                "category": cat,
                "stem": stem,
                "label": " ".join(htmllib.unescape(re.sub(r"<[^>]+>", "", label)).split()),
                "page_size": size or None,
                "page_version": version.rstrip(".") or None,
            }
        )
    return out


def candidates(layer: dict, page_zips: list[dict], known_layers=frozenset()) -> list[tuple]:
    """Zips that may hold the layer, in the order they are tried."""
    s, c, name = layer["scale"], layer["category"], layer["layer"]
    out = [(name, "layer")]
    linked = [
        z["stem"]
        for z in page_zips
        if (z["scale"], z["category"]) == (s, c)
        and z["stem"] not in known_layers
        and z["stem"] != name
        and name.startswith(z["stem"] + "_")
    ]
    out += [(b, "bundle") for b in sorted(linked, key=len, reverse=True)]
    package = f"{s}_{c}"
    if any((z["scale"], z["category"], z["stem"]) == (s, c, package) for z in page_zips):
        out.append((package, "theme package"))
    return out


def choose_source(layer, served, page_zips, known_layers=frozenset()):
    """The first candidate zip the CDN serves, as (stem, kind), or None."""
    for stem, kind in candidates(layer, page_zips, known_layers):
        if (layer["scale"], layer["category"], stem) in served:
            return stem, kind
    return None


def select(layers: list[dict], only: list[str] | None, limit: int | None) -> list[dict]:
    """--only takes layer names, <scale>_<category> groups or a scale; --limit the first N."""
    out = layers
    if only:
        want = set(only)
        out = [
            x
            for x in layers
            if x["layer"] in want or f"{x['scale']}_{x['category']}" in want or x["scale"] in want
        ]
        if not out:
            raise SystemExit(f"nothing matches --only {' '.join(only)}")
    if limit is not None:
        out = out[:limit]
    return out


def zip_layers(path: Path) -> dict[str, str]:
    """layer name -> member path without .shp, for every shapefile in the zip."""
    with zipfile.ZipFile(path) as z:
        return {
            Path(n).stem: n[: -len(".shp")]
            for n in z.namelist()
            if n.lower().endswith(".shp") and not n.startswith("__MACOSX")
        }


def zip_versions(path: Path) -> dict[str, str]:
    """Every *VERSION.txt member and what it says."""
    out = {}
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.upper().endswith("VERSION.TXT") or Path(n).name == "VERSION":
                out[n] = z.read(n).decode("utf-8", "replace").strip()
    return out


def backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after and retry_after.strip().isdigit():
        return float(retry_after)
    return float(min(300, 5 * 2**attempt))


def fmt_bytes(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


# ---------------------------------------------------------------- HTTP


def _request(url: str, method: str = "GET", headers: dict | None = None):
    """Open url politely; returns the response, or the HTTPError for a 4xx other than 429."""
    global _last_request
    for attempt in range(MAX_TRIES):
        wait = DELAY - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        h = {"User-Agent": UA, **(headers or {})}
        req = urllib.request.Request(url, headers=h, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=300)
            _last_request = time.monotonic()
            return r
        except urllib.error.HTTPError as e:
            _last_request = time.monotonic()
            if e.code in RETRY_STATUS and attempt + 1 < MAX_TRIES:
                pause = backoff(attempt, e.headers.get("Retry-After"))
                print(f"  {e.code} on {url}, waiting {pause:.0f}s", flush=True)
                time.sleep(pause)
                continue
            return e
        except (urllib.error.URLError, TimeoutError) as e:
            _last_request = time.monotonic()
            if attempt + 1 < MAX_TRIES:
                pause = backoff(attempt, None)
                print(f"  {e} on {url}, waiting {pause:.0f}s", flush=True)
                time.sleep(pause)
                continue
            raise
    raise RuntimeError("unreachable")


def get_text(url: str, headers: dict | None = None) -> str:
    r = _request(url, headers=headers)
    if isinstance(r, urllib.error.HTTPError):
        raise SystemExit(f"{url}: HTTP {r.code}")
    with r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8")


def get_json(url: str):
    return json.loads(get_text(url, {"Accept": "application/vnd.github+json"}))


def head(url: str) -> dict:
    r = _request(url, "HEAD")
    if isinstance(r, urllib.error.HTTPError):
        return {"status": r.code}
    with r:
        n = r.headers.get("Content-Length")
        return {
            "status": r.status,
            "bytes": int(n) if n else None,
            "last_modified": r.headers.get("Last-Modified", ""),
        }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    r = _request(url)
    if isinstance(r, urllib.error.HTTPError):
        raise SystemExit(f"{url}: HTTP {r.code}")
    with r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
        length = r.headers.get("Content-Length")
        lm = r.headers.get("Last-Modified", "")
        etag = r.headers.get("ETag", "")
    got = tmp.stat().st_size
    if length is not None and got != int(length):
        tmp.unlink()
        raise SystemExit(f"{url}: got {got} bytes, the server stated {length}")
    tmp.replace(dest)
    return {
        "url": url,
        "bytes": got,
        "sha256": sha256(dest),
        "last_modified": lm,
        "etag": etag,
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------- rasters


def raster_subpages(html: str, scale: str) -> list[str]:
    base = f"{SITE}/downloads/{scale}-raster-data/"
    out = set()
    for href in re.findall(rf'href="([^"]*downloads/{scale}-raster-data/[^"]+)"', html):
        u = urllib.parse.urljoin(base, href)
        u = u.replace(f"/downloads/{scale}-raster-data/downloads/", "/downloads/")
        u = u.rstrip("/") + "/"
        if u != base:
            out.add(u)
    return sorted(out)


def raster_zips(html: str) -> list[tuple[str, str]]:
    return sorted(
        set(re.findall(r'href="[^"]*/download/(10m|50m|110m)/raster/([^"/]+)\.zip"', html))
    )


def list_rasters() -> dict:
    found = set()
    for scale in ("10m", "50m"):
        index = get_text(f"{SITE}/downloads/{scale}-raster-data/")
        found.update(raster_zips(index))
        for sub in raster_subpages(index, scale):
            found.update(raster_zips(get_text(sub)))
    items = []
    for scale, stem in sorted(found):
        url = f"{CDN}/{scale}/raster/{stem}.zip"
        items.append({"url": url, **head(url)})
    total = sum(x.get("bytes") or 0 for x in items)
    print(f"rasters: {len(items)} zips, {fmt_bytes(total)} (listed, not downloaded)")
    return {
        "listed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zips": items,
        "total_bytes": total,
        "downloaded": False,
    }


# ---------------------------------------------------------------- main


def write_manifest(path: Path, manifest: dict) -> None:
    out = {**manifest, "files": dict(sorted(manifest["files"].items()))}
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--only", nargs="+", help="layer names, <scale>_<category> groups or scales")
    ap.add_argument("--limit", type=int, help="only the first N selected layers")
    ap.add_argument("--dry-run", action="store_true", help="print the estimate and stop")
    ap.add_argument("--list-rasters", action="store_true", help="record the raster zips and sizes")
    a = ap.parse_args()

    release = get_json(f"{API}/releases/latest")
    tag = release["tag_name"]
    ref = get_json(f"{API}/git/ref/tags/{urllib.parse.quote(tag)}")["object"]
    if ref["type"] == "tag":
        ref = get_json(ref["url"])["object"]
    tree = get_json(f"{API}/git/trees/{urllib.parse.quote(tag)}?recursive=1")
    layers = layers_from_tree(tree)
    known = {x["layer"] for x in layers}
    print(f"{GITHUB_REPO} {tag} ({ref['sha'][:12]}): {len(layers)} layers", flush=True)

    page_zips, pages = [], []
    for s in SCALES:
        for c in CATEGORIES:
            url = f"{SITE}/downloads/{s}-{c}-vectors/"
            page_zips += parse_download_page(get_text(url))
            pages.append(url)
    linked = {(z["scale"], z["category"], z["stem"]): z for z in page_zips}
    print(f"download pages: {len(page_zips)} zip links", flush=True)

    d = RAW / tag
    d.mkdir(parents=True, exist_ok=True)
    mpath = d / "MANIFEST.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {"files": {}, "layers": {}}
    manifest.setdefault("unavailable", {})
    manifest.update(
        {
            "version": tag,
            "catalogue": {
                "layer_list": f"https://github.com/{GITHUB_REPO}/tree/{tag}",
                "api": f"{API}/git/trees/{tag}?recursive=1",
                "release": tag,
                "release_published_at": release.get("published_at"),
                "commit": ref["sha"],
                "tree_sha": tree["sha"],
                "layers_in_tree": len(layers),
                "read_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "download_pages": pages,
            "distribution": f"{CDN}/<scale>/<category>/<layer>.zip",
            "licence": f"public domain ({TERMS_URL})",
        }
    )
    files, mlayers = manifest["files"], manifest["layers"]

    def done(key: str) -> bool:
        e = files.get(key)
        return (
            bool(e)
            and (d / key).exists()
            and (d / key).stat().st_size == e["bytes"]
            and (sha256(d / key) == e["sha256"])
        )

    chosen = select(layers, a.only, a.limit)
    heads: dict[tuple, dict] = {}
    plan: dict[str, list] = {}  # zip key -> [layers]
    sizes: dict[str, int | None] = {}
    for layer in chosen:
        name = layer["layer"]
        prev = mlayers.get(name)
        if prev and done(prev["zip"]):
            continue
        source = None
        for stem, kind in candidates(layer, page_zips, known):
            key3 = (layer["scale"], layer["category"], stem)
            key = f"{layer['scale']}/{layer['category']}/{stem}.zip"
            if key in files and done(key):
                source = (stem, kind, key)
                break
            if key3 not in heads:
                heads[key3] = head(cdn_url(*key3))
            if heads[key3]["status"] == 200:
                source = (stem, kind, key)
                sizes[key] = heads[key3].get("bytes")
                break
        if source is None:
            tried = {
                s: heads.get((layer["scale"], layer["category"], s), {}).get("status")
                for s, _ in candidates(layer, page_zips, known)
            }
            manifest["unavailable"][name] = {**layer, "tried": tried}
            print(f"  {name}: no zip on the CDN holds it ({tried})", flush=True)
            continue
        manifest["unavailable"].pop(name, None)
        stem, kind, key = source
        mlayers[name] = {**layer, "zip": key, "source": kind}
        plan.setdefault(key, []).append(name)

    todo = [k for k in plan if not done(k)]
    total = sum(sizes.get(k) or 0 for k in todo)
    print(
        f"plan: {len(chosen)} layers selected, {len(todo)} zips to fetch, "
        f"{len(todo)} requests, {fmt_bytes(total)} stated by Content-Length; "
        f"at least {len(todo) * DELAY:.0f}s at {DELAY:.0f}s per request",
        flush=True,
    )
    if a.dry_run:
        return 0

    for i, key in enumerate(todo, 1):
        scale, cat, fname = key.split("/")
        stem = fname[: -len(".zip")]
        entry = fetch(cdn_url(scale, cat, stem), d / key)
        z = linked.get((scale, cat, stem))
        if z:
            entry["page"] = {k: z[k] for k in ("label", "page_size", "page_version")}
        entry["layers"] = sorted(zip_layers(d / key))
        entry["declared_versions"] = zip_versions(d / key)
        files[key] = entry
        missing = [n for n in plan[key] if n not in entry["layers"]]
        if missing:
            raise SystemExit(f"{key} does not hold {missing}")
        write_manifest(mpath, manifest)
        print(
            f"[{i}/{len(todo)}] {key}: {entry['bytes']:,} bytes, {entry['last_modified']}",
            flush=True,
        )

    for name, x in mlayers.items():
        if x["zip"] in files:
            x["member"] = zip_layers(d / x["zip"])[name]

    if not a.only and a.limit is None:
        used = {tuple(k[: -len(".zip")].split("/")) for k in files}
        not_taken = []
        for key3, z in sorted(linked.items()):
            if key3 in used:
                continue
            h = heads.get(key3) or head(cdn_url(*key3))
            not_taken.append({**z, "url": cdn_url(*key3), **h})
        manifest["page_links_not_fetched"] = not_taken
        manifest["tree_layers_not_linked_on_pages"] = sorted(
            x["layer"] for x in layers if (x["scale"], x["category"], x["layer"]) not in linked
        )

    if a.list_rasters:
        manifest["rasters"] = list_rasters()

    write_manifest(mpath, manifest)
    print(
        f"{tag}: {len(mlayers)} layers in {len(files)} zips recorded, "
        f"{len(manifest['unavailable'])} unavailable, {len(todo)} fetched this run"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
