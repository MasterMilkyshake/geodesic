"""Review actions: verification rules, edits that preserve the original extraction, stage and relation decisions."""

from __future__ import annotations

import pytest

from conftest import finding_by_wording
from geodesic import audit, findings as F, review
from geodesic.errors import ReviewError, ValidationError
from geodesic.util import SYSTEM, Actor, loads

CHECKS = {c: "confirmed" for c in review.REQUIRED_CHECKS}


def _half_time(conn) -> dict:
    return finding_by_wording(conn, "Release half-time was 4.5 h")


# ------------------------------------------------------------------ verification rules
def test_verification_needs_every_check_recorded(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    with pytest.raises(ReviewError, match="timepoint, denominator, qualifier"):
        review.verify(conn, f["id"], reviewer_a, {"outcome": "confirmed", "condition": "confirmed"})
    with pytest.raises(ReviewError, match="outcome"):
        review.verify(conn, f["id"], reviewer_a, {**CHECKS, "outcome": "looks fine"})
    assert conn.execute("SELECT review_status FROM finding WHERE id = ?", (f["id"],)).fetchone()[0] == "unreviewed"

    rid = review.verify(conn, f["id"], reviewer_a, {**CHECKS, "timepoint": "not_applicable"}, note="checked D5")
    row = conn.execute("SELECT * FROM extraction_review WHERE id = ?", (rid,)).fetchone()
    assert row["action"] == "verify" and row["reviewer"] == reviewer_a.name and row["reviewer_kind"] == "human"
    assert row["finding_version_no"] == 1 and row["is_stale"] == 0
    assert loads(row["checks_json"]) == {**CHECKS, "timepoint": "not_applicable"}
    assert conn.execute("SELECT review_status FROM finding WHERE id = ?", (f["id"],)).fetchone()[0] == "verified"


def test_only_named_reviewers_can_verify(imported, real_project, workbook_v1, mapping, reviewer_a):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    with pytest.raises(ReviewError, match="named reviewer"):
        review.verify(conn, f["id"], SYSTEM, CHECKS)
    with pytest.raises(ReviewError, match="Automated components cannot verify"):
        review.verify(conn, f["id"], Actor("rule-engine", "automated"), CHECKS)
    # simulated reviewers exist for the offline demonstration, in synthetic projects only
    rid = review.verify(conn, f["id"], Actor("Simulated Reviewer", "simulated"), CHECKS)
    assert conn.execute("SELECT reviewer_kind FROM extraction_review WHERE id = ?", (rid,)).fetchone()[0] == "simulated"

    from test_workbook_import import _untagged_copy
    from geodesic.workbook.importer import import_workbook
    from conftest import OPERATOR
    untagged = _untagged_copy(workbook_v1.parent.parent, workbook_v1)
    import_workbook(real_project, untagged, mapping, declare="real", actor=OPERATOR)
    g = _half_time(real_project.conn)
    with pytest.raises(ReviewError, match="only permitted in synthetic projects"):
        review.verify(real_project.conn, g["id"], Actor("Simulated Reviewer", "simulated"), CHECKS)


def test_reject_reopen_and_comment_keep_history(imported, reviewer_a, reviewer_b):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    with pytest.raises(ReviewError, match="needs a reason"):
        review.reject(conn, f["id"], reviewer_a, "  ")
    review.verify(conn, f["id"], reviewer_a, CHECKS)
    review.comment(conn, f["id"], reviewer_b, "Unit of the ± term is not stated in the source.")
    review.reject(conn, f["id"], reviewer_b, "Value belongs to a different run.")
    assert conn.execute("SELECT review_status FROM finding WHERE id = ?", (f["id"],)).fetchone()[0] == "rejected"
    review.reopen(conn, f["id"], reviewer_a, "Re-checking against the table.")
    assert conn.execute("SELECT review_status FROM finding WHERE id = ?", (f["id"],)).fetchone()[0] == "unreviewed"

    history = F.reviews(conn, f["id"])
    assert sorted(r["action"] for r in history) == ["comment", "reject", "reopen", "verify"]
    assert {r["reviewer"] for r in history} == {reviewer_a.name, reviewer_b.name}
    actions = [e["action"] for e in audit.history(conn, entity_type="finding", entity_id=f["id"])]
    assert sorted(actions) == ["finding.comment", "finding.reject", "finding.reopen", "finding.verify"]
    # rejected findings are kept, not deleted
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 32


# ------------------------------------------------------------------ edits
def test_review_edit_preserves_the_original_extraction(imported, reviewer_a, reviewer_b):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    review.verify(conn, f["id"], reviewer_a, CHECKS)

    out = review.edit(conn, f["id"], reviewer_b, {"outcome": "release half-time", "uncertainty_type": "sd"},
                      note="uncertainty type taken from the table footnote")
    assert out["version_no"] == 2 and out["grounding_status"] == "grounded"
    assert out["changed_fields"] == ["outcome", "uncertainty_type"]

    now = conn.execute("SELECT * FROM finding WHERE id = ?", (f["id"],)).fetchone()
    assert now["outcome"] == "release half-time" and now["uncertainty_type"] == "sd"
    assert now["current_version"] == 2
    assert now["review_status"] == "unreviewed"              # the earlier verification does not carry over
    assert now["source_wording"] == f["source_wording"]      # original wording is never changed by an edit
    assert now["extraction_method"] == f["extraction_method"]

    versions = F.versions(conn, f["id"])
    assert [(v["version_no"], v["change_kind"], v["created_by"]) for v in versions] == [
        (1, "imported", "Synthetic Operator"), (2, "review_edit", reviewer_b.name)]
    original = loads(versions[0]["snapshot_json"])["content"]
    assert original["outcome"] is None and original["uncertainty_type"] == "unspecified_plus_minus"
    assert original["value_literal"] == "4.5"

    reviews = {r["action"]: r for r in F.reviews(conn, f["id"])}
    assert reviews["verify"]["is_stale"] == 1 and reviews["verify"]["finding_version_no"] == 1
    assert reviews["edit"]["finding_version_no"] == 2 and reviews["edit"]["reviewer"] == reviewer_b.name

    event = audit.history(conn, entity_type="finding", entity_id=f["id"], action_prefix="finding.edit")[0]
    details = loads(event["details_json"])
    assert details["before"] == {"outcome": None, "uncertainty_type": "unspecified_plus_minus"}
    assert details["after"] == {"outcome": "release half-time", "uncertainty_type": "sd"}
    assert details["from_version"] == 1 and details["to_version"] == 2 and details["reviews_marked_stale"] == 1
    assert event["actor"] == reviewer_b.name
    assert audit.verify_chain(conn)[0]


def test_changed_literal_without_a_source_span_cannot_be_verified(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)

    out = review.edit(conn, f["id"], reviewer_a, {"value_literal": "4.6", "value_num": 4.6})
    assert out["grounding_status"] == "unsupported"
    assert out["grounding_problems"][0]["field"] == "value_literal"
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (f["id"],)).fetchone()
    assert row["grounding_status"] == "unsupported" and F.UNSUPPORTED_FLAG in loads(row["review_flags_json"])
    failure = conn.execute("SELECT * FROM parse_failure").fetchall()
    assert len(failure) == 1 and failure[0]["claimed_literal"] == "4.6" and failure[0]["cell_ref"] == "D5"
    with pytest.raises(ReviewError, match="unsupported"):
        review.verify(conn, f["id"], reviewer_a, CHECKS)

    # a span that does not contain the claimed string is rejected the same way
    out = review.edit(conn, f["id"], reviewer_a, {"value_literal": "4.7", "value_num": 4.7},
                      spans={"value_literal": [22, 25]})
    assert out["grounding_status"] == "unsupported"
    assert "4.7" in str(out["grounding_problems"])
    with pytest.raises(ReviewError, match="unsupported"):
        review.verify(conn, f["id"], reviewer_a, CHECKS)

    # correcting the provenance (literal present at the stated span) restores grounding
    out = review.edit(conn, f["id"], reviewer_a, {"value_literal": "4.5", "value_num": 4.5},
                      spans={"value_literal": [22, 25]})
    assert out["grounding_status"] == "grounded" and out["grounding_problems"] == []
    review.verify(conn, f["id"], reviewer_a, CHECKS)
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (f["id"],)).fetchone()
    assert row["review_status"] == "verified" and row["current_version"] == 4
    assert F.UNSUPPORTED_FLAG not in loads(row["review_flags_json"])
    assert [v["change_kind"] for v in F.versions(conn, f["id"])] == ["imported"] + ["review_edit"] * 3


