"""The offline demonstration: complete workflow in a synthetic project, review actions simulated and labelled."""

from __future__ import annotations

import json
import socket

import pytest

from geodesic import audit, demo, screening
from geodesic.cli import main
from geodesic.errors import GeodesicError
from geodesic.project import open_project


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    """One demonstration run with every network connection attempt turned into an error."""
    root = tmp_path_factory.mktemp("demo")
    lines: list[str] = []
    real_connect = socket.socket.connect

    def refuse(self, address, *a, **kw):                                  # pragma: no cover - must not be reached
        raise AssertionError(f"the offline demonstration tried to open a network connection to {address!r}")

    socket.socket.connect = refuse
    try:
        result = demo.run_demo(root, echo=lines.append)
    finally:
        socket.socket.connect = real_connect
    return root, result, "\n".join(lines)


def test_demo_runs_the_whole_workflow_offline_in_a_synthetic_project(demo_run):
    root, result, text = demo_run
    assert result["steps"] == 19 and result["audit_chain_ok"]
    assert text.startswith("GEODESIC offline demonstration - SYNTHETIC DATA")
    assert "SIMULATED" in text and "fixture responses, not live" in text
    for phrase in ("importing with the unconfirmed starter mapping is refused",
                   "a final eligibility decision is refused while the protocol is provisional",
                   "candidates are not findings", "count as 1 independent unit, not two",
                   "does not verify that findings represent their sources correctly",
                   "hash chain intact"):
        assert phrase in text, phrase
    with open_project(result["project_dir"]) as project:
        conn = project.conn
        assert project.data_class == "synthetic" and project.is_synthetic
        assert audit.verify_chain(conn)[0]
        # every step left its record
        for table, minimum in (("workbook_version", 2), ("search_run", 2), ("document", 2), ("study", 1),
                               ("experiment", 1), ("derived_value", 1), ("comparison_set", 1),
                               ("validation_run", 1), ("vocabulary_term", 2), ("numeric_candidate", 20)):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= minimum, table
        assert conn.execute("SELECT COUNT(*) FROM search_run WHERE mode = 'live'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM search_raw_response WHERE is_fixture = 0").fetchone()[0] == 0
        # the validation run of the demonstration reports no error: what it verified stays verified
        from geodesic.extraction import validation
        assert validation.summary(conn)["by_severity"]["error"] == 0
    # the first workbook version is preserved byte for byte beside the second
    first = result["reconciliation"]
    assert first["findings"]["in_workbook"] == 32 and first["evidence_rows"] == 14


def test_demo_review_actions_are_recorded_as_simulated_never_as_human(demo_run):
    _, result, _ = demo_run
    with open_project(result["project_dir"]) as project:
        conn = project.conn
        kinds = {r[0]: r[1] for r in conn.execute("SELECT actor_kind, COUNT(*) FROM audit_event GROUP BY actor_kind")}
        assert kinds.get("human", 0) == 0 and kinds["simulated"] > 0
        assert conn.execute("SELECT COUNT(*) FROM extraction_review WHERE reviewer_kind != 'simulated'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM extraction_review WHERE action = 'verify'").fetchone()[0] == 5
        for stage in screening.STAGES:
            by = screening.summary(conn)["stages"][stage]["current_decisions_by_decider"]
            assert by["human"] == 0
        ta = screening.summary(conn)["stages"]["title_abstract"]["current_decisions_by_decider"]
        assert ta["simulated"] == 10 and ta["automated"] == 2
        assert len(screening.conflicts(conn, "title_abstract")) == 1


def test_demo_report_and_inputs_are_labelled_synthetic(demo_run):
    root, result, _ = demo_run
    report_dir = root / "report"
    names = sorted(p.name for p in report_dir.iterdir())
    assert names and all(n.startswith("SYNTHETIC_") for n in names)
    assert result["report"].name == "SYNTHETIC_report.html"
    html = result["report"].read_text(encoding="utf-8")
    assert "SYNTHETIC" in html and "<script" not in html
    manifest = json.loads((report_dir / "SYNTHETIC_manifest.json").read_text(encoding="utf-8"))
    assert manifest["project"]["data_class"] == "synthetic" and "SYNTHETIC" in manifest["label"]
    assert (root / "inputs" / "profile" / "mapping.starter.yaml").exists()
    assert "unconfirmed" in (root / "inputs" / "profile" / "mapping.starter.yaml").read_text(encoding="utf-8")


def test_demo_never_overwrites_an_existing_project(demo_run):
    root, result, _ = demo_run
    with pytest.raises(GeodesicError, match="already holds a project"):
        demo.run_demo(root)
    again = demo.run_demo(root, keep_existing=True)
    assert again["existing"] and again["project_dir"] == result["project_dir"]


def test_demo_command(tmp_path, capsys):
    assert main(["demo", "--dir", str(tmp_path / "d"), "--no-report"]) == 0
    out = capsys.readouterr().out
    assert "Demo finished" in out and "SYNTHETIC" in out and "geodesic dashboard --project" in out
    assert not (tmp_path / "d" / "report").exists()
    assert main(["demo", "--dir", str(tmp_path / "d")]) == 2
