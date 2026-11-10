"""Compatibility assessment before comparison, comparison tables and independence counting.

Nothing here pools, averages or ranks values.

Independence
------------
Findings are not observations of equal weight. Two findings are treated as
*not independent* when they are assigned to the same experiment, or when a
reviewer confirmed a ``dependent`` or ``possible_duplicate`` relation between
them (directly or through a chain). Such findings form one *unit*. A finding
with no experiment and no confirmed relation is its own unit, but its
independence has not been assessed, so every count is reported as "at most N
independent units" together with the number of findings that were not assessed
and the number of distinct reports.

Compatibility
-------------
:func:`assess` compares the recorded fields of a set of findings and lists
what is equal, what differs and what is missing. A set is classified
``quantitative_candidate`` only when no blocking check fails; otherwise it is
``descriptive_only``. "Quantitative candidate" means the recorded fields do not
rule a quantitative comparison out. Whether and how to compare remains the
reviewer's decision.

Original terms are compared as written. Two different terms count as the same
only when a reviewer mapped both to one normalized term
(:mod:`geodesic.vocabulary`).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from . import audit, papers, query, vocabulary
from .db import transaction
from .errors import NotFoundError, ReviewError, ValidationError
from .export.tables import location_text
from .quantities import units
from .util import Actor, canonical_json, loads, new_id, utc_now

NUMERIC = ("exact", "approximate", "range", "bound")
ELIGIBILITY = ("not_assessed", "descriptive_only", "quantitative_candidate")
STATEMENT = ("A 'quantitative candidate' is a set whose recorded fields do not rule out a quantitative comparison. "
             "Whether and how to compare is the reviewer's decision; values are never pooled or averaged here.")
_ABSOLUTE = (None, "absolute")


# ------------------------------------------------------------------ independence
class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        parent = self.parent
        root = x
        while parent.setdefault(root, root) != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def independence_units(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``finding_id -> {"unit", "basis", "size"}`` for every active finding.

    ``basis`` is ``experiment`` (same experiment), ``dependency_relation``
    (confirmed dependent / duplicate relation) or ``not_assessed`` (no
    experiment, no confirmed relation: counted as its own unit).
    """
    uf = _UnionFind()
    ids = []
    for r in conn.execute("SELECT id, experiment_id FROM finding WHERE record_status = 'active'"):
        ids.append(r["id"])
        uf.find(r["id"])
        if r["experiment_id"]:
            uf.union(r["id"], "exp:" + r["experiment_id"])
    known = set(ids)
    for r in conn.execute("SELECT finding_a, finding_b FROM finding_relation WHERE status = 'confirmed' AND"
                          " relation_type IN ('dependent', 'possible_duplicate')"):
        if r["finding_a"] in known and r["finding_b"] in known:
            uf.union(r["finding_a"], r["finding_b"])
    members: dict[str, list[str]] = {}
    for node in list(uf.parent):
        members.setdefault(uf.find(node), []).append(node)
    out: dict[str, dict[str, Any]] = {}
    for nodes in members.values():
        exps = sorted(n for n in nodes if n.startswith("exp:"))
        fids = sorted(n for n in nodes if not n.startswith("exp:"))
        if exps:
            unit, basis = "+".join(exps), "experiment"
        elif len(fids) > 1:
            unit, basis = "dep:" + fids[0], "dependency_relation"
        else:
            unit, basis = "fnd:" + fids[0], "not_assessed"
        for fid in fids:
            out[fid] = {"unit": unit, "basis": basis, "size": len(fids)}
    return out


def count_units(unit_of: dict[str, dict[str, Any]], finding_ids: Iterable[str]) -> dict[str, Any]:
    """Counts for a set of findings: findings, units (at most independent) and how many were not assessed."""
    fids = [f for f in finding_ids if f in unit_of]
    seen: dict[str, str] = {}
    for f in fids:
        seen[unit_of[f]["unit"]] = unit_of[f]["basis"]
    by_basis = {"experiment": 0, "dependency_relation": 0, "not_assessed": 0}
    for basis in seen.values():
        by_basis[basis] += 1
    return {"findings": len(fids), "independent_units_at_most": len(seen), "units_by_basis": by_basis,
            "findings_independence_not_assessed": by_basis["not_assessed"]}


