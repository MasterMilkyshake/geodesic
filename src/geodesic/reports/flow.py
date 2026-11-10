"""Plain-text search and screening flow summary.

The figures come from :func:`geodesic.screening.summary`; the dashboard, the CLI
and the reports all show the same counts. The layout is PRISMA-style in the
sense that it follows records from identification through screening; no claim of
compliance with any reporting guideline is made.
"""

from __future__ import annotations

from typing import Any

from ..export.tables import Table

STAGE_TITLES = {"title_abstract": "Title/abstract screening", "full_text": "Full-text screening"}


def _kv(d: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in d.items()) or "none"


def format_flow(summary: dict[str, Any]) -> str:
    """Render the flow summary as text. Human, automated and simulated decisions are listed separately."""
    ident = summary["identification"]
    lines = [f"Protocol: {summary['protocol']['label']}", "", "Identification",
             f"  search runs by mode: {_kv(ident['search_runs'])}",
             f"  records retrieved by mode: {_kv(ident['records_retrieved'])}",
             f"  search runs truncated or incomplete: {ident['runs_not_complete']}",
             f"  paper records: {ident['paper_records']} ({_kv(ident['paper_records_by_origin'])})",
             f"  grouped as duplicates of another record: {ident['grouped_as_duplicates']}",
             f"  distinct records after grouping: {ident['distinct_records_after_grouping']}",
             f"  duplicate candidates awaiting review: {ident['duplicate_candidates_awaiting_review']}"]
    for stage, st in summary["stages"].items():
        kinds = st["current_decisions_by_decider"]
        lines += ["", STAGE_TITLES.get(stage, stage),
                  f"  records eligible for this stage: {st['eligible_for_stage']}",
                  f"  with a current decision: {st['screened']}   awaiting a decision: {st['awaiting_decision']}",
                  f"  outcomes: {_kv(st['outcomes'])}",
                  f"  papers by finality: {_kv(st['papers_by_finality'])}",
                  f"  current decisions — human: {kinds['human']}, automated: {kinds['automated']},"
                  f" simulated: {kinds['simulated']}",
                  f"  papers decided only by automation: {st['papers_decided_only_by_automation']}",
                  f"  superseded decisions kept in history: {st['superseded_decisions']}",
                  f"  decisions flagged for re-review: {st['flagged_for_rereview']}",
                  f"  open suggestions (not decisions): {st['open_suggestions']}"]
        for r in st["exclusion_reasons"]:
            lines.append(f"  excluded — {r['reason_kind']}"
                         + (f" [{r['criterion_id']}]" if r["criterion_id"] else "")
                         + f" by {r['decider_kind']} decision: {r['papers']} paper(s)")
    lines += [""] + [f"Note: {n}" for n in summary["notes"]]
    return "\n".join(lines)


def flow_table(summary: dict[str, Any]) -> Table:
    """The flow summary in long form (section, item, count) for the table exports."""
    ident = summary["identification"]
    rows: list[dict[str, Any]] = []

    def add(section: str, item: str, count: Any) -> None:
        rows.append({"section": section, "item": item, "count": count})

    for mode, n in ident["search_runs"].items():
        add("identification", f"search runs, mode {mode}", n)
    for mode, n in ident["records_retrieved"].items():
        add("identification", f"records retrieved, mode {mode}", n)
    add("identification", "search runs truncated or incomplete", ident["runs_not_complete"])
    add("identification", "paper records", ident["paper_records"])
    for origin, n in ident["paper_records_by_origin"].items():
        add("identification", f"paper records, origin {origin}", n)
    add("identification", "grouped as duplicates of another record", ident["grouped_as_duplicates"])
    add("identification", "distinct records after grouping", ident["distinct_records_after_grouping"])
    add("identification", "duplicate candidates awaiting review", ident["duplicate_candidates_awaiting_review"])
    for stage, st in summary["stages"].items():
        add(stage, "records eligible for this stage", st["eligible_for_stage"])
        add(stage, "with a current decision", st["screened"])
        add(stage, "awaiting a decision", st["awaiting_decision"])
        for k, n in st["outcomes"].items():
            add(stage, f"outcome {k}", n)
        for k, n in st["papers_by_finality"].items():
            add(stage, f"papers with {k} decisions", n)
        for k, n in st["current_decisions_by_decider"].items():
            add(stage, f"current decisions by {k} decider", n)
        add(stage, "papers decided only by automation", st["papers_decided_only_by_automation"])
        add(stage, "superseded decisions kept in history", st["superseded_decisions"])
        add(stage, "decisions flagged for re-review", st["flagged_for_rereview"])
        add(stage, "open suggestions (not decisions)", st["open_suggestions"])
        for r in st["exclusion_reasons"]:
            add(stage, f"excluded: {r['reason_kind']}" + (f" [{r['criterion_id']}]" if r["criterion_id"] else "")
                + f" by {r['decider_kind']} decision", r["papers"])
    return Table("search_screening_flow", "Search and screening flow", ["section", "item", "count"], rows,
                 f"Protocol: {summary['protocol']['label']}. " + " ".join(summary["notes"])
                 + " PRISMA-style layout; no compliance with a reporting guideline is claimed.")
