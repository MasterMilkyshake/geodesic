"""Missing-information summaries and the modelling-readiness inventory.

These are inventories of what the project's records contain. They keep the four
reasons a field can be empty apart - ``not_reported`` (the source does not
state it), ``not_applicable``, ``not_yet_extracted`` (nobody has looked) and
``could_not_be_parsed`` - and add ``no_status_recorded`` for empty fields
nobody has classified yet. An empty field is never reported as "not reported"
unless a person or the importer recorded exactly that.

Nothing here fits a model, pools values or judges suitability: suitability for
modelling is a reviewer's assessment (``geodesic assess``), shown as recorded.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import query
from ..export.tables import Table
from ..findings import FIELD_STATUSES
from ..util import loads

STATES = ("recorded", *FIELD_STATUSES, "no_status_recorded")
#: (status key, finding column that holds the value, population)
FIELDS = (
    ("outcome", "outcome", "measurement"),
    ("system_context", "system_context", "measurement"),
    ("experimental_context", "experimental_context", "measurement"),
    ("measurement_method", "measurement_method", "measurement"),
    ("observation_type", "observation_type", "measurement"),
    ("unit_original", "unit_original", "quantitative"),
    ("timepoint", "timepoint_literal", "measurement"),
    ("sample_size", "sample_size", "measurement"),
    ("uncertainty", "uncertainty_type", "quantitative"),
    ("denominator", "denominator", "measurement"),
    ("reference_definition", "reference_definition", "relative"),
)
POPULATIONS = {
    "measurement": "measurement findings",
    "quantitative": "measurement findings stating a number, range or bound",
    "relative": "measurement findings stating a relative quantity",
}
ALL_STAGES = "(all stages)"
NO_STAGE = "(no stage)"


def _in_population(pop: str, r: sqlite3.Row) -> bool:
    if pop == "quantitative":
        return r["statement_type"] in query.QUANTITATIVE_TYPES
    if pop == "relative":
        return r["statement_type"] in query.QUANTITATIVE_TYPES and r["relative_semantics"] not in (None, "absolute")
    return True


def missing_information(conn: sqlite3.Connection, flt: query.FindingFilter | None = None) -> dict[str, Any]:
    """Per stage and field: how many measurement findings record a value and why the others do not."""
    flt = flt or query.FindingFilter()
    w = query._finding_where(flt)
    w.add("f.kind = 'measurement'")
    cols = sorted({c for _, c, _ in FIELDS})
    rows = conn.execute(
        f"SELECT f.id, f.statement_type, f.relative_semantics, f.field_status_json, {', '.join('f.' + c for c in cols)}"  # noqa: S608
        " FROM finding f JOIN paper p ON p.id = f.paper_id" + w.sql() + " ORDER BY f.pk", w.params).fetchall()
    stages = [(r["key"], r["name"]) for r in conn.execute("SELECT key, name FROM stage ORDER BY position")]
    stages_of: dict[str, list[str]] = {}
    for r in conn.execute("SELECT fs.finding_id, s.key FROM finding_stage fs JOIN stage s ON s.id = fs.stage_id"
                          " WHERE fs.status = 'active'"):
        stages_of.setdefault(r[0], []).append(r[1])
    tally: dict[tuple[str, str], dict[str, int]] = {}

    def bump(stage: str, fld: str, state: str) -> None:
        t = tally.setdefault((stage, fld), {"findings": 0, **{s: 0 for s in STATES}})
        t["findings"] += 1
        t[state] += 1

    for r in rows:
        status = loads(r["field_status_json"], {})
        keys = stages_of.get(r["id"]) or [NO_STAGE]
        for key, col, pop in FIELDS:
            if not _in_population(pop, r):
                continue
            if r[col] not in (None, ""):
                state = "recorded"
            else:
                state = status.get(key, "no_status_recorded")
                if state not in STATES:
                    state = "no_status_recorded"
            bump(ALL_STAGES, key, state)
            for k in keys:
                bump(k, key, state)
    out_rows = []
    order = [(ALL_STAGES, ALL_STAGES), *stages, (NO_STAGE, NO_STAGE)]
    for skey, sname in order:
        for key, _, pop in FIELDS:
            t = tally.get((skey, key))
            if t:
                out_rows.append({"stage_key": skey, "stage": sname, "field": key, "population": POPULATIONS[pop], **t})
    return {
        "states": list(STATES), "fields": [k for k, _, _ in FIELDS], "rows": out_rows,
        "measurement_findings": len(rows),
        "notes": [
            "Counted over measurement findings (narrative statements and limitations have no such fields). "
            "A finding assigned to several stages is counted in each; '(all stages)' counts each finding once.",
            "'not_reported' means someone recorded that the source does not state it; 'no_status_recorded' means "
            "the field is empty and nobody has yet said why. The two are not the same.",
            "Unit and uncertainty are counted for findings that state a number, range or bound; the reference "
            "definition for relative quantities only.",
        ]}


def missing_table(summary: dict[str, Any]) -> Table:
    cols = ["stage_key", "stage", "field", "population", "findings", *STATES]
    return Table("missing_information", "Missing-information summary", cols, summary["rows"],
                 " ".join(summary["notes"]))


def format_missing(summary: dict[str, Any], stage_key: str | None = None) -> str:
    short = {"recorded": "recorded", "not_reported": "not rep.", "not_applicable": "not appl.",
             "not_yet_extracted": "not extr.", "could_not_be_parsed": "unparsed", "no_status_recorded": "no status"}
    lines = ["Missing-information summary (measurement findings)", ""]
    head = "field".ljust(22) + "findings".rjust(9) + "".join(short[s].rjust(11) for s in STATES)
    current = None
    for r in summary["rows"]:
        if stage_key and r["stage_key"] not in (stage_key,):
            continue
        if r["stage_key"] != current:
            current = r["stage_key"]
            lines += ["", r["stage"], head, "-" * len(head)]
        lines.append(r["field"].ljust(22) + str(r["findings"]).rjust(9) + "".join(str(r[s]).rjust(11) for s in STATES))
    if not summary["rows"]:
        lines.append("No measurement findings.")
    lines += [""] + [f"Note: {n}" for n in summary["notes"]]
    return "\n".join(lines)


# ------------------------------------------------------------------ full text
FULLTEXT_ORDER = ("document_attached", "open_access_reported", "abstract_only", "not_available", "retrieval_failed",
                  "not_checked")


def fulltext_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Full-text availability of the project's reports (records grouped under another record are not counted)."""
    counts = {k: 0 for k in FULLTEXT_ORDER}
    for r in conn.execute("SELECT fulltext_availability, COUNT(*) FROM paper WHERE record_status = 'active' AND"
                          " canonical_id IS NULL GROUP BY fulltext_availability"):
        counts[r[0]] = r[1]
    docs = {r[0]: r[1] for r in conn.execute(
        "SELECT s.status, COUNT(*) FROM document d JOIN document_snapshot s ON s.document_id = d.id"
        " WHERE d.record_status = 'active' AND s.snapshot_no = (SELECT MAX(x.snapshot_no) FROM document_snapshot x"
        " WHERE x.document_id = d.id) GROUP BY s.status")}
    with_findings = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(p.canonical_id, p.id)) FROM finding f JOIN paper p ON p.id = f.paper_id"
        " WHERE f.record_status = 'active'").fetchone()[0]
    return {"reports": sum(counts.values()), "by_availability": counts, "documents_by_parse_status": docs,
            "reports_with_findings": with_findings,
            "notes": ["'not_checked' means nobody has looked for the full text yet; 'not_available' and "
                      "'abstract_only' were recorded by a person; 'retrieval_failed' is an automatic open-access "
                      "retrieval that did not succeed. Findings of reports without an attached document rest on "
                      "the imported workbook or on the abstract only."]}


