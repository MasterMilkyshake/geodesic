"""Re-import: idempotency, change classification, identity across moved rows, stale reviews."""

from __future__ import annotations

import openpyxl
import pytest

from conftest import OPERATOR, finding_by_wording
from geodesic import audit, findings as F, review
from geodesic.errors import ReviewError
from geodesic.synthetic.workbook import true_counts
from geodesic.workbook.importer import import_workbook

CHECKS = {"outcome": "confirmed", "condition": "confirmed", "timepoint": "not_applicable",
          "denominator": "not_applicable", "qualifier": "confirmed"}


def _table_counts(conn) -> dict[str, int]:
    tables = ["paper", "paper_identifier", "finding", "finding_version", "source_location", "finding_stage",
              "paper_stage", "finding_relation", "workbook_version", "source_cell", "unparsed_content",
              "metadata_conflict", "extraction_review", "import_run"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_reimport_of_the_same_file_changes_nothing(imported, workbook_v1, mapping):
    project, first = imported
    conn = project.conn
    ids_before = [r[0] for r in conn.execute("SELECT id FROM finding ORDER BY pk")]
    papers_before = [r[0] for r in conn.execute("SELECT id FROM paper ORDER BY id")]
    counts_before = _table_counts(conn)
    audit_before = conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]

    again = import_workbook(project, workbook_v1, mapping, actor=OPERATOR)

    assert again.status == "already_imported"
    assert again.workbook_version_id == first.workbook_version_id and again.version_no == 1
    assert again.stats["findings"] == first.stats["findings"]
    assert _table_counts(conn) == counts_before
    assert [r[0] for r in conn.execute("SELECT id FROM finding ORDER BY pk")] == ids_before
    assert [r[0] for r in conn.execute("SELECT id FROM paper ORDER BY id")] == papers_before
    assert conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == audit_before


def test_a_renamed_copy_of_the_same_bytes_is_the_same_version(imported, workbook_v1, mapping, tmp_path):
    project, first = imported
    copy = tmp_path / "renamed copy.xlsx"
    copy.write_bytes(workbook_v1.read_bytes())
    again = import_workbook(project, copy, mapping, actor=OPERATOR)
    assert again.status == "already_imported"
    assert project.conn.execute("SELECT COUNT(*) FROM workbook_version").fetchone()[0] == 1