# ------------------------------------------------------------------ loading
def _load(conn: sqlite3.Connection, finding_ids: list[str]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(finding_ids))
    rows: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(
                "SELECT f.*, p.citation_text AS paper_citation, p.title AS paper_title, p.year AS paper_year,"
                " p.authors_json AS paper_authors_json, COALESCE(p.canonical_id, p.id) AS report_id"
                f" FROM finding f JOIN paper p ON p.id = f.paper_id WHERE f.id IN ({','.join('?' * len(part))})", part):
            rows[r["id"]] = dict(r)
    missing = [i for i in ids if i not in rows]
    if missing:
        raise NotFoundError(f"Unknown finding(s): {', '.join(missing[:5])}")
    out = [rows[i] for i in ids]
    query._decorate_findings(conn, out, with_locations=True)
    return out


MAX_SET = 500


def select_findings(conn: sqlite3.Connection, finding_ids: list[str] | None = None,
                    flt: query.FindingFilter | None = None) -> list[str]:
    """The findings of a comparison: the IDs named, else the findings matching a (non-empty) filter."""
    if finding_ids:
        return list(dict.fromkeys(finding_ids))
    if flt is None or flt == query.FindingFilter():
        raise ValidationError("Name the findings by ID, or select them with a filter (e.g. outcome, stage).")
    total = query.search_findings(conn, flt, limit=1).total
    if total > MAX_SET:
        raise ValidationError(f"The filter matches {total} findings; a comparison set holds at most {MAX_SET}. "
                              "Narrow the filter.")
    return [f["id"] for f in query.iter_findings(conn, flt, with_locations=False)]


def _citation(f: dict[str, Any]) -> str:
    return papers.citation_label({"authors": loads(f.get("paper_authors_json"), []), "year": f.get("paper_year"),
                                  "citation_text": f.get("paper_citation"), "title": f.get("paper_title"),
                                  "id": f["paper_id"]})


def _stated(f: dict[str, Any]) -> str:
    if f["statement_type"] not in NUMERIC:
        return f"({f['statement_type']})"
    text = f["value_literal"] or ""
    if f["uncertainty_literal"]:
        text += f" ± {f['uncertainty_literal']}" if f["uncertainty_type"] in ("sd", "se", "unspecified_plus_minus") \
            else f" ({f['uncertainty_type']}: {f['uncertainty_literal']})"
    return f"{text} {f['unit_original'] or ''}".strip()


# ------------------------------------------------------------------ finding-level eligibility
def eligibility_blockers(f: dict[str, Any]) -> list[str]:
    """Reasons a single finding cannot be marked a quantitative-comparison candidate (empty list: none)."""
    out = []
    if f["record_status"] != "active":
        out.append(f"record is {f['record_status']}")
    if f["kind"] != "measurement" or f["statement_type"] not in NUMERIC:
        out.append(f"no numeric statement ({f['kind']}/{f['statement_type']})")
    if f["grounding_status"] != "grounded":
        out.append("unsupported (its source does not resolve)")
    if f["review_status"] != "verified":
        out.append(f"not verified against its source (review status: {f['review_status']})")
    status, unit = units.lookup(f["unit_original"])
    if status == "missing":
        out.append("no unit transcribed")
    relative = f["relative_semantics"] not in _ABSOLUTE or \
        (unit is not None and unit.dimension in units.RELATIVE_DIMENSIONS)
    if relative and not f["reference_definition"]:
        out.append("relative quantity without a recorded reference definition")
    if not f["outcome"]:
        out.append("no outcome recorded")
    return out