# ------------------------------------------------------------------ modelling readiness
def modeling_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the records would offer a later modelling step - an inventory, not an assessment and not a fit."""
    stages = [(r["id"], r["key"], r["name"]) for r in conn.execute(
        "SELECT id, key, name FROM stage WHERE active = 1 ORDER BY position")]
    quant = "', '".join(query.QUANTITATIVE_TYPES)
    base = (f"FROM finding f WHERE f.record_status = 'active' AND f.kind = 'measurement' AND"  # noqa: S608
            f" f.statement_type IN ('{quant}')")
    exprs = {
        "quantitative_findings": "COUNT(*)",
        "with_unit": "SUM(f.unit_original IS NOT NULL)",
        "absolute_quantities": "SUM(f.relative_semantics IS NULL OR f.relative_semantics = 'absolute')",
        "with_timepoint": "SUM(f.timepoint_value IS NOT NULL)",
        "with_uncertainty": "SUM(f.uncertainty_type IS NOT NULL)",
        "with_sample_size": "SUM(f.sample_size IS NOT NULL)",
        "verified": "SUM(f.review_status = 'verified')",
        "quantitative_candidates": "SUM(f.comparison_eligibility = 'quantitative_candidate')",
        "assigned_to_experiment": "SUM(f.experiment_id IS NOT NULL)",
    }
    select = "SELECT " + ", ".join(f"COALESCE({e}, 0) AS {k}" for k, e in exprs.items()) + " "

    def series(extra: str, params: tuple) -> int:
        """Experiments holding three or more distinct time points for one outcome and unit."""
        return conn.execute(
            "SELECT COUNT(*) FROM (SELECT f.experiment_id FROM finding f WHERE f.record_status = 'active' AND"
            " f.kind = 'measurement' AND f.experiment_id IS NOT NULL AND f.timepoint_value IS NOT NULL AND"
            f" f.value_num IS NOT NULL {extra} GROUP BY f.experiment_id, f.outcome, f.unit_original"
            " HAVING COUNT(DISTINCT f.timepoint_value || ' ' || COALESCE(f.time_unit, '')) >= 3)", params).fetchone()[0]

    rows = [{"stage_key": "(all stages)", "stage": "(all stages)", **dict(conn.execute(select + base).fetchone()),
             "time_series_candidates": series("", ())}]
    for sid, key, name in stages:
        in_stage = (" AND f.id IN (SELECT fs.finding_id FROM finding_stage fs WHERE fs.stage_id = ? AND"
                    " fs.status = 'active')")
        rows.append({"stage_key": key, "stage": name, **dict(conn.execute(select + base + in_stage, (sid,)).fetchone()),
                     "time_series_candidates": series(in_stage, (sid,))})
    assessed = {}
    for dim in ("quantitative_comparison", "mathematical_modeling"):
        assessed[dim] = {r[0]: r[1] for r in conn.execute(
            "SELECT value, COUNT(DISTINCT paper_id) FROM paper_assessment WHERE dimension = ? AND is_current = 1"
            " GROUP BY value ORDER BY value", (dim,))}
    return {
        "rows": rows, "columns": ["stage_key", "stage", *exprs, "time_series_candidates"],
        "reviewer_assessments": assessed,
        "statement": "Inventory of recorded fields only. No model is fitted, no values are pooled, and whether a "
                     "report suits modelling is a reviewer's assessment, shown here as recorded.",
        "notes": [
            "'time_series_candidates' counts groups of findings of one experiment with the same outcome and unit "
            "at three or more distinct time points; findings not assigned to an experiment are not considered.",
            "Reviewer assessments count papers per value; several reviewers can assess one paper differently, so a "
            "paper can appear under more than one value.",
        ]}


def modeling_table(inv: dict[str, Any]) -> Table:
    return Table("modeling_inventory", "Modelling-readiness inventory", inv["columns"], inv["rows"],
                 inv["statement"] + " " + " ".join(inv["notes"]))


def format_modeling(inv: dict[str, Any]) -> str:
    lines = ["Modelling-readiness inventory", "", inv["statement"], ""]
    for r in inv["rows"]:
        lines.append(f"{r['stage']}: " + ", ".join(
            f"{k.replace('_', ' ')} {r[k]}" for k in inv["columns"][2:]))
    for dim, vals in inv["reviewer_assessments"].items():
        lines.append(f"Reviewer assessments, {dim.replace('_', ' ')}: "
                     + (", ".join(f"{k}: {v} paper(s)" for k, v in vals.items()) or "none recorded"))
    lines += [""] + [f"Note: {n}" for n in inv["notes"]]
    return "\n".join(lines)
