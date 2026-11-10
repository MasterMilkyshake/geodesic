"""Reviewer actions on findings: verify, reject, edit, comment, reopen, relate.

Rules enforced here
-------------------
* Only a grounded finding can be verified. Grounding is necessary, not
  sufficient: verification also requires the reviewer to check the claimed
  outcome, condition, timepoint, denominator and qualifier against the source.
* An edit never overwrites the original extraction. It creates a new version,
  marks earlier reviews stale and returns the finding to ``unreviewed``.
* Rule-based components cannot verify. Scripted demonstration actions are
  recorded as ``simulated`` and are refused in real projects.
* Every action writes an audit event.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import audit, findings as F
from .errors import NotFoundError, ReviewError, ValidationError
from .util import Actor, canonical_json, new_id, utc_now

#: What a reviewer must check before a finding counts as verified.
REQUIRED_CHECKS = ("outcome", "condition", "timepoint", "denominator", "qualifier")
CHECK_VALUES = ("confirmed", "not_applicable")
CHECK_DESCRIPTIONS = {
    "outcome": "the finding names the outcome the source actually reports",
    "condition": "system, intervention/comparator and experimental condition match the source",
    "timepoint": "the timepoint matches the source (or the source gives none)",
    "denominator": "denominator/normalization and reference of relative values match the source",
    "qualifier": "approximation, bound, range and caveat wording is preserved",
}


def _load(conn: sqlite3.Connection, finding_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown finding {finding_id}")
    if row["record_status"] == "removed":
        raise ReviewError(f"Finding {finding_id} was removed and cannot be reviewed.")
    return row


def _reviewer_actor(conn: sqlite3.Connection, actor: Actor, action: str) -> None:
    if actor.kind == "system":
        raise ReviewError("Review actions need a named reviewer, not the system actor.")
    if actor.kind == "automated" and action in ("verify", "reject", "edit"):
        raise ReviewError(f"Automated components cannot {action} findings; only a reviewer can.")
    if actor.kind == "simulated":
        data_class = conn.execute("SELECT data_class FROM project").fetchone()["data_class"]
        if data_class != "synthetic":
            raise ReviewError("Simulated review actions are only permitted in synthetic projects.")


def _add_review(conn: sqlite3.Connection, finding_id: str, version_no: int, actor: Actor, action: str,
                checks: dict[str, Any], note: str | None, now: str) -> str:
    rid = new_id("rev")
    conn.execute(
        "INSERT INTO extraction_review (id, finding_id, finding_version_no, reviewer, reviewer_kind, action,"
        " checks_json, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, finding_id, version_no, actor.name, actor.kind, action, canonical_json(checks), note, now))
    return rid


def verify(conn: sqlite3.Connection, finding_id: str, actor: Actor, checks: dict[str, str],
           note: str | None = None, acknowledge_flags: bool = False) -> str:
    """Mark the current version of a finding as verified by ``actor``. Returns the review ID."""
    row = _load(conn, finding_id)
    _reviewer_actor(conn, actor, "verify")
    if row["record_status"] != "active":
        raise ReviewError("This finding is absent from the latest source version and cannot be verified.")
    if row["grounding_status"] != "grounded":
        raise ReviewError(
            "This finding is unsupported: its excerpt or a transcribed literal does not resolve to stored source "
            "text. Correct its provenance (edit the finding with valid source spans) before verifying it.")
    missing = [c for c in REQUIRED_CHECKS if checks.get(c) not in CHECK_VALUES]
    if missing:
        raise ReviewError(
            "Verification needs each of these checked against the source and recorded as 'confirmed' or "
            f"'not_applicable': {', '.join(missing)}. If a check fails, edit or reject the finding instead.")
    flags = F.loads(row["review_flags_json"], [])
    open_flags = sorted(set(flags) & F.DRAFT_FLAGS)
    if row["review_status"] == "draft" and not acknowledge_flags:
        raise ReviewError(
            f"This finding is an ambiguous draft ({', '.join(open_flags) or 'draft'}). Edit it to resolve the "
            "ambiguity, or verify with acknowledge_flags=True after checking each flagged point against the source.")
    now = utc_now()
    record = {c: checks[c] for c in REQUIRED_CHECKS}
    if open_flags:
        record["acknowledged_flags"] = open_flags
    rid = _add_review(conn, finding_id, row["current_version"], actor, "verify", record, note, now)
    conn.execute("UPDATE finding SET review_status = 'verified', updated_at = ? WHERE id = ?", (now, finding_id))
    audit.record(conn, actor, "finding.verify", "finding", finding_id,
                 {"version_no": row["current_version"], "review_id": rid, "checks": record,
                  "before_status": row["review_status"]})
    return rid


def reject(conn: sqlite3.Connection, finding_id: str, actor: Actor, note: str) -> str:
    """Reject a finding (it stays in the database with its history)."""
    row = _load(conn, finding_id)
    _reviewer_actor(conn, actor, "reject")
    if not note or not note.strip():
        raise ReviewError("Rejecting a finding needs a reason.")
    now = utc_now()
    rid = _add_review(conn, finding_id, row["current_version"], actor, "reject", {}, note, now)
    conn.execute("UPDATE finding SET review_status = 'rejected', updated_at = ? WHERE id = ?", (now, finding_id))
    audit.record(conn, actor, "finding.reject", "finding", finding_id,
                 {"version_no": row["current_version"], "review_id": rid, "before_status": row["review_status"],
                  "note": note})
    return rid


def reopen(conn: sqlite3.Connection, finding_id: str, actor: Actor, note: str | None = None) -> str:
    """Return a verified or rejected finding to ``unreviewed``; the earlier review is kept and marked stale."""
    row = _load(conn, finding_id)
    _reviewer_actor(conn, actor, "reopen")
    now = utc_now()
    F.mark_stale(conn, finding_id, "reopened by reviewer", now)
    rid = _add_review(conn, finding_id, row["current_version"], actor, "reopen", {}, note, now)
    conn.execute("UPDATE finding SET review_status = 'unreviewed', updated_at = ? WHERE id = ?", (now, finding_id))
    audit.record(conn, actor, "finding.reopen", "finding", finding_id,
                 {"version_no": row["current_version"], "before_status": row["review_status"], "note": note})
    return rid


def comment(conn: sqlite3.Connection, finding_id: str, actor: Actor, note: str) -> str:
    row = _load(conn, finding_id)
    _reviewer_actor(conn, actor, "comment")
    if not note or not note.strip():
        raise ReviewError("A comment must not be empty.")
    now = utc_now()
    rid = _add_review(conn, finding_id, row["current_version"], actor, "comment", {}, note, now)
    audit.record(conn, actor, "finding.comment", "finding", finding_id, {"review_id": rid})
    return rid


def edit(conn: sqlite3.Connection, finding_id: str, actor: Actor, changes: dict[str, Any],
         spans: dict[str, Any] | None = None, note: str | None = None,
         assignments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Correct a finding. The original extraction is preserved as an earlier version.

    ``changes`` maps content fields to new values. ``spans`` gives the source
    span of every transcribed literal that is changed (value, unit, uncertainty,
    timepoint, sample size); a changed literal without a matching span leaves
    the finding ``unsupported``, which blocks verification.

    Returns ``{"version_no", "grounding_status", "grounding_problems", "changed_fields"}``.
    """
    row = _load(conn, finding_id)
    _reviewer_actor(conn, actor, "edit")
    unknown = [k for k in changes if k not in F.CONTENT_KEYS]
    if unknown:
        raise ValidationError(f"Unknown finding fields: {', '.join(unknown)}")
    if "source_wording" in changes and changes["source_wording"] != row["source_wording"]:
        raise ReviewError("source_wording is the original wording of the source and cannot be edited. "
                          "Create a new finding from the correct source location instead.")
    before = F.content_from_row(row)
    after = {**before, **changes}
    after = F.validate_content(after)
    changed = sorted(k for k in F.CONTENT_KEYS if after.get(k) != before.get(k))
    new_assign = {k: row[k] for k in F.ASSIGNMENT_KEYS}
    if assignments:
        bad = [k for k in assignments if k not in F.ASSIGNMENT_KEYS]
        if bad:
            raise ValidationError(f"Unknown assignment fields: {', '.join(bad)}")
        new_assign.update(assignments)
    assign_changed = sorted(k for k in F.ASSIGNMENT_KEYS if new_assign[k] != row[k])
    if not changed and not assign_changed:
        raise ReviewError("The edit changes nothing.")
    new_spans = dict(F.loads(row["spans_json"], {}))
    for fld in F.LITERAL_FIELDS:
        if fld in changed:
            new_spans.pop(fld, None)          # a changed literal must be re-grounded
    new_spans.update(spans or {})
    for fld in list(new_spans):
        if fld in F.LITERAL_FIELDS and after.get(fld) in (None, ""):
            new_spans.pop(fld)
    locs = F.locations.current_locations(conn, "finding", finding_id)
    problems = F.grounding_problems(after, new_spans, F.stored_unit_texts(conn, locs), locs)
    grounding = "unsupported" if problems else "grounded"
    flags = [f for f in F.loads(row["review_flags_json"], []) if f not in F.DRAFT_FLAGS and f != F.UNSUPPORTED_FLAG]
    if problems:
        flags.append(F.UNSUPPORTED_FLAG)
        F.log_parse_failures(conn, "manual_extraction", locs[0] if locs else None, problems)
    now = utc_now()
    evidence_changed = bool(changed)
    stale = F.mark_stale(conn, finding_id, "finding edited by reviewer", now) if evidence_changed else \
        {"reviews": 0, "derived_values": 0}
    status = "unreviewed" if evidence_changed else row["review_status"]
    version_no = F.write_new_version(
        conn, finding_id, content=after, spans=new_spans, locs=None,
        stage_keys=F.active_stage_keys(conn, finding_id), actor=actor, change_kind="review_edit",
        review_status=status, review_flags=flags, grounding_status=grounding, note=note, assignments=new_assign)
    rid = _add_review(conn, finding_id, version_no, actor, "edit",
                      {"changed_fields": changed, "changed_assignments": assign_changed}, note, now)
    audit.record(conn, actor, "finding.edit", "finding", finding_id, {
        "from_version": row["current_version"], "to_version": version_no, "review_id": rid,
        "before": {**{k: before.get(k) for k in changed}, **{k: row[k] for k in assign_changed}},
        "after": {**{k: after.get(k) for k in changed}, **{k: new_assign[k] for k in assign_changed}},
        "grounding_status": grounding, "reviews_marked_stale": stale["reviews"],
        "derived_values_marked_stale": stale["derived_values"], "note": note})
    return {"version_no": version_no, "grounding_status": grounding, "grounding_problems": problems,
            "changed_fields": changed, "review_id": rid}


