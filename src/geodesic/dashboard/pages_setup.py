"""Dashboard pages: overview, protocol, workbook import."""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from .. import audit, comparison, protocol, query, screening
from ..extraction import candidates, validation
from ..util import loads
from . import ui


# ------------------------------------------------------------------ overview
def overview(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Overview")
    c = query.counts(conn)
    cols = st.columns(5)
    cols[0].metric("Paper records", c["reports"])
    cols[1].metric("Findings", c["findings"])
    cols[2].metric("Documents", c["documents"])
    cols[3].metric("Search runs", c["search_runs"])
    cols[4].metric("Studies / experiments", f"{c['studies']} / {c['experiments']}")
    st.caption(f"Paper records with an unresolved citation: {c['reports_unresolved']}; grouped under a canonical "
               f"record: {c['reports_grouped_under_canonical']}. Findings without an experiment: "
               f"{c['findings_without_experiment']}; without a stage: {c['findings_without_stage']}.")

    st.subheader("Findings by review status")
    ui.table([{"review status": k, "findings": v} for k, v in sorted(c["findings_by_review_status"].items())],
             empty="No findings yet.")
    st.caption("Only findings with status 'verified' were checked against their source by a reviewer. Imported "
               "summaries and citations are evidence awaiting verification, not verified facts.")

    st.subheader("Workflow status")
    flow = screening.summary(conn)
    ident = flow["identification"]
    pv = protocol.current(conn)
    val = validation.summary(conn)
    cand = candidates.counts(conn)
    n_versions = conn.execute("SELECT COUNT(*) FROM workbook_version").fetchone()[0]
    exports = sorted(p.name for p in ctx.project.exports_dir.iterdir()) if ctx.project.exports_dir.exists() else []
    stages = flow["stages"]
    rows = [
        {"step": "1 Protocol (research scope)", "state": f"protocol {pv.label}"},
        {"step": "2 Workbook import", "state": f"{n_versions} workbook version(s) imported"},
        {"step": "3 Literature search", "state": f"search runs by mode: {ui.kv(ident['search_runs'])}"},
        {"step": "4 Duplicates", "state": f"{ident['duplicate_candidates_awaiting_review']} candidate pair(s) await "
                                          f"review; {ident['grouped_as_duplicates']} record(s) grouped"},
        {"step": "5 Screening", "state": "; ".join(
            f"{name.replace('_', '/')}: {st_['awaiting_decision']} awaiting a decision, {st_['screened']} decided"
            for name, st_ in stages.items())},
        {"step": "6 Documents (full text)", "state": f"{c['documents']} document(s) stored"},
        {"step": "7 Extraction", "state": f"candidate values: {ui.kv(cand)}"},
        {"step": "8 Review findings", "state": ui.kv(c["findings_by_review_status"])},
        {"step": "9 Organise", "state": f"{c['studies']} study(ies), {c['experiments']} experiment(s); "
                                        f"{c['findings_without_experiment']} finding(s) without an experiment"},
        {"step": "10 Compare", "state": f"{len(comparison.list_sets(conn))} saved comparison set(s)"},
        {"step": "12 Validation", "state": (
            f"last run {val['created_at']}: {val['n_issues']} issue(s) ({ui.kv(val['by_severity'])})"
            if val.get("run_id") else "no validation run yet")},
        {"step": "13 Reports and exports", "state": f"{len(exports)} export/report directory(ies) in the project"},
    ]
    ui.table(rows)
    ok, n, bad = audit.verify_chain(conn)
    if ok:
        st.caption(f"Audit trail: {n} events, hash chain intact.")
    else:
        st.error(f"Audit trail: hash chain BROKEN at event {bad}.")
    for note in flow["notes"]:
        st.caption(f"Note: {note}")


# ------------------------------------------------------------------ protocol
def protocol_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Protocol (research scope and eligibility criteria)")
    pv = protocol.current(conn)
    st.write(f"Current protocol: **{pv.label}** — created {pv.created_at} by {pv.created_by}"
             + (f"; confirmed {pv.confirmed_at} by {pv.confirmed_by}" if pv.confirmed_at else ""))
    problems = pv.protocol.completeness_problems()
    if not pv.is_confirmed:
        for p in problems:
            st.caption(f"Incomplete: {p}")
    st.caption("The criteria are yours to write. GEODESIC does not draft or suggest criteria; it stores each "
               "version, labels unconfirmed versions provisional, and flags earlier screening decisions for "
               "re-review when criteria change.")
    text = st.text_area("Protocol (YAML)", value=protocol.dump_protocol(pv.protocol), height=420, key="protocol_text")
    summary = st.text_input("What changed (stored with the new version)", key="protocol_summary")
    c1, c2 = st.columns(2)
    if c1.button("Save as a new protocol version", key="protocol_save"):
        def save():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "protocol.yaml"
                path.write_text(text, encoding="utf-8")
                new = protocol.load_protocol_file(path)
            return protocol.save_new_version(conn, new, ctx.reviewer(), summary or "")
        ok, saved = ui.attempt(save)
        if ok:
            ui.done(f"Protocol is now {saved.label}. It stays provisional until you confirm it.")
    if c2.button("Confirm the current protocol", key="protocol_confirm", disabled=pv.is_confirmed):
        ok, confirmed = ui.attempt(lambda: protocol.confirm(conn, ctx.reviewer(), ctx.project.data_class))
        if ok:
            ui.done(f"Protocol confirmed: {confirmed.label}")
    st.download_button("Download the empty protocol template", protocol.template_text(),
                       file_name="protocol_template.yaml", mime="text/yaml", key="protocol_template")

    st.subheader("Versions")
    ui.table([{"version": v.label, "created": v.created_at, "by": v.created_by, "confirmed": v.confirmed_at,
               "confirmed by": v.confirmed_by, "change summary": v.change_summary}
              for v in protocol.list_versions(conn)])
    st.subheader("Decisions flagged for re-review after a criteria change")
    ui.table(screening.rereview_queue(conn), ["paper_id", "stage", "decision", "reviewer", "protocol_version",
                                              "rereview_reason"], empty="No decision is flagged for re-review.")


# ------------------------------------------------------------------ workbook
def workbook_page(ctx: ui.Ctx) -> None:
    from ..workbook.importer import format_reconciliation, import_workbook
    from ..workbook.mapping import load_mapping
    from ..workbook.profiler import render_profile_md, write_profile

    conn = ctx.conn
    st.header("Workbook import")
    st.caption("Workbooks are read, never written. Each imported file is preserved byte for byte in the project "
               "data directory with its SHA-256. Paths are read from this computer; nothing is uploaded.")

    st.subheader("a) Profile a workbook and write a starter mapping")
    with st.form("wb_profile"):
        src = st.text_input("Workbook file (.xlsx)", key="wb_profile_src")
        out = st.text_input("Directory for the profile and the starter mapping", key="wb_profile_out")
        go = st.form_submit_button("Profile")
    if go:
        if not ui.opt(src) or not ui.opt(out):
            st.error("Give the workbook file and an output directory.")
        else:
            stages = [{"key": s.key, "name": s.name} for s in ctx.project.config.stages]
            ok, written = ui.attempt(write_profile, ui.path_of(src), ui.path_of(out), stages)
            if ok:
                st.success(f"Wrote {written['profile_md']}, {written['profile_json']} and {written['mapping']}. "
                           "Every inferred mapping entry is marked 'confirmed: false': review each one in the "
                           "mapping file and set 'confirmed: true'. The importer refuses to run until the "
                           "required entries are confirmed.")
                st.markdown(render_profile_md(written["profile"]))

    st.subheader("b) Import through a confirmed mapping")
    with st.form("wb_import"):
        wb = st.text_input("Workbook file (.xlsx)", key="wb_import_src")
        mp = st.text_input("Mapping file (.yaml)", key="wb_import_map")
        declare = st.selectbox("Data class of this file (required when the file carries no synthetic tag)",
                               ["(not declared)", "synthetic", "real"], key="wb_import_declare")
        go = st.form_submit_button("Import")
    if go:
        if not ui.opt(wb) or not ui.opt(mp):
            st.error("Give the workbook file and the mapping file.")
        else:
            def run():
                return import_workbook(ctx.project, ui.path_of(wb), load_mapping(ui.path_of(mp)),
                                       declare=None if declare.startswith("(") else declare, actor=ctx.operator())
            ok, result = ui.attempt(run)
            if ok:
                if result.status == "already_imported":
                    st.info("This exact workbook file was already imported with this mapping; nothing was changed. "
                            "Counts from that import:")
                else:
                    st.success(f"Imported as workbook version {result.version_no}.")
                st.code(format_reconciliation(result.stats, with_definitions=True), language=None)

    st.subheader("Imported workbook versions")
    ui.table([dict(r) for r in conn.execute(
        "SELECT workbook_key, version_no, original_filename, sha256, size_bytes, declared_data_class, imported_at,"
        " imported_by FROM workbook_version ORDER BY workbook_key, version_no")],
        empty="No workbook has been imported.")
    last = conn.execute("SELECT stats_json, finished_at FROM import_run WHERE kind = 'workbook' AND"
                        " status = 'complete' ORDER BY started_at DESC LIMIT 1").fetchone()
    if last and last["stats_json"]:
        with st.expander(f"Reconciliation counts of the latest import ({last['finished_at']})"):
            st.code(format_reconciliation(loads(last["stats_json"], {}), with_definitions=True), language=None)
    unparsed = conn.execute("SELECT COUNT(*) FROM unparsed_content").fetchone()[0]
    st.caption(f"Workbook content that could not be parsed and is kept for manual extraction: {unparsed} item(s) "
               "(listed in the table exports as 'unparsed_content').")