def set_eligibility(conn: sqlite3.Connection, finding_ids: list[str], value: str, actor: Actor,
                    note: str | None = None) -> dict[str, Any]:
    """Record a reviewer's judgement of suitability for quantitative comparison on single findings."""
    if value not in ELIGIBILITY:
        raise ValidationError(f"value must be one of {list(ELIGIBILITY)}")
    _reviewer(conn, actor)
    rows = _load(conn, finding_ids)
    if value == "quantitative_candidate":
        blocked = {f["id"]: b for f in rows if (b := eligibility_blockers(f))}
        if blocked:
            first = next(iter(blocked.items()))
            raise ValidationError(
                f"{len(blocked)} finding(s) cannot be marked quantitative candidates; e.g. {first[0]}: "
                f"{'; '.join(first[1])}.")
    now = utc_now()
    with transaction(conn):
        for f in rows:
            conn.execute("UPDATE finding SET comparison_eligibility = ?, updated_at = ? WHERE id = ?",
                         (value, now, f["id"]))
            audit.record(conn, actor, "finding.comparison_eligibility", "finding", f["id"],
                         {"before": f["comparison_eligibility"], "after": value, "version_no": f["current_version"],
                          "note": note})
    return {"findings": len(rows), "value": value}


def _reviewer(conn: sqlite3.Connection, actor: Actor) -> None:
    if actor.kind not in ("human", "simulated"):
        raise ReviewError("Comparison judgements need a named reviewer, not an automated component.")
    if actor.kind == "simulated" and \
            conn.execute("SELECT data_class FROM project").fetchone()["data_class"] != "synthetic":
        raise ReviewError("Simulated review actions are only permitted in synthetic projects.")


# ------------------------------------------------------------------ set-level assessment
def _check(checks: list[dict], key: str, level: str, status: str, detail: str, values: Any = None) -> None:
    checks.append({"check": key, "level": level, "status": status, "detail": detail, "values": values})


def _distinct(rows: list[dict], field: str, mapped: dict[str, str] | None = None) -> tuple[dict[str, list[str]], int, bool]:
    """Distinct recorded values -> original terms, number of findings without a value, mapping used."""
    groups: dict[str, list[str]] = {}
    missing, used_mapping = 0, False
    for f in rows:
        label, from_map = vocabulary.group_label(mapped or {}, f.get(field))
        if label is None:
            missing += 1
            continue
        used_mapping = used_mapping or from_map
        if f[field] not in groups.setdefault(label, []):
            groups[label].append(f[field])
    return groups, missing, used_mapping


