"""Normalized and derived values, each traceable to the source text it came from.

Two kinds of computed value exist, and both live in their own table. They never
overwrite a finding's original wording, literal or unit.

``normalized``
    One finding's value converted to another unit of the same dimension with a
    factor from the explicit unit table (:mod:`geodesic.quantities.units`).
``derived``
    A value computed from several findings by a named rule (``difference`` or
    ``ratio``).

Every computed value stores its rule, a readable formula and, for each input,
the finding ID, the finding *version*, the transcribed literal and the unit. The
:func:`trace` function follows those links back to the source location and
re-reads the stored text, so a computed value can always be shown next to the
words it was computed from.

What is never done
------------------
* Relative quantities (percent, fold, arbitrary units, parts-per) are not
  converted, and are not combined unless unit, relative semantics and stated
  reference are identical.
* Ambiguous and unrecognised units are not converted.
* A range stays a range and a bound stays a bound; no midpoint is invented.
* Uncertainty is scaled by a unit conversion but is not propagated through
  ``difference`` or ``ratio`` (that would need assumptions the source does not
  state). The inputs' uncertainty is carried along for the reader.
* Nothing is pooled or averaged.

When a finding is edited, every computed value that used it is marked stale
(:func:`geodesic.findings.mark_stale`); stale values are excluded from tables
unless asked for and must be recomputed explicitly.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import audit, findings as F, locations, papers
from ..db import transaction
from ..errors import NotFoundError, ValidationError
from ..project import Project
from ..quantities import units
from ..util import Actor, canonical_json, loads, new_id, utc_now

RULES = {
    "difference": "a - b (both converted to the unit of a)",
    "ratio": "a / b (b converted to the unit of a when both have the same dimension)",
}
_POINT = ("exact", "approximate")
_RELATIVE_OK = (None, "absolute")


def _finding(conn: sqlite3.Connection, finding_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown finding {finding_id}")
    f = dict(row)
    if f["record_status"] != "active":
        raise ValidationError(f"Finding {finding_id} is not active ({f['record_status']}).")
    if f["review_status"] == "rejected":
        raise ValidationError(f"Finding {finding_id} was rejected in review; nothing is computed from it.")
    if f["grounding_status"] != "grounded":
        raise ValidationError(f"Finding {finding_id} is unsupported (its source does not resolve); "
                              "nothing is computed from it.")
    return f


def _input(f: dict[str, Any], field: str, value: dict[str, Any], unit: str | None) -> dict[str, Any]:
    literal_field = "timepoint_literal" if field == "timepoint" else "value_literal"
    return {"finding_id": f["id"], "version_no": f["current_version"], "field": literal_field,
            "source_literal": f[literal_field], "unit": unit, "statement_type": f["statement_type"]
            if field == "value" else "exact", **value}


def _store(conn: sqlite3.Connection, actor: Actor, kind: str, rule_id: str, formula: str, inputs: list[dict],
           out: dict[str, Any], unit: str, carries: dict[str, Any]) -> str:
    did, now = new_id("drv"), utc_now()
    conn.execute(
        "INSERT INTO derived_value (id, kind, rule_id, formula, inputs_json, output_value, output_low, output_high,"
        " output_unit, carries_json, status, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, kind, rule_id, formula, canonical_json(inputs), out.get("value"), out.get("low"), out.get("high"),
         unit, canonical_json(carries), "current", now, actor.name))
    conn.executemany("INSERT INTO derived_value_input (derived_value_id, finding_id) VALUES (?,?)",
                     [(did, fid) for fid in dict.fromkeys(i["finding_id"] for i in inputs)])
    audit.record(conn, actor, f"derived_value.{kind}", "derived_value", did, {
        "rule_id": rule_id, "formula": formula, "output_unit": unit,
        "inputs": [{k: i[k] for k in ("finding_id", "version_no", "field")} for i in inputs]})
    return did


def _existing(conn: sqlite3.Connection, kind: str, rule_id: str, inputs: list[dict], unit: str) -> str | None:
    key = [(i["finding_id"], i["version_no"], i["field"]) for i in inputs]
    for r in conn.execute(
            "SELECT d.id, d.inputs_json FROM derived_value d JOIN derived_value_input di ON di.derived_value_id = d.id"
            " WHERE di.finding_id = ? AND d.kind = ? AND d.rule_id = ? AND d.output_unit = ? AND d.status = 'current'",
            (inputs[0]["finding_id"], kind, rule_id, unit)):
        if [(i["finding_id"], i["version_no"], i["field"]) for i in loads(r["inputs_json"], [])] == key:
            return r["id"]
    return None


def _qualifier_texts(f: dict[str, Any]) -> list[str]:
    return [q.get("text") for q in loads(f["qualifiers_json"], []) if q.get("text")]


# ------------------------------------------------------------------ normalization
def normalize_finding(project: Project, finding_id: str, actor: Actor, *, target_unit: str | None = None,
                      field: str = "value") -> dict[str, Any]:
    """Convert a finding's value (or timepoint) to ``target_unit`` (default: the dimension's base unit).

    Refuses with an explanation when the unit is missing, unrecognised, ambiguous, relative or has
    no fixed factor. Returns the stored (or already existing) normalized value.
    """
    conn = project.conn
    f = _finding(conn, finding_id)
    if field not in ("value", "timepoint"):
        raise ValidationError("field must be 'value' or 'timepoint'.")
    if field == "value":
        unit = f["unit_original"]
        if f["statement_type"] not in ("exact", "approximate", "range", "bound"):
            raise ValidationError(f"Finding {finding_id} states no numeric value ({f['statement_type']}).")
        if f["relative_semantics"] not in _RELATIVE_OK:
            raise ValidationError(f"Finding {finding_id} is a relative quantity ({f['relative_semantics']}); "
                                  "relative quantities are never converted automatically.")
        value = {"value": f["value_num"], "value_low": f["value_low"], "value_high": f["value_high"]}
    else:
        unit = f["time_unit"]
        if f["timepoint_value"] is None:
            raise ValidationError(f"Finding {finding_id} has no parsed timepoint.")
        value = {"value": f["timepoint_value"], "value_low": None, "value_high": None}
    if not unit:
        raise ValidationError(f"Finding {finding_id} has no transcribed unit; nothing can be converted.")
    target = target_unit or units.base_unit(unit)
    try:
        if target is None:
            units.conversion(unit, unit)       # raises with the specific reason
            raise units.ConversionError(f"no base unit is defined for {unit!r}")
        factor, rule_id = units.conversion(unit, target)
    except units.ConversionError as exc:
        raise ValidationError(f"Not converted: {exc}") from exc
    inputs = [_input(f, field, value, unit)]
    done = _existing(conn, "normalized", rule_id, inputs, target)
    if done:
        return {**get(conn, done), "already_present": True}
    out = {k: (None if value[src] is None else value[src] * factor)
           for k, src in (("value", "value"), ("low", "value_low"), ("high", "value_high"))}
    carries: dict[str, Any] = {"field": field, "factor": factor, "statement_type": inputs[0]["statement_type"]}
    if field == "value":
        carries.update(bound_type=f["bound_type"], qualifiers=_qualifier_texts(f))
        if f["uncertainty_type"]:
            carries["uncertainty"] = {
                "type": f["uncertainty_type"], "literal": f["uncertainty_literal"],
                "confidence_level": f["confidence_level"], "unit": target,
                **{k: (None if f[src] is None else f[src] * factor) for k, src in (
                    ("value", "uncertainty_value"), ("low", "uncertainty_low"), ("high", "uncertainty_high"))}}
    formula = f"{f['timepoint_literal' if field == 'timepoint' else 'value_literal']!s} [{unit}] × {factor:.12g} = [{target}]"
    with transaction(conn):
        did = _store(conn, actor, "normalized", rule_id, formula, inputs, out, target, carries)
    return {**get(conn, did), "already_present": False}


# ------------------------------------------------------------------ derivation
def _combinable(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
    """Factor converting b's unit to a's unit, with a note. Raises ValidationError when they cannot be combined."""
    ua, ub = a["unit_original"], b["unit_original"]
    if not ua or not ub:
        raise ValidationError("Both findings need a transcribed unit.")
    relative = a["relative_semantics"] not in _RELATIVE_OK or b["relative_semantics"] not in _RELATIVE_OK
    if not relative:
        try:
            factor, rule = units.conversion(ub, ua)
            return factor, rule
        except units.ConversionError as exc:
            if ua != ub:
                raise ValidationError(f"Not computed: {exc}") from exc
            status, unit = units.lookup(ua)
            if status == "ambiguous" or (unit and unit.dimension in units.RELATIVE_DIMENSIONS):
                relative = True
            else:
                return 1.0, "identical_unit"
    if ua != ub or a["relative_semantics"] != b["relative_semantics"] or not a["reference_definition"] or \
            a["reference_definition"] != b["reference_definition"]:
        raise ValidationError(
            "Not computed: relative quantities are only combined when unit, relative semantics and the stated "
            "reference are identical and recorded on both findings.")
    return 1.0, "identical_relative_unit_and_reference"


def derive(project: Project, rule: str, finding_ids: list[str], actor: Actor) -> dict[str, Any]:
    """Compute ``difference`` (a - b) or ``ratio`` (a / b) from two point-valued findings."""
    conn = project.conn
    if rule not in RULES:
        raise ValidationError(f"Unknown rule {rule!r}. Available: {', '.join(RULES)}.")
    if len(finding_ids) != 2 or finding_ids[0] == finding_ids[1]:
        raise ValidationError(f"The rule '{rule}' needs two different findings (a, b).")
    a, b = (_finding(conn, fid) for fid in finding_ids)
    for f in (a, b):
        if f["statement_type"] not in _POINT or f["value_num"] is None:
            raise ValidationError(f"Finding {f['id']} is a {f['statement_type']} statement; '{rule}' is only "
                                  "defined for point values (ranges and bounds are not reduced to a number).")
    ua, ub = a["unit_original"], b["unit_original"]
    if rule == "difference":
        factor, how = _combinable(a, b)
        out = {"value": a["value_num"] - b["value_num"] * factor}
        unit = ua
        formula = f"{a['value_literal']} [{ua}] − {b['value_literal']} [{ub}] × {factor:.12g}"
    else:
        try:
            factor, how = _combinable(a, b)
            unit = "1"
        except ValidationError:
            if a["relative_semantics"] not in _RELATIVE_OK or b["relative_semantics"] not in _RELATIVE_OK or \
                    not ua or not ub or ua == ub:
                raise
            for u in (ua, ub):
                status, _ = units.lookup(u)
                if status != "recognized":
                    raise ValidationError(f"Not computed: unit {u!r} is {status}.") from None
            factor, how, unit = 1.0, "quotient_of_units", f"({ua})/({ub})"
        if b["value_num"] == 0:
            raise ValidationError("Not computed: division by a value of zero.")
        out = {"value": a["value_num"] / (b["value_num"] * factor)}
        formula = f"{a['value_literal']} [{ua}] ÷ ({b['value_literal']} [{ub}] × {factor:.12g})"
    inputs = [_input(f, "value", {"value": f["value_num"], "value_low": None, "value_high": None},
                     f["unit_original"]) for f in (a, b)]
    for name, i in zip("ab", inputs):
        i["operand"] = name
    done = _existing(conn, "derived", rule, inputs, unit)
    if done:
        return {**get(conn, done), "already_present": True}
    same_exp = a["experiment_id"] is not None and a["experiment_id"] == b["experiment_id"]
    carries = {
        "statement_type": "approximate" if "approximate" in (a["statement_type"], b["statement_type"]) else "exact",
        "unit_handling": how, "factor_b_to_a": factor,
        "qualifiers": {"a": _qualifier_texts(a), "b": _qualifier_texts(b)},
        "input_uncertainty": {n: ({"type": f["uncertainty_type"], "literal": f["uncertainty_literal"]}
                                  if f["uncertainty_type"] else None) for n, f in (("a", a), ("b", b))},
        "uncertainty": "not propagated",
        "same_paper": a["paper_id"] == b["paper_id"], "same_experiment": same_exp}
    with transaction(conn):
        did = _store(conn, actor, "derived", rule, formula, inputs, out, unit, carries)
    return {**get(conn, did), "already_present": False}


def withdraw(conn: sqlite3.Connection, derived_value_id: str, actor: Actor, reason: str) -> None:
    """Withdraw a computed value (it stays in the record)."""
    d = get(conn, derived_value_id)
    if d["status"] == "withdrawn":
        raise ValidationError("This value is already withdrawn.")
    if not reason or not reason.strip():
        raise ValidationError("A reason is required.")
    with transaction(conn):
        conn.execute("UPDATE derived_value SET status = 'withdrawn', stale_reason = ? WHERE id = ?",
                     (reason.strip(), derived_value_id))
        audit.record(conn, actor, "derived_value.withdraw", "derived_value", derived_value_id, {"reason": reason})


# ------------------------------------------------------------------ reading and tracing
def get(conn: sqlite3.Connection, derived_value_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM derived_value WHERE id = ?", (derived_value_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown derived value {derived_value_id}")
    out = dict(row)
    out["inputs"] = loads(out.pop("inputs_json"), [])
    out["carries"] = loads(out.pop("carries_json"), {})
    return out


def list_values(conn: sqlite3.Connection, *, finding_id: str | None = None, kind: str | None = None,
                include_stale: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
    sql, params = "SELECT d.* FROM derived_value d WHERE 1 = 1", []
    if finding_id:
        sql += " AND d.id IN (SELECT derived_value_id FROM derived_value_input WHERE finding_id = ?)"
        params.append(finding_id)
    if kind:
        sql += " AND d.kind = ?"
        params.append(kind)
    if not include_stale:
        sql += " AND d.status = 'current'"
    sql += " ORDER BY d.created_at, d.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["inputs"] = loads(d.pop("inputs_json"), [])
        d["carries"] = loads(d.pop("carries_json"), {})
        out.append(d)
    return out


def trace(conn: sqlite3.Connection, derived_value_id: str) -> dict[str, Any]:
    """Follow a computed value back to stored source text.

    For every input: the finding version that was used, the transcribed literal, where it sits
    (location and character span), the stored text re-read at that span and whether it still
    equals the literal. ``traceable`` is true only when that holds for every input.
    """
    d = get(conn, derived_value_id)
    steps = []
    for i in d["inputs"]:
        f = conn.execute("SELECT id, paper_id, current_version, review_status, record_status FROM finding WHERE id = ?",
                         (i["finding_id"],)).fetchone()
        ver = conn.execute("SELECT snapshot_json, created_at, created_by, change_kind FROM finding_version WHERE"
                           " finding_id = ? AND version_no = ?", (i["finding_id"], i["version_no"])).fetchone()
        snap = loads(ver["snapshot_json"], {}) if ver else {}
        role, start, end = F._span_target((snap.get("spans") or {}).get(i["field"]))
        locs = [l for l in snap.get("locations") or [] if (l.get("role") or F.PRIMARY_ROLE) == role]
        loc = locs[0] if locs else None
        span = [start, end] if start is not None and end is not None else None
        step: dict[str, Any] = {
            "finding_id": i["finding_id"], "version_used": i["version_no"],
            "current_version": f["current_version"] if f else None,
            "uses_current_version": bool(f) and f["current_version"] == i["version_no"],
            "review_status_now": f["review_status"] if f else None, "field": i["field"],
            "source_literal": i["source_literal"], "unit": i.get("unit"), "operand": i.get("operand"),
            "source_wording": (snap.get("content") or {}).get("source_wording"),
            "location": loc, "address": locations.describe(loc) if loc else None, "literal_span": span,
            "stored_text_at_span": None, "literal_found_at_span": False, "excerpt_resolves": False, "problem": None}
        if f:
            paper = conn.execute("SELECT * FROM paper WHERE id = ?", (f["paper_id"],)).fetchone()
            step["paper_id"] = f["paper_id"]
            step["paper"] = papers.citation_label(paper)
        if loc is None:
            step["problem"] = "the finding version records no supporting source location"
        else:
            ok, _, problem = locations.resolve(conn, loc)
            step["excerpt_resolves"], step["problem"] = ok, problem
            text, problem = locations.unit_text(conn, loc)
            if text is not None and span is not None:
                step["stored_text_at_span"] = text[span[0]:span[1]]
                step["literal_found_at_span"] = step["stored_text_at_span"] == i["source_literal"]
                if not step["literal_found_at_span"]:
                    step["problem"] = "stored text at the literal's span differs from the transcribed literal"
            elif text is not None:
                step["problem"] = "no source span is recorded for this literal"
        steps.append(step)
    return {"derived_value": d, "steps": steps,
            "traceable": bool(steps) and all(s["excerpt_resolves"] and s["literal_found_at_span"] for s in steps),
            "all_inputs_current": all(s["uses_current_version"] for s in steps)}
