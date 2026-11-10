"""Sparse evidence hierarchy: paper > study > experiment > comparison group > finding.

Every level above the finding is optional. A finding that is not assigned to a
study, experiment or comparison group stays explicitly unassigned (NULL); no
placeholder records are created. A study may be reported by several papers
(``study_report``), which is how the toolkit knows that findings from different
papers describe the same underlying work and are not independent.

Records here are descriptions written by a reviewer. They are never inferred
from document text.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import audit, findings as F, review
from ..db import transaction
from ..documents.store import group_paper_ids
from ..errors import NotFoundError, ValidationError
from ..util import Actor, new_id, utc_now


def _get(conn: sqlite3.Connection, table: str, what: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()  # noqa: S608 (fixed names)
    if row is None:
        raise NotFoundError(f"Unknown {what} {record_id}")
    return row


def _label(label: str | None) -> str:
    if not label or not label.strip():
        raise ValidationError("A label is required.")
    return label.strip()


def create_study(conn: sqlite3.Connection, actor: Actor, label: str, *, description: str | None = None,
                 design: str | None = None, paper_ids: list[str] | None = None) -> dict[str, Any]:
    """A study: one underlying piece of work, reported by one or more papers."""
    review._reviewer_actor(conn, actor, "edit")
    sid, now = new_id("std"), utc_now()
    with transaction(conn):
        conn.execute("INSERT INTO study (id, label, description, design, created_at, created_by) VALUES (?,?,?,?,?,?)",
                     (sid, _label(label), description, design, now, actor.name))
        audit.record(conn, actor, "study.create", "study", sid, {"label": label.strip(), "design": design})
        for pid in paper_ids or []:
            link_study_report(conn, sid, pid, actor)
    return get_study(conn, sid)


def link_study_report(conn: sqlite3.Connection, study_id: str, paper_id: str, actor: Actor,
                      note: str | None = None, active: bool = True) -> None:
    """Record (or withdraw) that a paper reports a study."""
    review._reviewer_actor(conn, actor, "edit")
    _get(conn, "study", "study", study_id)
    _get(conn, "paper", "paper", paper_id)
    now = utc_now()
    with transaction(conn):
        conn.execute(
            "INSERT INTO study_report (study_id, paper_id, note, active, created_at, created_by) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (study_id, paper_id) DO UPDATE SET active = excluded.active,"
            " note = COALESCE(excluded.note, note)", (study_id, paper_id, note, 1 if active else 0, now, actor.name))
        audit.record(conn, actor, "study.report_link" if active else "study.report_unlink", "study", study_id,
                     {"paper_id": paper_id, "note": note})


def create_experiment(conn: sqlite3.Connection, actor: Actor, paper_id: str, label: str, *,
                      study_id: str | None = None, description: str | None = None,
                      system_context: str | None = None, design: str | None = None) -> dict[str, Any]:
    """An experiment reported in a paper, optionally part of a study that the paper reports."""
    review._reviewer_actor(conn, actor, "edit")
    _get(conn, "paper", "paper", paper_id)
    if study_id:
        _get(conn, "study", "study", study_id)
    eid, now = new_id("exp"), utc_now()
    with transaction(conn):
        conn.execute(
            "INSERT INTO experiment (id, paper_id, study_id, label, description, system_context, design, created_at,"
            " created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, paper_id, study_id, _label(label), description, system_context, design, now, actor.name))
        audit.record(conn, actor, "experiment.create", "experiment", eid,
                     {"paper_id": paper_id, "study_id": study_id, "label": label.strip()})
        if study_id and conn.execute("SELECT 1 FROM study_report WHERE study_id = ? AND paper_id = ? AND active = 1",
                                     (study_id, paper_id)).fetchone() is None:
            link_study_report(conn, study_id, paper_id, actor, note="linked when an experiment was added")
    return dict(_get(conn, "experiment", "experiment", eid))


def create_group(conn: sqlite3.Connection, actor: Actor, experiment_id: str, label: str, *,
                 role: str | None = None, description: str | None = None) -> dict[str, Any]:
    """A comparison group (arm, condition, cohort) inside an experiment. ``role`` is free text."""
    review._reviewer_actor(conn, actor, "edit")
    _get(conn, "experiment", "experiment", experiment_id)
    gid, now = new_id("grp"), utc_now()
    with transaction(conn):
        conn.execute("INSERT INTO comparison_group (id, experiment_id, label, role, description, created_at,"
                     " created_by) VALUES (?,?,?,?,?,?,?)",
                     (gid, experiment_id, _label(label), role, description, now, actor.name))
        audit.record(conn, actor, "comparison_group.create", "comparison_group", gid,
                     {"experiment_id": experiment_id, "label": label.strip(), "role": role})
    return dict(_get(conn, "comparison_group", "comparison group", gid))


def remove(conn: sqlite3.Connection, level: str, record_id: str, actor: Actor, reason: str) -> None:
    """Soft-remove a study, experiment or comparison group that no active finding is assigned to."""
    review._reviewer_actor(conn, actor, "edit")
    tables = {"study": ("study", "study_id"), "experiment": ("experiment", "experiment_id"),
              "group": ("comparison_group", "comparison_group_id")}
    if level not in tables:
        raise ValidationError("level must be 'study', 'experiment' or 'group'.")
    if not reason or not reason.strip():
        raise ValidationError("A reason is required.")
    table, column = tables[level]
    _get(conn, table, level, record_id)
    used = conn.execute(f"SELECT COUNT(*) FROM finding WHERE {column} = ? AND record_status != 'removed'",  # noqa: S608
                        (record_id,)).fetchone()[0]
    if used:
        raise ValidationError(f"{used} finding(s) are assigned to this {level}; reassign them first.")
    with transaction(conn):
        conn.execute(f"UPDATE {table} SET record_status = 'removed' WHERE id = ?", (record_id,))  # noqa: S608
        audit.record(conn, actor, f"{table}.remove", table, record_id, {"reason": reason.strip()})


# ------------------------------------------------------------------ assignment
def check_assignments(conn: sqlite3.Connection, paper_id: str, assignments: dict[str, Any]) -> dict[str, Any]:
    """Validate a finding's study/experiment/group assignment and fill what the lower levels imply.

    Rules: a comparison group belongs to its experiment; an experiment belongs to the
    finding's paper (or a record grouped with it); a study must be reported by that paper;
    an experiment's study, when it has one, is the finding's study. Nothing is guessed:
    only levels that are *implied* by a given lower level are filled in.
    """
    bad = [k for k in assignments if k not in F.ASSIGNMENT_KEYS]
    if bad:
        raise ValidationError(f"Unknown assignment fields: {', '.join(bad)}")
    out = {k: assignments.get(k) or None for k in F.ASSIGNMENT_KEYS}
    group_ids = group_paper_ids(conn, paper_id)
    if out["comparison_group_id"]:
        grp = _get(conn, "comparison_group", "comparison group", out["comparison_group_id"])
        if grp["record_status"] != "active":
            raise ValidationError("This comparison group was removed.")
        if out["experiment_id"] and out["experiment_id"] != grp["experiment_id"]:
            raise ValidationError("The comparison group belongs to a different experiment.")
        out["experiment_id"] = grp["experiment_id"]
    if out["experiment_id"]:
        exp = _get(conn, "experiment", "experiment", out["experiment_id"])
        if exp["record_status"] != "active":
            raise ValidationError("This experiment was removed.")
        if exp["paper_id"] not in group_ids:
            raise ValidationError(f"Experiment {exp['id']} is reported in paper {exp['paper_id']}, "
                                  f"not in the finding's paper {paper_id}.")
        if exp["study_id"]:
            if out["study_id"] and out["study_id"] != exp["study_id"]:
                raise ValidationError("The experiment belongs to a different study.")
            out["study_id"] = exp["study_id"]
    if out["study_id"]:
        study = _get(conn, "study", "study", out["study_id"])
        if study["record_status"] != "active":
            raise ValidationError("This study was removed.")
        marks = ",".join("?" * len(group_ids))
        if conn.execute(f"SELECT 1 FROM study_report WHERE study_id = ? AND active = 1 AND paper_id IN ({marks})",
                        (out["study_id"], *group_ids)).fetchone() is None:
            raise ValidationError(f"Study {study['id']} is not recorded as reported by paper {paper_id}. "
                                  "Link the paper to the study first.")
    return out


def assign(conn: sqlite3.Connection, finding_id: str, actor: Actor, *, study_id: str | None = None,
           experiment_id: str | None = None, comparison_group_id: str | None = None, clear: bool = False,
           note: str | None = None) -> dict[str, Any]:
    """Assign a finding to a study / experiment / comparison group, or clear the assignment.

    Assignment does not change the evidence, so an existing verification stays valid; the change is a
    new finding version with its own audit entry.
    """
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown finding {finding_id}")
    if clear:
        new = {k: None for k in F.ASSIGNMENT_KEYS}
    else:
        given = {"study_id": study_id, "experiment_id": experiment_id, "comparison_group_id": comparison_group_id}
        if not any(given.values()):
            raise ValidationError("Give a study, experiment or comparison group, or clear the assignment.")
        new = check_assignments(conn, row["paper_id"], given)
    with transaction(conn):
        out = review.edit(conn, finding_id, actor, {}, note=note, assignments=new)
    return {"finding_id": finding_id, "version_no": out["version_no"], **new}


# ------------------------------------------------------------------ reading
def get_study(conn: sqlite3.Connection, study_id: str) -> dict[str, Any]:
    out = dict(_get(conn, "study", "study", study_id))
    out["papers"] = [r["paper_id"] for r in conn.execute(
        "SELECT paper_id FROM study_report WHERE study_id = ? AND active = 1 ORDER BY created_at", (study_id,))]
    return out


def tree(conn: sqlite3.Connection, paper_id: str | None = None) -> dict[str, Any]:
    """Studies, experiments and comparison groups with finding counts, plus the unassigned findings count."""
    params: list[Any] = []
    where = ""
    if paper_id:
        ids = group_paper_ids(conn, paper_id)
        where = f" AND e.paper_id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    counts = {(r["experiment_id"], r["comparison_group_id"]): r["n"] for r in conn.execute(
        "SELECT experiment_id, comparison_group_id, COUNT(*) n FROM finding WHERE record_status = 'active'"
        " AND experiment_id IS NOT NULL GROUP BY experiment_id, comparison_group_id")}
    experiments = []
    for e in conn.execute(f"SELECT e.* FROM experiment e WHERE e.record_status = 'active'{where}"  # noqa: S608
                          " ORDER BY e.created_at, e.id", params):
        groups = [{**dict(g), "n_findings": counts.get((e["id"], g["id"]), 0)} for g in conn.execute(
            "SELECT * FROM comparison_group WHERE experiment_id = ? AND record_status = 'active'"
            " ORDER BY created_at, id", (e["id"],))]
        experiments.append({**dict(e), "groups": groups,
                            "n_findings": sum(n for (eid, _), n in counts.items() if eid == e["id"])})
    study_ids = sorted({e["study_id"] for e in experiments if e["study_id"]})
    if paper_id:
        ids = group_paper_ids(conn, paper_id)
        study_ids = sorted(set(study_ids) | {r["study_id"] for r in conn.execute(
            f"SELECT study_id FROM study_report WHERE active = 1 AND paper_id IN ({','.join('?' * len(ids))})",
            ids)})
        studies = [get_study(conn, s) for s in study_ids]
    else:
        studies = [get_study(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM study WHERE record_status = 'active' ORDER BY created_at, id")]
    for s in studies:
        s["n_findings"] = conn.execute("SELECT COUNT(*) FROM finding WHERE study_id = ? AND record_status = 'active'",
                                       (s["id"],)).fetchone()[0]
    sql = ("SELECT COUNT(*) FROM finding WHERE record_status = 'active' AND study_id IS NULL AND experiment_id IS NULL"
           " AND comparison_group_id IS NULL")
    if paper_id:
        ids = group_paper_ids(conn, paper_id)
        unassigned = conn.execute(sql + f" AND paper_id IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
    else:
        unassigned = conn.execute(sql).fetchone()[0]
    return {"studies": studies, "experiments": experiments, "unassigned_findings": unassigned}