def test_edit_rules(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    with pytest.raises(ReviewError, match="source_wording"):
        review.edit(conn, f["id"], reviewer_a, {"source_wording": "Release half-time was 5 h."})
    with pytest.raises(ValidationError, match="Unknown finding fields"):
        review.edit(conn, f["id"], reviewer_a, {"confidence": 0.9})
    with pytest.raises(ReviewError, match="changes nothing"):
        review.edit(conn, f["id"], reviewer_a, {"value_literal": "4.5"})
    with pytest.raises(ReviewError, match="Automated components cannot edit"):
        review.edit(conn, f["id"], Actor("rule-engine", "automated"), {"outcome": "x"})
    with pytest.raises(Exception):                      # schema validation: a bound needs a bound type
        review.edit(conn, f["id"], reviewer_a, {"statement_type": "bound"})
    assert conn.execute("SELECT current_version FROM finding WHERE id = ?", (f["id"],)).fetchone()[0] == 1
    assert len(F.versions(conn, f["id"])) == 1


def test_field_status_distinguishes_kinds_of_missing(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    status = loads(f["field_status_json"])
    assert status["sample_size"] == "not_yet_extracted"
    new_status = {**status, "sample_size": "not_reported", "timepoint": "not_applicable"}
    review.edit(conn, f["id"], reviewer_a, {"field_status": new_status})
    row = conn.execute("SELECT field_status_json FROM finding WHERE id = ?", (f["id"],)).fetchone()
    assert loads(row[0])["sample_size"] == "not_reported" and loads(row[0])["timepoint"] == "not_applicable"
    with pytest.raises(Exception):
        review.edit(conn, f["id"], reviewer_a, {"field_status": {**new_status, "sample_size": "unknown"}})


# ------------------------------------------------------------------ stages and relations
def test_reviewer_stage_assignments_are_recorded_separately_from_imported_ones(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    f = _half_time(conn)
    keys = review.assign_stages(conn, f["id"], reviewer_a, add=["transit"])
    assert sorted(keys) == ["release", "transit"]                # a finding can belong to several stages
    rows = {r["key"]: r for r in conn.execute(
        "SELECT s.key, fs.assignment_source, fs.status, fs.assigned_by FROM finding_stage fs"
        " JOIN stage s ON s.id = fs.stage_id WHERE fs.finding_id = ?", (f["id"],))}
    assert rows["release"]["assignment_source"] == "imported"
    assert rows["transit"]["assignment_source"] == "reviewer" and rows["transit"]["assigned_by"] == reviewer_a.name

    keys = review.assign_stages(conn, f["id"], reviewer_a, remove=["release"])
    assert keys == ["transit"]
    kept = conn.execute("SELECT fs.status FROM finding_stage fs JOIN stage s ON s.id = fs.stage_id"
                        " WHERE fs.finding_id = ? AND s.key = 'release'", (f["id"],)).fetchone()
    assert kept["status"] == "removed"                           # the imported assignment is kept as history
    with pytest.raises(ValidationError, match="Unknown stage key"):
        review.assign_stages(conn, f["id"], reviewer_a, add=["compartment_1"])
    actions = [e["action"] for e in audit.history(conn, entity_type="finding", entity_id=f["id"])]
    assert actions.count("finding.stages") == 2


def test_relations_are_decided_by_reviewers_and_nothing_is_deleted(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    candidate = conn.execute("SELECT * FROM finding_relation WHERE relation_type = 'possible_duplicate'").fetchone()
    review.decide_relation(conn, candidate["id"], reviewer_a, "confirmed", note="same sentence on two sheets")
    row = conn.execute("SELECT * FROM finding_relation WHERE id = ?", (candidate["id"],)).fetchone()
    assert row["status"] == "confirmed" and row["decided_by"] == reviewer_a.name and row["decided_at"]
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE record_status = 'active'").fetchone()[0] == 32

    a, b = _half_time(conn), finding_by_wording(conn, "Release rate stayed > 5 mL/min")
    rid = review.relate(conn, a["id"], b["id"], "dependent", reviewer_a, note="same run")
    rel = conn.execute("SELECT * FROM finding_relation WHERE id = ?", (rid,)).fetchone()
    assert rel["relation_type"] == "dependent" and rel["status"] == "confirmed"
    with pytest.raises(ValidationError, match="itself"):
        review.relate(conn, a["id"], a["id"], "dependent", reviewer_a)
    with pytest.raises(ValidationError, match="Unsupported relation type"):
        review.relate(conn, a["id"], b["id"], "identical", reviewer_a)
    review.decide_relation(conn, rid, reviewer_a, "rejected")
    assert conn.execute("SELECT status FROM finding_relation WHERE id = ?", (rid,)).fetchone()[0] == "rejected"