def assess(conn: sqlite3.Connection, finding_ids: list[str]) -> dict[str, Any]:
    """Compatibility assessment of a set of findings. Stores nothing."""
    rows = _load(conn, finding_ids)
    n = len(rows)
    vocab = vocabulary.mapping(conn)
    checks: list[dict] = []
    if n < 2:
        _check(checks, "set_size", "blocking", "fails", "A comparison needs at least two findings.")
    bad = [f["id"] for f in rows if f["record_status"] != "active" or f["review_status"] == "rejected"]
    _check(checks, "record_state", "blocking", "fails" if bad else "ok",
           f"{len(bad)} finding(s) are rejected or no longer active." if bad else "All findings are active.", bad)
    bad = [f["id"] for f in rows if f["grounding_status"] != "grounded"]
    _check(checks, "grounding", "blocking", "fails" if bad else "ok",
           f"{len(bad)} finding(s) are unsupported (their source does not resolve)." if bad
           else "Every finding resolves to its stored source text.", bad)
    bad = [f["id"] for f in rows if f["review_status"] != "verified"]
    _check(checks, "verification", "blocking", "fails" if bad else "ok",
           f"{len(bad)} of {n} finding(s) are not verified against their source by a reviewer." if bad
           else "Every finding was verified against its source by a reviewer.", bad)
    bad = [f["id"] for f in rows if f["kind"] != "measurement" or f["statement_type"] not in NUMERIC]
    _check(checks, "numeric_statement", "blocking", "fails" if bad else "ok",
           f"{len(bad)} finding(s) state no numeric value." if bad else "Every finding states a numeric value.", bad)
    forms = sorted({f["statement_type"] for f in rows if f["statement_type"] in NUMERIC})
    if len(forms) > 1 or (forms and forms[0] in ("range", "bound")):
        _check(checks, "statement_form", "caution", "differs",
               f"Statement forms: {', '.join(forms)}. Ranges and bounds are shown as stated; they are not reduced "
               "to a single number.", forms)
    # outcome
    groups, missing, used = _distinct(rows, "outcome", vocab.get("outcome"))
    if missing:
        _check(checks, "outcome", "blocking", "missing", f"{missing} finding(s) have no outcome recorded.")
    elif len(groups) > 1:
        _check(checks, "outcome", "blocking", "differs",
               f"{len(groups)} different outcome terms (terms are compared as written; map them in the vocabulary "
               "only if they mean the same thing).", groups)
    else:
        _check(checks, "outcome", "blocking", "ok", "Same outcome term"
               + (" through a reviewer's vocabulary mapping." if used and len(next(iter(groups.values()))) > 1
                  else "."), groups)
    # units and relative quantities
    _unit_checks(rows, checks)
    # descriptive context
    for field, level_when_differs in (("observation_type", "blocking"), ("denominator", "blocking"),
                                      ("normalization", "blocking"), ("system_context", "caution"),
                                      ("experimental_context", "caution"), ("measurement_method", "caution"),
                                      ("intervention", "caution"), ("comparator", "caution")):
        groups, missing, used = _distinct(rows, field, vocab.get(field))
        if len(groups) > 1:
            _check(checks, field, level_when_differs, "differs",
                   f"{len(groups)} different recorded values" + (f"; {missing} finding(s) without one." if missing else "."),
                   groups)
        elif missing and groups:
            _check(checks, field, "caution", "missing", f"Recorded on {n - missing} of {n} finding(s) only.", groups)
        elif missing:
            _check(checks, field, "info", "missing", "Not recorded on any finding of the set.")
        else:
            _check(checks, field, "info", "ok", "Same recorded value on every finding.", groups)
    # timepoints
    with_t = [f for f in rows if f["timepoint_literal"]]
    tps = sorted({f"{f['timepoint_literal']}" for f in with_t})
    if with_t and len(with_t) < n:
        _check(checks, "timepoint", "caution", "missing", f"A timepoint is recorded on {len(with_t)} of {n} finding(s).", tps)
    elif len(tps) > 1:
        _check(checks, "timepoint", "caution", "differs", f"{len(tps)} different timepoints as written.", tps)
    else:
        _check(checks, "timepoint", "info", "ok" if tps else "missing",
               "Same timepoint as written." if tps else "No timepoint recorded on any finding.", tps)
    # uncertainty and sample size (reported, never required)
    unc = sorted({f["uncertainty_type"] or "none recorded" for f in rows})
    _check(checks, "uncertainty", "caution" if len(unc) > 1 or "unspecified_plus_minus" in unc else "info",
           "differs" if len(unc) > 1 else "ok",
           f"Uncertainty types: {', '.join(unc)}. Different types are not interchangeable.", unc)
    no_n = sum(1 for f in rows if f["sample_size"] is None)
    _check(checks, "sample_size", "caution" if no_n else "info", "missing" if no_n else "ok",
           f"Sample size recorded on {n - no_n} of {n} finding(s).")
    # independence
    unit_of = independence_units(conn)
    ind = count_units(unit_of, [f["id"] for f in rows])
    ind["reports"] = len({f["report_id"] for f in rows})
    ind["units"] = {f["id"]: unit_of.get(f["id"], {}).get("unit") for f in rows}
    dependent = ind["independent_units_at_most"] < ind["findings"]
    _check(checks, "independence", "caution" if dependent or ind["findings_independence_not_assessed"] else "info",
           "differs" if dependent else "ok",
           f"{ind['findings']} finding(s) form at most {ind['independent_units_at_most']} independent unit(s) from "
           f"{ind['reports']} report(s); independence was not assessed for "
           f"{ind['findings_independence_not_assessed']} finding(s)."
           + (" Findings of one unit must not be counted as independent observations." if dependent else ""))
    blocking = [c["check"] for c in checks if c["level"] == "blocking" and c["status"] != "ok"]
    return {"finding_ids": [f["id"] for f in rows],
            "finding_versions": {f["id"]: f["current_version"] for f in rows},
            "classification": "descriptive_only" if blocking else "quantitative_candidate",
            "blocking": blocking, "cautions": [c["check"] for c in checks if c["level"] == "caution"],
            "checks": checks, "independence": ind, "statement": STATEMENT}


