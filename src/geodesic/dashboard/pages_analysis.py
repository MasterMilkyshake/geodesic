"""Dashboard pages: organise (hierarchy, vocabulary), compare, evidence maps, validation."""

from __future__ import annotations

from typing import Any

import streamlit as st

from .. import comparison, query, screening, vocabulary
from ..cli_reports import _assessment_lines
from ..cli_review import _derived_line
from ..export import tables as T
from ..extraction import derived, hierarchy, validation
from ..reports import figures, flow, maps, missing
from ..reports.html import figure_banner
from . import ui


# ------------------------------------------------------------------ organise
def organise_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Organise: studies, experiments, groups and vocabulary")
    st.caption("The hierarchy is optional and sparse: a finding may belong to a paper only. Findings of one "
               "experiment count as one independent unit in evidence maps and comparisons. Original terms are "
               "always kept; grouping terms under a normalised term is a recorded reviewer decision and never "
               "rewrites the stored wording.")
    tab_h, tab_v = st.tabs(["Studies, experiments, groups", "Vocabulary"])
    with tab_h:
        paper_id = ui.paper_picker(ctx, "org_paper", "Limit to one paper (ID, or words from its title or citation)")
        tree = hierarchy.tree(conn, paper_id)
        st.markdown("**Studies**")
        ui.table(tree["studies"], ["id", "label", "papers", "n_findings", "description", "design"],
                 empty="No study recorded.", key="org_studies")
        st.markdown("**Experiments**")
        ui.table([{**e, "groups": [f"{g['label']} ({g['id']})" for g in e["groups"]]} for e in tree["experiments"]],
                 ["id", "label", "paper_id", "study_id", "n_findings", "groups"], empty="No experiment recorded.",
                 key="org_experiments")
        st.caption(f"Findings not assigned to any study, experiment or group: {tree['unassigned_findings']}.")

        c1, c2, c3 = st.columns(3)
        with c1, st.form("org_study"):
            st.markdown("**New study**")
            label = st.text_input("Label", key="org_study_label")
            papers = st.text_input("Paper IDs reporting it (comma-separated)", key="org_study_papers")
            desc = st.text_input("Description", key="org_study_desc")
            go = st.form_submit_button("Create study")
        if go:
            ok, out = ui.attempt(hierarchy.create_study, conn, ctx.reviewer(), label, description=ui.opt(desc),
                                 paper_ids=ui.csv_list(papers) or None)
            if ok:
                ui.done(f"Study {out['id']} created.")
        with c2, st.form("org_experiment"):
            st.markdown("**New experiment**")
            exp_paper = st.text_input("Paper ID", value=paper_id or "", key="org_exp_paper")
            label = st.text_input("Label", key="org_exp_label")
            study = st.text_input("Study ID (optional)", key="org_exp_study")
            system = st.text_input("System / context (optional)", key="org_exp_system")
            go = st.form_submit_button("Create experiment")
        if go:
            ok, out = ui.attempt(hierarchy.create_experiment, conn, ctx.reviewer(), ui.opt(exp_paper) or "", label,
                                 study_id=ui.opt(study), system_context=ui.opt(system))
            if ok:
                ui.done(f"Experiment {out['id']} created.")
        with c3, st.form("org_group"):
            st.markdown("**New comparison group**")
            exp = st.text_input("Experiment ID", key="org_group_exp")
            label = st.text_input("Label", key="org_group_label")
            role = st.text_input("Role (free text, optional)", key="org_group_role")
            go = st.form_submit_button("Create group")
        if go:
            ok, out = ui.attempt(hierarchy.create_group, conn, ctx.reviewer(), ui.opt(exp) or "", label,
                                 role=ui.opt(role))
            if ok:
                ui.done(f"Comparison group {out['id']} created.")

        c1, c2 = st.columns(2)
        with c1, st.form("org_assign"):
            st.markdown("**Assign a finding**")
            fid = st.text_input("Finding ID", key="org_assign_fid")
            study = st.text_input("Study ID", key="org_assign_study")
            exp = st.text_input("Experiment ID", key="org_assign_exp")
            group = st.text_input("Comparison group ID", key="org_assign_group")
            clear = st.checkbox("Clear the current assignment instead", key="org_assign_clear")
            note = st.text_input("Note", key="org_assign_note")
            go = st.form_submit_button("Assign")
        if go:
            ok, out = ui.attempt(hierarchy.assign, conn, ui.opt(fid) or "", ctx.reviewer(), study_id=ui.opt(study),
                                 experiment_id=ui.opt(exp), comparison_group_id=ui.opt(group), clear=clear,
                                 note=ui.opt(note))
            if ok:
                ui.done(f"Finding {fid.strip()} assignment recorded as a new version; its verification of the "
                        "evidence is unchanged.")
        with c2, st.form("org_remove"):
            st.markdown("**Remove an unused study, experiment or group**")
            level = st.selectbox("Level", ["study", "experiment", "group"], key="org_remove_level")
            rid = st.text_input("ID", key="org_remove_id")
            reason = st.text_input("Reason", key="org_remove_reason")
            go = st.form_submit_button("Remove (kept in the record)")
        if go:
            ok, _ = ui.attempt(hierarchy.remove, conn, level, ui.opt(rid) or "", ctx.reviewer(), ui.opt(reason) or "")
            if ok:
                ui.done(f"{level} {rid.strip()} marked removed.")

    with tab_v:
        field = st.selectbox("Field", list(vocabulary.FIELDS), key="voc_field")
        st.markdown("**Terms as stored in active findings**")
        ui.table(vocabulary.usage(conn, field), ["original_term", "findings", "normalized_term"],
                 empty="No finding records a term in this field.", key="voc_usage")
        st.markdown("**Term mappings (reviewer decisions)**")
        ui.table(vocabulary.terms(conn, field, include_inactive=True),
                 ["field", "original_term", "normalized_term", "active", "created_by", "created_at", "note"],
                 empty="No mapping recorded for this field.", key="voc_terms")
        c1, c2 = st.columns(2)
        with c1, st.form("voc_set"):
            original = st.text_input("Original term (exactly as stored)", key="voc_set_original")
            normalized = st.text_input("Group it under", key="voc_set_normalized")
            note = st.text_input("Note", key="voc_set_note")
            go = st.form_submit_button("Record mapping")
        if go:
            ok, out = ui.attempt(vocabulary.set_term, conn, field, original, normalized, ctx.reviewer(),
                                 note=ui.opt(note))
            if ok:
                ui.done(f"{out['field']}: {out['original_term']!r} is now grouped under {out['normalized_term']!r} "
                        f"({out['findings_with_term']} finding(s) carry the term; their stored wording is unchanged).")
        with c2, st.form("voc_remove"):
            original = st.text_input("Original term", key="voc_remove_original")
            reason = st.text_input("Reason", key="voc_remove_reason")
            go = st.form_submit_button("Withdraw mapping")
        if go:
            ok, _ = ui.attempt(vocabulary.remove_term, conn, field, original, ctx.reviewer(), ui.opt(reason) or "")
            if ok:
                ui.done(f"Mapping of {field} {original!r} withdrawn (kept in the record).")


