"""Command-line commands for review, evidence hierarchy, validation and normalized/derived values."""

from __future__ import annotations

import argparse
from typing import Any, Callable

from . import findings as F
from .errors import ValidationError


def _fmt(x: Any) -> str:
    return "-" if x is None else f"{x:.6g}" if isinstance(x, float) else str(x)


def _derived_line(d: dict[str, Any]) -> str:
    value = _fmt(d["output_value"]) if d["output_value"] is not None else \
        f"{_fmt(d['output_low'])} to {_fmt(d['output_high'])}"
    state = "" if d["status"] == "current" else f"  [{d['status'].upper()}: {d['stale_reason'] or ''}]"
    return f"{d['id']}  {d['kind']}  {value} {d['output_unit']}  rule {d['rule_id']}  formula: {d['formula']}{state}"


# ------------------------------------------------------------------ review
def cmd_review(args: argparse.Namespace) -> int:
    from . import review
    from .cli import _banner, _csv_list, _open, emit, reviewer
    from .cli_docs import _finding_lines, parse_assignments, parse_spans, parse_statuses
    from .extraction import derived

    with _open(args) as project:
        conn = project.conn
        a = args.action
        fid = args.finding_id
        if a == "show":
            f = F.get_finding(conn, fid)
            lines = [_banner(project), *_finding_lines(f), "  history:"]
            for v in F.versions(conn, fid):
                lines.append(f"    version {v['version_no']}  {v['change_kind']}  by {v['created_by']} "
                             f"({v['actor_kind']})  {v['created_at']}" + (f"  note: {v['note']}" if v["note"] else ""))
            revs = F.reviews(conn, fid)
            for r in revs:
                checks = ", ".join(f"{k}: {v}" for k, v in r["checks"].items())
                lines.append(f"    review  {r['action']}  version {r['finding_version_no']}  by {r['reviewer']} "
                             f"({r['reviewer_kind']})  {r['created_at']}" + (f"  checks: {checks}" if checks else "")
                             + (f"  note: {r['note']}" if r["note"] else ""))
            dvs = derived.list_values(conn, finding_id=fid, include_stale=True)
            if dvs:
                lines.append("  normalized/derived values using this finding:")
                lines += ["    " + _derived_line(d) for d in dvs]
            emit(args, {**f, "versions": F.versions(conn, fid), "reviews": revs, "derived_values": dvs},
                 "\n".join(lines))
            return 0
        actor = reviewer(args, project)
        if a == "verify":
            checks: dict[str, str] = {}
            if args.all_confirmed:
                checks = {name: "confirmed" for name in review.REQUIRED_CHECKS}
            for pair in args.check or []:
                name, _, value = pair.partition("=")
                checks[name] = value
            rid = review.verify(conn, fid, actor, checks, note=args.note, acknowledge_flags=args.acknowledge_flags)
            f = F.get_finding(conn, fid)
            emit(args, {"review_id": rid, "finding_id": fid, "review_status": f["review_status"],
                        "version": f["current_version"]},
                 f"finding {fid} version {f['current_version']} verified against its source by {actor.name}"
                 f"{' [SIMULATED review action]' if actor.kind == 'simulated' else ''}")
        elif a == "reject":
            review.reject(conn, fid, actor, args.note)
            emit(args, {"finding_id": fid, "review_status": "rejected"}, f"finding {fid} rejected (kept in the record)")
        elif a == "reopen":
            review.reopen(conn, fid, actor, args.note)
            emit(args, {"finding_id": fid}, f"finding {fid} reopened for review")
        elif a == "comment":
            review.comment(conn, fid, actor, args.note)
            emit(args, {"finding_id": fid}, f"comment recorded on finding {fid}")
        elif a == "edit":
            changes = parse_assignments(args.set)
            statuses = parse_statuses(args.status_of)
            if statuses:
                changes["field_status"] = {**F.get_finding(conn, fid)["content"].get("field_status", {}), **statuses}
            spans = parse_spans(args.span) or None
            if not changes and not spans:
                raise ValidationError("Nothing to change: give --set, --status-of or --span.")
            out = review.edit(conn, fid, actor, changes, spans=spans, note=args.note)
            text = (f"finding {fid} is now version {out['version_no']} ({out['grounding_status']}); changed: "
                    f"{', '.join(out['changed_fields']) or 'spans only'}. Earlier verification no longer applies.")
            if out["grounding_problems"]:
                text += "\n  grounding problems: " + "; ".join(
                    f"{p['field']}: {p['reason']}" for p in out["grounding_problems"])
            emit(args, out, text)
        elif a == "stages":
            keys = review.assign_stages(conn, fid, actor, add=_csv_list(args.add), remove=_csv_list(args.remove),
                                        note=args.note)
            emit(args, {"finding_id": fid, "stages": keys}, f"finding {fid} stages: {', '.join(keys) or '(none)'}")
        elif a == "relate":
            rid = review.relate(conn, fid, args.other_id, args.type, actor, note=args.note)
            emit(args, {"relation_id": rid}, f"relation recorded: {fid} {args.type} {args.other_id}")
    return 0