# ------------------------------------------------------------------ stages
def assign_stages(conn: sqlite3.Connection, finding_id: str, actor: Actor, add: list[str] | None = None,
                  remove: list[str] | None = None, note: str | None = None) -> list[str]:
    """Reviewer stage assignment. Adds reviewer links; removal deactivates every link to that stage.

    Imported and suggested links are kept (status ``removed``/``dismissed``) so the record shows where an
    assignment came from. Returns the active stage keys afterwards.
    """
    _load(conn, finding_id)
    _reviewer_actor(conn, actor, "stage")
    ids = F.stage_id_map(conn)
    for key in [*(add or []), *(remove or [])]:
        if key not in ids:
            raise ValidationError(f"Unknown stage key {key!r}; project stages: {', '.join(ids)}")
    now = utc_now()
    before = F.active_stage_keys(conn, finding_id)
    for key in add or []:
        row = conn.execute("SELECT id FROM finding_stage WHERE finding_id = ? AND stage_id = ? AND"
                           " assignment_source = 'reviewer'", (finding_id, ids[key])).fetchone()
        if row:
            conn.execute("UPDATE finding_stage SET status = 'active', updated_at = ?, assigned_by = ? WHERE id = ?",
                         (now, actor.name, row["id"]))
        else:
            conn.execute("INSERT INTO finding_stage (finding_id, stage_id, assignment_source, status, assigned_by,"
                         " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                         (finding_id, ids[key], "reviewer", "active", actor.name, now, now))
        conn.execute("UPDATE finding_stage SET status = 'dismissed', updated_at = ? WHERE finding_id = ? AND"
                     " stage_id = ? AND assignment_source = 'suggested' AND status = 'suggested'",
                     (now, finding_id, ids[key]))
    for key in remove or []:
        conn.execute("UPDATE finding_stage SET status = CASE assignment_source WHEN 'suggested' THEN 'dismissed'"
                     " ELSE 'removed' END, updated_at = ?, assigned_by = ? WHERE finding_id = ? AND stage_id = ?"
                     " AND status IN ('active','suggested')", (now, actor.name, finding_id, ids[key]))
    after = F.active_stage_keys(conn, finding_id)
    if after != before:
        conn.execute("UPDATE finding SET updated_at = ? WHERE id = ?", (now, finding_id))
        audit.record(conn, actor, "finding.stages", "finding", finding_id,
                     {"before": before, "after": after, "note": note})
    return after


# ------------------------------------------------------------------ relations between findings
def relate(conn: sqlite3.Connection, finding_a: str, finding_b: str, relation_type: str, actor: Actor,
           status: str = "confirmed", note: str | None = None) -> str:
    """Record a reviewer-asserted relation (e.g. ``dependent``: the two findings are not independent)."""
    if relation_type not in ("possible_duplicate", "dependent", "limited_by"):
        raise ValidationError(f"Unsupported relation type {relation_type!r}")
    if finding_a == finding_b:
        raise ValidationError("A finding cannot be related to itself.")
    _load(conn, finding_a), _load(conn, finding_b)
    a, b = (finding_a, finding_b) if relation_type == "limited_by" else sorted((finding_a, finding_b))
    now = utc_now()
    row = conn.execute("SELECT id FROM finding_relation WHERE finding_a = ? AND finding_b = ? AND relation_type = ?",
                       (a, b, relation_type)).fetchone()
    if row:
        rid = row["id"]
        conn.execute("UPDATE finding_relation SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                     (status, now, actor.name, rid))
    else:
        rid = new_id("frl")
        conn.execute(
            "INSERT INTO finding_relation (id, finding_a, finding_b, relation_type, status, method, details_json,"
            " created_at, created_by, decided_at, decided_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, a, b, relation_type, status, "reviewer", canonical_json({"note": note}), now, actor.name, now,
             actor.name))
    audit.record(conn, actor, "finding.relation", "finding_relation", rid,
                 {"finding_a": a, "finding_b": b, "relation_type": relation_type, "status": status, "note": note})
    return rid


def decide_relation(conn: sqlite3.Connection, relation_id: str, actor: Actor, status: str,
                    note: str | None = None) -> None:
    """Confirm or reject a candidate relation. Nothing is deleted or merged."""
    if status not in ("confirmed", "rejected", "candidate"):
        raise ValidationError("status must be confirmed, rejected or candidate")
    _reviewer_actor(conn, actor, "relation")
    row = conn.execute("SELECT * FROM finding_relation WHERE id = ?", (relation_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown finding relation {relation_id}")
    now = utc_now()
    conn.execute("UPDATE finding_relation SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                 (status, now, actor.name, relation_id))
    audit.record(conn, actor, "finding.relation_decision", "finding_relation", relation_id,
                 {"relation_type": row["relation_type"], "before": row["status"], "after": status, "note": note})


def relations_of(conn: sqlite3.Connection, finding_id: str) -> list[dict]:
    """Relations (any status) in which a finding takes part, with the other finding's id."""
    out = []
    for r in conn.execute("SELECT * FROM finding_relation WHERE finding_a = ? OR finding_b = ?"
                          " ORDER BY relation_type, created_at, id", (finding_id, finding_id)):
        d = dict(r)
        d["other_finding"] = d["finding_b"] if d["finding_a"] == finding_id else d["finding_a"]
        out.append(d)
    return out