def test_new_version_is_classified_and_ids_survive_moved_rows(imported, workbook_v2, mapping):
    project, _ = imported
    conn = project.conn
    before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM finding")}
    liner = finding_by_wording(conn, "Between 10 and 20 % of the tracer remained bound")
    pilot = finding_by_wording(conn, "Release appeared faster at higher stirring speed")
    removed = finding_by_wording(conn, "Dispersion increased from 2 to 8 mL/min")
    bypass = finding_by_wording(conn, "No retention was observed in the bypass line")
    conc = finding_by_wording(conn, "18 mg/L")
    v1_locs = {fid: {l["role"]: l for l in F.locations.current_locations(conn, "finding", fid)}
               for fid in (liner["id"], pilot["id"])}
    assert v1_locs[liner["id"]]["supporting_excerpt"]["cell_ref"] == "D6"

    result = import_workbook(project, workbook_v2, mapping, actor=OPERATOR)
    s = result.stats
    assert result.status == "imported" and result.version_no == 2
    assert (s["findings"]["added"], s["findings"]["unchanged"], s["findings"]["modified"], s["findings"]["absent"],
            s["findings"]["ambiguous_identity"]) == (1, 29, 2, 1, 0)
    assert s["papers"]["matched"] == 8 and s["papers"]["created"] == 0
    truth = true_counts(2)
    assert s["evidence_rows_by_sheet"] == {k: v["evidence_rows"] for k, v in truth.items() if k != "__all__"}
    assert all(r["status"] == "match" for r in s["reconciliation"])       # version 2's summary sheet is correct

    after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM finding")}
    assert set(before) <= set(after) and len(after) == 33                 # nothing deleted; one finding added
    assert conn.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 8  # no duplicate papers

    # moved rows keep their identity; the location now points at the new cell in the new version
    for fid, new_ref in ((liner["id"], "D8"), (pilot["id"], "D7")):
        assert after[fid]["current_version"] == 1 and after[fid]["record_status"] == "active"
        loc = {l["role"]: l for l in F.locations.current_locations(conn, "finding", fid)}["supporting_excerpt"]
        assert loc["cell_ref"] == new_ref and loc["workbook_version_id"] == result.workbook_version_id
        assert loc["resolves"] == 1
        # the earlier location is kept as history, not overwritten
        old = conn.execute("SELECT * FROM source_location WHERE owner_id = ? AND role = 'supporting_excerpt'"
                           " AND is_current = 0", (fid,)).fetchall()
        assert len(old) == 1 and old[0]["cell_ref"] == v1_locs[fid]["supporting_excerpt"]["cell_ref"]

    # removed row: kept, marked absent from the latest version
    assert after[removed["id"]]["record_status"] == "absent_from_latest"

    # modified findings: same id, new version, earlier version preserved
    for fid, old_text, new_text in ((bypass["id"], "No retention was observed in the bypass line.",
                                     "No retention was observed in the bypass line during the test."),
                                    (conc["id"], "18 mg/L", "21 mg/L")):
        assert after[fid]["current_version"] == 2
        assert after[fid]["source_wording"] == new_text
        versions = F.versions(conn, fid)
        assert [v["version_no"] for v in versions] == [1, 2]
        assert [v["change_kind"] for v in versions] == ["imported", "reimport_modified"]
        assert F.loads(versions[0]["snapshot_json"])["content"]["source_wording"] == old_text
    assert after[conc["id"]]["value_literal"] == "21" and after[conc["id"]]["value_num"] == 21

    added = [f for fid, f in after.items() if fid not in before]
    assert len(added) == 1 and "secondary tank reached 55 %" in added[0]["source_wording"]
    assert audit.verify_chain(conn)[0]


def test_changed_evidence_makes_reviews_stale_and_verification_is_not_carried_forward(
        imported, workbook_v2, mapping, reviewer_a):
    project, _ = imported
    conn = project.conn
    changed = finding_by_wording(conn, "18 mg/L")
    unchanged = finding_by_wording(conn, "Release appeared faster at higher stirring speed")
    gone = finding_by_wording(conn, "Dispersion increased from 2 to 8 mL/min")
    # 'gone' is an ambiguous draft ("from 2 to 8": a range or a change?) and needs an explicit acknowledgement
    with pytest.raises(ReviewError, match="ambiguous draft"):
        review.verify(conn, gone["id"], reviewer_a, CHECKS)
    for f in (changed, unchanged, gone):
        review.verify(conn, f["id"], reviewer_a, CHECKS, acknowledge_flags=f is gone)
    assert {r[0] for r in conn.execute("SELECT review_status FROM finding WHERE id IN (?,?,?)",
                                       (changed["id"], unchanged["id"], gone["id"]))} == {"verified"}

    result = import_workbook(project, workbook_v2, mapping, actor=OPERATOR)
    assert result.stats["reviews_marked_stale"] == 2
    assert result.stats["requiring_review"]["stale"] == 2

    status = {r["id"]: r["review_status"] for r in conn.execute("SELECT id, review_status FROM finding")}
    assert status[changed["id"]] == "stale"            # evidence changed: not verified any more
    assert status[gone["id"]] == "stale"               # evidence no longer in the source
    assert status[unchanged["id"]] == "verified"       # same content (row moved): verification stands

    for f, stale in ((changed, 1), (gone, 1), (unchanged, 0)):
        revs = F.reviews(conn, f["id"])
        assert len(revs) == 1 and revs[0]["is_stale"] == stale       # review history is kept either way
        assert revs[0]["finding_version_no"] == 1
        if stale:
            assert revs[0]["stale_reason"]

    # a finding absent from the latest source cannot be verified again
    with pytest.raises(ReviewError, match="absent from the latest source version"):
        review.verify(conn, gone["id"], reviewer_a, CHECKS)
    # the changed finding can be re-verified, and that applies to version 2 only
    review.verify(conn, changed["id"], reviewer_a, CHECKS)
    revs = F.reviews(conn, changed["id"])
    assert sorted((r["finding_version_no"], r["is_stale"]) for r in revs) == [(1, 1), (2, 0)]