# ------------------------------------------------------------------ compare
def compare_page(ctx: ui.Ctx) -> None:
    conn, project = ctx.conn, ctx.project
    st.header("Compare")
    st.caption("GEODESIC lists findings side by side and checks whether their recorded fields allow a "
               "comparison. It never pools values, never ranks sources and draws no conclusion. "
               + comparison.STATEMENT)
    ids_text = st.text_area("Finding IDs (comma- or line-separated), or leave empty and use the filter",
                            key="cmp_ids", height=70)
    flt = ui.finding_filter(ctx, "cmp", label="Select findings with a filter instead")
    typed = [t for part in ids_text.replace("\n", ",").split(",") if (t := part.strip())]
    saved = comparison.list_sets(conn)
    ids: list[str] = []
    if typed or flt != query.FindingFilter():
        ok, ids = ui.attempt(comparison.select_findings, conn, typed, flt)
        ids = ids if ok else []

    if ids:
        st.subheader(f"Compatibility assessment of {len(ids)} finding(s)")
        ok, assessment = ui.attempt(comparison.assess, conn, ids)
        if ok:
            st.code("\n".join(_assessment_lines(assessment)), language=None)
        unit = ui.opt(st.text_input("Show values in this unit where the conversion table allows (optional)",
                                    key="cmp_unit"))
        ok, tab = ui.attempt(comparison.table, conn, ids, unit)
        if ok:
            ui.table(tab["rows"], tab["columns"], key="cmp_table")
            st.caption(tab["note"])
            st.caption("Relative quantities (percent of a reference, fold change, arbitrary units) are never "
                       "converted. Values 'as stated' are the transcribed source values.")
        c1, c2 = st.columns(2)
        with c1, st.form("cmp_save"):
            st.markdown("**Save this set**")
            name = st.text_input("Name", key="cmp_save_name")
            cls = st.selectbox("Classification (default: as assessed)", ["(as assessed)", "descriptive_only",
                                                                         "quantitative_candidate"], key="cmp_save_cls")
            desc = st.text_input("Description", key="cmp_save_desc")
            go = st.form_submit_button("Save comparison set")
        if go:
            ok, out = ui.attempt(comparison.save_set, conn, name, ids, ctx.reviewer(),
                                 classification=None if cls.startswith("(") else cls, description=ui.opt(desc))
            if ok:
                ui.done(f"Saved comparison set {out['id']} '{out['name']}' as {out['classification']} "
                        f"({len(out['finding_ids'])} findings). Nothing was pooled.")
        with c2, st.form("cmp_elig"):
            st.markdown("**Suitability of these findings for quantitative comparison**")
            value = st.selectbox("Mark as", list(comparison.ELIGIBILITY), key="cmp_elig_value")
            note = st.text_input("Note", key="cmp_elig_note")
            go = st.form_submit_button("Record")
        if go:
            ok, out = ui.attempt(comparison.set_eligibility, conn, ids, value, ctx.reviewer(), note=ui.opt(note))
            if ok:
                ui.done(f"{out['findings']} finding(s) marked {out['value']}.")
    else:
        st.caption("Name findings or set a filter to assess a set.")

    st.subheader("Saved comparison sets")
    ui.table([{**s, "findings": len(s["finding_ids"]),
               "current": "yes" if s.get("is_current", True) else "findings changed since the assessment"}
              for s in saved], ["id", "name", "classification", "findings", "current", "created_by", "created_at",
                                "description"], empty="No comparison set saved.", key="cmp_sets")
    if saved:
        labels = {s["id"]: f"{s['name']} · {s['id']}" for s in saved}
        sid = st.selectbox("Show a saved set", list(labels), format_func=labels.get, key="cmp_set_pick")
        s = comparison.get_set(conn, sid)
        if not s.get("is_current", True):
            st.warning("The stored assessment was made before these findings changed: "
                       + ", ".join(s.get("changed_since_assessment") or []) + ". Assess the set again.")
        st.code("\n".join(["Assessment as stored:", *_assessment_lines(s["assessment"]), "", "Findings:",
                           *[f"  {fid}" for fid in s["finding_ids"]]]), language=None)

    st.subheader("Normalised and derived values")
    st.caption("A normalised value is a unit conversion by an explicit rule of the conversion table; a derived "
               "value is a difference or ratio of two findings. Both are stored separately from the transcribed "
               "values, record their inputs and formula, and become stale when an input changes.")
    c1, c2 = st.columns(2)
    with c1, st.form("drv_norm"):
        fid = st.text_input("Finding ID", key="drv_norm_fid")
        to = st.text_input("Target unit (empty: the base unit of its dimension)", key="drv_norm_to")
        go = st.form_submit_button("Normalise the value")
    if go:
        ok, d = ui.attempt(derived.normalize_finding, project, ui.opt(fid) or "", ctx.reviewer(),
                           target_unit=ui.opt(to))
        if ok:
            ui.done(("Already present: " if d.get("already_present") else "Stored: ") + _derived_line(d)
                    + " The transcribed value and unit of the finding are unchanged.")
    with c2, st.form("drv_compute"):
        rule = st.selectbox("Rule", list(derived.RULES), format_func=lambda r: f"{r}: {derived.RULES[r]}",
                            key="drv_rule")
        a = st.text_input("Finding a", key="drv_a")
        b = st.text_input("Finding b", key="drv_b")
        go = st.form_submit_button("Compute")
    if go:
        ok, d = ui.attempt(derived.derive, project, rule, [ui.opt(a) or "", ui.opt(b) or ""], ctx.reviewer())
        if ok:
            ui.done("Stored: " + _derived_line(d) + f" Carried: {d['carries']}")
    values = derived.list_values(conn, include_stale=True, limit=200)
    ui.table(values, ["id", "kind", "rule_id", "formula", "output_value", "output_low", "output_high",
                      "output_unit", "status", "stale_reason"], empty="No normalised or derived value stored.",
             key="drv_values")
    if values:
        vid = st.selectbox("Trace a value back to its source text", [v["id"] for v in values], key="drv_trace_pick")
        t = derived.trace(conn, vid)
        st.write(f"Traceable to stored source text: **{'yes' if t['traceable'] else 'NO'}**; all inputs at their "
                 f"current version: **{'yes' if t['all_inputs_current'] else 'NO'}**")
        ui.table(t["steps"], ["finding_id", "version_used", "current_version", "review_status_now", "field",
                              "source_literal", "unit", "address", "stored_text_at_span", "literal_found_at_span",
                              "paper", "problem"], key="drv_trace")
        with st.form("drv_withdraw"):
            reason = st.text_input("Reason for withdrawing this value", key="drv_withdraw_reason")
            go = st.form_submit_button("Withdraw (kept in the record)")
        if go:
            ok, _ = ui.attempt(derived.withdraw, conn, vid, ctx.reviewer(), ui.opt(reason) or "")
            if ok:
                ui.done(f"Derived value {vid} withdrawn.")


