"""Command-line interface.

Every command calls the same public functions the dashboard uses; the CLI holds
no logic of its own beyond argument handling and printing.

Run ``geodesic --help`` or ``geodesic <command> --help``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__, audit, config, protocol, query
from .errors import GeodesicError
from .logging_utils import configure_logging
from .project import Project, create_project, load_config_file, open_project, reload_config
from .util import Actor

ENV_PROJECT = "GEODESIC_PROJECT"
Handler = Callable[[argparse.Namespace], int]


# ------------------------------------------------------------------ helpers
def project_path(value: str | None) -> Path:
    """Resolve ``--project``: a path, or a bare name inside ``$GEODESIC_HOME``."""
    value = value or os.environ.get(ENV_PROJECT)
    if not value:
        raise GeodesicError(f"No project given. Pass --project DIR or set {ENV_PROJECT}.")
    p = Path(value).expanduser()
    if not p.is_absolute() and not p.exists() and len(p.parts) == 1:
        p = config.default_home() / p
    return p


def _open(args: argparse.Namespace) -> Project:
    project = open_project(project_path(args.project))
    configure_logging(project.logs_dir / "geodesic.log")
    return project


def reviewer(args: argparse.Namespace, project: Project) -> Actor:
    """The named human reviewer for this command (from --reviewer, the environment or local configuration)."""
    return Actor(config.resolve_reviewer(getattr(args, "reviewer", None), project.root), "human")


def operator(args: argparse.Namespace, project: Project) -> Actor:
    """Who ran a non-review command: the configured reviewer when there is one, else the system actor."""
    try:
        return Actor(config.resolve_reviewer(getattr(args, "reviewer", None), project.root), "human")
    except GeodesicError:
        return Actor("geodesic-cli", "system")


def emit(args: argparse.Namespace, data: Any, text: str | None = None) -> None:
    if getattr(args, "json", False) or text is None:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    else:
        print(text)


def _banner(project: Project) -> str:
    pv = protocol.current(project.conn)
    cls = "SYNTHETIC project" if project.is_synthetic else "real project"
    return f"[{cls} '{project.name}' | protocol {pv.label}]"


def _csv_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _tristate(value: str | None) -> bool | None:
    return None if value is None else value == "yes"


# ------------------------------------------------------------------ project
def cmd_init(args: argparse.Namespace) -> int:
    cfg = load_config_file(args.config)
    project = create_project(project_path(args.project), cfg, args.data_class)
    print(f"Created {args.data_class} project '{project.name}' in {project.root}")
    print(f"Stages: {', '.join(s.key for s in cfg.stages) or '(none defined)'}")
    print("The protocol is provisional until you load and confirm your criteria "
          "(geodesic protocol template / load / confirm).")
    project.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with _open(args) as project:
        c = query.counts(project.conn)
        ok, n, bad = audit.verify_chain(project.conn)
        data = {"project": project.name, "root": str(project.root), "data_class": project.data_class,
                "protocol": protocol.current(project.conn).label, "counts": c,
                "audit_events": n, "audit_chain_intact": ok, "audit_first_bad_seq": bad}
        lines = [_banner(project), f"data directory: {project.root}",
                 f"reports: {c['reports']} (unresolved citations: {c['reports_unresolved']}; grouped under a "
                 f"canonical record: {c['reports_grouped_under_canonical']})",
                 f"studies: {c['studies']}   experiments: {c['experiments']}   documents: {c['documents']}   "
                 f"search runs: {c['search_runs']}",
                 f"findings: {c['findings']} (no experiment assigned: {c['findings_without_experiment']}; "
                 f"no stage: {c['findings_without_stage']})",
                 "findings by review status: " + (", ".join(
                     f"{k}: {v}" for k, v in sorted(c["findings_by_review_status"].items())) or "none"),
                 f"audit events: {n} (hash chain {'intact' if ok else f'BROKEN at seq {bad}'})"]
        emit(args, data, "\n".join(lines))
    return 0


def cmd_reload_config(args: argparse.Namespace) -> int:
    with _open(args) as project:
        res = reload_config(project, operator(args, project))
        emit(args, res, f"Reloaded project.yaml: {res}")
    return 0


# ------------------------------------------------------------------ workbook
def cmd_profile_workbook(args: argparse.Namespace) -> int:
    from .workbook.profiler import write_profile

    src = Path(args.workbook).expanduser()
    out = Path(args.out).expanduser() if args.out else Path.cwd() / f"{src.stem}_profile"
    stages: list[dict] = []
    if args.project or os.environ.get(ENV_PROJECT):
        with open_project(project_path(args.project)) as project:
            stages = [{"key": s.key, "name": s.name} for s in project.config.stages]
    elif args.config:
        stages = [{"key": s.key, "name": s.name} for s in load_config_file(args.config).stages]
    written = write_profile(src, out, stages, args.workbook_key)   # read-only; the workbook is never written
    print(f"Profile:          {written['profile_md']}")
    print(f"Profile (JSON):   {written['profile_json']}")
    print(f"Starter mapping:  {written['mapping']}")
    print("Every inferred entry in the starter mapping is marked 'confirmed: false'. Review each entry, correct "
          "it where needed and set 'confirmed: true'; the importer refuses to run until required entries are "
          "confirmed.")
    if not stages:
        print("No project or configuration was given, so stage names could not be matched to sheets or columns.")
    return 0


def cmd_import_workbook(args: argparse.Namespace) -> int:
    from .workbook.importer import format_reconciliation, import_workbook
    from .workbook.mapping import load_mapping

    with _open(args) as project:
        mapping = load_mapping(args.mapping)
        result = import_workbook(project, Path(args.workbook).expanduser(), mapping, declare=args.declare,
                                 actor=operator(args, project))
        if args.json:
            emit(args, {"status": result.status, "run_id": result.run_id, "stats": result.stats})
        else:
            print(_banner(project))
            if result.status == "already_imported":
                print("This exact workbook file was already imported with this mapping; nothing was changed. "
                      "Counts from that import:")
            print(format_reconciliation(result.stats, with_definitions=True))
    return 0


# ------------------------------------------------------------------ protocol
def cmd_protocol(args: argparse.Namespace) -> int:
    if args.action == "template":
        text = protocol.template_text()
        if args.file:
            dest = Path(args.file).expanduser()
            if dest.exists():
                raise GeodesicError(f"{dest} already exists; choose another file name.")
            dest.write_text(text, encoding="utf-8")
            print(f"Wrote the empty protocol template to {dest}. Fill it in yourself; GEODESIC does not draft "
                  "criteria.")
        else:
            print(text)
        return 0
    with _open(args) as project:
        conn = project.conn
        if args.action == "show":
            pv = protocol.current(conn)
            print(_banner(project))
            print(protocol.dump_protocol(pv.protocol))
            problems = pv.protocol.completeness_problems()
            if not pv.is_confirmed:
                print("# PROVISIONAL: eligibility decisions cannot be finalised and automated exclusions are "
                      "disabled until this protocol is confirmed.")
                for p in problems:
                    print(f"# incomplete: {p}")
        elif args.action == "export":
            dest = Path(args.file).expanduser()
            dest.write_text(protocol.dump_protocol(protocol.current(conn).protocol), encoding="utf-8")
            print(f"Wrote protocol {protocol.current(conn).label} to {dest}")
        elif args.action == "load":
            new = protocol.load_protocol_file(Path(args.file).expanduser())
            pv = protocol.save_new_version(conn, new, reviewer(args, project), args.summary or "")
            print(f"Protocol is now {pv.label}. Confirm it with 'geodesic protocol confirm' when it is final.")
        elif args.action == "confirm":
            pv = protocol.confirm(conn, reviewer(args, project), project.data_class)
            print(f"Protocol confirmed: {pv.label}")
        elif args.action == "history":
            for pv in protocol.list_versions(conn):
                print(f"{pv.label}  created {pv.created_at} by {pv.created_by}"
                      + (f"  confirmed {pv.confirmed_at} by {pv.confirmed_by}" if pv.confirmed_at else "")
                      + (f"  — {pv.change_summary}" if pv.change_summary else ""))
    return 0


# ------------------------------------------------------------------ findings / papers
def finding_filter(args: argparse.Namespace) -> query.FindingFilter:
    """The finding filter described by the options of :func:`filter_args`."""
    return query.FindingFilter(
        text=args.text, stage_keys=_csv_list(args.stage), kinds=_csv_list(args.kind),
        statement_types=_csv_list(args.statement_type), quantitative=_tristate(args.quantitative),
        observation_types=_csv_list(args.observation_type), system_context=args.system, outcome=args.outcome,
        measurement_method=args.method, has_timepoint=_tristate(args.has_timepoint),
        has_uncertainty=_tristate(args.has_uncertainty), review_statuses=_csv_list(args.review_status),
        paper_ids=_csv_list(args.paper), year_min=args.year_min, year_max=args.year_max,
        flags=_csv_list(args.flag), paper_text=args.paper_text,
        screening_stage=args.screening_stage, screening_outcomes=_csv_list(args.screening_outcome))


def filter_args(p: argparse.ArgumentParser) -> None:
    """Finding-filter options shared by ``findings``, ``export``, ``map``, ``missing``, ``figures`` and ``report``."""
    g = p.add_argument_group("finding filter")
    g.add_argument("--text", help="full-text search over wording and context (all words; word* = prefix)")
    g.add_argument("--paper-text", help="full-text search over paper title/citation/authors")
    g.add_argument("--stage", help="comma-separated stage keys (any)")
    g.add_argument("--kind", help="measurement,narrative,limitation")
    g.add_argument("--statement-type", help="exact,approximate,range,bound,qualitative,unparsed")
    g.add_argument("--quantitative", choices=["yes", "no"])
    g.add_argument("--observation-type", help="e.g. direct,proxy")
    g.add_argument("--system", help="substring of the system/context")
    g.add_argument("--outcome", help="substring of the outcome")
    g.add_argument("--method", help="substring of the measurement method")
    g.add_argument("--has-timepoint", choices=["yes", "no"])
    g.add_argument("--has-uncertainty", choices=["yes", "no"])
    g.add_argument("--review-status", help="unreviewed,draft,verified,rejected,stale")
    g.add_argument("--flag", help="comma-separated review flags (any)")
    g.add_argument("--paper", help="comma-separated paper IDs")
    g.add_argument("--year-min", type=int)
    g.add_argument("--year-max", type=int)
    g.add_argument("--screening-stage", choices=["title_abstract", "full_text"])
    g.add_argument("--screening-outcome", help="include,exclude,uncertain,conflict,unscreened")


def cmd_findings(args: argparse.Namespace) -> int:
    with _open(args) as project:
        flt = finding_filter(args)
        page = query.search_findings(project.conn, flt, limit=args.limit, offset=args.offset, with_locations=True)
        if args.json:
            emit(args, {"total": page.total, "offset": page.offset, "rows": page.rows})
            return 0
        from .export.tables import finding_row

        print(_banner(project))
        print(f"{page.total} finding(s) match; showing {len(page.rows)} from offset {page.offset}")
        for f in page.rows:
            r = finding_row(f)
            print(f"\n{r['finding_id']}  [{r['review_status']}{'' if r['grounding_status'] == 'grounded' else ', ' + r['grounding_status']}]"
                  f"  {r['kind']}/{r['statement_type']}  stages: {r['stages'] or '(none)'}")
            print(f"  paper:   {r['citation']}  ({r['paper_id']})")
            print(f"  wording: {r['source_wording']}")
            if r["value_literal"]:
                bits = [f"value '{r['value_literal']}'", f"unit '{r['unit_original']}'" if r["unit_original"] else
                        "unit not stated", r["relative_semantics"] or ""]
                if r["timepoint_literal"]:
                    bits.append(f"time '{r['timepoint_literal']}'")
                if r["uncertainty_type"]:
                    bits.append(f"uncertainty {r['uncertainty_type']} '{r['uncertainty_literal']}'")
                if r["sample_size_literal"]:
                    bits.append(f"'{r['sample_size_literal']}'")
                print("  parsed:  " + ", ".join(b for b in bits if b))
            if r["qualifiers"]:
                print(f"  qualifiers: {r['qualifiers']}")
            if r["interpretation_limits"]:
                print(f"  limits:  {r['interpretation_limits']}")
            if r["review_flags"]:
                print(f"  flags:   {r['review_flags']}")
            print(f"  source:  {r['source_location']}")
    return 0


def cmd_papers(args: argparse.Namespace) -> int:
    with _open(args) as project:
        flt = query.PaperFilter(
            text=args.text, stage_keys=_csv_list(args.stage), year_min=args.year_min, year_max=args.year_max,
            resolution_statuses=_csv_list(args.resolution), screening_stage=args.screening_stage,
            screening_outcomes=_csv_list(args.screening_outcome), has_findings=_tristate(args.has_findings),
            origins=_csv_list(args.origin))
        page = query.search_papers(project.conn, flt, limit=args.limit, offset=args.offset)
        if args.json:
            emit(args, {"total": page.total, "offset": page.offset, "rows": page.rows})
            return 0
        from .papers import citation_label

        print(_banner(project))
        print(f"{page.total} paper(s) match; showing {len(page.rows)} from offset {page.offset}")
        for p in page.rows:
            ids = ", ".join(f"{k} {p[k]}" for k in ("doi", "pmid", "pmcid") if p[k]) or "no identifier"
            print(f"{p['id']}  {citation_label(p)}  [{p['resolution_status']}; identity {p['identity_status']}]  "
                  f"{ids}  findings: {p['n_findings']}  stages: {', '.join(p['stages']) or '(none)'}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import export_tables

    with _open(args) as project:
        res = export_tables(project, args.out, _csv_list(args.formats) or ["xlsx", "csv"],
                            flt=finding_filter(args), actor=operator(args, project))
        print(_banner(project))
        print(f"Export directory: {res['directory']}")
        for f in res["files"]:
            print(f"  {f.name}")
        print(f"  {res['manifest'].name}")
        print("Rows: " + ", ".join(f"{k}: {v}" for k, v in res["rows"].items()))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    with _open(args) as project:
        if args.action == "verify":
            ok, n, bad = audit.verify_chain(project.conn)
            print(f"{n} audit events; hash chain {'intact' if ok else f'BROKEN at seq {bad}'}")
            return 0 if ok else 2
        rows = audit.history(project.conn, entity_type=args.entity_type, entity_id=args.entity_id, limit=args.limit)
        emit(args, rows, "\n".join(
            f"{r['seq']:>6}  {r['ts']}  {r['actor']} ({r['actor_kind']})  {r['action']}  "
            f"{r['entity_type']}:{r['entity_id']}" for r in rows))
    return 0


# ------------------------------------------------------------------ parser
def _common(p: argparse.ArgumentParser, reviewer_opt: bool = True) -> None:
    p.add_argument("--project", "-p", help=f"project data directory, or a name inside $GEODESIC_HOME "
                                           f"(default: ${ENV_PROJECT})")
    if reviewer_opt:
        p.add_argument("--reviewer", help="reviewer name (default: $GEODESIC_REVIEWER or local configuration)")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geodesic", description="Traceable literature discovery and scientific evidence organisation.")
    parser.add_argument("--version", action="version", version=f"geodesic {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("init", help="create a project data directory")
    p.add_argument("--project", "-p", required=False)
    p.add_argument("--config", required=True, help="project configuration YAML (name, stages, ...)")
    p.add_argument("--data-class", required=True, choices=["synthetic", "real"],
                   help="fixed at creation and never changed")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="counts and audit-chain check")
    _common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reload-config", help="re-read project.yaml (stages, parser and search settings)")
    _common(p)
    p.set_defaults(func=cmd_reload_config)

    p = sub.add_parser("profile-workbook", help="profile a workbook and write a starter mapping (read-only)")
    p.add_argument("workbook")
    p.add_argument("--out", help="output directory (default: ./<workbook name>_profile)")
    p.add_argument("--project", "-p", help="project whose stage names should be matched")
    p.add_argument("--config", help="project configuration YAML to take stage names from (instead of --project)")
    p.add_argument("--workbook-key", default="main", help="name for this workbook within the project")
    p.set_defaults(func=cmd_profile_workbook)

    p = sub.add_parser("import-workbook", help="import a workbook through a confirmed mapping")
    p.add_argument("workbook")
    p.add_argument("--mapping", required=True)
    p.add_argument("--declare", choices=["synthetic", "real"],
                   help="provenance declaration for a workbook without an embedded synthetic tag")
    _common(p)
    p.set_defaults(func=cmd_import_workbook)

    p = sub.add_parser("protocol", help="show, export, load or confirm the protocol")
    p.add_argument("action", choices=["show", "template", "export", "load", "confirm", "history"])
    p.add_argument("file", nargs="?", help="protocol YAML file (template/export: destination; load: source)")
    p.add_argument("--summary", help="what changed (stored with the new version)")
    _common(p)
    p.set_defaults(func=cmd_protocol)

    p = sub.add_parser("findings", help="search and filter findings")
    filter_args(p)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    _common(p)
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("papers", help="search and filter papers")
    p.add_argument("--text")
    p.add_argument("--stage")
    p.add_argument("--year-min", type=int)
    p.add_argument("--year-max", type=int)
    p.add_argument("--resolution", help="resolved,unresolved")
    p.add_argument("--origin", help="workbook,search,file_import,manual")
    p.add_argument("--has-findings", choices=["yes", "no"])
    p.add_argument("--screening-stage", choices=["title_abstract", "full_text"])
    p.add_argument("--screening-outcome", help="include,exclude,uncertain,conflict,unscreened")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    _common(p)
    p.set_defaults(func=cmd_papers)

    p = sub.add_parser("export", help="write evidence tables (Excel workbook and CSV by default)")
    p.add_argument("--out", help="output directory (default: a new directory under the project's exports/)")
    p.add_argument("--formats", default="xlsx,csv", help="comma-separated: xlsx,csv,json,ris")
    filter_args(p)
    _common(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("audit", help="verify or list the append-only audit log")
    p.add_argument("action", choices=["verify", "list"])
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--limit", type=int, default=50)
    _common(p)
    p.set_defaults(func=cmd_audit)

    from . import cli_more

    cli_more.register(sub, _common)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except GeodesicError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
