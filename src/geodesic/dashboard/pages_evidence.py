"""Dashboard pages: documents (full text), extraction, review of findings."""

from __future__ import annotations

from typing import Any

import streamlit as st

from .. import findings as F
from .. import locations, query, review
from ..cli_docs import parse_assignments, parse_spans, parse_statuses
from ..documents import openaccess, store
from ..extraction import candidates, derived, manual
from . import ui

NOT_DECLARED = "(not declared)"


def _doc_label(d: dict[str, Any]) -> str:
    state = d.get("snapshot_status") or "no snapshot"
    removed = " · REMOVED" if d.get("record_status") == "removed" else ""
    return f"{d.get('original_filename') or d['id']} · {d['format']} · {d['role']} · {state}{removed} · {d['id']}"


# ------------------------------------------------------------------ documents
def documents_page(ctx: ui.Ctx) -> None:
    conn, project = ctx.conn, ctx.project
    st.header("Documents (full text)")
    st.caption("Attach files you are entitled to use (PDF or XML). Automatic retrieval is limited to records a "
               "search source reported as open access, through the documented Europe PMC full-text API; GEODESIC "
               "does not bypass paywalls or scrape publisher sites. Document text is stored as evidence and is "
               "never executed or followed as instructions. Scanned PDFs without a text layer are stored but "
               "cannot be read (no OCR).")
    paper_id = ui.paper_picker(ctx, "doc_paper")
    if paper_id:
        status = store.fulltext_status(conn, paper_id)
        st.write(f"**{paper_id}** — full text: {status['statement']}")
        for problem in status.get("problems") or []:
            st.caption(f"Problem: {problem}")

    st.subheader("Stored documents" + (f" of {paper_id}" if paper_id else ""))
    docs = store.list_documents(conn, paper_id, include_group=bool(paper_id))
    ui.table(docs[:500], ["id", "paper_id", "role", "format", "original_filename", "record_status", "snapshot_status",
                          "failure_reason", "n_units", "n_tables", "content_scope", "source", "license",
                          "declared_data_class", "sha256", "retrieved_at", "added_by"],
             empty="No document is stored" + (" for this paper." if paper_id else "."), key="doc_list")
    if len(docs) > 500:
        st.caption(f"{len(docs)} documents; the first 500 are listed. Choose a paper to narrow the list.")

    st.subheader("Add a document")
    if not paper_id:
        st.caption("Choose a paper above to attach a document to it.")
    else:
        with st.form("doc_add"):
            path = st.text_input("File on this computer (.pdf or .xml)", key="doc_add_path")
            c1, c2, c3 = st.columns(3)
            role = c1.selectbox("Role", ["main", "supplementary"], key="doc_add_role")
            scope = c2.selectbox("Content", ["full_text", "abstract_only", "unknown"], key="doc_add_scope")
            declare = c3.selectbox("Data class of the file", [NOT_DECLARED, "synthetic", "real"], key="doc_add_declare")
            parent = st.text_input("Main document ID (for a supplementary file)", key="doc_add_parent")
            lic = st.text_input("License, as stated by the source (optional)", key="doc_add_license")
            url = st.text_input("Where the file was obtained (URL, optional)", key="doc_add_url")
            mismatch = st.checkbox("Attach even though identifiers in the file differ from the paper record",
                                   key="doc_add_mismatch")
            go = st.form_submit_button("Add document")
        if go:
            if not ui.opt(path):
                st.error("Give the file to attach.")
            else:
                ok, out = ui.attempt(
                    store.add_document, project, paper_id, ui.path_of(path), ctx.operator(), role=role,
                    parent_document_id=ui.opt(parent), declare=None if declare == NOT_DECLARED else declare,
                    source_url=ui.opt(url), license=ui.opt(lic), content_scope=scope,
                    allow_identifier_mismatch=mismatch)
                if ok:
                    snap = out["snapshot"]
                    msg = (f"Document {out['document_id']} "
                           f"{'was already stored' if out['already_present'] else 'stored'} "
                           f"(SHA-256 {out['sha256'][:16]}…); parse status: {snap['status']}"
                           + (f" — {snap['failure_reason']}" if snap.get("failure_reason") else
                              f", {snap.get('n_units')} text unit(s), {snap.get('n_tables')} table(s)")
                           + f"; identifier check: {out['identifier_check']['outcome']}.")
                    ui.done(msg + ("  " + " ".join(out["notes"]) if out.get("notes") else ""))

    st.subheader("Open-access retrieval")
    waiting = openaccess.fetch_candidates(project, 50)
    st.caption(f"{len(waiting)} record(s) reported open access with a PMCID and no usable document (first 50).")
    if waiting:
        target = st.selectbox("Record to retrieve", waiting, key="doc_oa_paper")
        if project.is_synthetic:
            st.caption("Synthetic project: live retrieval is refused; the bundled synthetic fixture response is "
                       "used so the mechanics can be exercised.")
        if st.button("Retrieve open-access full text", key="doc_oa_go"):
            transport = None
            if project.is_synthetic:
                from ..synthetic.documents import fulltext_transport
                transport = fulltext_transport()
            ok, out = ui.attempt(openaccess.fetch_open_access, project, target, ctx.operator(), transport=transport)
            if ok:
                if out["outcome"] == "retrieval_failed":
                    st.error(f"Retrieval failed: {out['reason']} ({out.get('url')}). The failure is recorded.")
                else:
                    ui.done(f"Retrieved {out['url']} → document {out['document_id']} "
                            f"(license: {out.get('license') or 'not supplied'}; outcome: {out['outcome']}).")

    st.subheader("Record that full text is not accessible")
    if paper_id:
        with st.form("doc_unavailable"):
            status_value = st.selectbox("Status", ["not_available", "abstract_only"], key="doc_un_status")
            note = st.text_input("Note (where you looked, why it is not accessible)", key="doc_un_note")
            go = st.form_submit_button("Record")
        if go:
            ok, _ = ui.attempt(store.record_fulltext_unavailable, conn, paper_id, ctx.reviewer(), status_value,
                               ui.opt(note))
            if ok:
                ui.done(f"Full text of {paper_id} recorded as {status_value}.")
    else:
        st.caption("Choose a paper above.")

    st.subheader("Inspect a document")
    if not docs:
        return
    labels = {d["id"]: _doc_label(d) for d in docs[:500]}
    doc_id = st.selectbox("Document", list(labels), format_func=labels.get, key="doc_pick")
    doc = next(d for d in docs if d["id"] == doc_id)
    if doc.get("snapshot_id") and doc.get("snapshot_status") == "ok":
        _snapshot_view(ctx, doc["snapshot_id"], "doc")
    elif doc.get("snapshot_id"):
        st.warning(f"This file could not be read: {doc.get('failure_reason') or doc.get('snapshot_status')}. "
                   "It is stored unchanged; nothing was extracted from it.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Parse again with the current parser", key="doc_reparse"):
            ok, out = ui.attempt(store.reparse, project, doc_id, ctx.operator())
            if ok:
                ui.done(f"New snapshot {out['snapshot_no']} ({out['status']}); text identical to the previous "
                        f"snapshot: {out['text_identical_to_previous']}. Earlier snapshots are kept.")
    with c2:
        with st.form("doc_remove"):
            reason = st.text_input("Reason for removing this document from the project", key="doc_remove_reason")
            go = st.form_submit_button("Remove (the file and its history are kept)")
        if go:
            ok, out = ui.attempt(store.remove_document, conn, doc_id, ctx.reviewer(), ui.opt(reason) or "")
            if ok:
                ui.done(f"Document {doc_id} marked removed; {out['findings_citing_document']} finding(s) cite it.")


