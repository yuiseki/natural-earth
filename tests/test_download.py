import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIX = Path(__file__).parent / "fixtures"
_spec = importlib.util.spec_from_file_location("download", ROOT / "scripts" / "01_download.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def tree():
    return json.loads((FIX / "tree-v5.1.2.json").read_text())


def page():
    return (FIX / "10m-cultural-vectors.html").read_text()


def test_layers_from_tree_takes_the_six_data_directories_only():
    layers = m.layers_from_tree(tree())
    assert [x["layer"] for x in layers] == [
        "ne_10m_admin_0_countries",
        "ne_10m_parks_and_protected_lands_area",
        "ne_10m_bathymetry_A_10000",
        "ne_10m_land",
        "ne_50m_airports",
        "ne_110m_land",
    ]
    by = {x["layer"]: x for x in layers}
    assert by["ne_10m_admin_0_countries"] == {
        "layer": "ne_10m_admin_0_countries",
        "scale": "10m",
        "category": "cultural",
        "github_path": "10m_cultural/ne_10m_admin_0_countries.shp",
        "github_dir": None,
    }
    # a layer kept one directory deeper keeps that directory, which is also a zip on the CDN
    assert by["ne_10m_bathymetry_A_10000"]["github_dir"] == "ne_10m_bathymetry_all"


def test_layers_from_tree_refuses_a_truncated_tree():
    t = tree()
    t["truncated"] = True
    with pytest.raises(SystemExit, match="truncated"):
        m.layers_from_tree(t)


def test_cdn_url():
    assert m.cdn_url("10m", "cultural", "ne_10m_admin_0_countries") == (
        "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
    )


def test_parse_download_page_reads_link_label_size_and_version():
    links = m.parse_download_page(page())
    stems = [x["stem"] for x in links]
    assert stems == [
        "10m_cultural",
        "ne_10m_admin_0_countries",
        "ne_10m_admin_0_countries_lakes",
        "10m-admin-0-boundary-lines-maritime",
        "ne_10m_parks_and_protected_lands",
        "ne_10m_cultural_building_blocks_all",
    ]
    c = links[1]
    assert c == {
        "scale": "10m",
        "category": "cultural",
        "stem": "ne_10m_admin_0_countries",
        "label": "Download countries",
        "page_size": "4.7 MB",
        "page_version": "5.1.1",
    }
    assert links[0]["page_version"] == "5.1.1"
    assert links[3]["page_version"] == "5.1.0"


def test_choose_source_prefers_the_layer_zip():
    page_zips = m.parse_download_page(page())
    served = {("10m", "cultural", "ne_10m_admin_0_countries")}
    layer = {"layer": "ne_10m_admin_0_countries", "scale": "10m", "category": "cultural"}
    assert m.choose_source(layer, served, page_zips) == ("ne_10m_admin_0_countries", "layer")


def test_choose_source_falls_back_to_the_longest_linked_bundle_then_the_theme_package():
    page_zips = m.parse_download_page(page())
    served = {
        ("10m", "cultural", "10m_cultural"),
        ("10m", "cultural", "ne_10m_parks_and_protected_lands"),
    }
    parks = {
        "layer": "ne_10m_parks_and_protected_lands_area",
        "scale": "10m",
        "category": "cultural",
    }
    assert m.choose_source(parks, served, page_zips) == (
        "ne_10m_parks_and_protected_lands",
        "bundle",
    )
    names = {"layer": "ne_10m_admin_0_names", "scale": "10m", "category": "cultural"}
    assert m.choose_source(names, served, page_zips) == ("10m_cultural", "theme package")
    # nothing served: no source, rather than a guessed URL
    assert m.choose_source(names, set(), page_zips) is None


def test_select_by_layer_group_and_limit():
    layers = m.layers_from_tree(tree())
    assert [x["layer"] for x in m.select(layers, ["110m_physical"], None)] == ["ne_110m_land"]
    assert [x["layer"] for x in m.select(layers, ["ne_50m_airports", "10m_physical"], None)] == [
        "ne_10m_bathymetry_A_10000",
        "ne_10m_land",
        "ne_50m_airports",
    ]
    assert len(m.select(layers, None, 2)) == 2
    assert len(m.select(layers, ["10m"], None)) == 4
    with pytest.raises(SystemExit, match="nothing matches"):
        m.select(layers, ["ne_10m_nothing"], None)


def _zip(members: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return b.getvalue()


def test_zip_layers_and_versions(tmp_path):
    p = tmp_path / "a.zip"
    p.write_bytes(
        _zip(
            {
                "ne_10m_land.shp": b"",
                "ne_10m_land.dbf": b"",
                "ne_10m_land.VERSION.txt": b"5.0.0\n",
                "sub/ne_10m_ocean.shp": b"",
                "ne_10m_ocean.README.html": b"",
            }
        )
    )
    assert m.zip_layers(p) == {"ne_10m_land": "ne_10m_land", "ne_10m_ocean": "sub/ne_10m_ocean"}
    assert m.zip_versions(p) == {"ne_10m_land.VERSION.txt": "5.0.0"}


def test_backoff_honours_retry_after_and_grows():
    assert m.backoff(0, None) == 5
    assert m.backoff(2, None) == 20
    assert m.backoff(0, "30") == 30
    assert m.backoff(9, None) == 300


def test_fmt_bytes():
    assert m.fmt_bytes(459_484_960) == "459.5 MB"


def test_write_manifest_sorts_without_detaching_the_callers_dict(tmp_path):
    manifest = {"files": {"b.zip": {}, "a.zip": {}}}
    files = manifest["files"]
    p = tmp_path / "MANIFEST.json"
    m.write_manifest(p, manifest)
    files["c.zip"] = {}
    m.write_manifest(p, manifest)
    assert list(json.loads(p.read_text())["files"]) == ["a.zip", "b.zip", "c.zip"]
