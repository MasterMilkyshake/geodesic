"""The distributed configuration examples load, and the distributed example outputs are synthetic and labelled."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from geodesic import protocol
from geodesic.config import LocalSettings, load_project_config, load_yaml
from geodesic.provenance import SYNTHETIC_LABEL, detect_embedded_tag

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
DEMO = REPO / "examples" / "synthetic_demo"


def test_example_project_configuration_loads_and_names_no_research_field():
    cfg = load_project_config(CONFIG / "project.example.yaml")
    assert [s.key for s in cfg.stages] == ["stage_1", "stage_2", "stage_3", "stage_4"]
    assert cfg.search.concepts == [] and cfg.reviewers == []


def test_example_local_settings_and_credentials_hold_placeholders_only():
    local = LocalSettings.model_validate(load_yaml(CONFIG / "local.example.yaml"))
    assert local.contact_email.endswith("@example.org")          # reserved example domain, not a person
    assert local.reviewer == "Your Name"
    assignments = [ln for ln in (CONFIG / "credentials.env.example").read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    assert assignments == []                                       # no credential value is distributed


def test_distributed_protocol_template_is_the_empty_template():
    text = (CONFIG / "protocol.template.yaml").read_text(encoding="utf-8")
    assert text == protocol.template_text()
    loaded = protocol.load_protocol_file(CONFIG / "protocol.template.yaml")
    assert loaded.research_question == "" and loaded.completeness_problems()


def test_no_personal_email_address_in_source_configuration_or_documentation():
    pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    allowed = ("example.org", "example.com", "example.net", "anthropic.com", ".invalid", ".test", ".example")
    found = []
    for base in ("src", "config", "docs", "tests"):
        for path in (REPO / base).rglob("*"):
            if path.suffix in (".py", ".yaml", ".yml", ".md", ".txt", ".example", ".toml") and path.is_file():
                for m in pattern.findall(path.read_text(encoding="utf-8", errors="replace")):
                    if not m.lower().endswith(allowed):
                        found.append((str(path.relative_to(REPO)), m))
    assert found == []


@pytest.mark.skipif(not DEMO.exists(), reason="examples directory not distributed")
def test_distributed_example_outputs_are_synthetic_and_labelled():
    report = DEMO / "report"
    files = [p for p in report.iterdir() if p.is_file()]
    assert files and all(p.name.startswith("SYNTHETIC_") for p in files)
    manifest = json.loads((report / "SYNTHETIC_manifest.json").read_text(encoding="utf-8"))
    assert manifest["project"]["data_class"] == "synthetic"
    assert manifest["label"].startswith(SYNTHETIC_LABEL)
    assert SYNTHETIC_LABEL in (report / "SYNTHETIC_report.html").read_text(encoding="utf-8")
    assert "SIMULATED" in (DEMO / "demo_output.txt").read_text(encoding="utf-8")
    # no project database, local settings or credentials are distributed with the examples
    names = [p.name for p in (REPO / "examples").rglob("*") if p.is_file()]
    assert not [n for n in names if n.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm"))
                or n in ("local.yaml", "credentials.env")]


@pytest.mark.skipif(not DEMO.exists(), reason="examples directory not distributed")
def test_distributed_example_inputs_carry_the_synthetic_tag():
    inputs = DEMO / "inputs"
    tagged = [inputs / "workbook_v1" / "synthetic_workbook.xlsx", inputs / "workbook_v2" / "synthetic_workbook.xlsx",
              inputs / "synthetic_article.pdf", inputs / "synthetic_search_export.ris",
              *sorted((inputs / "search_fixture").glob("page_*.json"))]
    assert len(tagged) == 7
    for path in tagged:
        assert detect_embedded_tag(path) == "synthetic", path.name