def _snapshot_view(ctx: ui.Ctx, snapshot_id: str, key: str) -> dict[str, Any] | None:
    """Text units, tables and text search of one snapshot. Returns the chosen unit."""
    conn = ctx.conn
    info = store.snapshot_info(conn, snapshot_id)
    st.caption(f"Snapshot {snapshot_id}: parser {info.get('parser')} {info.get('parser_version')}, "
               f"{info.get('n_units')} text unit(s), {info.get('n_tables')} table(s). Locations are page indexes "
               "(PDF, 0 = first page) or element keys (XML) with character offsets in the stored text.")
    units = store.units(conn, snapshot_id)
    chosen = None
    if units:
        ulabels = {u["unit_key"]: f"{u['unit_key']} · {u.get('label') or ''} "
                                  f"{('· ' + ' / '.join(u['section_path'])) if isinstance(u.get('section_path'), list) and u['section_path'] else ''}"
                   for u in units}
        unit_key = st.selectbox("Text unit", list(ulabels), format_func=ulabels.get, key=f"{key}_unit")
        chosen = next(u for u in units if u["unit_key"] == unit_key)
        st.text_area("Stored text of the unit (read-only evidence)", value=chosen.get("text") or "", height=220,
                     disabled=True, key=f"{key}_unit_text_{snapshot_id}_{unit_key}")
    tabs = store.tables(conn, snapshot_id)
    if tabs:
        tlabels = {t["table_key"]: f"{t['table_key']} · {t.get('label') or ''} · {t['n_rows']}×{t['n_cols']}"
                   for t in tabs}
        table_key = st.selectbox("Table", list(tlabels), format_func=tlabels.get, key=f"{key}_table")
        tab = next(t for t in tabs if t["table_key"] == table_key)
        if tab.get("caption"):
            st.caption(f"Caption: {tab['caption']}")
        grid = [["" for _ in range(tab["n_cols"])] for _ in range(tab["n_rows"])]
        for cell in tab["cells"]:
            if cell["row_idx"] < tab["n_rows"] and cell["col_idx"] < tab["n_cols"]:
                grid[cell["row_idx"]][cell["col_idx"]] = cell["text"] or ""
        ui.table([{"row": i, **{f"col {j}": v for j, v in enumerate(row)}} for i, row in enumerate(grid)],
                 key=f"{key}_table_grid")
    needle = ui.opt(st.text_input("Find text in this document", key=f"{key}_find"))
    if needle:
        hits = store.find_text(conn, snapshot_id, needle, limit=50)
        ui.table([{"location": locations.describe(h), "context": h.get("context")} for h in hits],
                 empty="The text does not occur in the stored text of this document.", key=f"{key}_find_hits")
    return chosen