# ------------------------------------------------------------------ hierarchy
def cmd_hierarchy(args: argparse.Namespace) -> int:
    from .cli import _banner, _csv_list, _open, emit, reviewer
    from .extraction import hierarchy

    with _open(args) as project:
        conn = project.conn
        a = args.action
        if a == "tree":
            t = hierarchy.tree(conn, args.paper)
            lines = [_banner(project)]
            for s in t["studies"]:
                lines.append(f"study {s['id']}  {s['label']}  reported by: {', '.join(s.get('papers') or []) or '-'}")
            for e in t["experiments"]:
                lines.append(f"experiment {e['id']}  {e['label']}  paper {e['paper_id']}  study {e['study_id'] or '-'}  "
                             f"findings: {e['n_findings']}")
                for g in e["groups"]:
                    lines.append(f"    group {g['id']}  {g['label']}  role: {g['role'] or '-'}")
            lines.append(f"findings not assigned to any study, experiment or group: {t['unassigned_findings']}")
            emit(args, t, "\n".join(lines))
            return 0
        actor = reviewer(args, project)
        if a == "study-add":
            out = hierarchy.create_study(conn, actor, args.label, description=args.description, design=args.design,
                                         paper_ids=_csv_list(args.papers))
            emit(args, out, f"study {out['id']} created: {out['label']}")
        elif a == "study-link":
            out = hierarchy.link_study_report(conn, args.study_id, args.paper_id, actor, note=args.note,
                                              active=not args.unlink)
            emit(args, out, f"study {args.study_id} {'no longer ' if args.unlink else ''}reported by paper {args.paper_id}")
        elif a == "experiment-add":
            out = hierarchy.create_experiment(conn, actor, args.paper_id, args.label, study_id=args.study,
                                              description=args.description, system_context=args.system_context,
                                              design=args.design)
            emit(args, out, f"experiment {out['id']} created: {out['label']}")
        elif a == "group-add":
            out = hierarchy.create_group(conn, actor, args.experiment_id, args.label, role=args.role,
                                         description=args.description)
            emit(args, out, f"comparison group {out['id']} created: {out['label']}")
        elif a == "assign":
            out = hierarchy.assign(conn, args.finding_id, actor, study_id=args.study, experiment_id=args.experiment,
                                   comparison_group_id=args.group, clear=args.clear, note=args.note)
            emit(args, out, f"finding {args.finding_id}: study {out['study_id'] or '-'}, experiment "
                            f"{out['experiment_id'] or '-'}, group {out['comparison_group_id'] or '-'}")
        elif a == "remove":
            hierarchy.remove(conn, args.level, args.record_id, actor, args.reason)
            emit(args, {"removed": args.record_id}, f"{args.level} {args.record_id} marked removed (nothing deleted)")
    return 0


# ------------------------------------------------------------------ validation
def cmd_validate(args: argparse.Namespace) -> int:
    from .cli import _banner, _open, emit, operator
    from .extraction import validation

    with _open(args) as project:
        conn = project.conn
        if args.action == "run":
            out = validation.run_validation(project, operator(args, project))
        else:
            out = validation.summary(conn, args.run)
            if out["run_id"] is None:
                emit(args, out, out["statement"] + " Run: geodesic validate run")
                return 0
        lines = [_banner(project), out["statement"],
                 f"run {out['run_id']} at {out['created_at']} (rules {out['rules_version']}): {out['n_findings']} "
                 f"finding(s) checked, {out['n_issues']} issue(s) on {out['findings_with_issues']} finding(s); "
                 f"{out['findings_with_errors']} finding(s) with errors",
                 "by severity: " + (", ".join(f"{k} {v}" for k, v in out["by_severity"].items()) or "none")]
        lines += [f"  {code}: {n}" for code, n in out["by_code"].items()]
        data: Any = out
        if args.action == "show":
            rows = validation.issues(conn, out["run_id"], code=args.code, severity=args.severity,
                                     finding_id=args.finding, limit=args.limit)
            lines.append(f"{len(rows)} issue(s) listed:")
            lines += [f"  [{r['severity']}] {r['finding_id']}  {r['code']}"
                      f"{' (' + r['field'] + ')' if r['field'] else ''}: {r['message']}" for r in rows]
            data = {"summary": out, "issues": rows}
        emit(args, data, "\n".join(lines))
        return 4 if args.action == "run" and args.strict and out["findings_with_errors"] else 0