# ------------------------------------------------------------------ evidence maps
def _figure(canvas: Any, key: str, name: str) -> None:
    svg = canvas.render("svg")
    st.image(svg.decode("utf-8"))
    st.download_button(f"Download {name}.svg", svg, file_name=f"{name}.svg", mime="image/svg+xml", key=key)


def maps_page(ctx: ui.Ctx) -> None:
    conn, project = ctx.conn, ctx.project
    st.header("Evidence maps and missing information")
    st.caption("Counts describe what is recorded in this project, not what exists in the literature: an empty "
               "cell means nothing was recorded here, not that no evidence exists. Counts of independent units "
               "are upper bounds ('at most') because independence is only known where a reviewer recorded it.")
    flt = ui.finding_filter(ctx, "map")
    info = T.project_info(project)
    banner = figure_banner(info)
    prefix = "SYNTHETIC_" if project.is_synthetic else ""
    tab_map, tab_missing, tab_full, tab_model, tab_flow = st.tabs(
        ["Evidence map", "Missing information", "Full-text availability", "Modelling inventory",
         "Search and screening flow"])
    with tab_map:
        c1, c2, c3 = st.columns(3)
        by = c1.selectbox("Stages by", list(maps.MAP_FIELDS), key="map_by")
        metric = c2.selectbox("Count", list(maps.METRICS), format_func=lambda m: maps.METRIC_LABELS.get(m, m),
                              key="map_metric")
        max_cols = c3.number_input("Most frequent terms shown", min_value=2, max_value=40, value=12, key="map_cols")
        emap = maps.evidence_map(conn, by, flt=flt, max_columns=int(max_cols))
        _figure(figures.evidence_map_figure(emap, metric, banner=banner), "map_svg", f"{prefix}evidence_map_{by}")
        st.markdown("**Table view of the map** (the same numbers; all metrics)")
        ui.export_table(maps.map_table(emap), key="map_table")
    with tab_missing:
        summary = missing.missing_information(conn, flt)
        stage_options = [missing.ALL_STAGES, *[s.key for s in project.config.stages], missing.NO_STAGE]
        stage = st.selectbox("Stage", stage_options, key="miss_stage")
        _figure(figures.missing_figure(summary, stage, banner=banner), "miss_svg", f"{prefix}missing_information")
        st.caption("'not reported' (the source does not state it), 'not applicable', 'not yet extracted' and "
                   "'could not be parsed' are different states and are counted separately.")
        ui.export_table(missing.missing_table(summary), key="miss_table")
    with tab_full:
        full = missing.fulltext_summary(conn)
        st.write(f"{full['reports']} report(s); {full['reports_with_findings']} with findings.")
        ui.table([{"availability": k.replace("_", " "), "reports": v} for k, v in full["by_availability"].items()],
                 key="full_table")
        st.caption("Documents by parse status: " + ui.kv(full["documents_by_parse_status"]))
        for note in full["notes"]:
            st.caption(f"Note: {note}")
    with tab_model:
        inv = missing.modeling_inventory(conn)
        st.caption("An inventory of what is recorded, to judge later whether modelling is feasible. GEODESIC "
                   "fits no model.")
        st.code(missing.format_modeling(inv), language=None)
        ui.export_table(missing.modeling_table(inv), key="model_table")
    with tab_flow:
        summary = screening.summary(conn)
        _figure(figures.flow_figure(summary, banner=banner), "flow_svg", f"{prefix}search_screening_flow")
        st.caption("A PRISMA-style flow summary. No claim of compliance with any reporting guideline is made.")
        st.code(flow.format_flow(summary), language=None)


