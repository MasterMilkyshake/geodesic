"""Search and filtering over papers and findings (database queries with pagination).

The dashboard, the CLI and the exports all call these functions. Filters are
combined with AND; list-valued filters match any of their values. Nothing here
ranks evidence by quality: ordering is by a stable, stated key.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import ValidationError
from .util import loads

QUANTITATIVE_TYPES = ("exact", "approximate", "range", "bound")

#: Per paper and screening stage: the adjudicated decision when one exists, else the common decision of the
#: current independent decisions, else ``conflict``. ``finality`` is ``provisional`` if any contributing
#: decision is provisional.
SCREENING_STATUS_SQL = """
SELECT paper_id, stage,
       CASE WHEN SUM(decision_role = 'adjudication') > 0
                THEN MAX(CASE WHEN decision_role = 'adjudication' THEN decision END)
            WHEN COUNT(DISTINCT decision) = 1 THEN MIN(decision)
            ELSE 'conflict' END AS outcome,
       MAX(finality) AS finality,
       COUNT(*) AS n_decisions,
       SUM(decider_kind = 'human') AS n_human,
       SUM(decider_kind != 'human') AS n_non_human,
       MAX(needs_rereview) AS needs_rereview
FROM screening_decision WHERE is_current = 1 GROUP BY paper_id, stage
"""


@dataclass
class Page:
    """One page of results plus the total number of matching records."""

    rows: list[dict[str, Any]]
    total: int
    limit: int | None
    offset: int

    @property
    def has_more(self) -> bool:
        return self.limit is not None and self.offset + len(self.rows) < self.total


@dataclass
class FindingFilter:
    """Filters for findings. ``None`` means "do not filter on this"."""

    text: str | None = None                     # full-text query over wording and context fields
    stage_keys: list[str] | None = None         # any of these stages (active assignments)
    unassigned_stage: bool | None = None        # True: only findings without an active stage
    paper_ids: list[str] | None = None
    paper_text: str | None = None               # full-text query over paper title/citation/authors
    year_min: int | None = None
    year_max: int | None = None
    kinds: list[str] | None = None              # measurement | narrative | limitation
    statement_types: list[str] | None = None
    quantitative: bool | None = None            # True: exact/approximate/range/bound; False: the rest
    observation_types: list[str] | None = None  # direct | proxy | functional | ...
    system_context: str | None = None           # substring
    experimental_context: str | None = None     # substring
    outcome: str | None = None                  # substring
    measurement_method: str | None = None       # substring
    units: list[str] | None = None
    relative_semantics: list[str] | None = None
    has_timepoint: bool | None = None
    has_uncertainty: bool | None = None
    has_sample_size: bool | None = None
    review_statuses: list[str] | None = None
    grounding_statuses: list[str] | None = None
    record_statuses: list[str] | None = field(default_factory=lambda: ["active"])
    flags: list[str] | None = None              # any of these review flags
    origins: list[str] | None = None
    source_scopes: list[str] | None = None
    experiment_id: str | None = None
    study_id: str | None = None
    hierarchy_unassigned: bool | None = None    # True: no experiment assigned
    screening_stage: str | None = None          # with screening_outcomes
    screening_outcomes: list[str] | None = None  # include | exclude | uncertain | conflict | unscreened
    comparison_eligibility: list[str] | None = None


@dataclass
class PaperFilter:
    """Filters for papers (reports)."""

    text: str | None = None
    stage_keys: list[str] | None = None
    year_min: int | None = None
    year_max: int | None = None
    resolution_statuses: list[str] | None = None
    identity_statuses: list[str] | None = None
    publication_types: list[str] | None = None
    publication_statuses: list[str] | None = None
    fulltext: list[str] | None = None
    origins: list[str] | None = None
    record_statuses: list[str] | None = field(default_factory=lambda: ["active"])
    canonical_only: bool = True                 # hide records grouped under another canonical record
    has_findings: bool | None = None
    has_document: bool | None = None
    search_run_id: str | None = None
    screening_stage: str | None = None
    screening_outcomes: list[str] | None = None
    exclusion_criterion_ids: list[str] | None = None
    exclusion_reason_kinds: list[str] | None = None
    needs_rereview: bool | None = None
    has_open_conflicts: bool | None = None


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word must match; a trailing ``*`` is a prefix search."""
    terms = []
    for tok in text.split():
        prefix = tok.endswith("*")
        tok = tok.rstrip("*").replace('"', '""')
        if tok:
            terms.append(f'"{tok}"' + ("*" if prefix else ""))
    if not terms:
        raise ValidationError("The search text contains no searchable words.")
    return " ".join(terms)