def _unit_checks(rows: list[dict], checks: list[dict]) -> None:
    numeric = [f for f in rows if f["statement_type"] in NUMERIC]
    written = sorted({f["unit_original"] or "(none)" for f in numeric})
    no_unit = [f["id"] for f in numeric if not f["unit_original"]]
    if no_unit:
        _check(checks, "unit", "blocking", "missing", f"{len(no_unit)} finding(s) have no unit transcribed.", written)
        return
    relative = []
    for f in numeric:
        status, unit = units.lookup(f["unit_original"])
        if f["relative_semantics"] not in _ABSOLUTE or (unit is not None and unit.dimension in units.RELATIVE_DIMENSIONS):
            relative.append(f)
    if relative:
        sems = sorted({f["relative_semantics"] or "(not recorded)" for f in numeric})
        refs = sorted({f["reference_definition"] or "(not recorded)" for f in numeric})
        same = len(relative) == len(numeric) and len(written) == 1 and len(sems) == 1 and len(refs) == 1 \
            and "(not recorded)" not in sems + refs
        _check(checks, "unit", "blocking", "ok" if same else "differs",
               "Relative quantities with the same unit, the same relative meaning and the same stated reference."
               if same else
               "Relative quantities (percent, fold, arbitrary units ...) are comparable only when unit, relative "
               "meaning and stated reference are identical and recorded on every finding; they are never converted.",
               {"units": written, "relative_semantics": sems, "reference_definitions": refs})
        return
    if len(written) == 1:
        _check(checks, "unit", "blocking", "ok", f"Same unit as written ({written[0]}).", written)
        return
    target = numeric[0]["unit_original"]
    problems = []
    for u in written:
        try:
            units.conversion(u, target)
        except units.ConversionError as exc:
            problems.append(f"{u}: {exc}")
    if problems:
        _check(checks, "unit", "blocking", "differs",
               "Units cannot all be converted to one another: " + "; ".join(problems), written)
    else:
        _check(checks, "unit", "blocking", "ok",
               f"Units differ as written ({', '.join(written)}) but share a dimension; values can be shown in one "
               "unit with a factor from the unit table.", written)


# ------------------------------------------------------------------ comparison table
TABLE_COLUMNS = ["finding_id", "citation", "independence_unit", "independence_basis", "stages", "outcome",
                 "system_context", "experimental_context", "measurement_method", "statement_type", "as_stated",
                 "value_num", "value_low", "value_high", "bound_type", "unit_original", "relative_semantics",
                 "reference_definition", "display_value", "display_low", "display_high", "display_unit",
                 "display_rule", "uncertainty_type", "uncertainty_literal", "sample_size", "timepoint_literal",
                 "review_status", "comparison_eligibility", "source_location", "source_wording"]


