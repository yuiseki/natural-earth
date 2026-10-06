import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "publish", Path(__file__).parents[1] / "src" / "publish.py"
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _data(tmp_path, card_layers=("ne_10m_land",), raw=b"x", manifest_raw=b"x"):
    d = tmp_path / "data"
    p = d / "parquet" / "v1" / "10m_physical"
    p.mkdir(parents=True)
    (p / "ne_10m_land.parquet").write_bytes(b"PAR1")
    (d / "parquet" / "v1" / "LAYERS.json").write_text("{}")
    r = d / "raw" / "v1" / "10m" / "physical"
    r.mkdir(parents=True)
    (r / "ne_10m_land.zip").write_bytes(raw)
    files = {"10m/physical/ne_10m_land.zip": {"sha256": hashlib.sha256(manifest_raw).hexdigest()}}
    (d / "raw" / "v1" / "MANIFEST.json").write_text(json.dumps({"files": files}))
    card = "---\nconfigs:\n"
    card += "".join(m.subset_entry("v1", "10m_physical", n) for n in card_layers) + "---\n"
    (d / "README.md").write_text(card)
    for f in ("LICENSE", "provenance.yaml"):
        (d / f).write_text("x")
    return d


def test_subset_entry():
    assert m.subset_entry("v5.1.2", "10m_physical", "ne_10m_land") == (
        "- config_name: ne_10m_land\n"
        "  data_files: parquet/v5.1.2/10m_physical/ne_10m_land.parquet\n"
    )


def test_collect_lists_card_parquet_and_raw(tmp_path):
    assert m.collect(_data(tmp_path)) == [
        "README.md",
        "LICENSE",
        "provenance.yaml",
        "parquet/v1/10m_physical/ne_10m_land.parquet",
        "parquet/v1/LAYERS.json",
        "raw/v1/10m/physical/ne_10m_land.zip",
        "raw/v1/MANIFEST.json",
    ]


def test_a_parquet_the_card_does_not_declare_stops_it(tmp_path):
    with pytest.raises(SystemExit, match="ne_10m_land"):
        m.collect(_data(tmp_path, card_layers=("other",)))


def test_a_raw_file_unlike_its_manifest_stops_it(tmp_path):
    with pytest.raises(SystemExit, match="ne_10m_land.zip"):
        m.collect(_data(tmp_path, raw=b"changed"))