class _Where:
    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, *params: Any) -> None:
        self.clauses.append(clause)
        self.params.extend(params)

    def any_of(self, column: str, values: Iterable[Any] | None) -> None:
        values = list(values) if values is not None else None
        if values is None:
            return
        if not values:
            self.add("0")
            return
        self.add(f"{column} IN ({','.join('?' * len(values))})", *values)

    def like(self, column: str, needle: str | None) -> None:
        if needle:
            esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            self.add(f"{column} LIKE ? ESCAPE '\\'", f"%{esc}%")

    def flag(self, expr: str, wanted: bool | None) -> None:
        if wanted is not None:
            self.add(expr if wanted else f"NOT ({expr})")

    def screening(self, paper_col: str, stage: str | None, outcomes: list[str] | None) -> None:
        if not outcomes:
            return
        stage_sql = " AND ss.stage = ?" if stage else ""
        stage_p = [stage] if stage else []
        real = [o for o in outcomes if o != "unscreened"]
        parts = []
        params: list[Any] = []
        if real:
            parts.append(f"EXISTS (SELECT 1 FROM ({SCREENING_STATUS_SQL}) ss WHERE ss.paper_id = {paper_col}"
                         f"{stage_sql} AND ss.outcome IN ({','.join('?' * len(real))}))")
            params += [*stage_p, *real]
        if "unscreened" in outcomes:
            parts.append(f"NOT EXISTS (SELECT 1 FROM screening_decision ss WHERE ss.paper_id = {paper_col}"
                         f" AND ss.is_current = 1{stage_sql})")
            params += stage_p
        self.add("(" + " OR ".join(parts) + ")", *params)

    def sql(self) -> str:
        return (" WHERE " + " AND ".join(self.clauses)) if self.clauses else ""


#: paper records retrieved by one search run, directly or through a record grouped under them
IN_SEARCH_RUN_SQL = ("(p.id IN (SELECT sr.paper_id FROM search_result sr WHERE sr.search_run_id = ?) OR p.id IN"
                     " (SELECT m.canonical_id FROM search_result sr JOIN paper m ON m.id = sr.paper_id WHERE"
                     " sr.search_run_id = ? AND m.canonical_id IS NOT NULL))")

FINDING_ORDERS = {
    "paper": "p.year IS NULL, p.year, p.citation_text, f.pk",
    "newest": "f.pk DESC",
    "oldest": "f.pk",
    "review_status": "f.review_status, f.pk",
    "outcome": "f.outcome IS NULL, f.outcome, f.pk",
}
PAPER_ORDERS = {
    "year": "p.year IS NULL, p.year, p.citation_text, p.pk",
    "year_desc": "p.year IS NULL, p.year DESC, p.citation_text, p.pk",
    "title": "coalesce(p.title, p.citation_text), p.pk",
    "newest": "p.pk DESC",
}


