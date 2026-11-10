"""Document ingestion: page- and section-anchored text, document-to-paper linking, missing full text.

All documents are generated at test time by :mod:`geodesic.synthetic.documents`.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import (OPERATOR, REVIEWER_A, audit_details, open_access_paper, other_workbook_papers,
                      workbook_paper)
from geodesic import audit, locations, papers, screening
from geodesic.documents import openaccess, store
from geodesic.errors import NotFoundError, ProvenanceError, ValidationError
from geodesic.extraction import candidates
from geodesic.synthetic import documents as SD
from geodesic.util import sha256_file


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "document_inputs"
    d.mkdir()
    return d


def _availability(project, paper_id):
    return project.conn.execute("SELECT fulltext_availability FROM paper WHERE id = ?", (paper_id,)).fetchone()[0]


# ------------------------------------------------------------------ PDF: page-anchored text
def test_pdf_text_is_extracted_per_page_and_addressable(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    src = SD.write_pdf_fixture(files / "main.pdf")
    before = src.read_bytes()
    out = store.add_document(corpus, pid, src, OPERATOR)

    snap = out["snapshot"]
    assert (snap["status"], snap["n_units"], snap["n_tables"]) == ("ok", 3, 0)
    assert out["usable_text"] is True and out["declared_data_class"] == "synthetic"
    units = store.units(conn, snap["snapshot_id"])
    assert [(u["unit_kind"], u["unit_key"], u["label"]) for u in units] == \
        [("page", "0", "page 1"), ("page", "1", "page 2"), ("page", "2", "page 3")]
    assert units[1]["text"].startswith("2 Results\nTracer release reached 62 ± 5 %")

    hits = store.find_text(conn, snap["snapshot_id"], "62 ± 5 %")
    assert len(hits) == 1
    hit = hits[0]
    assert (hit["kind"], hit["unit_key"], hit["char_start"], hit["char_end"]) == ("pdf_page", "1", 33, 41)
    resolves, text, problem = locations.resolve(conn, hit)
    assert resolves and text == "62 ± 5 %" and problem is None
    assert locations.describe(hit) == f"document snapshot {snap['snapshot_id']} page index 1 chars 33-41"
    # a location that claims different text at the same span does not resolve
    assert locations.resolve(conn, {**hit, "excerpt": "63 ± 5 %"})[0] is False

    # the input file is untouched and a byte-identical copy is preserved under its SHA-256
    assert src.read_bytes() == before
    stored = corpus.abs(out["stored_path"])
    assert stored.read_bytes() == before and sha256_file(stored) == out["sha256"] == sha256_file(src)
    assert stored.parent == corpus.documents_dir
    assert _availability(corpus, pid) == "document_attached"
    info = store.snapshot_info(conn, snap["snapshot_id"])
    assert info["ocr_applied"] == 0 and info["parser"] == "pypdf-page-text"


def test_a_line_break_in_page_text_matches_a_space_in_a_quote(corpus, files):
    out = store.add_document(corpus, workbook_paper(corpus), SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    hits = store.find_text(corpus.conn, out["snapshot"]["snapshot_id"], "2 Results Tracer release")
    assert len(hits) == 1 and hits[0]["excerpt"] == "2 Results\nTracer release"
    assert locations.resolve(corpus.conn, hits[0])[0] is True


# ------------------------------------------------------------------ XML: section-anchored text
def test_xml_text_is_anchored_to_elements_sections_and_table_cells(corpus, files):
    conn = corpus.conn
    pid = open_access_paper(corpus)
    out = store.add_document(corpus, pid, SD.write_xml_fixture(files / "article.xml"), OPERATOR)
    snap = out["snapshot"]
    assert (snap["status"], snap["n_units"], snap["n_tables"]) == ("ok", 12, 1)
    assert out["license"] == SD.XML_LICENSE_URL
    assert out["identifier_check"]["outcome"] == "match"
    assert out["identifier_check"]["agreeing_schemes"] == ["doi", "pmcid"]

    units = {u["unit_key"]: u for u in store.units(conn, snap["snapshot_id"])}
    assert units["body[1]/sec[1]/sec[1]/p[1]"]["section_path"] == "Methods > Tracer injection"
    assert units["body[1]/sec[2]/p[1]"]["section_path"] == "Results"
    assert units["front[1]/article-meta[1]/abstract[1]/p[1]"]["section_path"] == "Abstract"
    assert all(u["unit_kind"] == "element" for u in units.values())

    (table,) = store.tables(conn, snap["snapshot_id"])
    assert (table["table_key"], table["label"], table["n_rows"], table["n_cols"]) == \
        ("body[1]/sec[2]/table-wrap[1]", "Table 1", 4, 3)
    cells = {(c["row_idx"], c["col_idx"]): c for c in table["cells"]}
    assert cells[(0, 1)]["text"] == "Transit time (min)" and cells[(0, 1)]["is_header"] == 1
    assert cells[(2, 1)]["text"] == "23 ± 2" and cells[(2, 1)]["is_header"] == 0
    assert cells[(3, 2)]["text"] == "not reported"

    hits = store.find_text(conn, snap["snapshot_id"], "42 ± 3")
    assert [(h["kind"], h.get("unit_key"), h.get("table_key"), h["char_start"], h["char_end"]) for h in hits] == [
        ("xml_element", "body[1]/sec[2]/p[1]", None, 22, 28),
        ("table_cell", None, "body[1]/sec[2]/table-wrap[1]", 0, 6)]
    for h in hits:
        assert locations.resolve(conn, h)[0] is True
    assert "table 'body[1]/sec[2]/table-wrap[1]' row 1 column 1" in locations.describe(hits[1])


# ------------------------------------------------------------------ files without usable text
def test_image_only_pdf_is_stored_but_is_not_full_text(corpus, files):
    conn = corpus.conn
    pid = other_workbook_papers(corpus)[0]
    out = store.add_document(corpus, pid, SD.write_scanned_pdf_fixture(files / "scan.pdf"), OPERATOR)
    snap = out["snapshot"]
    assert snap["status"] == "image_only" and snap["n_chars"] == 0 and out["usable_text"] is False
    assert "OCR is not implemented" in snap["failure_reason"]
    assert _availability(corpus, pid) == "not_checked"           # a scan does not count as attached full text
    status = store.fulltext_status(conn, pid)
    assert status["documents"] == 1 and status["documents_with_usable_text"] == 0
    assert "without usable text" in status["statement"] and "image_only" in status["problems"][0]
    assert [u["extraction_status"] for u in store.units(conn, snap["snapshot_id"])] == ["no_text", "no_text"]
    assert store.snapshot_info(conn, snap["snapshot_id"])["ocr_applied"] == 0
    with pytest.raises(ValidationError, match="no usable text"):
        candidates.detect(corpus, snap["snapshot_id"], OPERATOR)
    paper = conn.execute("SELECT * FROM paper WHERE id = ?", (pid,)).fetchone()
    assert screening.stage_texts(conn, paper, "full_text") == []


def test_partly_scanned_pdf_is_marked_partial_page_by_page(corpus, files):
    pid = other_workbook_papers(corpus)[0]
    out = store.add_document(corpus, pid, SD.write_mixed_pdf_fixture(files / "mixed.pdf"), OPERATOR)
    snap = out["snapshot"]
    assert snap["status"] == "partial" and snap["failure_reason"] == "1 of 2 page(s) have no extractable text."
    units = store.units(corpus.conn, snap["snapshot_id"])
    assert [(u["unit_key"], u["extraction_status"], u["has_images"]) for u in units] == [("0", "ok", 0), ("1", "no_text", 1)]
    status = store.fulltext_status(corpus.conn, pid)
    assert status["availability"] == "document_attached" and "partial" in status["problems"][0]


def test_unreadable_files_are_stored_with_a_failed_snapshot_and_reason(corpus, files):
    conn = corpus.conn
    a, b, c = other_workbook_papers(corpus)[:3]
    # the tag of an unreadable PDF cannot be read, so its provenance has to be declared
    with pytest.raises(ProvenanceError, match="carries no provenance tag"):
        store.add_document(corpus, a, SD.write_corrupt_pdf_fixture(files / "corrupt.pdf"), OPERATOR)
    bad_pdf = store.add_document(corpus, a, files / "corrupt.pdf", OPERATOR, declare="synthetic")
    bad_xml = store.add_document(corpus, b, SD.write_malformed_xml_fixture(files / "bad.xml"), OPERATOR)
    entity = store.add_document(corpus, c, SD.write_entity_xml_fixture(files / "entity.xml"), OPERATOR)
    for out, pid in ((bad_pdf, a), (bad_xml, b), (entity, c)):
        snap = out["snapshot"]
        assert snap["status"] == "failed" and snap["failure_reason"] and snap["n_units"] == 0
        assert out["usable_text"] is False
        assert _availability(corpus, pid) == "not_checked"
        assert corpus.abs(out["stored_path"]).is_file()           # kept as received
        assert store.fulltext_status(conn, pid)["documents_with_usable_text"] == 0
    assert "could not be parsed as XML" in bad_xml["snapshot"]["failure_reason"]
    assert "hardened parser refuses" in entity["snapshot"]["failure_reason"]
    assert conn.execute("SELECT COUNT(*) FROM text_unit tu JOIN document_snapshot ds ON ds.id = tu.snapshot_id"
                        " WHERE ds.status = 'failed'").fetchone()[0] == 0


def test_unsupported_or_missing_files_are_refused(corpus, files):
    pid = workbook_paper(corpus)
    txt = files / "notes.txt"
    txt.write_text("synthetic")
    with pytest.raises(ValidationError, match="HTML ingestion is not implemented"):
        store.add_document(corpus, pid, txt, OPERATOR)
    with pytest.raises(ValidationError, match="not found"):
        store.add_document(corpus, pid, files / "absent.pdf", OPERATOR)
    with pytest.raises(NotFoundError):
        store.add_document(corpus, "pap_does_not_exist", SD.write_pdf_fixture(files / "m.pdf"), OPERATOR)
    assert corpus.conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0


# ------------------------------------------------------------------ synthetic / real separation
def test_untagged_document_needs_a_declaration_matching_the_project(corpus, real_project, files):
    pid = other_workbook_papers(corpus)[0]
    untagged = SD.write_untagged_pdf_fixture(files / "untagged.pdf")
    with pytest.raises(ProvenanceError, match="carries no provenance tag"):
        store.add_document(corpus, pid, untagged, OPERATOR)
    with pytest.raises(ProvenanceError, match="declared 'real' but this project is 'synthetic'"):
        store.add_document(corpus, pid, untagged, OPERATOR, declare="real")
    assert corpus.conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0
    assert not list(corpus.documents_dir.glob("*.pdf"))
    out = store.add_document(corpus, pid, untagged, OPERATOR, declare="synthetic")
    assert out["declared_data_class"] == "synthetic"

    # a project declared real refuses the tagged synthetic fixture, whatever is declared
    rconn = real_project.conn
    rp = papers.create_paper(rconn, origin="manual", resolution_status="unresolved",
                             citation_text="Generated test citation")
    tagged = SD.write_pdf_fixture(files / "tagged.pdf")
    for declare in (None, "real", "synthetic"):
        with pytest.raises(ProvenanceError):
            store.add_document(real_project, rp, tagged, OPERATOR, declare=declare)
    assert rconn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0
    out = store.add_document(real_project, rp, untagged, OPERATOR, declare="real")
    assert out["declared_data_class"] == "real"


# ------------------------------------------------------------------ document-to-paper linking
def test_identify_lists_the_papers_carrying_the_identifiers_in_a_file(corpus, files):
    pid = workbook_paper(corpus)
    info = store.identify(corpus, SD.write_pdf_fixture(files / "main.pdf"))
    assert info["identifiers_in_document"] == {"doi": [SD.PDF_DOI]} and info["embedded_tag"] == "synthetic"
    matched = {m["paper_id"]: m["matched"] for m in info["matching_papers"]}
    assert matched[pid] == [f"doi:{SD.PDF_DOI}"]
    assert all(v == [f"doi:{SD.PDF_DOI}"] for v in matched.values())
    assert corpus.conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0      # identify changes nothing


def test_document_whose_identifier_belongs_to_another_paper_is_not_attached(corpus, files):
    conn = corpus.conn
    right, wrong = workbook_paper(corpus), other_workbook_papers(corpus)[-1]
    pdf = SD.write_pdf_fixture(files / "main.pdf")
    with pytest.raises(ValidationError, match="do not match paper"):
        store.add_document(corpus, wrong, pdf, OPERATOR)
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0
    assert not list(corpus.documents_dir.glob("*"))
    assert _availability(corpus, wrong) == "not_checked"

    ok = store.add_document(corpus, right, pdf, OPERATOR)
    assert ok["identifier_check"] == {"outcome": "match", "agreeing_schemes": ["doi"], "differing": []}

    # an explicit override is possible and is recorded with what differed
    forced = store.add_document(corpus, wrong, pdf, OPERATOR, allow_identifier_mismatch=True)
    assert forced["identifier_check"]["outcome"] == "mismatch"
    assert forced["also_attached_to"] == [right]
    details = audit_details(conn, "document.add")
    assert details[0]["identifier_mismatch_overridden"] is False
    assert details[1]["identifier_mismatch_overridden"] is True
    assert details[1]["identifier_check"]["differing"][0]["in_document"] == [SD.PDF_DOI]


def test_document_without_identifiers_or_for_an_unresolved_paper_is_marked_not_checkable(corpus, files):
    conn = corpus.conn
    unresolved = conn.execute("SELECT id FROM paper WHERE origin = 'workbook' AND resolution_status = 'unresolved'"
                              " ORDER BY pk").fetchone()[0]
    out = store.add_document(corpus, unresolved, SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    assert out["identifier_check"]["outcome"] == "not_checkable"      # the record has no identifier to compare
    # attaching a document does not resolve or validate the citation
    row = conn.execute("SELECT resolution_status, doi FROM paper WHERE id = ?", (unresolved,)).fetchone()
    assert (row["resolution_status"], row["doi"]) == ("unresolved", None)


def test_same_file_is_not_stored_twice_for_a_paper(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    pdf = SD.write_pdf_fixture(files / "main.pdf")
    first = store.add_document(corpus, pid, pdf, OPERATOR)
    again = store.add_document(corpus, pid, SD.write_pdf_fixture(files / "copy_of_main.pdf"), OPERATOR)
    assert again["already_present"] is True and again["document_id"] == first["document_id"]
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM document_snapshot").fetchone()[0] == 1
    assert len(list(corpus.documents_dir.glob("*.pdf"))) == 1


def test_supplementary_document_is_linked_to_its_main_document(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    sup = SD.write_supplement_pdf_fixture(files / "supplement.pdf")
    with pytest.raises(ValidationError, match="attach the main document first"):
        store.add_document(corpus, pid, sup, OPERATOR, role="supplementary")
    main = store.add_document(corpus, pid, SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    with pytest.raises(ValidationError, match="not an active main document"):
        store.add_document(corpus, pid, sup, OPERATOR, role="supplementary", parent_document_id="doc_other")
    with pytest.raises(ValidationError, match="Only a supplementary document has a parent"):
        store.add_document(corpus, pid, sup, OPERATOR, parent_document_id=main["document_id"])
    out = store.add_document(corpus, pid, sup, OPERATOR, role="supplementary")
    assert out["role"] == "supplementary"
    doc = store.get_document(conn, out["document_id"])
    assert doc["parent_document_id"] == main["document_id"] and doc["paper_id"] == pid
    assert store.get_document(conn, main["document_id"])["supplements"] == [out["document_id"]]
    listed = store.list_documents(conn, pid)
    assert sorted(d["role"] for d in listed) == ["main", "supplementary"]


# ------------------------------------------------------------------ missing / inaccessible full text
def test_missing_full_text_is_recorded_explicitly_and_never_assumed(corpus):
    conn = corpus.conn
    pid, other = other_workbook_papers(corpus)[:2]
    start = store.fulltext_status(conn, pid)
    assert start["availability"] == "not_checked" and start["statement"] == "full text not yet looked for"
    assert start["documents"] == 0

    with pytest.raises(ValidationError, match="note is required"):
        store.record_fulltext_unavailable(conn, pid, REVIEWER_A, "not_available", "  ")
    with pytest.raises(ValidationError):
        store.record_fulltext_unavailable(conn, pid, REVIEWER_A, "document_attached", "x")
    store.record_fulltext_unavailable(conn, pid, REVIEWER_A, "not_available", "Looked in the synthetic fixture set.")
    status = store.fulltext_status(conn, pid)
    assert status["availability"] == "not_available" and status["statement"] == "full text could not be obtained"
    (detail,) = [d for d in audit_details(conn, "paper.fulltext_availability")]
    assert detail == {"from": "not_checked", "to": "not_available", "note": "Looked in the synthetic fixture set."}

    store.record_fulltext_unavailable(conn, other, REVIEWER_A, "abstract_only", "Only the abstract is in the record.")
    assert store.fulltext_status(conn, other)["statement"] == "only an abstract is available to this project"
    # papers nobody looked at stay 'not_checked'; they are not counted as unavailable
    counts = dict(conn.execute("SELECT fulltext_availability, COUNT(*) FROM paper GROUP BY 1").fetchall())
    assert counts["not_available"] == 1 and counts["abstract_only"] == 1 and counts["not_checked"] >= 10


def test_unavailable_cannot_be_recorded_over_an_attached_document_and_attaching_updates_it(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    store.record_fulltext_unavailable(conn, pid, REVIEWER_A, "not_available", "Not in the synthetic set at first.")
    store.add_document(corpus, pid, SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    assert _availability(corpus, pid) == "document_attached"
    with pytest.raises(ValidationError, match="A document with extractable text is attached"):
        store.record_fulltext_unavailable(conn, pid, REVIEWER_A, "not_available", "x")


def test_abstract_only_document_does_not_count_as_full_text(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    store.add_document(corpus, pid, SD.write_pdf_fixture(files / "main.pdf"), OPERATOR, content_scope="abstract_only")
    assert _availability(corpus, pid) == "abstract_only"
    paper = conn.execute("SELECT * FROM paper WHERE id = ?", (pid,)).fetchone()
    assert screening.stage_texts(conn, paper, "full_text") == []


def test_full_text_screening_reads_the_ingested_text(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    paper = conn.execute("SELECT * FROM paper WHERE id = ?", (pid,)).fetchone()
    assert screening.stage_texts(conn, paper, "full_text") == []
    out = store.add_document(corpus, pid, SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    texts = screening.stage_texts(conn, paper, "full_text")
    assert [loc for loc, _ in texts] == [
        {"kind": "pdf_page", "snapshot_id": out["snapshot"]["snapshot_id"], "unit_key": k} for k in ("0", "1", "2")]
    assert "62 ± 5 %" in texts[1][1]


# ------------------------------------------------------------------ open-access retrieval (fixture transport)
def test_open_access_retrieval_attaches_xml_with_source_and_license(corpus):
    conn = corpus.conn
    pid = open_access_paper(corpus)
    assert _availability(corpus, pid) == "open_access_reported"
    assert openaccess.fetch_candidates(corpus) == [pid]
    requests: list = []
    out = openaccess.fetch_open_access(corpus, pid, OPERATOR, transport=SD.fulltext_transport(requests=requests),
                                       sleep=lambda s: None)
    assert out["outcome"] == "attached" and out["live"] is False and out["pmcid"] == SD.XML_PMCID
    assert out["license"] == SD.XML_LICENSE_URL
    doc = store.get_document(conn, out["document_id"])
    assert doc["source"] == "open_access_api:europepmc" and doc["format"] == "xml"
    assert doc["source_url"] == f"https://www.ebi.ac.uk/europepmc/webservices/rest/{SD.XML_PMCID}/fullTextXML"
    assert doc["declared_data_class"] == "synthetic" and doc["license"] == SD.XML_LICENSE_URL
    assert _availability(corpus, pid) == "document_attached" and openaccess.fetch_candidates(corpus) == []

    assert len(requests) == 1
    req = requests[0]
    assert req.method == "GET" and req.url.path.endswith(f"/{SD.XML_PMCID}/fullTextXML")
    assert not req.url.query and "authorization" not in req.headers and "cookie" not in req.headers
    (event,) = audit_details(conn, "document.retrieved")
    assert event["live"] is False and event["http_status"] == 200 and event["pmcid"] == SD.XML_PMCID
    assert event["response_sha256"] == doc["sha256"]
    # asking again does not store a second copy
    again = openaccess.fetch_open_access(corpus, pid, OPERATOR, transport=SD.fulltext_transport())
    assert again["outcome"] == "already_present" and again["document_id"] == out["document_id"]
    assert audit.verify_chain(conn)[0] is True


def test_open_access_retrieval_is_refused_for_records_not_reported_open_access(corpus):
    conn = corpus.conn
    requests: list = []
    closed = conn.execute("SELECT id FROM paper WHERE origin != 'workbook' AND pmcid IS NULL ORDER BY pk").fetchone()[0]
    with pytest.raises(ValidationError, match="limited to records reported as open access"):
        openaccess.fetch_open_access(corpus, closed, OPERATOR, transport=SD.fulltext_transport(requests=requests))
    with pytest.raises(ValidationError, match="limited to records reported as open access"):
        openaccess.fetch_open_access(corpus, workbook_paper(corpus), OPERATOR,
                                     transport=SD.fulltext_transport(requests=requests))
    assert requests == []                                   # nothing was requested
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0
    assert _availability(corpus, closed) == "not_checked"


def test_live_retrieval_is_refused_in_a_synthetic_project(corpus):
    with pytest.raises(ProvenanceError, match="not permitted in a synthetic project"):
        openaccess.fetch_open_access(corpus, open_access_paper(corpus), OPERATOR)


def test_failed_retrieval_is_recorded_as_failed_not_as_absent_full_text(corpus):
    conn = corpus.conn
    pid = open_access_paper(corpus)
    out = openaccess.fetch_open_access(corpus, pid, OPERATOR, transport=SD.fulltext_transport(available={}),
                                       sleep=lambda s: None)
    assert out["outcome"] == "retrieval_failed" and out["document_id"] is None and "HTTP 404" in out["reason"]
    assert _availability(corpus, pid) == "retrieval_failed"
    status = store.fulltext_status(conn, pid)
    assert status["statement"] == "automatic open-access retrieval failed; no document attached"
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0
    (event,) = audit_details(conn, "document.retrieval_failed")
    assert event["provider"] == "europepmc" and event["url"].endswith("/fullTextXML")
    assert openaccess.fetch_candidates(corpus) == [pid]          # it can be tried again

    # a response that is not parseable full-text XML is a failed retrieval too
    junk = SD.fulltext_transport(available={SD.XML_PMCID: SD.write_malformed_xml_fixture(
        corpus.root.parent / "junk.xml").read_bytes()})
    out = openaccess.fetch_open_access(corpus, pid, OPERATOR, transport=junk, sleep=lambda s: None)
    assert out["outcome"] == "retrieval_failed" and "not usable full-text XML" in out["reason"]
    assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0


def test_untagged_fixture_full_text_response_is_rejected(corpus):
    pid = open_access_paper(corpus)
    untagged = SD.fulltext_transport(available={SD.XML_PMCID: SD.xml_article(tagged=False).encode("utf-8")})
    with pytest.raises(ProvenanceError, match="must be tagged synthetic"):
        openaccess.fetch_open_access(corpus, pid, OPERATOR, transport=untagged)
    assert corpus.conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 0


# ------------------------------------------------------------------ snapshots, removal, integrity
def test_reparse_adds_a_snapshot_and_keeps_the_earlier_one(corpus, files):
    conn = corpus.conn
    out = store.add_document(corpus, workbook_paper(corpus), SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    first = out["snapshot"]["snapshot_id"]
    again = store.reparse(corpus, out["document_id"], OPERATOR)
    assert again["snapshot_no"] == 2 and again["text_identical_to_previous"] is True
    assert again["previous_snapshot_id"] == first and again["text_sha256"] == out["snapshot"]["text_sha256"]
    assert store.resolve_snapshot(conn, out["document_id"]) == again["snapshot_id"]
    assert store.resolve_snapshot(conn, first) == first
    assert len(store.units(conn, first)) == 3 and len(store.units(conn, again["snapshot_id"])) == 3
    # a preserved copy that no longer matches its hash is not parsed
    stored = corpus.abs(out["stored_path"])
    stored.chmod(0o644)
    stored.write_bytes(stored.read_bytes() + b"\n% changed")
    with pytest.raises(ValidationError, match="no longer matches its"):
        store.reparse(corpus, out["document_id"], OPERATOR)


def test_stored_document_text_cannot_be_changed_or_deleted(corpus, files):
    conn = corpus.conn
    out = store.add_document(corpus, workbook_paper(corpus), SD.write_pdf_fixture(files / "main.pdf"), OPERATOR)
    for sql in ("UPDATE text_unit SET text = 'changed'", "DELETE FROM text_unit", "DELETE FROM document_snapshot",
                "DELETE FROM document", "UPDATE document SET sha256 = 'x'",
                "UPDATE document_snapshot SET text_sha256 = 'x'"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    assert len(store.units(conn, out["snapshot"]["snapshot_id"])) == 3


def test_removing_a_document_keeps_the_record_and_the_file(corpus, files):
    conn = corpus.conn
    pid = workbook_paper(corpus)
    pdf = SD.write_pdf_fixture(files / "main.pdf")
    out = store.add_document(corpus, pid, pdf, OPERATOR)
    with pytest.raises(ValidationError):
        store.remove_document(conn, out["document_id"], REVIEWER_A, " ")
    res = store.remove_document(conn, out["document_id"], REVIEWER_A, "Attached to the wrong record (synthetic).")
    assert res["record_status"] == "removed" and res["fulltext_availability"] == "not_checked"
    assert conn.execute("SELECT record_status FROM document WHERE id = ?", (out["document_id"],)).fetchone()[0] == "removed"
    assert corpus.abs(out["stored_path"]).is_file() and len(store.units(conn, out["snapshot"]["snapshot_id"])) == 3
    assert store.list_documents(conn, pid, include_removed=False) == []
    back = store.add_document(corpus, pid, pdf, OPERATOR)
    assert back["already_present"] is True and back["reactivated"] is True
    assert _availability(corpus, pid) == "document_attached"
    assert audit.verify_chain(conn)[0] is True


@pytest.mark.parametrize("permissions, expected", [
    ('<license xlink:href="https://example.org/licence/a"><license-p>Synthetic.</license-p></license>',
     "https://example.org/licence/a"),
    ('<license><ali:license_ref xmlns:ali="http://www.niso.org/schemas/ali/1.0/">https://example.org/licence/b'
     '</ali:license_ref><license-p>Synthetic.</license-p></license>', "https://example.org/licence/b"),
    ('<license license-type="open-access"><license-p>Synthetic terms, see <ext-link ext-link-type="uri" '
     'xlink:href="https://example.org/licence/c">the licence</ext-link>.</license-p></license>',
     "https://example.org/licence/c"),
    ('<license license-type="synthetic-open"><license-p>Synthetic terms without a link.</license-p></license>',
     "synthetic-open"),
    ("<copyright-statement>Synthetic statement only</copyright-statement>", None),
])
def test_license_is_recorded_where_the_xml_supplies_one(tmp_path, permissions, expected):
    from geodesic.documents import parsers

    path = tmp_path / "licence.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<article xmlns:xlink="http://www.w3.org/1999/xlink"><front>'
        f"<article-meta><permissions>{permissions}</permissions></article-meta></front><body><sec><title>Synthetic"
        '</title><p>Synthetic text; see <ext-link xlink:href="https://example.org/not-a-licence">a link</ext-link>.'
        "</p></sec></body></article>", encoding="utf-8")
    parsed = parsers.parse_xml(path)
    assert parsed.status == "ok" and parsed.license == expected
