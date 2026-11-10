"""Command-line commands for the workflow steps after workbook import.

Every command calls the same public functions as the dashboard; nothing is
implemented here except argument handling and plain-text formatting.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from . import cli_docs
from .cli import _banner, _open, emit, operator, reviewer
from .errors import GeodesicError
from .reports.flow import format_flow


def _paper_label(p: dict) -> str:
    text = p.get("title") or p.get("citation_text") or "(no title)"
    year = f" ({p['year']})" if p.get("year") else ""
    return f"{text[:90]}{year}"


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.year_from is not None:
        out["year_from"] = args.year_from
    if args.year_to is not None:
        out["year_to"] = args.year_to
    if args.open_access:
        out["open_access"] = True
    return out


# ------------------------------------------------------------------ search
def cmd_search(args: argparse.Namespace) -> int:
    from .search import europepmc, runs, snapshot

    with _open(args) as project:
        conn = project.conn
        if args.action == "translate":
            q = europepmc.build_query(project, args.query, None, _filters(args))
            emit(args, q, f"{_banner(project)}\nconcept query: {q['concept_query']}\n"
                          f"translated for Europe PMC: {q['translated_query']}\nexecuted query: {q['executed_query']}")
        elif args.action in ("run", "resume"):
            fixture = Path(args.fixture_dir).expanduser() if args.fixture_dir else None
            kwargs: dict[str, Any] = dict(max_records=args.max_records, fixture_dir=fixture, declare=args.declare)
            if args.action == "run":
                kwargs.update(query=args.query, filters=_filters(args), page_size=args.page_size)
            else:
                kwargs.update(resume_run_id=args.run_id)
            out = europepmc.run_search(project, operator(args, project), **kwargs)
            run = out["run"]
            lines = [_banner(project), f"search run {run['id']}: {run['label']}",
                     f"executed query: {run['executed_query']}",
                     f"pages retrieved: {run['pages_retrieved']}   raw responses stored: {run['raw_responses']}",
                     "records linked: " + ", ".join(f"{k}: {v}" for k, v in out["links"].items()),
                     "duplicate detection: " + (", ".join(f"{k}: {v}" for k, v in out["dedup"].items()) or "not run")]
            if run["completeness_note"]:
                lines.append(f"completeness: {run['completeness_note']}")
            for e in run["errors"]:
                lines.append(f"  problem: {e.get('kind')}: {e.get('message', '')}")
            if run["status"] in ("incomplete", "truncated") and run["resume_cursor"]:
                lines.append(f"resume with: geodesic search resume {run['id']}")
            lines.append("A search run is a stored snapshot. Running the query again later is a new run and may "
                         "return different records. No search is exhaustive.")
            emit(args, out, "\n".join(lines))
        elif args.action == "list":
            rows = runs.list_runs(conn, limit=args.limit)
            emit(args, rows, "\n".join([_banner(project)] + [
                f"{r['id']}  {r['started_at']}  {r['label']}\n    query: {r['executed_query']}" for r in rows]
                or ["no search runs"]))
        elif args.action == "show":
            run = runs.get_run(conn, args.run_id)
            res = runs.run_results(conn, args.run_id, limit=args.limit)
            lines = [_banner(project), f"{run['id']}: {run['label']}", f"interface: {run['interface']}",
                     f"executed query: {run['executed_query']}", f"concept query: {run['concept_query']}",
                     f"translated query: {run['translated_query']}", f"filters: {run['filters']}",
                     f"started: {run['started_at']}   finished: {run['finished_at']}",
                     f"page size: {run['page_size']}   retrieval limit: {run['max_records']}   pages:"
                     f" {run['pages_retrieved']}", f"client identification: {run['client_ident']}",
                     f"completeness: {run['completeness_note'] or '-'}", f"errors: {run['errors'] or '-'}",
                     f"links: {run['link_counts']}", f"first {len(res['rows'])} of {res['total']} records:"]
            lines += [f"  {r['position']:>5}  {r['link_method']:<27} {r['paper_id']}  {_paper_label(r)}"
                      for r in res["rows"]]
            emit(args, {"run": run, "results": res}, "\n".join(lines))
        elif args.action == "verify":
            ids = [args.run_id] if args.run_id else [r["id"] for r in runs.list_runs(conn, limit=100000)]
            reports = [snapshot.verify_snapshot(project, i) for i in ids]
            emit(args, reports, "\n".join([_banner(project)] + [
                f"{r['run_id']}  {'intact' if r['intact'] else 'PROBLEM'}  {r['raw_responses']} raw response(s),"
                f" {r['records']} record(s) re-read of {r['recorded_results']} recorded"
                + "".join(f"\n    {p}" for p in r["problems"]) for r in reports]))
            return 0 if all(r["intact"] for r in reports) else 2
    return 0


def cmd_live_smoke(args: argparse.Namespace) -> int:
    from .project import load_config_file
    from .search import smoke

    cfg = load_config_file(args.config) if args.config else None
    report = smoke.run_in_temporary_project(cfg, keep_dir=Path(args.keep_dir) if args.keep_dir else None,
                                            with_crossref=not args.no_crossref,
                                            with_fulltext=not args.no_fulltext)
    emit(args, report, smoke.format_report(report))
    return 0 if report["failed"] == 0 else 2


def cmd_import_search_file(args: argparse.Namespace) -> int:
    from .search import fileimport

    with _open(args) as project:
        out = fileimport.import_search_file(project, Path(args.file).expanduser(), operator(args, project),
                                            fmt=args.format, declare=args.declare, source_label=args.source_label)
        run = out["run"]
        lines = [_banner(project)]
        if out["status"] == "already_imported":
            lines.append("This exact file was already imported; nothing was changed.")
        lines += [f"search run {run['id']}: {run['label']}",
                  "records linked: " + (", ".join(f"{k}: {v}" for k, v in out["links"].items()) or "-"),
                  "duplicate detection: " + (", ".join(f"{k}: {v}" for k, v in out["dedup"].items()) or "not run")]
        lines += [f"note: {n}" for n in out["notes"]]
        emit(args, out, "\n".join(lines))
    return 0


def cmd_crossref(args: argparse.Namespace) -> int:
    from .search import crossref

    with _open(args) as project:
        transport = None
        if args.synthetic_fixture:
            from .synthetic.search import crossref_transport
            transport = crossref_transport()
        if args.action == "validate":
            out = crossref.validate_identifiers(project, operator(args, project), limit=args.limit,
                                                paper_ids=[args.paper] if args.paper else None, transport=transport)
            lines = [_banner(project) + ("  [SYNTHETIC fixture responses]" if transport else "  [LIVE lookups]"),
                     f"Crossref lookups on {out['checked_on']}: {out['looked_up']} — validated {out['validated']},"
                     f" not found in Crossref {out['not_found']}, possible misassigned identifier"
                     f" {out['possible_misassignment']}, errors {len(out['errors'])}",
                     "duplicate detection after validation: "
                     + (", ".join(f"{k}: {v}" for k, v in out["dedup"].items()) or "not run"),
                     "Validation shows that an identifier is registered and what the registry holds for it. It does "
                     "not verify what a workbook says about the paper."]
            lines += [f"  error: {e['paper_id']}: {e['message']}" for e in out["errors"]]
            emit(args, out, "\n".join(lines))
        elif args.action == "propose":
            rows = crossref.propose_for_citation(project, args.paper, operator(args, project), transport=transport)
            emit(args, rows, "\n".join([_banner(project), "proposed registry records (a reviewer must decide):"] + [
                f"  {r['candidate_id']}  agreement {r['title_similarity']}  {r['doi']}  {_paper_label(r)}"
                for r in rows]))
        elif args.action == "decide":
            out = crossref.decide_candidate(project.conn, args.candidate, reviewer(args, project), args.status)
            emit(args, out, f"candidate {out['candidate_id']} {out['status']}; fields filled: "
                            f"{', '.join(out['filled']) or 'none'}")
    return 0


# ------------------------------------------------------------------ duplicates
def cmd_dedup(args: argparse.Namespace) -> int:
    from . import dedup

    with _open(args) as project:
        conn = project.conn
        if args.action == "run":
            out = dedup.find_duplicates(conn, project.config.dedup)
            emit(args, out, _banner(project) + "\n" + ", ".join(f"{k}: {v}" for k, v in out.items()))
        elif args.action == "list":
            res = dedup.relations(conn, status=None if args.status == "all" else args.status,
                                  relation_type=args.type, limit=args.limit)
            lines = [_banner(project), f"{res['total']} relation(s) with status '{args.status}'"
                     + (f" and type '{args.type}'" if args.type else "")]
            for r in res["rows"]:
                lines.append(f"{r['id']}  {r['relation_type']}  {r['status']}  method {r['method']}  score"
                             f" {r['score']}" + ("  [records already grouped]" if r["already_grouped"] else ""))
                lines.append(f"    A {r['paper_a']}  {_paper_label(r['a'])}  doi {r['a'].get('doi')}")
                lines.append(f"    B {r['paper_b']}  {_paper_label(r['b'])}  doi {r['b'].get('doi')}")
                if r["score_details"]:
                    lines.append(f"    details: {r['score_details']}")
            emit(args, res, "\n".join(lines))
        elif args.action == "decide":
            out = dedup.decide_relation(conn, args.relation, reviewer(args, project), args.status, note=args.note,
                                        as_type=args.as_type, canonical=args.canonical)
            emit(args, out, f"relation {out['relation_id']}: {out['status']}"
                            + (f"; records grouped ({out['grouping_id']})" if out.get("grouping_id") else "")
                            + (f"; re-classified from {out['reclassified_from']}"
                               if out.get("reclassified_from") else ""))
        elif args.action == "relate":
            out = dedup.relate(conn, args.paper_a, args.paper_b, args.type, reviewer(args, project), note=args.note)
            emit(args, out, f"recorded: {out}")
        elif args.action == "ungroup":
            gid = dedup.ungroup(conn, args.paper, reviewer(args, project), args.reason)
            emit(args, {"grouping_id": gid}, f"grouping {gid} reversed; the record stands on its own again and "
                                             "will not be grouped automatically with the same record.")
        elif args.action == "confirm-identifier-matches":
            out = dedup.confirm_identifier_candidates(conn, reviewer(args, project),
                                                      min_title_similarity=args.min_title_similarity, note=args.note)
            emit(args, out, f"confirmed {out['confirmed']}; left for individual review {out['left_for_review']}")
        elif args.action == "groups":
            rows = dedup.groupings(conn, status=None if args.status == "all" else args.status)
            emit(args, rows, "\n".join([_banner(project)] + [
                f"{g['id']}  {g['status']}  {g['member_paper_id']} under {g['canonical_paper_id']}  basis"
                f" {g['basis']}  by {g['created_by']}"
                + (f"  reversed by {g['reversed_by']}: {g['reversal_reason']}" if g["status"] == "reversed" else "")
                for g in rows] or ["no groupings"]))
    return 0


# ------------------------------------------------------------------ screening
def _decision_line(d: dict) -> str:
    return (f"  {d['decided_at']}  {d['stage']}  {d['decision']} ({d['reason_kind']}"
            + (f", {d['criterion_id']}" if d["criterion_id"] else "") + f")  by {d['reviewer']} [{d['decider_kind']}"
            + (", adjudication" if d["decision_role"] == "adjudication" else "") + f"]  {d['finality']}, protocol"
            f" v{d['protocol_version']}" + ("" if d["is_current"] else "  (superseded)")
            + ("  NEEDS RE-REVIEW: " + (d["rereview_reason"] or "") if d["needs_rereview"] and d["is_current"] else "")
            + (f"\n      reason: {d['reason_text']}" if d["reason_text"] else "")
            + (f"\n      support: \"{d['supporting_text']}\" {d['supporting_location_text']}"
               if d["supporting_text"] else ""))


def _support_location(args: argparse.Namespace) -> dict | None:
    if not args.support_field:
        return None
    if args.support_start is None or args.support_end is None:
        raise GeodesicError("--support-field needs --support-start and --support-end (0-based, end exclusive).")
    return {"kind": "record_field", "field": args.support_field, "char_start": args.support_start,
            "char_end": args.support_end}


def cmd_screen(args: argparse.Namespace) -> int:
    from . import screening as S

    with _open(args) as project:
        conn = project.conn
        banner = _banner(project)
        if args.action == "suggest":
            out = S.suggest(conn, args.stage)
            emit(args, out, f"{banner}\nsuggestions for {out['stage']} (generator {out['generator']}, protocol"
                            f" v{out['protocol_version']} {out['protocol_status']}): created {out['created']}"
                            f" {out['by_suggestion']}, unchanged {out['unchanged']}, superseded {out['superseded']};"
                            f" skipped: already decided {out['skipped_already_decided']}, no stored full text"
                            f" {out['skipped_no_text']}\nSuggestions are drafts. None of them is a decision.")
        elif args.action == "queue":
            res = S.queue(conn, args.stage, reviewer=reviewer(args, project).name if args.mine else None,
                          search_run_id=args.search_run, limit=args.limit)
            lines = [banner, f"{res['total']} paper(s) awaiting a {args.stage} decision"]
            for r in res["rows"]:
                lines.append(f"{r['id']}  {_paper_label(r)}  [{r['publication_type'] or 'type not reported'}]")
                lines += [f"    suggestion {s['id']}: {s['suggested_decision']} ({s['reason_kind']}"
                          + (f", {s['criterion_id']}" if s["criterion_id"] else "") + f") — {s['rationale']}"
                          for s in r["suggestions"]]
            emit(args, res, "\n".join(lines))
        elif args.action in ("decide", "adjudicate"):
            kwargs = dict(reason_kind=args.reason_kind, criterion_id=args.criterion, reason_text=args.reason,
                          supporting_text=args.support, supporting_location=_support_location(args),
                          require_final=args.final)
            fn = S.decide if args.action == "decide" else S.adjudicate
            out = fn(conn, args.paper, args.stage, args.decision, reviewer(args, project), **kwargs)
            emit(args, out, f"{banner}\nrecorded {out['decision']} ({out['finality'].upper()}) for {out['paper_id']} at"
                            f" {out['stage']}; current outcome: {out['outcome']}"
                            + (f"; supersedes {out['supersedes']}" if out["supersedes"] else ""))
        elif args.action == "accept":
            out = S.accept_suggestion(conn, args.suggestion, reviewer(args, project), note=args.note,
                                      require_final=args.final)
            emit(args, out, f"{banner}\nsuggestion accepted as your decision: {out['decision']}"
                            f" ({out['finality'].upper()}) for {out['paper_id']}")
        elif args.action == "dismiss":
            S.dismiss_suggestion(conn, args.suggestion, reviewer(args, project), note=args.note)
            emit(args, {"suggestion_id": args.suggestion, "status": "dismissed"}, "suggestion dismissed")
        elif args.action == "automate":
            out = S.apply_automation(conn, args.stage)
            emit(args, out, f"{banner}\nautomated decisions recorded: {out['applied']} {dict(sorted(out['by_rule'].items()))} (rules"
                            f" authorised by protocol v{out['protocol_version']}: {', '.join(out['allowed_rules'])});"
                            f" skipped: already decided {out['skipped_already_decided']}, rule not authorised"
                            f" {out['skipped_rule_not_authorised']}")
        elif args.action == "history":
            h = S.history(conn, args.paper, args.stage)
            lines = [banner, f"{len(h['decisions'])} decision(s), {len(h['suggestions'])} suggestion(s)"]
            lines += [_decision_line(d) for d in h["decisions"]]
            lines += [f"  suggestion {s['created_at']}  {s['stage']}  {s['suggested_decision']}  [{s['status']}]"
                      f"  {s['rationale']}" for s in h["suggestions"]]
            emit(args, h, "\n".join(lines))
        elif args.action == "conflicts":
            rows = S.conflicts(conn, args.stage)
            lines = [banner, f"{len(rows)} paper(s) with a recorded disagreement"]
            for r in rows:
                lines.append(f"{r['paper_id']}  {r['stage']}  {_paper_label(r)}")
                lines += [_decision_line(d) for d in r["decisions"]]
            emit(args, rows, "\n".join(lines))
        elif args.action == "rereview":
            if args.reaffirm:
                out = S.reaffirm(conn, args.reaffirm, reviewer(args, project), note=args.note)
                emit(args, out, f"decision kept and recorded again under protocol v{out['protocol_version']}"
                                f" ({out['finality']})")
            else:
                rows = S.rereview_queue(conn, args.stage)
                emit(args, rows, "\n".join([banner, f"{len(rows)} decision(s) flagged for re-review"] + [
                    f"{d['id']}  {d['paper_id']}  {_paper_label(d)}\n{_decision_line(d)}" for d in rows]))
        elif args.action == "summary":
            out = S.summary(conn)
            emit(args, out, banner + "\n" + format_flow(out))
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    from . import screening as S

    with _open(args) as project:
        if args.value is None:
            rows = S.assessments(project.conn, args.paper, current_only=False)
            emit(args, rows, "\n".join(
                f"{r['created_at']}  {r['dimension']}: {r['value']}  by {r['reviewer']}"
                + ("" if r["is_current"] else "  (superseded)") + (f"  — {r['rationale']}" if r["rationale"] else "")
                for r in rows) or "no assessments")
        else:
            if not args.dimension:
                raise GeodesicError("--dimension is required when recording an assessment.")
            aid = S.assess(project.conn, args.paper, args.dimension, args.value, reviewer(args, project),
                           args.rationale)
            emit(args, {"assessment_id": aid}, f"recorded {args.dimension} = {args.value} ({aid})")
    return 0


# ------------------------------------------------------------------ parser
def register(sub: argparse._SubParsersAction, common: Callable[..., None]) -> None:
    """Add the commands of later workflow steps to the parser."""
    from .screening import ASSESSMENT_VALUES, DECISIONS, REASON_KINDS, STAGES

    def stage_arg(p: argparse.ArgumentParser, required: bool = True) -> None:
        p.add_argument("--stage", choices=STAGES, required=required)

    # search
    p = sub.add_parser("search", help="run, resume, list, show or verify literature search runs (Europe PMC)")
    ss = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    for name, text in (("run", "execute a search and import what it returns"),
                       ("translate", "show the source-specific query without running it")):
        q = ss.add_parser(name, help=text)
        q.add_argument("--query", help="exact source-specific query (default: built from search.concepts)")
        q.add_argument("--year-from", type=int)
        q.add_argument("--year-to", type=int)
        q.add_argument("--open-access", action="store_true", help="restrict to records flagged open access")
        if name == "run":
            q.add_argument("--max-records", type=int, help="retrieval limit (default: search.max_records)")
            q.add_argument("--page-size", type=int)
            q.add_argument("--fixture-dir", help="replay stored synthetic responses instead of contacting the provider")
            q.add_argument("--declare", choices=["synthetic", "real"], help="provenance of untagged fixture files")
        common(q)
    q = ss.add_parser("resume", help="continue an incomplete or truncated run from its stored cursor")
    q.add_argument("run_id")
    q.add_argument("--max-records", type=int, help="new, higher retrieval limit")
    q.add_argument("--fixture-dir")
    q.add_argument("--declare", choices=["synthetic", "real"])
    common(q)
    q = ss.add_parser("list", help="list search runs")
    q.add_argument("--limit", type=int, default=50)
    common(q)
    q = ss.add_parser("show", help="show one run with its exact query and records")
    q.add_argument("run_id")
    q.add_argument("--limit", type=int, default=25)
    common(q)
    q = ss.add_parser("verify", help="re-read stored snapshots and check their hashes (all runs by default)")
    q.add_argument("run_id", nargs="?")
    common(q)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("live-smoke", help="bounded LIVE smoke test (mechanics, counts and schema only) in a "
                                          "throw-away real project")
    p.add_argument("--config", help="project configuration to take the smoke query and limits from")
    p.add_argument("--keep-dir", help="create the real project here and keep it (default: temporary, deleted)")
    p.add_argument("--no-crossref", action="store_true", help="skip the Crossref lookup check")
    p.add_argument("--no-fulltext", action="store_true", help="skip the open-access full-text retrieval check")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_live_smoke)

    p = sub.add_parser("import-search-file", help="import search results exported as RIS or CSV")
    p.add_argument("file")
    p.add_argument("--format", choices=["ris", "csv"], help="default: from the file extension")
    p.add_argument("--declare", choices=["synthetic", "real"], help="provenance of an untagged file")
    p.add_argument("--source-label", help="database or tool the file was exported from, as you state it")
    common(p)
    p.set_defaults(func=cmd_import_search_file)

    p = sub.add_parser("crossref", help="identifier validation and metadata enrichment through Crossref")
    ss = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = ss.add_parser("validate", help="look up unvalidated DOIs")
    q.add_argument("--limit", type=int, default=50, help="maximum number of lookups")
    q.add_argument("--paper", help="validate one paper record")
    q = ss.add_parser("propose", help="ask for registry records resembling an unresolved citation")
    q.add_argument("paper")
    q = ss.add_parser("decide", help="accept or reject a proposed registry record")
    q.add_argument("candidate")
    q.add_argument("status", choices=["accepted", "rejected"])
    for q in ss.choices.values():
        q.add_argument("--synthetic-fixture", action="store_true",
                       help="use the bundled synthetic registry responses (synthetic projects only)")
        common(q)
    p.set_defaults(func=cmd_crossref)

    # duplicates
    p = sub.add_parser("dedup", help="duplicate candidates, groupings and their reversal")
    ss = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    ss.add_parser("run", help="detect identifier matches and similarity candidates")
    q = ss.add_parser("list", help="list relations between paper records")
    q.add_argument("--status", choices=["candidate", "confirmed", "rejected", "all"], default="candidate")
    q.add_argument("--type", choices=["duplicate", "preprint_published", "same_study_report", "identifier_conflict"])
    q.add_argument("--limit", type=int, default=50)
    q = ss.add_parser("decide", help="confirm or reject a candidate")
    q.add_argument("relation")
    q.add_argument("status", choices=["confirmed", "rejected"])
    q.add_argument("--as-type", choices=["duplicate", "preprint_published", "same_study_report"],
                   help="confirm as a different relation (e.g. a second report of the same study)")
    q.add_argument("--canonical", help="record to keep as canonical when confirming a duplicate")
    q.add_argument("--note")
    q = ss.add_parser("relate", help="record a relation between two records yourself")
    q.add_argument("paper_a")
    q.add_argument("paper_b")
    q.add_argument("type", choices=["duplicate", "preprint_published", "same_study_report"])
    q.add_argument("--note")
    q = ss.add_parser("ungroup", help="reverse a grouping (the record stands on its own again)")
    q.add_argument("paper", help="the grouped (non-canonical) record")
    q.add_argument("--reason", required=True)
    q = ss.add_parser("confirm-identifier-matches",
                      help="confirm all candidates that share an unvalidated identifier and near-identical titles")
    q.add_argument("--min-title-similarity", type=float, default=0.9)
    q.add_argument("--note")
    q = ss.add_parser("groups", help="list groupings")
    q.add_argument("--status", choices=["active", "reversed", "all"], default="active")
    for q in ss.choices.values():
        common(q)
    p.set_defaults(func=cmd_dedup)

    # screening
    p = sub.add_parser("screen", help="eligibility screening: suggestions, decisions, history, counts")
    ss = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = ss.add_parser("suggest", help="generate draft suggestions from the protocol (never decisions)")
    stage_arg(q)
    q = ss.add_parser("queue", help="papers awaiting a decision")
    stage_arg(q)
    q.add_argument("--mine", action="store_true", help="papers you have not decided yet (second screening)")
    q.add_argument("--search-run")
    q.add_argument("--limit", type=int, default=25)
    for name, text in (("decide", "record your decision"), ("adjudicate", "resolve a recorded disagreement")):
        q = ss.add_parser(name, help=text)
        q.add_argument("paper")
        stage_arg(q)
        q.add_argument("--decision", choices=DECISIONS, required=True)
        q.add_argument("--reason-kind", choices=REASON_KINDS)
        q.add_argument("--criterion", help="criterion id the decision relies on")
        q.add_argument("--reason", help="written reason")
        q.add_argument("--support", help="supporting text quoted from the record")
        q.add_argument("--support-field", choices=["title", "abstract"])
        q.add_argument("--support-start", type=int)
        q.add_argument("--support-end", type=int)
        q.add_argument("--final", action="store_true",
                       help="refuse instead of recording a provisional draft when the protocol is provisional")
    for name, text in (("accept", "accept a suggestion as your own decision"),
                       ("dismiss", "set a suggestion aside without deciding")):
        q = ss.add_parser(name, help=text)
        q.add_argument("suggestion")
        q.add_argument("--note")
        if name == "accept":
            q.add_argument("--final", action="store_true")
    q = ss.add_parser("automate", help="apply rule-based exclusions the confirmed protocol authorises")
    stage_arg(q)
    q = ss.add_parser("history", help="every decision and suggestion for a paper")
    q.add_argument("paper")
    stage_arg(q, required=False)
    q = ss.add_parser("conflicts", help="papers on which reviewers disagree")
    stage_arg(q, required=False)
    q = ss.add_parser("rereview", help="decisions flagged for re-review after a protocol change")
    stage_arg(q, required=False)
    q.add_argument("--reaffirm", metavar="DECISION_ID", help="keep this decision under the current protocol")
    q.add_argument("--note")
    ss.add_parser("summary", help="search and screening flow counts")
    for q in ss.choices.values():
        common(q)
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("assess", help="relevance / comparison / modelling suitability of a paper (separate from "
                                      "eligibility)")
    p.add_argument("paper")
    p.add_argument("--dimension", choices=tuple(ASSESSMENT_VALUES))
    p.add_argument("--value", help="omit to list the paper's assessments")
    p.add_argument("--rationale")
    common(p)
    p.set_defaults(func=cmd_assess)

    cli_docs.register(sub, common)