def _finding_where(flt: FindingFilter) -> _Where:
    w = _Where()
    if flt.text:
        w.add("f.pk IN (SELECT rowid FROM finding_fts WHERE finding_fts MATCH ?)", fts_query(flt.text))
    if flt.paper_text:
        w.add("p.pk IN (SELECT rowid FROM paper_fts WHERE paper_fts MATCH ?)", fts_query(flt.paper_text))
    if flt.stage_keys is not None:
        marks = ",".join("?" * len(flt.stage_keys)) or "NULL"
        # uncorrelated subqueries: evaluated once, not once per finding (matters at 100,000 findings)
        w.add("f.id IN (SELECT fs.finding_id FROM finding_stage fs JOIN stage s ON s.id = fs.stage_id WHERE"
              f" fs.status = 'active' AND s.key IN ({marks}))", *flt.stage_keys)
    w.flag("f.id NOT IN (SELECT fs.finding_id FROM finding_stage fs WHERE fs.status = 'active')",
           flt.unassigned_stage)
    w.any_of("f.paper_id", flt.paper_ids)
    if flt.year_min is not None:
        w.add("p.year >= ?", flt.year_min)
    if flt.year_max is not None:
        w.add("p.year <= ?", flt.year_max)
    w.any_of("f.kind", flt.kinds)
    w.any_of("f.statement_type", flt.statement_types)
    w.flag("f.statement_type IN ('exact','approximate','range','bound')", flt.quantitative)
    w.any_of("f.observation_type", flt.observation_types)
    w.like("f.system_context", flt.system_context)
    w.like("f.experimental_context", flt.experimental_context)
    w.like("f.outcome", flt.outcome)
    w.like("f.measurement_method", flt.measurement_method)
    w.any_of("f.unit_original", flt.units)
    w.any_of("f.relative_semantics", flt.relative_semantics)
    w.flag("f.timepoint_literal IS NOT NULL", flt.has_timepoint)
    w.flag("f.uncertainty_type IS NOT NULL", flt.has_uncertainty)
    w.flag("f.sample_size IS NOT NULL", flt.has_sample_size)
    w.any_of("f.review_status", flt.review_statuses)
    w.any_of("f.grounding_status", flt.grounding_statuses)
    w.any_of("f.record_status", flt.record_statuses)
    w.any_of("f.origin", flt.origins)
    w.any_of("f.source_scope", flt.source_scopes)
    w.any_of("f.comparison_eligibility", flt.comparison_eligibility)
    if flt.flags:
        w.add("EXISTS (SELECT 1 FROM json_each(f.review_flags_json) j WHERE j.value IN (%s))"
              % ",".join("?" * len(flt.flags)), *flt.flags)
    if flt.experiment_id:
        w.add("f.experiment_id = ?", flt.experiment_id)
    if flt.study_id:
        w.add("f.study_id = ?", flt.study_id)
    w.flag("f.experiment_id IS NULL", flt.hierarchy_unassigned)
    w.screening("f.paper_id", flt.screening_stage, flt.screening_outcomes)
    return w


def _decorate_findings(conn: sqlite3.Connection, rows: list[dict[str, Any]], with_locations: bool) -> None:
    if not rows:
        return
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["qualifiers"] = loads(r.pop("qualifiers_json", None), [])
        r["review_flags"] = loads(r.pop("review_flags_json", None), [])
        r["field_status"] = loads(r.pop("field_status_json", None), {})
        r["spans"] = loads(r.pop("spans_json", None), {})
        r["stages"] = []
        r["stage_sources"] = {}
        if with_locations:
            r["locations"] = []
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for s in conn.execute(
                "SELECT fs.finding_id, s.key, fs.assignment_source FROM finding_stage fs JOIN stage s ON"
                f" s.id = fs.stage_id WHERE fs.status = 'active' AND fs.finding_id IN ({marks}) ORDER BY s.position",
                part):
            r = by_id[s["finding_id"]]
            if s["key"] not in r["stages"]:
                r["stages"].append(s["key"])
            r["stage_sources"].setdefault(s["key"], []).append(s["assignment_source"])
        if with_locations:
            for l in conn.execute(
                    "SELECT l.*, wv.version_no AS workbook_version_no, wv.workbook_key AS loc_workbook_key,"
                    " ds.document_id, ds.snapshot_no FROM source_location l"
                    " LEFT JOIN workbook_version wv ON wv.id = l.workbook_version_id"
                    " LEFT JOIN document_snapshot ds ON ds.id = l.snapshot_id"
                    f" WHERE l.owner_type = 'finding' AND l.is_current = 1 AND l.owner_id IN ({marks})"
                    " ORDER BY (l.role != 'supporting_excerpt'), l.role, l.created_at", part):
                by_id[l["owner_id"]]["locations"].append(dict(l))


_FINDING_SELECT = ("SELECT f.*, p.citation_text AS paper_citation, p.title AS paper_title, p.year AS paper_year,"
                   " p.doi AS paper_doi, p.authors_json AS paper_authors_json,"
                   " p.resolution_status AS paper_resolution_status, p.identity_status AS paper_identity_status")


def _finding_query(flt: FindingFilter | None, order: str) -> tuple[str, _Where]:
    if order not in FINDING_ORDERS:
        raise ValidationError(f"order must be one of {sorted(FINDING_ORDERS)}")
    w = _finding_where(flt or FindingFilter())
    return " FROM finding f JOIN paper p ON p.id = f.paper_id" + w.sql(), w


