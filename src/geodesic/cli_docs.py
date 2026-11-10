"""Command-line commands for documents, candidate values and manual extraction.

Every command calls the same public functions as the dashboard
(:mod:`geodesic.documents`, :mod:`geodesic.extraction`).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from . import findings as F, locations
from .errors import ValidationError

_FLOAT_FIELDS = ("value_num", "value_low", "value_high", "timepoint_value", "uncertainty_value", "uncertainty_low",
                 "uncertainty_high", "confidence_level")
_INT_FIELDS = ("sample_size",)


def parse_assignments(pairs: list[str] | None) -> dict[str, Any]:
    """``field=value`` options into finding content. ``field=`` or ``field=null`` clears a field."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValidationError(f"Expected field=value, got {pair!r}.")
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in F.CONTENT_KEYS or key == "field_status":
            raise ValidationError(f"Unknown finding field {key!r}. Fields: "
                                  f"{', '.join(k for k in F.CONTENT_KEYS if k != 'field_status')}.")
        if value in ("", "null", "None"):
            out[key] = None
        elif key in _FLOAT_FIELDS or key in _INT_FIELDS:
            try:
                out[key] = int(value) if key in _INT_FIELDS else float(value)
            except ValueError:
                raise ValidationError(f"{key} needs a number, got {value!r}.") from None
        elif key == "qualifiers":
            try:
                out[key] = json.loads(value)
            except ValueError:
                raise ValidationError("qualifiers needs a JSON list.") from None
        else:
            out[key] = value
    return out