# ------------------------------------------------------------------ normalized / derived values
def cmd_derived(args: argparse.Namespace) -> int:
    from .cli import _banner, _open, emit, reviewer
    from .extraction import derived
    from .quantities import units

    if args.action == "units":
        rows = units.unit_table_rows()
        emit(args, rows, "\n".join(
            ["Explicit unit table (conversion = value × factor to the base unit of the dimension):",
             *[f"  {r['symbol']:<12} {r['dimension']:<22} base {str(r['base_unit']):<8} factor "
               f"{r['factor_to_base']}{'  [ambiguous: ' + (r['note'] or 'needs review') + ']' if r['ambiguous'] else ''}"
               for r in rows]]))
        return 0
    with _open(args) as project:
        conn = project.conn
        a = args.action
        if a == "normalize":
            d = derived.normalize_finding(project, args.finding_id, reviewer(args, project), target_unit=args.to,
                                          field=args.field)
            emit(args, d, ("already present: " if d.get("already_present") else "stored: ") + _derived_line(d)
                 + "\n  The transcribed value and unit of the finding are unchanged.")
        elif a == "compute":
            d = derived.derive(project, args.rule, [args.finding_a, args.finding_b], reviewer(args, project))
            emit(args, d, "stored: " + _derived_line(d) + f"\n  carried: {d['carries']}")
        elif a == "list":
            rows = derived.list_values(conn, finding_id=args.finding, kind=args.kind, include_stale=args.all,
                                       limit=args.limit)
            emit(args, rows, "\n".join([_banner(project), f"{len(rows)} value(s)", *map(_derived_line, rows)]))
        elif a == "trace":
            t = derived.trace(conn, args.derived_id)
            lines = [_banner(project), _derived_line(t["derived_value"]),
                     f"traceable to stored source text: {'yes' if t['traceable'] else 'NO'}; all inputs at their "
                     f"current version: {'yes' if t['all_inputs_current'] else 'NO'}"]
            for s in t["steps"]:
                lines.append(f"  input {s['finding_id']} v{s['version_used']} (current v{s['current_version']}, "
                             f"{s['review_status_now']})  {s['field']}: {s['source_literal']!r} "
                             f"{s['unit'] or ''}".rstrip())
                lines.append(f"    at {s['address']}")
                span = s["literal_span"]
                where = f"chars {span[1]}-{span[2]}" if isinstance(span, (list, tuple)) and len(span) == 3 else \
                    f"chars {span[0]}-{span[1]}" if span else "no recorded span"
                lines.append(f"    stored text at the literal's span ({where}): {s['stored_text_at_span']!r}"
                             f"{'' if s['literal_found_at_span'] else '   ** DIFFERS **'}")
                lines.append(f"    paper: {s['paper']}")
                if s["problem"]:
                    lines.append(f"    problem: {s['problem']}")
            emit(args, t, "\n".join(lines))
        elif a == "withdraw":
            derived.withdraw(conn, args.derived_id, reviewer(args, project), args.reason)
            emit(args, {"withdrawn": args.derived_id}, f"{args.derived_id} withdrawn (kept in the record)")
    return 0