# ------------------------------------------------------------------ extraction
KINDS = ("measurement", "narrative", "limitation")
EDIT_HELP = ("One entry per line, `field=value`; `field=` clears a field. Fields: "
             + ", ".join(k for k in F.CONTENT_KEYS if k != "field_status") + ".")
STATUS_HELP = ("One entry per line, `FIELD=STATUS`. FIELD: " + ", ".join(F.STATUS_FIELDS) + ". STATUS: "
               + ", ".join(F.FIELD_STATUSES) + ".")
SPAN_HELP = ("One entry per line, `FIELD=START:END` (character offsets in the cited unit text). FIELD: "
             + ", ".join(F.LITERAL_FIELDS) + ".")


def extraction_page(ctx: ui.Ctx) -> None:
    conn, project = ctx.conn, ctx.project
    st.header("Extraction")
    st.caption("Extraction is deterministic and reviewable: candidate values are numbers with units found in the "
               "stored text by fixed rules, and every finding cites the exact stored text it was read from. "
               "Nothing here is verified until a reviewer verifies it on the review page. No language model, OCR "
               "or figure digitisation is used.")
    docs = [d for d in store.list_documents(conn, include_removed=False)
            if d.get("snapshot_id") and d.get("snapshot_status") == "ok"]
    if not docs:
        st.info("No readable document is stored yet. Add documents on the documents page first.")
        return
    labels = {d["snapshot_id"]: _doc_label(d) for d in docs[:500]}
    snapshot_id = st.selectbox("Document", list(labels), format_func=labels.get, key="ex_snapshot")
    info = store.snapshot_info(conn, snapshot_id)
    paper_id = info["paper_id"]
    stage_keys = [s.key for s in project.config.stages]
    tab_cand, tab_manual = st.tabs(["Candidate values", "Manual extraction from a location"])

    with tab_cand:
        if st.button("Detect candidate values in this document", key="ex_detect"):
            ok, out = ui.attempt(candidates.detect, project, snapshot_id, ctx.operator())
            if ok:
                ui.done("Candidates were already detected for this snapshot; nothing was added."
                        if out.get("already_detected") else
                        f"{out['candidates']} candidate value(s) recorded ({out['with_unit']} with a unit; "
                        f"{out['parse_failures']} parse failure(s)). They are suggestions, not findings.")
        counts = candidates.counts(conn, snapshot_id)
        st.caption(f"Candidates of this document — {ui.kv(counts)}.")
        c1, c2 = st.columns(2)
        status = c1.selectbox("Show", ["open", "promoted", "dismissed"], key="ex_cand_status")
        with_unit = c2.checkbox("Only values with a unit", key="ex_cand_unit")
        offset = ui.pager(counts.get(status, 0), "ex_cand_page")
        cands = candidates.list_candidates(conn, snapshot_id=snapshot_id, status=status, with_unit_only=with_unit,
                                           limit=50, offset=offset)
        ui.table([{"id": c["id"], "literal": c["literal"], "location": c.get("address"),
                   "context": c.get("context_excerpt"), "kind": c.get("location_kind"), "status": c["status"]}
                  for c in cands], empty="No candidate with this status.", key="ex_cand_table")
        if cands and status == "open":
            clabels = {c["id"]: f"{c['literal']} · {c.get('address')}" for c in cands}
            cid = st.selectbox("Candidate", list(clabels), format_func=clabels.get, key="ex_cand_pick")
            ok, prop = ui.attempt(candidates.proposal, project, cid)
            if ok:
                st.write(f"Draft reading (nothing is stored yet): **{prop['summary']}**")
                st.caption(f"Excerpt: {prop['location'].get('excerpt')}")
                if prop["flags"]:
                    st.caption("Flags: " + ", ".join(prop["flags"]))
            with st.form("ex_promote"):
                stages = st.multiselect("Stages", stage_keys, key="ex_promote_stages")
                changes = st.text_area("Your corrections to the draft (optional)", key="ex_promote_changes",
                                       help=EDIT_HELP)
                statuses = st.text_area("Missing-information statuses (optional)", key="ex_promote_statuses",
                                        help=STATUS_HELP)
                note = st.text_input("Note", key="ex_promote_note")
                b1, b2 = st.columns(2)
                promote = b1.form_submit_button("Create a finding from this candidate")
                dismiss = b2.form_submit_button("Dismiss this candidate")
            if promote:
                def run_promote():
                    ch = parse_assignments(ui.lines_of(changes))
                    sts = parse_statuses(ui.lines_of(statuses))
                    if sts:
                        ch["field_status"] = sts
                    return candidates.promote(project, cid, ctx.reviewer(), changes=ch, stage_keys=stages or None,
                                              note=ui.opt(note))
                ok, f = ui.attempt(run_promote)
                if ok:
                    ui.done(f"Finding {f['id']} created from the candidate (unreviewed; verify it on the review "
                            "page).")
            if dismiss:
                ok, n = ui.attempt(candidates.dismiss, conn, [cid], ctx.reviewer(), ui.opt(note))
                if ok:
                    ui.done(f"{n} candidate dismissed (kept in the record).")

    with tab_manual:
        st.caption(f"The finding will belong to paper {paper_id} and cite the excerpt you choose.")
        unit = _snapshot_view(ctx, snapshot_id, "ex")
        st.markdown("**Location of the supporting excerpt**")
        kind_of_loc = st.radio("Cite", ["an excerpt of the text unit above", "a table cell"], horizontal=True,
                               key="ex_loc_kind")
        quote = table_key = None
        row_idx = col_idx = None
        if kind_of_loc.startswith("an excerpt"):
            quote = st.text_area("Exact text of the excerpt (copy it from the unit; it must occur exactly once)",
                                 key="ex_quote", height=80)
        else:
            c1, c2, c3 = st.columns(3)
            table_key = c1.text_input("Table key", key="ex_table_key")
            row_idx = c2.number_input("Row", min_value=0, step=1, key="ex_row")
            col_idx = c3.number_input("Column", min_value=0, step=1, key="ex_col")
        if not (ui.opt(quote) or ui.opt(table_key)):
            st.caption("Give the excerpt (or a table cell) to see how it reads.")
            return

        def build():
            if ui.opt(table_key):
                loc = manual.build_location(conn, snapshot_id, table_key=ui.opt(table_key), row_idx=int(row_idx),
                                            col_idx=int(col_idx))
            else:
                loc = manual.build_location(conn, snapshot_id, unit_key=unit["unit_key"] if unit else None,
                                            quote=quote.strip())
            return loc, manual.propose_from_span(project, loc)
        ok, built = ui.attempt(build)
        if not ok:
            return
        loc, props = built
        st.write(f"Location: {locations.describe(loc)}")
        st.caption("Deterministic readings of the excerpt (drafts; nothing is stored):")
        ui.table([{"reading": i, "summary": p["summary"], "flags": p["flags"],
                   "spans": ", ".join(f"{k}={v[0]}:{v[1]}" for k, v in p["spans"].items())}
                  for i, p in enumerate(props, 1)], empty="The excerpt has no parsed reading.", key="ex_props")
        with st.form("ex_create"):
            numbered = [i for i, p in enumerate(props, 1) if p["content"].get("value_literal") is not None]
            quantity = st.selectbox("Start from reading", ["(none: narrative or my own fields)", *numbered],
                                    key="ex_quantity")
            kind = st.selectbox("Kind", ["(as read)", *KINDS], key="ex_kind")
            stages = st.multiselect("Stages", stage_keys, key="ex_create_stages")
            changes = st.text_area("Field values (optional)", key="ex_create_changes", help=EDIT_HELP)
            statuses = st.text_area("Missing-information statuses (optional)", key="ex_create_statuses",
                                    help=STATUS_HELP)
            spans = st.text_area("Spans of literals you typed (optional)", key="ex_create_spans", help=SPAN_HELP)
            note = st.text_input("Note", key="ex_create_note")
            go = st.form_submit_button("Store the finding (unreviewed)")
        if go:
            def create():
                content, sp, flags = manual.draft_from_proposal(
                    props, quantity=quantity if isinstance(quantity, int) else None,
                    changes=parse_assignments(ui.lines_of(changes)), kind=None if kind.startswith("(") else kind,
                    statuses=parse_statuses(ui.lines_of(statuses)), spans=parse_spans(ui.lines_of(spans)))
                return manual.create_finding(project, paper_id, ctx.reviewer(), location=loc, content=content,
                                             spans=sp, stage_keys=stages or None, flags=flags, note=ui.opt(note))
            ok, f = ui.attempt(create)
            if ok:
                ui.done(f"Finding {f['id']} stored (unreviewed, {f['grounding_status']}). Verify it on the review "
                        "page.")