def parse_statuses(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        if key not in F.STATUS_FIELDS or value not in F.FIELD_STATUSES:
            raise ValidationError(f"Expected FIELD=STATUS with FIELD in ({', '.join(F.STATUS_FIELDS)}) and STATUS in "
                                  f"({', '.join(F.FIELD_STATUSES)}); got {pair!r}.")
        out[key] = value
    return out


def parse_spans(pairs: list[str] | None) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        try:
            start, end = (int(v) for v in value.split(":"))
        except ValueError:
            raise ValidationError(f"Expected FIELD=START:END (character offsets in the unit text), got {pair!r}.") from None
        if key not in F.LITERAL_FIELDS:
            raise ValidationError(f"Spans are given for transcribed literals ({', '.join(F.LITERAL_FIELDS)}).")
        out[key] = [start, end]
    return out


def _location_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--snapshot", required=True, help="document snapshot ID, or a document ID (its latest snapshot)")
    p.add_argument("--unit", help="text unit key: PDF page index (0 = first page) or XML element key")
    p.add_argument("--table", help="table key (with --row and --col) for a table cell")
    p.add_argument("--row", type=int)
    p.add_argument("--col", type=int)
    p.add_argument("--quote", help="exact text of the supporting excerpt (must occur once in the unit)")
    p.add_argument("--start", type=int, help="start offset of the excerpt in the unit text (0-based)")
    p.add_argument("--end", type=int, help="end offset (exclusive)")


def _location(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    from .extraction import manual

    return manual.build_location(conn, args.snapshot, unit_key=args.unit, table_key=args.table, row_idx=args.row,
                                 col_idx=args.col, char_start=args.start, char_end=args.end, quote=args.quote)


def _finding_lines(f: dict[str, Any]) -> list[str]:
    c = f["content"]
    lines = [f"{f['id']}  [{f['review_status']}, {f['grounding_status']}]  {c['kind']}/{c['statement_type']}  "
             f"origin: {f['origin']}  version {f['current_version']}",
             f"  wording: {c['source_wording']}"]
    shown = {k: v for k, v in c.items() if k not in ("kind", "statement_type", "source_wording", "field_status",
                                                     "qualifiers") and v not in (None, "", [], {})}
    if shown:
        lines.append("  fields:  " + ", ".join(f"{k}={v!r}" for k, v in shown.items()))
    if c.get("qualifiers"):
        lines.append("  qualifiers: " + ", ".join(q.get("text", "") for q in c["qualifiers"]))
    if c.get("field_status"):
        lines.append("  missing-information status: " + ", ".join(f"{k}: {v}" for k, v in sorted(c["field_status"].items())))
    if f["review_flags"]:
        lines.append("  flags:   " + ", ".join(f["review_flags"]))
    stages = [s["key"] for s in f["stages"] if s["status"] == "active"]
    lines.append("  stages:  " + (", ".join(dict.fromkeys(stages)) or "(none)"))
    for loc in f["locations"]:
        lines.append(f"  source [{loc['role']}]: {locations.describe(loc)}"
                     f"{'' if loc['resolves'] else '  ** DOES NOT RESOLVE **'}")
    return lines


# ------------------------------------------------------------------ documents
def cmd_documents(args: argparse.Namespace) -> int:
    from .cli import _banner, _open, emit, operator, reviewer
    from .documents import openaccess, store

    with _open(args) as project:
        conn = project.conn
        a = args.action
        if a == "add":
            out = store.add_document(
                project, args.paper_id, Path(args.file).expanduser(), operator(args, project), role=args.role,
                parent_document_id=args.parent, declare=args.declare, license=args.license,
                content_scope=args.content_scope, allow_identifier_mismatch=args.allow_identifier_mismatch)
            if out["already_present"]:
                snap = out["snapshot"]
                emit(args, out, f"{_banner(project)}\ndocument {out['document_id']} with this SHA-256 is already "
                                f"attached to paper {out['paper_id']}"
                                f"{' (it had been removed and is active again)' if out['reactivated'] else ''}; "
                                f"latest snapshot {snap['id']} (status {snap['status']}). Nothing was added.")
                return 0
            snap = out["snapshot"]
            lines = [_banner(project),
                     f"document {out['document_id']} ({out['format']}, {out['role']}) attached to paper "
                     f"{out['paper_id']}  [declared {out['declared_data_class']}]",
                     f"  sha256 {out['sha256']}  ({out['size_bytes']} bytes; stored copy: {out['stored_path']})",
                     f"  snapshot {snap['snapshot_id']}: status {snap['status']}, {snap['n_units']} text unit(s), "
                     f"{snap['n_chars']} characters, {snap['n_tables']} table(s)  [{snap['parser']} {snap['parser_version']}]"]
            if snap.get("failure_reason"):
                lines.append(f"  note: {snap['failure_reason']}")
            chk = out["identifier_check"]
            lines.append(f"  identifier check: {chk['outcome']}"
                         + (f" ({', '.join(chk['agreeing_schemes'])})" if chk.get("agreeing_schemes") else "")
                         + (f"  differing: {chk['differing']}" if chk.get("differing") else ""))
            if out["also_attached_to"]:
                lines.append(f"  note: the same file is also attached to {', '.join(out['also_attached_to'])}")
            lines.append(f"  license: {out['license'] or 'not supplied'}")
            lines.append(f"  full-text availability of the paper: {out['fulltext_availability']}")
            lines += [f"  note: {n}" for n in out.get("notes") or []]
            emit(args, out, "\n".join(lines))
        elif a == "list":
            rows = store.list_documents(conn, args.paper_id, include_group=bool(args.paper_id))
            lines = [_banner(project), f"{len(rows)} document(s)"]
            for d in rows:
                lines.append(f"{d['id']}  paper {d['paper_id']}  {d['role']}/{d['format']}  [{d['record_status']}]  "
                             f"snapshot {d['snapshot_id']} ({d['snapshot_status']}; {d['n_units']} units, "
                             f"{d['n_tables']} tables)  source: {d['source']}  license: {d['license'] or '-'}")
            emit(args, rows, "\n".join(lines))
        elif a == "show":
            sid = store.resolve_snapshot(conn, args.ref)
            info = store.snapshot_info(conn, sid)
            units = store.units(conn, sid)
            tables = store.tables(conn, sid)
            if args.unit is not None:
                unit = next((u for u in units if u["unit_key"] == args.unit), None)
                if unit is None:
                    raise ValidationError(f"Snapshot {sid} has no text unit {args.unit!r}.")
                emit(args, unit, f"[{unit['label'] or unit['unit_key']}] {unit['section_path'] or ''} "
                                 f"({len(unit['text'])} characters, status {unit['extraction_status']})\n{unit['text']}")
                return 0
            lines = [_banner(project),
                     f"snapshot {sid} (no. {info['snapshot_no']}) of document {info['document_id']}  paper "
                     f"{info['paper_id']}  {info['format']}  status {info['status']}"
                     f"{'  — ' + info['failure_reason'] if info['failure_reason'] else ''}",
                     f"parser {info['parser']} {info['parser_version']}; OCR applied: "
                     f"{'yes' if info['ocr_applied'] else 'no'}", "text units (key | label | section | characters):"]
            for u in units:
                state = "" if u["extraction_status"] == "ok" else f"  [{u['extraction_status']}]"
                lines.append(f"  {u['unit_key']} | {u['label'] or ''} | {u['section_path'] or ''} | "
                             f"{len(u['text'])}{state}")
            for t in tables:
                lines.append(f"table {t['table_key']} | {t['label'] or ''} | {t['n_rows']} rows x {t['n_cols']} columns"
                             f" | {t['caption'] or ''}")
                if args.tables:
                    for c in t["cells"]:
                        lines.append(f"    r{c['row_idx']} c{c['col_idx']}{' (header)' if c['is_header'] else ''}: {c['text']}")
            emit(args, {"snapshot": info, "units": [{k: v for k, v in u.items() if k != 'text'} | {"n_chars": len(u["text"])}
                                                    for u in units], "tables": tables}, "\n".join(lines))
        elif a == "identify":
            out = store.identify(project, Path(args.file).expanduser())
            lines = [f"{out['file']}: {out['format']}, parse status {out['parse_status']}",
                     f"identifiers found in the file: {out['identifiers_in_document'] or 'none'}"]
            for m in out.get("matching_papers") or []:
                lines.append(f"  matches paper {m['paper_id']}  {m['label']}  via {', '.join(m['matched'])}")
            if not out.get("matching_papers"):
                lines.append("  no paper record carries these identifiers; choose the paper yourself when adding the file")
            emit(args, out, "\n".join(lines))
        elif a == "reparse":
            out = store.reparse(project, args.ref, operator(args, project))
            emit(args, out, f"new snapshot {out['snapshot_id']} (no. {out['snapshot_no']}): status {out['status']}; "
                            f"text identical to the previous snapshot: {out['text_identical_to_previous']}")
        elif a == "remove":
            out = store.remove_document(conn, args.ref, reviewer(args, project), args.reason)
            emit(args, out, f"document {args.ref} marked removed (nothing deleted); "
                            f"{out['findings_citing_document']} finding(s) cite it")
        elif a == "unavailable":
            out = store.record_fulltext_unavailable(conn, args.paper_id, reviewer(args, project), args.status, args.note)
            emit(args, out, f"paper {args.paper_id}: full text recorded as {args.status}")
        elif a == "status":
            out = store.fulltext_status(conn, args.paper_id)
            lines = [f"paper {args.paper_id}: {out['availability']} — {out['statement']}"]
            lines += [f"  problem: {p}" for p in out["problems"]]
            emit(args, out, "\n".join(lines))
        elif a == "find":
            sid = store.resolve_snapshot(conn, args.ref)
            hits = store.find_text(conn, sid, args.text, limit=args.limit)
            lines = [f"{len(hits)} occurrence(s) in snapshot {sid}"]
            for h in hits:
                lines.append(f"  {locations.describe(h)}\n      …{h['context']}…")
            emit(args, hits, "\n".join(lines))
        elif a == "fetch-oa":
            if args.candidates:
                ids = openaccess.fetch_candidates(project, args.limit)
                emit(args, ids, "\n".join([f"{len(ids)} paper(s) reported open access with a PMCID and no usable "
                                           "document:", *[f"  {i}" for i in ids]]))
                return 0
            if not args.paper_id:
                raise ValidationError("Give a paper ID, or --candidates to list papers that can be tried.")
            transport = None
            if args.synthetic_fixture:
                from .synthetic.documents import fulltext_transport
                transport = fulltext_transport()
            out = openaccess.fetch_open_access(project, args.paper_id, operator(args, project), transport=transport)
            mode = "[SYNTHETIC fixture response]" if transport else "[LIVE retrieval]"
            if out["outcome"] == "retrieval_failed":
                text = f"{mode} retrieval failed and was recorded: {out['reason']}"
            else:
                snap = out["snapshot"]
                text = (f"{mode} {out['outcome']}: document {out['document_id']} from {out['url']}\n"
                        f"  license: {out['license'] or 'not supplied'}\n"
                        f"  snapshot {snap.get('snapshot_id') or snap.get('id')}: status {snap['status']}, "
                        f"{snap['n_units']} text unit(s), {snap['n_tables']} table(s)")
            emit(args, out, text)
            return 0 if out["outcome"] != "retrieval_failed" else 3
    return 0


# ------------------------------------------------------------------ candidates
def cmd_candidates(args: argparse.Namespace) -> int:
    from .cli import _banner, _csv_list, _open, emit, operator, reviewer
    from .extraction import candidates

    with _open(args) as project:
        conn = project.conn
        a = args.action
        if a == "detect":
            out = candidates.detect(project, args.ref, operator(args, project))
            text = (f"snapshot {out['snapshot_id']}: candidates were already detected ({out['candidates']})"
                    if out["already_detected"] else
                    f"snapshot {out['snapshot_id']}: {out['candidates']} candidate value(s) "
                    f"{out['by_location_kind']}; {out['with_unit']} with a unit; "
                    f"{out['parse_failures']} parse failure(s)\n"
                    "Candidates are pointers to numbers in the text, not findings. Promote or dismiss them in review.")
            emit(args, out, text)
        elif a == "list":
            from .documents import store
            sid = store.resolve_snapshot(conn, args.snapshot) if args.snapshot else None
            rows = candidates.list_candidates(conn, snapshot_id=sid, paper_id=args.paper,
                                              status=None if args.status == "all" else args.status,
                                              with_unit_only=args.with_unit, limit=args.limit, offset=args.offset)
            lines = [_banner(project), f"{len(rows)} candidate(s) shown; totals {candidates.counts(conn, sid)}"]
            for c in rows:
                q = c["parsed"].get("quantity", {})
                head = c["parsed"].get("column_header")
                lines.append(f"{c['id']}  [{c['status']}]  {c['literal']!r}  {c['address']}"
                             + (f"  column: {head!r}" if head else "")
                             + (f"  flags: {', '.join(q['flags'])}" if q.get("flags") else ""))
                lines.append(f"    context: {c['context_excerpt']}")
            emit(args, rows, "\n".join(lines))
        elif a == "show":
            prop = candidates.proposal(project, args.candidate_id)
            c = prop["candidate"]
            lines = [f"{c['id']}  [{c['status']}]  {c['literal']!r}  {c['address']}",
                     f"  context: {c['context_excerpt']}",
                     f"  draft reading (not stored): {prop['summary']}",
                     "  fields: " + ", ".join(f"{k}={v!r}" for k, v in prop["content"].items()
                                              if v not in (None, "", [], {}) and k != "field_status"),
                     f"  flags: {', '.join(prop['flags']) or '-'}"]
            emit(args, prop, "\n".join(lines))
        elif a == "promote":
            changes = parse_assignments(args.set)
            statuses = parse_statuses(args.status_of)
            if statuses:
                changes["field_status"] = statuses
            f = candidates.promote(project, args.candidate_id, reviewer(args, project), changes=changes,
                                   stage_keys=_csv_list(args.stage), note=args.note)
            emit(args, f, "\n".join(["finding created from the candidate (not yet verified):", *_finding_lines(f)]))
        elif a == "dismiss":
            n = candidates.dismiss(conn, args.candidate_ids, reviewer(args, project), args.note)
            emit(args, {"dismissed": n}, f"{n} candidate(s) dismissed (kept in the record)")
    return 0


# ------------------------------------------------------------------ manual extraction
def cmd_extract(args: argparse.Namespace) -> int:
    from .cli import _banner, _csv_list, _open, emit, reviewer
    from .extraction import manual

    with _open(args) as project:
        conn = project.conn
        loc = _location(args, conn)
        props = manual.propose_from_span(project, loc)
        if args.action == "propose":
            lines = [_banner(project), f"location: {locations.describe(loc)}", f"excerpt:  {loc['excerpt']}",
                     "deterministic reading of the excerpt (drafts; nothing is stored):"]
            for i, pr in enumerate(props, 1):
                lines.append(f"  [{i}] {pr['summary']}" + (f"   flags: {', '.join(pr['flags'])}" if pr["flags"] else ""))
                lines.append("      spans: " + ", ".join(f"{k}={v[0]}:{v[1]}" for k, v in pr["spans"].items()))
            emit(args, {"location": loc, "proposals": props}, "\n".join(lines))
            return 0
        content, spans, flags = manual.draft_from_proposal(
            props, quantity=args.quantity, changes=parse_assignments(args.set), kind=args.kind,
            statuses=parse_statuses(args.status_of), spans=parse_spans(args.span))
        f = manual.create_finding(project, args.paper_id, reviewer(args, project), location=loc, content=content,
                                  spans=spans, stage_keys=_csv_list(args.stage), flags=flags, note=args.note)
        emit(args, f, "\n".join([_banner(project), "finding stored (unreviewed; verify it in review):",
                                 *_finding_lines(f)]))
    return 0


def register(sub: argparse._SubParsersAction, common: Callable[..., None]) -> None:
    """Add the document, extraction and review commands to the parser."""
    p = sub.add_parser("documents", help="attach, list, inspect and retrieve full-text documents")
    ds = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = ds.add_parser("add", help="attach a PDF or XML file to a paper (the file is copied, never modified)")
    q.add_argument("paper_id")
    q.add_argument("file")
    q.add_argument("--role", choices=["main", "supplementary"], default="main")
    q.add_argument("--parent", help="main document a supplementary file belongs to")
    q.add_argument("--declare", choices=["synthetic", "real"], help="provenance of an untagged file")
    q.add_argument("--license", help="license of the file, as supplied by its source")
    q.add_argument("--content-scope", choices=["full_text", "abstract_only", "unknown"], default="full_text")
    q.add_argument("--allow-identifier-mismatch", action="store_true",
                   help="attach although the identifiers inside the file differ from the paper's (recorded)")
    common(q)
    q = ds.add_parser("list", help="list documents (of one paper and its grouped records, or all)")
    q.add_argument("paper_id", nargs="?")
    common(q, False)
    q = ds.add_parser("show", help="text units and tables of a document snapshot; --unit prints one unit's text")
    q.add_argument("ref", help="snapshot ID or document ID")
    q.add_argument("--unit")
    q.add_argument("--tables", action="store_true", help="print table cells")
    common(q, False)
    q = ds.add_parser("identify", help="read the identifiers inside a file and list matching papers (changes nothing)")
    q.add_argument("file")
    common(q, False)
    q = ds.add_parser("reparse", help="parse a stored document again into a new snapshot")
    q.add_argument("ref", help="document ID")
    common(q)
    q = ds.add_parser("remove", help="mark a document removed (nothing is deleted)")
    q.add_argument("ref", help="document ID")
    q.add_argument("--reason", required=True)
    common(q)
    q = ds.add_parser("unavailable", help="record that full text could not be obtained for a paper")
    q.add_argument("paper_id")
    q.add_argument("--status", choices=["not_available", "abstract_only"], required=True)
    q.add_argument("--note", required=True, help="what was tried and where")
    common(q)
    q = ds.add_parser("status", help="full-text availability of a paper")
    q.add_argument("paper_id")
    common(q, False)
    q = ds.add_parser("find", help="find exact text in a snapshot and print ready-made source locations")
    q.add_argument("ref", help="snapshot ID or document ID")
    q.add_argument("text")
    q.add_argument("--limit", type=int, default=20)
    common(q, False)
    q = ds.add_parser("fetch-oa", help="retrieve open-access full-text XML through the Europe PMC API")
    q.add_argument("paper_id", nargs="?")
    q.add_argument("--candidates", action="store_true", help="list papers reported open access with a PMCID")
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--synthetic-fixture", action="store_true",
                   help="use the bundled synthetic full-text response (synthetic projects only)")
    common(q)
    p.set_defaults(func=cmd_documents)

    p = sub.add_parser("candidates", help="detect, list, promote or dismiss candidate numerical values")
    cs = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = cs.add_parser("detect", help="run the deterministic detector over a document snapshot")
    q.add_argument("ref", help="snapshot ID or document ID")
    common(q)
    q = cs.add_parser("list", help="list candidates")
    q.add_argument("--snapshot")
    q.add_argument("--paper")
    q.add_argument("--status", choices=["open", "promoted", "dismissed", "all"], default="open")
    q.add_argument("--with-unit", action="store_true", help="hide bare numbers in running text")
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--offset", type=int, default=0)
    common(q, False)
    q = cs.add_parser("show", help="the draft reading a promotion would store")
    q.add_argument("candidate_id")
    common(q, False)
    q = cs.add_parser("promote", help="create an unreviewed finding from a candidate")
    q.add_argument("candidate_id")
    q.add_argument("--set", action="append", metavar="FIELD=VALUE", help="set a descriptive field (repeatable)")
    q.add_argument("--status-of", action="append", metavar="FIELD=STATUS",
                   help="missing-information status, e.g. sample_size=not_reported (repeatable)")
    q.add_argument("--stage", help="comma-separated stage keys")
    q.add_argument("--note")
    common(q)
    q = cs.add_parser("dismiss", help="mark candidates as not being evidence")
    q.add_argument("candidate_ids", nargs="+")
    q.add_argument("--note")
    common(q)
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("extract", help="manual extraction from a document location")
    es = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    q = es.add_parser("propose", help="show the deterministic reading of an excerpt (stores nothing)")
    _location_args(q)
    common(q, False)
    q = es.add_parser("add", help="store a finding whose wording is the stored text at the location")
    q.add_argument("paper_id")
    _location_args(q)
    q.add_argument("--quantity", type=int, help="start from parsed quantity N of 'extract propose'")
    q.add_argument("--kind", choices=["measurement", "narrative", "limitation"])
    q.add_argument("--set", action="append", metavar="FIELD=VALUE", help="set a field (repeatable)")
    q.add_argument("--span", action="append", metavar="FIELD=START:END",
                   help="where a transcribed literal sits in the unit text (repeatable)")
    q.add_argument("--status-of", action="append", metavar="FIELD=STATUS",
                   help="missing-information status, e.g. timepoint=not_reported (repeatable)")
    q.add_argument("--stage", help="comma-separated stage keys")
    q.add_argument("--note")
    common(q)
    p.set_defaults(func=cmd_extract)

    from . import cli_review

    cli_review.register(sub, common)