def search_findings(conn: sqlite3.Connection, flt: FindingFilter | None = None, *, limit: int | None = 50,
                    offset: int = 0, order: str = "paper", with_locations: bool = False) -> Page:
    """Findings matching ``flt``, one page at a time, each with its paper citation and stages."""
    base, w = _finding_query(flt, order)
    total = conn.execute("SELECT COUNT(*)" + base, w.params).fetchone()[0]
    sql = _FINDING_SELECT + base + f" ORDER BY {FINDING_ORDERS[order]}"
    params = list(w.params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    rows = [dict(r) for r in conn.execute(sql, params)]
    _decorate_findings(conn, rows, with_locations)
    return Page(rows, total, limit, offset)


def iter_findings(conn: sqlite3.Connection, flt: FindingFilter | None = None, *, order: str = "paper",
                  with_locations: bool = True, batch: int = 2000):
    """Yield every finding matching ``flt`` in the given order (for exports).

    The ordered query runs once; rows are read and decorated ``batch`` at a
    time, so memory stays bounded and nothing is sorted twice.
    """
    base, w = _finding_query(flt, order)
    cur = conn.cursor()
    try:
        cur.execute(_FINDING_SELECT + base + f" ORDER BY {FINDING_ORDERS[order]}", w.params)
        while True:
            rows = [dict(r) for r in cur.fetchmany(batch)]
            if not rows:
                return
            _decorate_findings(conn, rows, with_locations)
            yield from rows
    finally:
        cur.close()


def _paper_where(flt: PaperFilter) -> _Where:
    w = _Where()
    if flt.text:
        w.add("p.pk IN (SELECT rowid FROM paper_fts WHERE paper_fts MATCH ?)", fts_query(flt.text))
    if flt.stage_keys is not None:
        marks = ",".join("?" * len(flt.stage_keys)) or "NULL"
        w.add("p.id IN (SELECT ps.paper_id FROM paper_stage ps JOIN stage s ON s.id = ps.stage_id WHERE"
              f" ps.status = 'active' AND s.key IN ({marks}))", *flt.stage_keys)
    if flt.year_min is not None:
        w.add("p.year >= ?", flt.year_min)
    if flt.year_max is not None:
        w.add("p.year <= ?", flt.year_max)
    w.any_of("p.resolution_status", flt.resolution_statuses)
    w.any_of("p.identity_status", flt.identity_statuses)
    w.any_of("p.publication_type", flt.publication_types)
    w.any_of("p.publication_status", flt.publication_statuses)
    w.any_of("p.fulltext_availability", flt.fulltext)
    w.any_of("p.origin", flt.origins)
    w.any_of("p.record_status", flt.record_statuses)
    if flt.canonical_only:
        w.add("p.canonical_id IS NULL")
    # the unary + keeps the planner on the paper_id index rather than the unselective record_status one
    w.flag("EXISTS (SELECT 1 FROM finding f WHERE f.paper_id = p.id AND +f.record_status = 'active')",
           flt.has_findings)
    w.flag("EXISTS (SELECT 1 FROM document d WHERE d.paper_id = p.id AND d.record_status = 'active')",
           flt.has_document)
    if flt.search_run_id:
        w.add(IN_SEARCH_RUN_SQL, flt.search_run_id, flt.search_run_id)
    w.screening("p.id", flt.screening_stage, flt.screening_outcomes)
    if flt.exclusion_criterion_ids or flt.exclusion_reason_kinds:
        sub = _Where()
        sub.add("sd.paper_id = p.id")
        sub.add("sd.is_current = 1")
        sub.add("sd.decision = 'exclude'")
        if flt.screening_stage:
            sub.add("sd.stage = ?", flt.screening_stage)
        sub.any_of("sd.criterion_id", flt.exclusion_criterion_ids or None)
        sub.any_of("sd.reason_kind", flt.exclusion_reason_kinds or None)
        w.add("EXISTS (SELECT 1 FROM screening_decision sd" + sub.sql() + ")", *sub.params)
    w.flag("EXISTS (SELECT 1 FROM screening_decision sd WHERE sd.paper_id = p.id AND sd.is_current = 1 AND"
           " sd.needs_rereview = 1)", flt.needs_rereview)
    w.flag("EXISTS (SELECT 1 FROM metadata_conflict mc WHERE mc.paper_id = p.id AND mc.status = 'open')",
           flt.has_open_conflicts)
    return w


def search_papers(conn: sqlite3.Connection, flt: PaperFilter | None = None, *, limit: int | None = 50,
                  offset: int = 0, order: str = "year") -> Page:
    """Papers matching ``flt``, with finding counts, stages and current screening outcomes."""
    flt = flt or PaperFilter()
    if order not in PAPER_ORDERS:
        raise ValidationError(f"order must be one of {sorted(PAPER_ORDERS)}")
    w = _paper_where(flt)
    base = " FROM paper p" + w.sql()
    total = conn.execute("SELECT COUNT(*)" + base, w.params).fetchone()[0]
    sql = ("SELECT p.*, (SELECT COUNT(*) FROM finding f WHERE f.paper_id = p.id AND +f.record_status = 'active')"
           " AS n_findings, (SELECT COUNT(*) FROM document d WHERE d.paper_id = p.id AND"
           " d.record_status = 'active') AS n_documents" + base + f" ORDER BY {PAPER_ORDERS[order]}")
    params = list(w.params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    rows = [dict(r) for r in conn.execute(sql, params)]
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["authors"] = loads(r.pop("authors_json", None), [])
        r["urls"] = loads(r.pop("urls_json", None), [])
        r["other_ids"] = loads(r.pop("other_ids_json", None), {})
        r["stages"] = []
        r["screening"] = {}
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for s in conn.execute("SELECT ps.paper_id, s.key FROM paper_stage ps JOIN stage s ON s.id = ps.stage_id"
                              f" WHERE ps.status = 'active' AND ps.paper_id IN ({marks}) ORDER BY s.position", part):
            if s["key"] not in by_id[s["paper_id"]]["stages"]:
                by_id[s["paper_id"]]["stages"].append(s["key"])
        for s in conn.execute(f"SELECT * FROM ({SCREENING_STATUS_SQL}) WHERE paper_id IN ({marks})", part):
            by_id[s["paper_id"]]["screening"][s["stage"]] = {
                "outcome": s["outcome"], "finality": s["finality"], "n_decisions": s["n_decisions"],
                "needs_rereview": bool(s["needs_rereview"])}
    return Page(rows, total, limit, offset)


def iter_papers(conn: sqlite3.Connection, flt: PaperFilter | None = None, *, order: str = "year", batch: int = 2000):
    """Yield every paper matching ``flt`` in batches (for exports)."""
    offset = 0
    while True:
        page = search_papers(conn, flt, limit=batch, offset=offset, order=order)
        yield from page.rows
        offset += len(page.rows)
        if not page.rows or offset >= page.total:
            return


def distinct_values(conn: sqlite3.Connection, column: str, limit: int = 200) -> list[tuple[str, int]]:
    """Distinct original terms of a categorical finding column with their counts (for filter controls)."""
    allowed = {"system_context", "experimental_context", "outcome", "measurement_method", "unit_original",
               "observation_type", "statement_type", "relative_semantics", "review_status", "kind",
               "intervention", "comparator", "time_unit"}
    if column not in allowed:
        raise ValidationError(f"column must be one of {sorted(allowed)}")
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT {column}, COUNT(*) FROM finding WHERE record_status = 'active' AND {column} IS NOT NULL"
        f" GROUP BY {column} ORDER BY COUNT(*) DESC, {column} LIMIT ?", (limit,))]


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Headline counts. Reports, studies, experiments and findings are different things and counted separately."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "reports": one("SELECT COUNT(*) FROM paper WHERE record_status = 'active' AND canonical_id IS NULL"),
        "reports_grouped_under_canonical": one("SELECT COUNT(*) FROM paper WHERE canonical_id IS NOT NULL"),
        "reports_unresolved": one("SELECT COUNT(*) FROM paper WHERE record_status = 'active' AND"
                                  " canonical_id IS NULL AND resolution_status = 'unresolved'"),
        "studies": one("SELECT COUNT(*) FROM study WHERE record_status = 'active'"),
        "experiments": one("SELECT COUNT(*) FROM experiment WHERE record_status = 'active'"),
        "findings": one("SELECT COUNT(*) FROM finding WHERE record_status = 'active'"),
        "findings_without_experiment": one("SELECT COUNT(*) FROM finding WHERE record_status = 'active' AND"
                                           " experiment_id IS NULL"),
        "findings_without_stage": one(
            "SELECT COUNT(*) FROM finding f WHERE record_status = 'active' AND NOT EXISTS (SELECT 1 FROM"
            " finding_stage fs WHERE fs.finding_id = f.id AND fs.status = 'active')"),
        "findings_by_review_status": {r[0]: r[1] for r in conn.execute(
            "SELECT review_status, COUNT(*) FROM finding WHERE record_status = 'active' GROUP BY review_status")},
        "findings_by_kind": {r[0]: r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM finding WHERE record_status = 'active' GROUP BY kind")},
        "documents": one("SELECT COUNT(*) FROM document WHERE record_status = 'active'"),
        "search_runs": one("SELECT COUNT(*) FROM search_run"),
    }
