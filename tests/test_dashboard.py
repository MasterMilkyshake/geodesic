"""Headless smoke test of the Streamlit dashboard (streamlit.testing.v1.AppTest; no browser, no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from geodesic import demo
from geodesic.dashboard import app as dashboard_app
from geodesic.dashboard import ui
from geodesic.project import open_project

APP = str(Path(dashboard_app.__file__))
TESTER = "Dashboard Tester"


@pytest.fixture(scope="module")
def demo_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("dashboard_demo")
    out = demo.run_demo(root, with_report=False, echo=lambda line: None)
    return Path(out["project_dir"])


def launch(monkeypatch, project_dir, reviewer: str | None = TESTER) -> AppTest:
    monkeypatch.setenv(dashboard_app.ENV_PROJECT, str(project_dir))
    if reviewer is None:
        monkeypatch.delenv(dashboard_app.ENV_REVIEWER, raising=False)
    else:
        monkeypatch.setenv(dashboard_app.ENV_REVIEWER, reviewer)
    return AppTest.from_file(APP, default_timeout=120).run()


def go_to(at: AppTest, page_prefix: str) -> AppTest:
    radio = at.sidebar.radio[0]
    page = next(o for o in radio.options if o.startswith(page_prefix))
    return radio.set_value(page).run()


def submit(at: AppTest, form: str, label: str) -> AppTest:
    return at.button(key=f"FormSubmitter:{form}-{label}").click().run()


def one(db: Path, sql: str, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def test_every_workflow_step_is_a_page_and_renders_without_errors(monkeypatch, demo_project):
    at = launch(monkeypatch, demo_project)
    assert not at.exception
    pages = at.sidebar.radio[0].options
    assert pages == list(dashboard_app.PAGES)
    for step in ("Protocol", "Workbook import", "Literature search", "Duplicates", "Screening", "Documents",
                 "Extraction", "Review findings", "Organise", "Compare", "Evidence maps", "Validation",
                 "Reports and exports", "Audit trail"):
        assert any(step in p for p in pages), step
    for page in pages:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])
        assert not at.error, (page, [e.value for e in at.error])
        assert len(at.header) == 1, page
        # every page of a synthetic project carries the synthetic label
        assert at.caption[0].value.startswith("SYNTHETIC DATA"), page
    assert any("SYNTHETIC project" in w.value for w in at.sidebar.warning)


def test_validation_can_be_run_from_the_dashboard_and_is_recorded_under_the_reviewer(monkeypatch, demo_project):
    db = demo_project / "geodesic.sqlite"
    before = one(db, "SELECT COUNT(*) FROM validation_run")[0]
    at = go_to(launch(monkeypatch, demo_project), "12 Validation")
    at.button(key="val_run").click().run()
    assert not at.exception and not at.error
    assert any("Validation run" in s.value for s in at.success)
    assert one(db, "SELECT COUNT(*) FROM validation_run")[0] == before + 1
    actor, kind = one(db, "SELECT actor, actor_kind FROM audit_event WHERE action LIKE 'validation.%'"
                          " ORDER BY seq DESC LIMIT 1")
    assert (actor, kind) == (TESTER, "human")


def test_a_finding_is_verified_through_the_review_page_only_with_all_checks_and_a_named_reviewer(
        monkeypatch, demo_project):
    db = demo_project / "geodesic.sqlite"
    fid = one(db, "SELECT id FROM finding WHERE review_status = 'unreviewed' AND grounding_status = 'grounded'"
                  " AND record_status = 'active' ORDER BY pk LIMIT 1")[0]

    # no reviewer name: the action is refused and nothing is stored
    at = go_to(launch(monkeypatch, demo_project, reviewer=None), "8 Review")
    assert at.sidebar.text_input(key="reviewer_name").value == ""
    at.text_input(key="rv_id").input(fid).run()
    for name in ("outcome", "condition", "timepoint", "denominator", "qualifier"):
        at.selectbox(key=f"rv_check_{name}").set_value("confirmed")
    at.checkbox(key="rv_ack").check()
    submit(at, "rv_verify", "Verify against the source")
    assert any(ui.NO_REVIEWER in e.value for e in at.error)
    assert one(db, "SELECT review_status FROM finding WHERE id = ?", fid)[0] == "unreviewed"

    # named reviewer but an unchecked item: refused by the same rule the CLI applies
    at = go_to(launch(monkeypatch, demo_project), "8 Review")
    at.text_input(key="rv_id").input(fid).run()
    assert any(fid in s.value for s in at.subheader)
    at.selectbox(key="rv_check_outcome").set_value("confirmed")
    at.checkbox(key="rv_ack").check()
    submit(at, "rv_verify", "Verify against the source")
    assert at.error and not at.exception
    assert one(db, "SELECT review_status FROM finding WHERE id = ?", fid)[0] == "unreviewed"

    # all five checks: verified, recorded as a human review by the named reviewer
    for name in ("condition", "timepoint", "denominator"):
        at.selectbox(key=f"rv_check_{name}").set_value("confirmed")
    at.selectbox(key="rv_check_qualifier").set_value("not_applicable")
    at.checkbox(key="rv_ack").check()
    submit(at, "rv_verify", "Verify against the source")
    assert not at.exception and not at.error, [e.value for e in at.error]
    assert any("verified against its source" in s.value for s in at.success)
    assert one(db, "SELECT review_status FROM finding WHERE id = ?", fid)[0] == "verified"
    reviewer, kind = one(db, "SELECT reviewer, reviewer_kind FROM extraction_review WHERE finding_id = ? AND"
                             " action = 'verify' ORDER BY created_at DESC LIMIT 1", fid)
    assert (reviewer, kind) == (TESTER, "human")


def test_provisional_protocol_is_flagged_on_every_page_of_an_empty_project(monkeypatch, project):
    root = project.root
    project.close()
    at = launch(monkeypatch, root)
    for page in at.sidebar.radio[0].options:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])
        assert not at.error, (page, [e.value for e in at.error])
        assert any("PROVISIONAL" in w.value and "cannot be finalised" in w.value for w in at.main.warning), page


def test_a_real_project_is_not_labelled_synthetic(monkeypatch, real_project):
    root = real_project.root
    real_project.close()
    at = launch(monkeypatch, root)
    assert not at.exception and not at.error
    assert not any("SYNTHETIC" in c.value for c in at.caption)
    assert any("Real project" in i.value for i in at.sidebar.info)
    assert not at.sidebar.warning


def test_missing_project_is_reported_instead_of_crashing(monkeypatch, tmp_path):
    at = launch(monkeypatch, tmp_path / "does-not-exist")
    assert not at.exception
    assert at.error
    monkeypatch.delenv(dashboard_app.ENV_PROJECT)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert any("No project was given" in e.value for e in at.error)


def test_project_directory_comes_from_the_command_line_or_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv(dashboard_app.ENV_PROJECT, raising=False)
    assert dashboard_app.project_dir_from_argv([]) is None
    assert dashboard_app.project_dir_from_argv(["--project", str(tmp_path)]) == tmp_path
    assert dashboard_app.project_dir_from_argv([f"--project={tmp_path}"]) == tmp_path
    monkeypatch.setenv(dashboard_app.ENV_PROJECT, str(tmp_path / "env"))
    assert dashboard_app.project_dir_from_argv([]) == tmp_path / "env"


def test_dashboard_leaves_the_project_database_closed_and_unchanged_when_only_viewing(monkeypatch, demo_project):
    with open_project(demo_project) as p:
        before = p.conn.execute("SELECT COUNT(*), MAX(seq) FROM audit_event").fetchone()
        before = tuple(before)
    at = launch(monkeypatch, demo_project)
    for page in at.sidebar.radio[0].options:
        at.sidebar.radio[0].set_value(page).run()
    with open_project(demo_project) as p:
        after = tuple(p.conn.execute("SELECT COUNT(*), MAX(seq) FROM audit_event").fetchone())
    assert after == before