def test_identity_does_not_depend_on_row_position(project, workbook_v1, mapping, tmp_path):
    """Insert blank-free shifts: sort an evidence sheet's rows differently and re-import."""
    import_workbook(project, workbook_v1, mapping, actor=OPERATOR)
    conn = project.conn
    before = {r["source_wording"]: r["id"] for r in conn.execute(
        "SELECT f.id, f.source_wording FROM finding f JOIN source_location l ON l.owner_id = f.id"
        " AND l.role = 'supporting_excerpt' AND l.is_current = 1 WHERE l.sheet = 'Discharge'")}
    wb = openpyxl.load_workbook(workbook_v1)
    ws = wb["Discharge"]
    rows = [[c.value for c in row] for row in ws.iter_rows(min_row=4, max_row=6)]
    for target, values in zip(range(4, 7), reversed(rows)):          # reverse the three evidence rows
        for col, v in enumerate(values, start=1):
            ws.cell(row=target, column=col).value = v
    shuffled = tmp_path / "shuffled" / "synthetic_workbook.xlsx"
    shuffled.parent.mkdir()
    wb.save(shuffled)

    result = import_workbook(project, shuffled, mapping, actor=OPERATOR)
    f = result.stats["findings"]
    assert (f["added"], f["modified"], f["absent"], f["ambiguous_identity"]) == (0, 0, 0, 0)
    assert f["unchanged"] == 32
    after = {r["source_wording"]: r["id"] for r in conn.execute(
        "SELECT f.id, f.source_wording FROM finding f JOIN source_location l ON l.owner_id = f.id"
        " AND l.role = 'supporting_excerpt' AND l.is_current = 1 WHERE l.sheet = 'Discharge'")}
    assert after == before


def test_ambiguous_identity_is_flagged_not_guessed(project, tmp_path):
    """Two near-identical statements both reworded: the importer must not pick a pairing silently."""
    from geodesic.synthetic.workbook import fixture_mapping

    def make(path, sentences):
        wb = openpyxl.load_workbook(_base)
        ws = wb["Transit"]
        ws.cell(row=4, column=4).value = " ".join(sentences)
        path.parent.mkdir(exist_ok=True)
        wb.save(path)
        return path

    from geodesic.synthetic.workbook import write_fixture_workbook
    _base = write_fixture_workbook(tmp_path / "base.xlsx", version=1)
    v1 = make(tmp_path / "a" / "wb.xlsx", ["Signal in the upper conduit rose slowly over the run.",
                                           "Signal in the lower conduit rose slowly over the run."])
    v2 = make(tmp_path / "b" / "wb.xlsx", ["Signal in the outer conduit rose slowly over the whole run.",
                                           "Signal in the inner conduit rose slowly over the whole run."])
    mapping = fixture_mapping(confirmed=True)
    import_workbook(project, v1, mapping, actor=OPERATOR)
    conn = project.conn
    n_before = conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0]
    result = import_workbook(project, v2, mapping, actor=OPERATOR)
    f = result.stats["findings"]
    assert f["ambiguous_identity"] >= 1
    assert f["modified"] == 0                              # nothing was silently treated as "the same finding"
    rels = conn.execute("SELECT * FROM finding_relation WHERE relation_type = 'ambiguous_identity'").fetchall()
    assert rels and all(r["status"] == "candidate" for r in rels)
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] > n_before
    flagged = conn.execute("SELECT COUNT(*) FROM finding WHERE review_flags_json LIKE '%ambiguous_identity%'"
                           ).fetchone()[0]
    assert flagged >= 1
