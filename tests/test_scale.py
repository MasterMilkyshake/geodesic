"""Scale smoke test: about 10,000 papers and 100,000 findings in a synthetic project.

Deselected by default. Run it with::

    python -m pytest tests -m scale -s

It builds the project with the ordinary application functions (see
``geodesic.synthetic.scale.run_scale``), prints the measured time of every step
and writes ``scale_report.json``. It checks that the counts are right and that
nothing was lost; it sets no performance targets (timings are measurements to
report, and depend on the machine).

Environment variables: ``GEODESIC_SCALE_PAPERS`` (default 10000),
``GEODESIC_SCALE_FINDINGS`` (default 100000), ``GEODESIC_SCALE_DIR`` (an empty or
new directory to keep the project and the report in; default: a temporary one).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from geodesic.project import open_project
from geodesic.provenance import SYNTHETIC_LABEL
from geodesic.synthetic.scale import run_scale

pytestmark = pytest.mark.scale

N_PAPERS = int(os.environ.get("GEODESIC_SCALE_PAPERS", "10000"))
N_FINDINGS = int(os.environ.get("GEODESIC_SCALE_FINDINGS", "100000"))
APP = Path(__file__).resolve().parents[1] / "src" / "geodesic" / "dashboard" / "app.py"
DASHBOARD_PAGES = ("Overview", "5 Screening", "8 Review findings", "11 Evidence maps", "Audit trail")


@pytest.fixture(scope="module")
def scale(tmp_path_factory):
    given = os.environ.get("GEODESIC_SCALE_DIR")
    root = Path(given).expanduser() if given else tmp_path_factory.mktemp("scale")
    report = run_scale(root, N_PAPERS, N_FINDINGS)
    return root, report


def test_scale_counts_are_complete_and_nothing_is_lost(scale):
    root, report = scale
    seen = report["observed"]
    generated = report["generated"]
    assert report["label"] == SYNTHETIC_LABEL
    assert seen["paper_records"] >= N_PAPERS
    assert seen["findings"] >= N_FINDINGS
    # every generated search record was retrieved; every workbook row and statement was imported
    assert seen["search_records_retrieved"] == generated["search"]["records"]
    recon = seen["workbook_reconciliation"]
    assert recon["evidence_rows"] == generated["workbook"]["rows"]
    assert recon["papers"]["referenced"] == generated["workbook"]["papers_referenced"]
    assert recon["findings"]["by_kind"]["measurement"] == (generated["workbook"]["narrative_statements"]
                                                           + generated["workbook"]["value_cells"])
    assert recon["findings"]["by_kind"]["limitation"] >= generated["workbook"]["limitations"]
    assert recon["parse_failures"] == 0 and recon["unparsed_content"]["total"] == 0
    assert seen["export_rows"]["findings"] == seen["findings"]
    assert seen["export_rows"]["papers"] == seen["paper_records"]
    assert seen["validation"]["n_findings"] == seen["findings"]
    assert seen["validation"]["by_severity"]["error"] == 0
    assert seen["audit_chain_ok"] is True
    assert (root / "scale_report.json").exists()


def test_scale_project_keeps_provenance_for_every_finding(scale):
    root, report = scale
    with open_project(root / "project") as project:
        conn = project.conn
        assert project.is_synthetic
        n = conn.execute("SELECT COUNT(*) FROM finding WHERE record_status = 'active'").fetchone()[0]
        located = conn.execute("SELECT COUNT(DISTINCT owner_id) FROM source_location WHERE owner_type = 'finding'"
                               " AND is_current = 1").fetchone()[0]
        versions = conn.execute("SELECT COUNT(DISTINCT finding_id) FROM finding_version").fetchone()[0]
        assert n == located == versions == report["observed"]["findings"]
        assert conn.execute("SELECT COUNT(*) FROM finding WHERE grounding_status != 'grounded'").fetchone()[0] == 0
        # simulated review actions of the test are recorded as simulated, never as human
        kinds = dict(conn.execute("SELECT reviewer_kind, COUNT(*) FROM extraction_review GROUP BY 1").fetchall())
        assert kinds == {"simulated": report["observed"]["simulated_verifications"]}


def test_scale_dashboard_pages_render(scale, monkeypatch):
    from streamlit.testing.v1 import AppTest

    root, report = scale
    monkeypatch.setenv("GEODESIC_PROJECT", str(root / "project"))
    monkeypatch.setenv("GEODESIC_REVIEWER", "Scale Tester")
    timings = []
    start = time.perf_counter()
    at = AppTest.from_file(str(APP), default_timeout=600).run()
    timings.append({"step": "dashboard: first render (Overview)", "seconds": round(time.perf_counter() - start, 3)})
    assert not at.exception
    for page in DASHBOARD_PAGES[1:]:
        start = time.perf_counter()
        at.sidebar.radio[0].set_value(next(p for p in at.sidebar.radio[0].options if p.startswith(page))).run()
        timings.append({"step": f"dashboard: render page '{page}'", "seconds": round(time.perf_counter() - start, 3)})
        assert not at.exception, page
        assert not at.error, page
    for t in timings:
        print(f"  {t['seconds']:9.2f} s  {t['step']}")
    path = root / "scale_report.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["dashboard_timings"] = timings
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