# ------------------------------------------------------------------ review
NOT_CHECKED = "(not checked)"
CHECK_LABELS = {
    "outcome": "The outcome is the one the source reports",
    "condition": "The system, condition and comparator match the source",
    "timepoint": "The time point matches the source",
    "denominator": "The denominator / reference / normalisation matches the source",
    "qualifier": "Qualifiers and limitations of the source are carried over",
}


def review_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Review findings")
    st.caption("A finding counts as verified only after a named reviewer compared it with its cited source text "
               "and confirmed the five checks below. Imported and extracted findings stay unverified until then. "
               "Editing a finding creates a new version; earlier versions and reviews are kept.")
    flt = ui.finding_filter(ctx, "rv")
    total = query.search_findings(conn, flt, limit=1).total
    offset = ui.pager(total, "rv_page")
    page = query.search_findings(conn, flt, limit=50, offset=offset)
    st.caption(f"{page.total} finding(s) match.")
    ui.table(ui.finding_rows(page.rows), ui.FINDING_COLUMNS, empty="No finding matches the filter.", key="rv_table")

    typed = ui.opt(st.text_input("Finding ID (or choose from the list below)", key="rv_id"))
    fid = typed
    if not fid and page.rows:
        labels = {r["id"]: f"{(r.get('source_wording') or '')[:80]} · {r['review_status']} · {r['id']}"
                  for r in page.rows}
        fid = st.selectbox("Finding", list(labels), format_func=labels.get, key="rv_pick")
    if not fid:
        return
    ok, f = ui.attempt(F.get_finding, conn, fid)
    if not ok:
        return
    st.subheader(f"Finding {fid}")
    st.write(f"Paper: {ui.paper_label(f.get('paper') or {})} · {f['paper_id']}")
    st.code(ui.finding_text(f), language=None, wrap_lines=True)
    st.markdown("**Cited source text** (as stored; compare the finding with it)")
    for loc in f["locations"]:
        resolved, text, note = locations.resolve(conn, loc)
        st.caption(f"[{loc['role']}] {locations.describe(loc)}" + ("" if resolved else f" — DOES NOT RESOLVE: {note}"))
        if text:
            st.text(text)

    with st.expander("History: versions, reviews, derived values"):
        ui.table(F.versions(conn, fid), ["version_no", "change_kind", "created_by", "actor_kind", "created_at",
                                         "note", "content_sha256"], key="rv_versions")
        ui.table(F.reviews(conn, fid), ["action", "finding_version_no", "reviewer", "reviewer_kind", "created_at",
                                        "checks", "is_stale", "note"], empty="No review action yet.",
                 key="rv_reviews")
        ui.table(derived.list_values(conn, finding_id=fid, include_stale=True),
                 ["id", "kind", "rule_id", "formula", "output_value", "output_unit", "status"],
                 empty="No normalised or derived value uses this finding.", key="rv_derived")

    tabs = st.tabs(["Verify", "Edit", "Reject / reopen / comment", "Stages", "Relations"])
    with tabs[0]:
        with st.form("rv_verify"):
            checks = {name: st.selectbox(CHECK_LABELS[name], [NOT_CHECKED, *review.CHECK_VALUES],
                                         key=f"rv_check_{name}") for name in review.REQUIRED_CHECKS}
            ack = st.checkbox("I looked at the review flags of this finding and they are resolved or acceptable",
                              key="rv_ack")
            note = st.text_input("Note", key="rv_verify_note")
            go = st.form_submit_button("Verify against the source")
        if go:
            given = {k: v for k, v in checks.items() if v != NOT_CHECKED}
            ok, _ = ui.attempt(review.verify, conn, fid, ctx.reviewer(), given, note=ui.opt(note),
                               acknowledge_flags=ack)
            if ok:
                ui.done(f"Finding {fid} verified against its source by {ctx.reviewer().name}.")
    with tabs[1]:
        with st.form("rv_edit"):
            changes = st.text_area("Field changes", key="rv_edit_changes", help=EDIT_HELP)
            statuses = st.text_area("Missing-information statuses", key="rv_edit_statuses", help=STATUS_HELP)
            spans = st.text_area("Spans of changed literals", key="rv_edit_spans", help=SPAN_HELP)
            note = st.text_input("Note", key="rv_edit_note")
            go = st.form_submit_button("Save as a new version")
        if go:
            def run_edit():
                ch = parse_assignments(ui.lines_of(changes))
                sts = parse_statuses(ui.lines_of(statuses))
                if sts:
                    ch["field_status"] = {**f["content"].get("field_status", {}), **sts}
                sp = parse_spans(ui.lines_of(spans)) or None
                if not ch and not sp:
                    raise ValueError("Nothing to change: give a field change, a status or a span.")
                return review.edit(conn, fid, ctx.reviewer(), ch, spans=sp, note=ui.opt(note))
            ok, out = ui.attempt(run_edit)
            if ok:
                msg = (f"Finding {fid} is now version {out['version_no']} ({out['grounding_status']}); changed: "
                       f"{', '.join(out['changed_fields']) or 'spans only'}. Earlier verification no longer applies.")
                if out["grounding_problems"]:
                    msg += " Grounding problems: " + "; ".join(
                        f"{p['field']}: {p['reason']}" for p in out["grounding_problems"])
                ui.done(msg)
    with tabs[2]:
        with st.form("rv_other"):
            action = st.selectbox("Action", ["comment", "reject", "reopen"], key="rv_other_action")
            note = st.text_input("Note (required for a rejection or a comment)", key="rv_other_note")
            go = st.form_submit_button("Record")
        if go:
            fn = {"comment": review.comment, "reject": review.reject, "reopen": review.reopen}[action]
            ok, _ = ui.attempt(fn, conn, fid, ctx.reviewer(), ui.opt(note) or ("" if action != "reopen" else None))
            if ok:
                ui.done({"comment": f"Comment recorded on finding {fid}.",
                         "reject": f"Finding {fid} rejected (kept in the record).",
                         "reopen": f"Finding {fid} reopened for review."}[action])
    with tabs[3]:
        current = list(dict.fromkeys(s["key"] for s in f["stages"] if s["status"] == "active"))
        st.write("Current stages: " + (", ".join(current) or "(none)"))
        with st.form("rv_stages"):
            keys = [s.key for s in ctx.project.config.stages]
            add = st.multiselect("Add", [k for k in keys if k not in current], key="rv_stage_add")
            remove = st.multiselect("Remove", current, key="rv_stage_remove")
            note = st.text_input("Note", key="rv_stage_note")
            go = st.form_submit_button("Change stages")
        if go:
            ok, now = ui.attempt(review.assign_stages, conn, fid, ctx.reviewer(), add=add or None,
                                 remove=remove or None, note=ui.opt(note))
            if ok:
                ui.done(f"Finding {fid} stages: {', '.join(now) or '(none)'}.")
    with tabs[4]:
        rels = review.relations_of(conn, fid)
        ui.table(rels, ["id", "relation_type", "status", "other_finding", "method", "score", "decided_by",
                        "decided_at"], empty="No relation recorded for this finding.", key="rv_rels")
        st.caption("'dependent' findings are not counted as independent evidence; 'possible_duplicate' pairs "
                   "are never merged automatically.")
        pending = [r for r in rels if r["status"] == "candidate"]
        if pending:
            with st.form("rv_rel_decide"):
                rid = st.selectbox("Candidate relation", [r["id"] for r in pending], key="rv_rel_id")
                decision = st.selectbox("Decision", ["confirmed", "rejected"], key="rv_rel_decision")
                note = st.text_input("Note", key="rv_rel_note")
                go = st.form_submit_button("Decide")
            if go:
                ok, _ = ui.attempt(review.decide_relation, conn, rid, ctx.reviewer(), decision, ui.opt(note))
                if ok:
                    ui.done(f"Relation {rid} {decision}.")
        with st.form("rv_relate"):
            other = st.text_input("Other finding ID", key="rv_relate_other")
            rtype = st.selectbox("Relation", ["dependent", "possible_duplicate", "limited_by"], key="rv_relate_type")
            note = st.text_input("Note", key="rv_relate_note")
            go = st.form_submit_button("Record relation")
        if go:
            ok, _ = ui.attempt(review.relate, conn, fid, ui.opt(other) or "", rtype, ctx.reviewer(), note=ui.opt(note))
            if ok:
                ui.done(f"Relation recorded: {fid} {rtype} {other.strip()}.")
