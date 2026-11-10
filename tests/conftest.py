"""Shared fixtures. Everything here is synthetic and generated at test time.

No test reads a real workbook, a real paper or a live service (live tests are
marked ``network`` and deselected by default).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geodesic.project import Project, create_project
from geodesic.synthetic.workbook import example_config, fixture_mapping, write_fixture_workbook
from geodesic.util import Actor
from geodesic.workbook.importer import import_workbook

REVIEWER_A = Actor("Synthetic Reviewer A", "human")
REVIEWER_B = Actor("Synthetic Reviewer B", "human")
OPERATOR = Actor("Synthetic Operator", "human")


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Keep tests away from the user's GEODESIC home, settings and credentials."""
    monkeypatch.setenv("GEODESIC_HOME", str(tmp_path / "_home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "_xdg"))
    for var in ("GEODESIC_PROJECT", "GEODESIC_REVIEWER", "GEODESIC_CONTACT_EMAIL", "GEODESIC_CROSSREF_PLUS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def reviewer_a() -> Actor:
    return REVIEWER_A


@pytest.fixture
def reviewer_b() -> Actor:
    return REVIEWER_B


@pytest.fixture
def workbook_v1(tmp_path) -> Path:
    path = tmp_path / "inputs" / "synthetic_workbook.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_fixture_workbook(path, version=1)


@pytest.fixture
def workbook_v2(tmp_path) -> Path:
    path = tmp_path / "inputs_v2" / "synthetic_workbook.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_fixture_workbook(path, version=2)


@pytest.fixture
def mapping():
    return fixture_mapping(confirmed=True)


@pytest.fixture
def project(tmp_path) -> Project:
    """An empty synthetic project."""
    proj = create_project(tmp_path / "project", example_config(), "synthetic", OPERATOR)
    yield proj
    proj.close()


@pytest.fixture
def real_project(tmp_path) -> Project:
    """An empty project declared 'real' (it only ever receives generated test content)."""
    proj = create_project(tmp_path / "real_project", example_config("Declared-real test project"), "real", OPERATOR)
    yield proj
    proj.close()


@pytest.fixture
def imported(project, workbook_v1, mapping):
    """Synthetic project with fixture workbook version 1 imported. Returns (project, ImportResult)."""
    result = import_workbook(project, workbook_v1, mapping, actor=OPERATOR)
    return project, result


def finding_by_wording(conn, fragment: str, kind: str | None = None) -> dict:
    """The single active finding whose source wording contains ``fragment``."""
    sql = "SELECT * FROM finding WHERE record_status = 'active' AND source_wording LIKE ?"
    params: list = [f"%{fragment}%"]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    rows = conn.execute(sql, params).fetchall()
    assert len(rows) == 1, f"expected one finding containing {fragment!r}, found {len(rows)}"
    return dict(rows[0])


# ------------------------------------------------------------------ search helpers
def cursor_transport(pages: list[bytes], *, failures: dict | None = None, requests: list | None = None,
                     end_with_empty_page: bool = False):
    """Mock transport answering Europe PMC-shaped requests from ``pages`` by ``cursorMark``.

    ``failures`` maps a cursor to a list of ``httpx.Response`` objects sent before the page
    (a list ending in ``...`` repeats its last response for ever). Every request is appended
    to ``requests`` as a dict of its query parameters.
    """
    import json

    import httpx

    by_cursor, cursor = {}, "*"
    for page in pages:
        by_cursor[cursor] = page
        try:
            cursor = json.loads(page).get("nextCursorMark")
        except (ValueError, AttributeError):
            cursor = None
        if cursor is None:
            break
    pending = {k: list(v) for k, v in (failures or {}).items()}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if requests is not None:
            requests.append(params)
        cur = params.get("cursorMark", "*")
        queue = pending.get(cur)
        if queue:
            if queue[-1] is ... and len(queue) == 2:
                return queue[0]
            return queue.pop(0)
        page = by_cursor.get(cur)
        if page is None:
            if end_with_empty_page:
                last = json.loads(pages[-1])
                return httpx.Response(200, json={**last, "nextCursorMark": cur, "resultList": {"result": []}})
            return httpx.Response(404, text="unknown cursor")
        return httpx.Response(200, content=page, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def paper_id(project, title, *, origin=None, year=None):
    """ID of the single paper record with this title (optionally narrowed by origin/year)."""
    sql, params = "SELECT id FROM paper WHERE title = ?", [title]
    if origin:
        sql += " AND origin = ?"
        params.append(origin)
    if year:
        sql += " AND year = ?"
        params.append(year)
    rows = project.conn.execute(sql, params).fetchall()
    assert len(rows) == 1, f"{len(rows)} paper records match {title!r}"
    return rows[0]["id"]


@pytest.fixture
def searched(project):
    """Synthetic project holding the ten records of the bundled search fixture (one fixture search run)."""
    from geodesic.search import europepmc
    from geodesic.synthetic import search as synthetic_search

    out = europepmc.run_search(project, OPERATOR, query="synthetic", page_size=4, mode="fixture",
                               transport=cursor_transport(synthetic_search.europepmc_pages(4)))
    project.search_out = out
    return project


# ------------------------------------------------------------------ document helpers
@pytest.fixture
def corpus(imported, searched) -> Project:
    """One synthetic project holding the imported fixture workbook and the fixture search records."""
    return searched


def workbook_paper(project) -> str:
    """The workbook paper whose DOI is printed inside the synthetic PDF fixture."""
    from geodesic.synthetic.documents import PDF_DOI

    return project.conn.execute("SELECT id FROM paper WHERE doi = ? AND origin = 'workbook'", (PDF_DOI,)).fetchone()[0]


def open_access_paper(project) -> str:
    """The search record reported as open access with a PMCID (the synthetic XML fixture is its full text)."""
    from geodesic.synthetic.documents import XML_PMCID

    return project.conn.execute("SELECT id FROM paper WHERE pmcid = ?", (XML_PMCID,)).fetchone()[0]


def other_workbook_papers(project) -> list[str]:
    """Workbook papers with a DOI other than the one in the PDF fixture."""
    from geodesic.synthetic.documents import PDF_DOI

    return [r["id"] for r in project.conn.execute(
        "SELECT id FROM paper WHERE origin = 'workbook' AND doi IS NOT NULL AND doi != ? ORDER BY pk", (PDF_DOI,))]


def audit_details(conn, action: str) -> list[dict]:
    import json

    return [json.loads(r["details_json"]) for r in conn.execute(
        "SELECT details_json FROM audit_event WHERE action = ? ORDER BY seq", (action,))]


class Evidence:
    """Handles of the ``evidence`` fixture (everything in it is synthetic)."""

    def __init__(self, project, pdf_paper, pdf_doc, pdf_snap, xml_paper, xml_doc, xml_snap):
        self.project, self.conn = project, project.conn
        self.pdf_paper, self.pdf_doc, self.pdf_snap = pdf_paper, pdf_doc, pdf_snap
        self.xml_paper, self.xml_doc, self.xml_snap = xml_paper, xml_doc, xml_snap

    def candidate(self, snapshot_id: str, literal: str, kind: str | None = None) -> dict:
        from geodesic.extraction import candidates

        found = [c for c in candidates.list_candidates(self.conn, snapshot_id=snapshot_id, status=None, limit=500)
                 if c["literal"] == literal and (kind is None or c["location_kind"] == kind)]
        assert len(found) == 1, f"{len(found)} candidates with literal {literal!r}"
        return found[0]


@pytest.fixture
def evidence(corpus, tmp_path) -> Evidence:
    """``corpus`` plus the synthetic PDF (workbook paper) and the synthetic XML (open-access record), with
    numeric candidates detected in both."""
    from geodesic.documents import openaccess, store
    from geodesic.extraction import candidates
    from geodesic.synthetic import documents as SD

    pdf_paper, xml_paper = workbook_paper(corpus), open_access_paper(corpus)
    pdf = store.add_document(corpus, pdf_paper, SD.write_pdf_fixture(tmp_path / "evidence_docs" / "main.pdf"), OPERATOR)
    xml = openaccess.fetch_open_access(corpus, xml_paper, OPERATOR, transport=SD.fulltext_transport())
    for snap in (pdf["snapshot"]["snapshot_id"], xml["snapshot"]["snapshot_id"]):
        candidates.detect(corpus, snap, OPERATOR)
    return Evidence(corpus, pdf_paper, pdf["document_id"], pdf["snapshot"]["snapshot_id"],
                    xml_paper, xml["document_id"], xml["snapshot"]["snapshot_id"])


ALL_CONFIRMED = {"outcome": "confirmed", "condition": "confirmed", "timepoint": "confirmed",
                 "denominator": "confirmed", "qualifier": "confirmed"}
