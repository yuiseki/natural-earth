import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("card", ROOT / "scripts" / "04_card.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

LAYERS = {
    "ne_10m_admin_0_countries": {
        "scale": "10m", "category": "cultural", "file": "v1/10m_cultural/ne_10m_admin_0_countries.parquet",
        "source": "layer", "zip": "10m/cultural/ne_10m_admin_0_countries.zip",
        "declared_version": "5.1.1", "features": 258, "fields": 168,
    },
    "ne_50m_ports": {
        "scale": "50m", "category": "cultural", "file": "v1/50m_cultural/ne_50m_ports.parquet",
        "source": "theme package", "zip": "50m/cultural/50m_cultural.zip",
        "declared_version": None, "features": 143, "fields": 9,
    },
}  # fmt: skip


def test_configs_declare_every_layer_and_one_default():
    block = m.configs("v1", LAYERS)
    assert m.publish.subset_entry("v1", "10m_cultural", "ne_10m_admin_0_countries") in block
    assert m.publish.subset_entry("v1", "50m_cultural", "ne_50m_ports") in block
    assert block.count("default: true") == 1


def test_replace_configs_keeps_the_rest_of_the_front_matter():
    card = "---\nlicense: other\nconfigs:\n- old\n---\n\n# body\n"
    out = m.replace_configs(card, "- new\n")
    assert out == "---\nlicense: other\nconfigs:\n- new\n---\n\n# body\n"


def test_table_names_the_bundle_a_layer_came_from():
    verify = {"ne_50m_ports": {"geometry": {"types": {"POINT": 143}, "invalid": 0}}}
    t = m.table(LAYERS, verify)
    assert "| ne_50m_ports | 143 | 9 | POINT 143 | 0 | none | 50m_cultural.zip |" in t
    assert "| ne_10m_admin_0_countries | 258 | 168 |  | ? | 5.1.1 | own |" in t