# ------------------------------------------------------------------ validation
def validation_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Validation")
    st.caption("Validation re-checks every active finding against the stored source text and the grounding "
               "rules. It reports problems; it changes nothing and verifies nothing. It cannot detect "
               "undisclosed synthetic or fabricated content in a source.")
    if st.button("Run validation now", key="val_run"):
        ok, out = ui.attempt(validation.run_validation, ctx.project, ctx.operator())
        if ok:
            ui.done(f"Validation run {out['run_id']}: {out['n_findings']} finding(s) checked, "
                    f"{out['n_issues']} issue(s) ({ui.kv(out['by_severity'])}).")
    summary = validation.summary(conn)
    if not summary.get("run_id"):
        st.info("No validation run yet.")
        return
    st.write(f"Latest run **{summary['run_id']}** ({summary['created_at']}, by {summary['actor']}, rules "
             f"{summary['rules_version']}): {summary['n_findings']} finding(s) checked, {summary['n_issues']} "
             f"issue(s); findings with an error: {summary['findings_with_errors']}.")
    st.caption(summary["statement"])
    c1, c2 = st.columns(2)
    with c1:
        ui.table([{"severity": k, "issues": v} for k, v in summary["by_severity"].items()], key="val_sev")
    with c2:
        ui.table([{"code": k, "issues": v} for k, v in summary["by_code"].items()],
                 empty="No issue in this run.", key="val_codes")
    c1, c2, c3 = st.columns(3)
    severity = c1.selectbox("Severity", ["(all)", "error", "warning", "info"], key="val_f_sev")
    code = c2.selectbox("Code", ["(all)", *summary["by_code"]], key="val_f_code")
    fid = c3.text_input("Finding ID", key="val_f_fid")
    total = summary["by_severity"].get(severity, 0) if severity != "(all)" else summary["n_issues"]
    offset = ui.pager(total, "val_page", 100) if code == "(all)" and not ui.opt(fid) else 0
    rows = validation.issues(conn, summary["run_id"], code=None if code == "(all)" else code,
                             severity=None if severity == "(all)" else severity, finding_id=ui.opt(fid),
                             limit=100, offset=offset)
    ui.table(rows, ["severity", "code", "finding_id", "field", "message", "paper_id", "kind", "review_status"],
             empty="No issue matches.", key="val_issues")