def register(sub: argparse._SubParsersAction, common: Callable[..., None]) -> None:
    from . import review as review_mod

    p = sub.add_parser("review", help="inspect, verify, reject, edit or annotate a finding")
    rs = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = rs.add_parser("show", help="a finding with its source locations, history and derived values")
    q.add_argument("finding_id")
    common(q, False)
    q = rs.add_parser("verify", help="record that a reviewer checked the finding against its source")
    q.add_argument("finding_id")
    q.add_argument("--check", action="append", metavar="NAME=confirmed|not_applicable",
                   help=f"one of: {', '.join(review_mod.REQUIRED_CHECKS)} (repeatable; all are required)")
    q.add_argument("--all-confirmed", action="store_true", help="state that every required check was confirmed")
    q.add_argument("--acknowledge-flags", action="store_true",
                   help="verify although ambiguity flags are open (they are recorded as acknowledged)")
    q.add_argument("--note")
    common(q)
    for name, hlp in (("reject", "mark a finding rejected (kept, excluded from evidence views)"),
                      ("reopen", "return a verified or rejected finding to unreviewed"),
                      ("comment", "add a reviewer comment")):
        q = rs.add_parser(name, help=hlp)
        q.add_argument("finding_id")
        q.add_argument("--note", required=name != "reopen")
        common(q)
    q = rs.add_parser("edit", help="correct fields of a finding (creates a new version; needs re-verification)")
    q.add_argument("finding_id")
    q.add_argument("--set", action="append", metavar="FIELD=VALUE")
    q.add_argument("--span", action="append", metavar="FIELD=START:END")
    q.add_argument("--status-of", action="append", metavar="FIELD=STATUS")
    q.add_argument("--note")
    common(q)
    q = rs.add_parser("stages", help="add or remove stage assignments")
    q.add_argument("finding_id")
    q.add_argument("--add", help="comma-separated stage keys")
    q.add_argument("--remove", help="comma-separated stage keys")
    q.add_argument("--note")
    common(q)
    q = rs.add_parser("relate", help="record that two findings are possible duplicates, dependent, or limit one another")
    q.add_argument("finding_id")
    q.add_argument("other_id")
    q.add_argument("--type", choices=["possible_duplicate", "dependent", "limited_by"], required=True)
    q.add_argument("--note")
    common(q)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("hierarchy", help="studies, experiments and comparison groups (all optional)")
    hs = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = hs.add_parser("tree", help="show the hierarchy")
    q.add_argument("--paper")
    common(q, False)
    q = hs.add_parser("study-add", help="describe a study (reviewer-written, never inferred)")
    q.add_argument("label")
    q.add_argument("--description")
    q.add_argument("--design")
    q.add_argument("--papers", help="comma-separated IDs of papers reporting the study")
    common(q)
    q = hs.add_parser("study-link", help="record that a paper reports a study")
    q.add_argument("study_id")
    q.add_argument("paper_id")
    q.add_argument("--unlink", action="store_true")
    q.add_argument("--note")
    common(q)
    q = hs.add_parser("experiment-add", help="describe an experiment reported by a paper")
    q.add_argument("paper_id")
    q.add_argument("label")
    q.add_argument("--study")
    q.add_argument("--description")
    q.add_argument("--system-context")
    q.add_argument("--design")
    common(q)
    q = hs.add_parser("group-add", help="describe a comparison group of an experiment")
    q.add_argument("experiment_id")
    q.add_argument("label")
    q.add_argument("--role")
    q.add_argument("--description")
    common(q)
    q = hs.add_parser("assign", help="assign a finding to a study, experiment or comparison group")
    q.add_argument("finding_id")
    q.add_argument("--study")
    q.add_argument("--experiment")
    q.add_argument("--group")
    q.add_argument("--clear", action="store_true")
    q.add_argument("--note")
    common(q)
    q = hs.add_parser("remove", help="mark a study, experiment or group removed")
    q.add_argument("level", choices=["study", "experiment", "group"])
    q.add_argument("record_id")
    q.add_argument("--reason", required=True)
    common(q)
    p.set_defaults(func=cmd_hierarchy)

    p = sub.add_parser("validate", help="run the deterministic consistency checks over all findings")
    vs = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = vs.add_parser("run", help="check every active finding and store the result as a new validation run")
    q.add_argument("--strict", action="store_true", help="exit with status 4 when any finding has an error")
    common(q)
    q = vs.add_parser("show", help="issues of the latest (or a named) validation run")
    q.add_argument("--run")
    q.add_argument("--code")
    q.add_argument("--severity", choices=["info", "warning", "error"])
    q.add_argument("--finding")
    q.add_argument("--limit", type=int, default=100)
    common(q, False)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("derived", help="normalized and derived values (stored beside, never over, transcribed values)")
    xs = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = xs.add_parser("normalize", help="convert a finding's value (or timepoint) with the explicit unit table")
    q.add_argument("finding_id")
    q.add_argument("--to", help="target unit (default: base unit of the dimension)")
    q.add_argument("--field", choices=["value", "timepoint"], default="value")
    common(q)
    q = xs.add_parser("compute", help="difference (A − B) or ratio (A / B) of two point values")
    q.add_argument("rule", choices=["difference", "ratio"])
    q.add_argument("finding_a")
    q.add_argument("finding_b")
    common(q)
    q = xs.add_parser("list", help="computed values with their rule and formula")
    q.add_argument("--finding")
    q.add_argument("--kind", choices=["normalized", "derived"])
    q.add_argument("--all", action="store_true", help="include stale and withdrawn values")
    q.add_argument("--limit", type=int)
    common(q, False)
    q = xs.add_parser("trace", help="follow a value back to the stored source text of each input")
    q.add_argument("derived_id")
    common(q, False)
    q = xs.add_parser("withdraw", help="withdraw a computed value (kept, excluded from tables)")
    q.add_argument("derived_id")
    q.add_argument("--reason", required=True)
    common(q)
    q = xs.add_parser("units", help="print the explicit unit-conversion table")
    q.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_derived)

    from . import cli_reports

    cli_reports.register(sub, common)
