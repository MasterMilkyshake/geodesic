"""Dashboard pages: reports and exports, audit trail."""

from __future__ import annotations

import json

import streamlit as st

from .. import audit
from ..export import export_tables
from ..reports import build
from . import ui

TABLE_FORMATS = ("xlsx", "csv", "json", "ris")
FIGURE_FORMATS = ("svg", "pdf", "png")


def reports_page(ctx: ui.Ctx) -> None:
    project = ctx.project
    st.header("Reports and exports")
    st.caption("A report is written to a new directory with a manifest (file hashes, project, protocol version, "
               "filter, software version). The HTML report is self-contained and works offline. Reports "
               "organise and trace evidence; they draw no conclusions, rank nothing and pool nothing. "
               + ("Every file of this SYNTHETIC project is labelled synthetic."
                  if project.is_synthetic else
                  "Exports of a real project contain real project data: keep them out of source control."))
    flt = ui.finding_filter(ctx, "rep", label="Limit the report to findings matching a filter (optional)")

    st.subheader("Reproducible report (HTML, figures, tables)")
    with st.form("rep_build"):
        title = st.text_input("Report title (optional)", key="rep_title")
        out_dir = st.text_input("Output directory (empty: a new timestamped directory in the project's exports)",
                                key="rep_dir")
        c1, c2, c3 = st.columns(3)
        parts = c1.multiselect("Parts", ["html", "figures", "tables"], default=["html", "figures", "tables"],
                               key="rep_parts")
        tfmt = c2.multiselect("Table formats", list(TABLE_FORMATS), default=list(TABLE_FORMATS), key="rep_tfmt")
        ffmt = c3.multiselect("Figure formats", list(FIGURE_FORMATS), default=list(FIGURE_FORMATS), key="rep_ffmt")
        dpi = st.number_input("PNG resolution (DPI, at least 300)", min_value=300, max_value=1200, value=300,
                              step=50, key="rep_dpi")
        go = st.form_submit_button("Build report")
    if go:
        ok, out = ui.attempt(build.build_report, project, ui.path_of(out_dir), flt=flt, parts=tuple(parts),
                             table_formats=tuple(tfmt), figure_formats=tuple(ffmt), dpi=int(dpi),
                             title=ui.opt(title), actor=ctx.operator())
        if ok:
            ui.done(f"Report written to {out['directory']} ({len(out['files'])} files; manifest {out['manifest']}"
                    + (f"; open {out['html']} in a browser" if out.get("html") else "") + ").")

    st.subheader("Tables only")
    with st.form("rep_tables"):
        out_dir2 = st.text_input("Output directory (empty: a new timestamped directory in the project's exports)",
                                 key="exp_dir")
        fmts = st.multiselect("Formats", list(TABLE_FORMATS), default=["xlsx", "csv"], key="exp_fmts")
        go = st.form_submit_button("Export tables")
    if go:
        ok, out = ui.attempt(export_tables, project, ui.path_of(out_dir2), formats=tuple(fmts), flt=flt,
                             actor=ctx.operator())
        if ok:
            ui.done(f"Tables written to {out['directory']} ({len(out['files'])} files; rows per table: "
                    f"{ui.kv(out['rows'])}).")

    st.subheader("Exports in the project data directory")
    rows = []
    if project.exports_dir.exists():
        for d in sorted(project.exports_dir.iterdir(), reverse=True):
            if d.is_dir():
                files = [f for f in d.rglob("*") if f.is_file()]
                rows.append({"directory": str(d), "files": len(files),
                             "manifest": "yes" if any(f.name.endswith("manifest.json") for f in files) else "no"})
    ui.table(rows[:200], empty="No export has been written to the project's exports directory.", key="rep_dirs")


def audit_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Audit trail")
    st.caption("Every change is appended here with who made it, when (UTC) and what changed. Events cannot be "
               "edited or deleted; each event carries the hash of the previous one.")
    ok, n, bad = audit.verify_chain(conn)
    if ok:
        st.success(f"{n} events; the hash chain is intact.")
    else:
        st.error(f"The hash chain is BROKEN at event {bad}.")
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT actor_kind, COUNT(*) FROM audit_event GROUP BY actor_kind ORDER BY actor_kind")}
    st.caption("Events by kind of actor — " + ui.kv(by_kind)
               + ". 'simulated' actors exist only in synthetic projects and are never counted as human review.")
    c1, c2, c3 = st.columns(3)
    etype = ui.opt(c1.text_input("Entity type (e.g. finding, paper, document)", key="aud_type"))
    eid = ui.opt(c2.text_input("Entity ID", key="aud_id"))
    prefix = ui.opt(c3.text_input("Action starts with (e.g. finding., screening.)", key="aud_prefix"))
    total = audit.count(conn) if not (etype or eid or prefix) else 0
    offset = ui.pager(total, "aud_page", 100)
    rows = audit.history(conn, etype, eid, prefix, limit=100, offset=offset)
    shown = []
    for r in rows:
        details = r.get("details_json")
        if isinstance(details, str):
            try:
                details = json.dumps(json.loads(details), ensure_ascii=False, sort_keys=True)
            except ValueError:
                pass
        shown.append({**r, "details": (details or "")[:400]})
    ui.table(shown, ["seq", "ts", "actor", "actor_kind", "action", "entity_type", "entity_id", "details"],
             empty="No event matches.", key="aud_table")
    if etype or eid or prefix:
        st.caption(f"Showing the {len(rows)} most recent matching event(s) (at most 100).")
