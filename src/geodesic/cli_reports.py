"""Command-line commands for comparison, vocabulary, evidence maps, summaries, figures, reports and the dashboard.

Every command prints what one public function returns; the dashboard and the
HTML report call the same functions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from .errors import ValidationError  # noqa: F401  (re-exported for command modules)


def _finding_ids(args: argparse.Namespace, conn) -> list[str]:
    """Finding IDs named on the command line, or the findings matching the filter options."""
    from . import comparison
    from .cli import finding_filter

    return comparison.select_findings(conn, args.finding_ids, finding_filter(args))


def _assessment_lines(a: dict) -> list[str]:
    ind = a["independence"]
    lines = [f"Classification: {a['classification']}"
             + (f"   (blocking: {', '.join(a['blocking'])})" if a["blocking"] else ""),
             f"Findings: {ind['findings']}   reports: {ind['reports']}   independent units, at most: "
             f"{ind['independent_units_at_most']}   independence not assessed: "
             f"{ind['findings_independence_not_assessed']}", "", "Checks:"]
    for c in a["checks"]:
        lines.append(f"  [{c['level']:<8}] {c['check']:<22} {c['status']:<8} {c['detail']}")
    lines += ["", a["statement"]]
    return lines


# ------------------------------------------------------------------ compare
def cmd_compare(args: argparse.Namespace) -> int:
    from . import comparison
    from .cli import _banner, _open, emit, reviewer

    with _open(args) as project:
        conn = project.conn
        a = args.action
        if a == "assess":
            out = comparison.assess(conn, _finding_ids(args, conn))
            emit(args, out, "\n".join([_banner(project), *_assessment_lines(out)]))
        elif a == "table":
            out = comparison.table(conn, _finding_ids(args, conn), target_unit=args.unit)
            if args.csv:
                from .export import tables as T
                from .export import write_table_file

                dest = write_table_file(project, T.Table("comparison_table", "Comparison table", out["columns"],
                                                         out["rows"], out["note"]), Path(args.csv).expanduser())
                print(f"Wrote {len(out['rows'])} row(s) to {dest}")
                return 0
            lines = [_banner(project), f"{len(out['rows'])} finding(s); display unit: {out['display_unit'] or '-'}", ""]
            for r in out["rows"]:
                shown = ""
                if r["display_rule"]:
                    vals = [v for v in (r["display_value"], r["display_low"], r["display_high"]) if v is not None]
                    shown = (f"   in {r['display_unit']}: {' to '.join(f'{v:.6g}' for v in vals)} ({r['display_rule']})"
                             if vals else f"   ({r['display_rule']})")
                lines.append(f"{r['finding_id']}  {r['citation']}")
                lines.append(f"  outcome: {r['outcome'] or '-'}   system: {r['system_context'] or '-'}   "
                             f"stages: {r['stages'] or '-'}")
                lines.append(f"  as stated: {r['as_stated']}{shown}")
                lines.append(f"  independence unit: {r['independence_unit']} ({r['independence_basis']})   "
                             f"review: {r['review_status']}   eligibility: {r['comparison_eligibility']}")
                lines.append(f"  source: {r['source_location']}")
            lines += ["", out["note"]]
            emit(args, out, "\n".join(lines))
        elif a == "save":
            out = comparison.save_set(conn, args.name, _finding_ids(args, conn), reviewer(args, project),
                                      classification=args.classification, description=args.description)
            emit(args, out, f"Saved comparison set {out['id']} '{out['name']}' as {out['classification']} "
                            f"({len(out['finding_ids'])} findings). Nothing was pooled.")
        elif a == "list":
            sets = comparison.list_sets(conn)
            lines = [_banner(project), f"{len(sets)} comparison set(s)"]
            for s in sets:
                lines.append(f"{s['id']}  {s['name']}  [{s['classification']}]  {len(s['finding_ids'])} findings  "
                             f"saved {s['created_at']} by {s['created_by']}"
                             + ("" if s["is_current"] else f"  ** {len(s['changed_since_assessment'])} finding(s) "
                                                           "changed since the assessment **"))
            emit(args, sets, "\n".join(lines))
        elif a == "show":
            s = comparison.get_set(conn, args.set_id)
            lines = [_banner(project), f"{s['id']}  {s['name']}  [{s['classification']}]  saved {s['created_at']} "
                                       f"by {s['created_by']}"]
            if s["description"]:
                lines.append(f"  {s['description']}")
            if not s["is_current"]:
                lines.append("  ** The assessment below was made before these findings changed: "
                             + ", ".join(s["changed_since_assessment"]) + ". Assess the set again. **")
            lines += ["", "Assessment as stored:", *_assessment_lines(s["assessment"]), "", "Findings:"]
            lines += [f"  {fid}" for fid in s["finding_ids"]]
            emit(args, s, "\n".join(lines))
        elif a == "eligibility":
            out = comparison.set_eligibility(conn, _finding_ids(args, conn), args.value, reviewer(args, project),
                                             note=args.note)
            emit(args, out, f"{out['findings']} finding(s) marked {out['value']}")
        elif a == "units":
            from . import query
            from .cli import finding_filter

            unit_of = comparison.independence_units(conn)
            ids = args.finding_ids or [f["id"] for f in query.iter_findings(conn, finding_filter(args),
                                                                           with_locations=False)]
            out = comparison.count_units(unit_of, ids)
            emit(args, out, "\n".join([
                _banner(project),
                f"{out['findings']} finding(s) form at most {out['independent_units_at_most']} independent unit(s).",
                "  units by basis: " + ", ".join(f"{k}: {v}" for k, v in out["units_by_basis"].items()),
                "  Findings of one experiment, and findings a reviewer related as dependent, form one unit. "
                "Independence of the others was not assessed, so the count is an upper bound."]))
    return 0


# ------------------------------------------------------------------ vocabulary
def cmd_vocabulary(args: argparse.Namespace) -> int:
    from . import vocabulary
    from .cli import _banner, _open, emit, reviewer

    with _open(args) as project:
        conn = project.conn
        a = args.action
        if a == "list":
            rows = vocabulary.terms(conn, args.field, include_inactive=args.all)
            lines = [_banner(project), f"{len(rows)} term mapping(s) (reviewer decisions; original terms stay stored)"]
            for r in rows:
                lines.append(f"{r['field']}: {r['original_term']!r} -> {r['normalized_term']!r}  by {r['created_by']} "
                             f"{r['created_at']}" + ("" if r["active"] else "  [withdrawn]")
                             + (f"  note: {r['note']}" if r["note"] else ""))
            emit(args, rows, "\n".join(lines))
        elif a == "usage":
            rows = vocabulary.usage(conn, args.field, limit=args.limit)
            lines = [_banner(project), f"Terms of '{args.field}' as stored in active findings:"]
            for r in rows:
                lines.append(f"  {r['findings']:>6}  {r['original_term']!r}"
                             + (f"  -> grouped under {r['normalized_term']!r}" if r["normalized_term"] else ""))
            emit(args, rows, "\n".join(lines))
        elif a == "set":
            out = vocabulary.set_term(conn, args.field, args.original, args.normalized, reviewer(args, project),
                                      note=args.note)
            emit(args, out, f"{out['field']}: {out['original_term']!r} is now grouped under "
                            f"{out['normalized_term']!r} ({out['findings_with_term']} finding(s) carry the term; "
                            "their stored wording is unchanged)")
        elif a == "remove":
            vocabulary.remove_term(conn, args.field, args.original, reviewer(args, project), args.reason)
            emit(args, {"removed": [args.field, args.original]},
                 f"Mapping of {args.field} {args.original!r} withdrawn (kept in the record)")
    return 0


# ------------------------------------------------------------------ maps and summaries
def cmd_map(args: argparse.Namespace) -> int:
    from .cli import _banner, _open, emit, finding_filter
    from .reports import maps

    with _open(args) as project:
        emap = maps.evidence_map(project.conn, args.by, flt=finding_filter(args), max_columns=args.max_columns)
        emit(args, emap, _banner(project) + "\n" + maps.format_map(emap, args.metric))
    return 0


def cmd_missing(args: argparse.Namespace) -> int:
    from .cli import _banner, _open, emit, finding_filter
    from .reports import missing

    with _open(args) as project:
        summary = missing.missing_information(project.conn, finding_filter(args))
        full = missing.fulltext_summary(project.conn)
        lines = [_banner(project), missing.format_missing(summary, args.only_stage), "",
                 f"Full-text availability ({full['reports']} reports; {full['reports_with_findings']} with findings)"]
        lines += [f"  {k.replace('_', ' ')}: {v}" for k, v in full["by_availability"].items()]
        lines.append("  documents by parse status: "
                     + (", ".join(f"{k}: {v}" for k, v in full["documents_by_parse_status"].items()) or "none"))
        lines += [f"Note: {n}" for n in full["notes"]]
        emit(args, {"missing_information": summary, "fulltext": full}, "\n".join(lines))
    return 0


def cmd_modeling(args: argparse.Namespace) -> int:
    from .cli import _banner, _open, emit
    from .reports import missing

    with _open(args) as project:
        inv = missing.modeling_inventory(project.conn)
        emit(args, inv, _banner(project) + "\n" + missing.format_modeling(inv))
    return 0


# ------------------------------------------------------------------ figures and reports
def _print_result(project, res: dict, what: str) -> None:
    from .cli import _banner

    print(_banner(project))
    print(f"{what} directory: {res['directory']}")
    for f in res["files"]:
        print(f"  {f.name}")
    print(f"  {res['manifest'].name}")
    if res["rows"]:
        print("Rows: " + ", ".join(f"{k}: {v}" for k, v in res["rows"].items()))


def cmd_figures(args: argparse.Namespace) -> int:
    from .cli import _csv_list, _open, finding_filter, operator
    from .reports import build

    with _open(args) as project:
        res = build.build_report(project, args.out, flt=finding_filter(args), parts=["figures"],
                                 figure_formats=_csv_list(args.formats) or list(build.F.FORMATS), dpi=args.dpi,
                                 actor=operator(args, project))
        _print_result(project, res, "Figure")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .cli import _csv_list, _open, finding_filter, operator
    from .reports import build

    with _open(args) as project:
        res = build.build_report(
            project, args.out, flt=finding_filter(args), parts=_csv_list(args.parts) or list(build.PARTS),
            table_formats=_csv_list(args.table_formats) or ["xlsx", "csv", "json", "ris"],
            figure_formats=_csv_list(args.figure_formats) or list(build.F.FORMATS), dpi=args.dpi, title=args.title,
            actor=operator(args, project))
        _print_result(project, res, "Report")
        if res["html"]:
            print(f"Open {res['html']} in a browser; it is one self-contained file.")
    return 0


# ------------------------------------------------------------------ dashboard
def dashboard_command(project_dir: Path, port: int | None = None, headless: bool = False) -> list[str]:
    """The command that starts the dashboard for one project directory."""
    app = Path(__file__).with_name("dashboard") / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--browser.gatherUsageStats", "false",
           "--server.address", "localhost"]
    if port:
        cmd += ["--server.port", str(port)]
    if headless:
        cmd += ["--server.headless", "true"]
    return [*cmd, "--", "--project", str(project_dir)]


def cmd_dashboard(args: argparse.Namespace) -> int:
    import os
    import subprocess

    from .cli import project_path
    from .project import open_project

    root = project_path(args.project)
    open_project(root).close()                      # fail early, with a clear message, on a wrong directory
    env = dict(os.environ)
    if args.reviewer:
        env["GEODESIC_REVIEWER"] = args.reviewer
    cmd = dashboard_command(root, args.port, args.headless)
    print("Starting the dashboard (local only; stop it with Ctrl+C):\n  " + " ".join(cmd))
    return subprocess.call(cmd, env=env)  # noqa: S603


def cmd_demo(args: argparse.Namespace) -> int:
    from . import demo

    out = demo.run_demo(Path(args.dir).expanduser() if args.dir else None, keep_existing=False,
                        with_report=not args.no_report, echo=print)
    print("\nDemo finished. Everything above happened in a SYNTHETIC project; review actions were simulated and "
          "are recorded as such.")
    print(f"Project directory: {out['project_dir']}")
    if out.get("report"):
        print(f"Report:            {out['report']}")
    print(f"Look at it in the dashboard:  geodesic dashboard --project {out['project_dir']}")
    return 0


# ------------------------------------------------------------------ parser
def register(sub: argparse._SubParsersAction, common: Callable[..., None]) -> None:
    from . import comparison, vocabulary
    from .cli import filter_args
    from .reports import maps

    p = sub.add_parser("compare", help="compatibility assessment, side-by-side tables and saved comparison sets "
                                       "(nothing is ever pooled)")
    cs = p.add_subparsers(dest="action", required=True)
    for name, hlp in (("assess", "check whether a set of findings can be compared (stores nothing)"),
                      ("table", "side-by-side table, one row per finding, values as stated"),
                      ("units", "how many independent units a set of findings forms, at most")):
        q = cs.add_parser(name, help=hlp)
        q.add_argument("finding_ids", nargs="*", help="finding IDs (or use the filter options)")
        if name == "table":
            q.add_argument("--unit", help="unit for the display columns (default: unit of the first finding)")
            q.add_argument("--csv", help="write the table to this CSV file instead of printing it")
        filter_args(q)
        common(q, False)
    q = cs.add_parser("save", help="store a set with its assessment as it stands now")
    q.add_argument("name")
    q.add_argument("finding_ids", nargs="*")
    q.add_argument("--classification", choices=["descriptive_only", "quantitative_candidate"],
                   help="default: the assessed classification; a blocked set can only be descriptive_only")
    q.add_argument("--description")
    filter_args(q)
    common(q)
    q = cs.add_parser("list", help="saved comparison sets")
    common(q, False)
    q = cs.add_parser("show", help="a saved set with its stored assessment")
    q.add_argument("set_id")
    common(q, False)
    q = cs.add_parser("eligibility", help="record a reviewer's judgement of suitability for quantitative comparison")
    q.add_argument("value", choices=list(comparison.ELIGIBILITY))
    q.add_argument("finding_ids", nargs="*")
    q.add_argument("--note")
    filter_args(q)
    common(q)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("vocabulary", help="reviewer-made groupings of original terms (never silent, never destructive)")
    vs = p.add_subparsers(dest="action", required=True)
    q = vs.add_parser("list", help="term mappings")
    q.add_argument("--field", choices=list(vocabulary.FIELDS))
    q.add_argument("--all", action="store_true", help="include withdrawn mappings")
    common(q, False)
    q = vs.add_parser("usage", help="original terms of a field with counts")
    q.add_argument("field", choices=list(vocabulary.FIELDS))
    q.add_argument("--limit", type=int, default=200)
    common(q, False)
    q = vs.add_parser("set", help="group an original term under a normalized term")
    q.add_argument("field", choices=list(vocabulary.FIELDS))
    q.add_argument("original", help="the term exactly as stored")
    q.add_argument("normalized")
    q.add_argument("--note")
    common(q)
    q = vs.add_parser("remove", help="withdraw a mapping")
    q.add_argument("field", choices=list(vocabulary.FIELDS))
    q.add_argument("original")
    q.add_argument("--reason", required=True)
    common(q)
    p.set_defaults(func=cmd_vocabulary)

    p = sub.add_parser("map", help="evidence map: stages by the terms of one field")
    p.add_argument("--by", default="system_context", choices=list(maps.MAP_FIELDS))
    p.add_argument("--metric", default="findings", choices=list(maps.METRICS))
    p.add_argument("--max-columns", type=int, default=12)
    filter_args(p)
    common(p, False)
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("missing", help="missing-information summary and full-text availability")
    p.add_argument("--only-stage", help="print one stage key only (or '(all stages)')")
    filter_args(p)
    common(p, False)
    p.set_defaults(func=cmd_missing)

    p = sub.add_parser("modeling", help="modelling-readiness inventory (an inventory; nothing is fitted)")
    common(p, False)
    p.set_defaults(func=cmd_modeling)

    p = sub.add_parser("figures", help="write the standard figures (SVG, PDF and PNG at 300 DPI or more)")
    p.add_argument("--out", help="output directory (default: a new directory under the project's exports/)")
    p.add_argument("--formats", default="svg,pdf,png", help="comma-separated: svg,pdf,png")
    p.add_argument("--dpi", type=int, default=300, help="PNG resolution, 300 or more")
    filter_args(p)
    common(p)
    p.set_defaults(func=cmd_figures)

    p = sub.add_parser("report", help="write a report directory: self-contained HTML, figures, tables, manifest")
    p.add_argument("--out", help="output directory (default: a new directory under the project's exports/)")
    p.add_argument("--parts", default="html,figures,tables", help="comma-separated: html,figures,tables")
    p.add_argument("--table-formats", default="xlsx,csv,json,ris")
    p.add_argument("--figure-formats", default="svg,pdf,png")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--title")
    filter_args(p)
    common(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dashboard", help="start the local Streamlit dashboard for a project")
    p.add_argument("--port", type=int)
    p.add_argument("--headless", action="store_true", help="do not open a browser window")
    common(p)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("demo", help="run the complete workflow offline in a new SYNTHETIC project "
                                    "(review actions are simulated and labelled)")
    p.add_argument("--dir", help="directory for the demo project and its generated input files "
                                 "(default: a new temporary directory)")
    p.add_argument("--no-report", action="store_true", help="skip writing the report directory")
    p.set_defaults(func=cmd_demo)