def table(conn: sqlite3.Connection, finding_ids: list[str], target_unit: str | None = None) -> dict[str, Any]:
    """Side-by-side rows for a set of findings. One row per finding; no summary row is ever added.

    ``display_*`` columns hold the value in one common unit where the unit table allows it (the rule
    is named in ``display_rule``); they are computed for display and stored nowhere. Where a value
    cannot be converted the columns stay empty and ``display_rule`` says why.
    """
    rows = _load(conn, finding_ids)
    unit_of = independence_units(conn)
    numeric = [f for f in rows if f["statement_type"] in NUMERIC and f["unit_original"]]
    target = target_unit or (numeric[0]["unit_original"] if numeric else None)
    out = []
    for f in rows:
        primary = next((l for l in f.get("locations", []) if l["role"] == "supporting_excerpt"), None)
        row = {c: f.get(c) for c in TABLE_COLUMNS}
        row.update(finding_id=f["id"], citation=_citation(f), stages="; ".join(f.get("stages", [])),
                   independence_unit=unit_of.get(f["id"], {}).get("unit"),
                   independence_basis=unit_of.get(f["id"], {}).get("basis"), as_stated=_stated(f),
                   source_location=location_text(primary) if primary else "",
                   display_value=None, display_low=None, display_high=None, display_unit=None, display_rule=None)
        if f["statement_type"] in NUMERIC and f["unit_original"] and target:
            factor = rule = None
            if f["unit_original"] == target:
                factor, rule = 1.0, "identical_unit"
            elif f["relative_semantics"] not in _ABSOLUTE:
                rule = "not converted: relative quantity"
            else:
                try:
                    factor, rule = units.conversion(f["unit_original"], target)
                except units.ConversionError as exc:
                    rule = f"not converted: {exc}"
            if factor is not None:
                row.update(display_unit=target, display_rule=rule,
                           **{f"display_{k}": (None if f[src] is None else float(f"{f[src] * factor:.12g}"))
                              for k, src in (("value", "value_num"), ("low", "value_low"), ("high", "value_high"))})
            else:
                row["display_rule"] = rule
        out.append(row)
    return {"columns": TABLE_COLUMNS, "rows": out, "display_unit": target,
            "note": "One row per finding, values as stated. display_* columns are unit conversions for reading "
                    "side by side; nothing is pooled, averaged or ranked."}


# ------------------------------------------------------------------ saved comparison sets
def save_set(conn: sqlite3.Connection, name: str, finding_ids: list[str], actor: Actor, *,
             classification: str | None = None, description: str | None = None) -> dict[str, Any]:
    """Store a comparison set with its assessment as it stands now (the stored record is never changed)."""
    _reviewer(conn, actor)
    if not name or not name.strip():
        raise ValidationError("A comparison set needs a name.")
    a = assess(conn, finding_ids)
    if len(a["finding_ids"]) < 2:
        raise ValidationError("A comparison set needs at least two findings.")
    chosen = classification or a["classification"]
    if chosen not in ("descriptive_only", "quantitative_candidate"):
        raise ValidationError("classification must be descriptive_only or quantitative_candidate")
    if chosen == "quantitative_candidate" and a["blocking"]:
        raise ValidationError("This set cannot be classified as a quantitative candidate; blocking checks: "
                              + ", ".join(a["blocking"]) + ". It can be saved as descriptive_only.")
    sid, now = new_id("cmp"), utc_now()
    a["assessed_classification"] = a["classification"]
    with transaction(conn):
        conn.execute("INSERT INTO comparison_set (id, name, description, finding_ids_json, assessment_json,"
                     " classification, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
                     (sid, name.strip(), description, canonical_json(a["finding_ids"]), canonical_json(a), chosen,
                      now, actor.name))
        audit.record(conn, actor, "comparison_set.create", "comparison_set", sid, {
            "name": name.strip(), "classification": chosen, "assessed_classification": a["classification"],
            "blocking": a["blocking"], "finding_versions": a["finding_versions"]})
    return get_set(conn, sid)


def get_set(conn: sqlite3.Connection, set_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM comparison_set WHERE id = ?", (set_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown comparison set {set_id}")
    out = dict(row)
    out["finding_ids"] = loads(out.pop("finding_ids_json"), [])
    out["assessment"] = loads(out.pop("assessment_json"), {})
    versions = out["assessment"].get("finding_versions", {})
    changed = []
    for i in range(0, len(out["finding_ids"]), 500):
        part = out["finding_ids"][i:i + 500]
        for r in conn.execute("SELECT id, current_version, record_status, review_status FROM finding WHERE id IN"
                              f" ({','.join('?' * len(part))})", part):
            if r["current_version"] != versions.get(r["id"]) or r["record_status"] != "active" or \
                    r["review_status"] in ("rejected", "stale"):
                changed.append(r["id"])
    out["changed_since_assessment"] = sorted(changed)
    out["is_current"] = not changed
    return out


def list_sets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [get_set(conn, r["id"]) for r in conn.execute("SELECT id FROM comparison_set ORDER BY created_at, id")]
