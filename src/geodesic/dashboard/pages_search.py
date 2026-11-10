"""Dashboard pages: literature search, duplicates, screening."""

from __future__ import annotations

from typing import Any

import streamlit as st

from .. import dedup, papers, protocol, screening
from ..errors import GeodesicError
from ..reports.flow import format_flow
from ..search import crossref, europepmc, fileimport, runs
from . import ui

DECLARE = ["(not declared)", "synthetic", "real"]


def _declared(value: str) -> str | None:
    return None if value.startswith("(") else value


# ------------------------------------------------------------------ search
def search_page(ctx: ui.Ctx) -> None:
    conn, project = ctx.conn, ctx.project
    st.header("Literature search")
    pv = protocol.current(conn)
    st.caption("Connector: Europe PMC REST search. Each run stores the raw responses with their hashes and is a "
               "snapshot: running a query again later is a new run and may return different records. No search "
               "is exhaustive." + ("" if pv.is_confirmed else f" Runs made now are labelled exploratory under "
                                                             f"protocol {pv.label}."))

    st.subheader("Run a search")
    with st.form("search_run"):
        q = st.text_input("Query (leave empty to use the concept groups of the project configuration)",
                          key="search_query")
        c1, c2, c3, c4 = st.columns(4)
        year_from = c1.number_input("Year from (0 = no limit)", min_value=0, max_value=3000, value=0, key="search_yf")
        year_to = c2.number_input("Year to (0 = no limit)", min_value=0, max_value=3000, value=0, key="search_yt")
        max_records = c3.number_input("Retrieval limit (0 = configured)", min_value=0, value=0, key="search_max")
        open_access = c4.checkbox("Open access only", key="search_oa")
        mode = st.selectbox("Mode", ["configured default", "fixture", "live"], key="search_mode",
                            help="'fixture' replays stored responses from a directory and never uses the network. "
                                 "Live searches are not permitted in a synthetic project.")
        fixture_dir = st.text_input("Fixture directory (fixture mode)", key="search_fixture")
        declare = st.selectbox("Data class of untagged fixture files", DECLARE, key="search_declare")
        translate = st.form_submit_button("Show the translated query")
        go = st.form_submit_button("Run the search")
    filters: dict[str, Any] = {}
    if year_from:
        filters["year_from"] = int(year_from)
    if year_to:
        filters["year_to"] = int(year_to)
    if open_access:
        filters["open_access"] = True
    if translate:
        ok, built = ui.attempt(europepmc.build_query, project, ui.opt(q), None, filters)
        if ok:
            st.write(f"Concept query: `{built['concept_query']}`")
            st.write(f"Translated for Europe PMC: `{built['translated_query']}`")
            st.write(f"Executed query: `{built['executed_query']}`")
    if go:
        ok, out = ui.attempt(
            europepmc.run_search, project, ctx.operator(), query=ui.opt(q), filters=filters,
            max_records=int(max_records) or None, mode=None if mode.startswith("configured") else mode,
            fixture_dir=ui.path_of(fixture_dir), declare=_declared(declare))
        if ok:
            run = out["run"]
            ui.done(f"Search run {run['id']}: {run['label']}. Records linked: {ui.kv(out['links'])}. "
                    f"Duplicate detection: {ui.kv(out['dedup'])}."
                    + (f" Completeness: {run['completeness_note']}" if run["completeness_note"] else ""))

    st.subheader("Import a search-result file (RIS or CSV)")
    with st.form("search_file"):
        path = st.text_input("File", key="sfile_path")
        fmt = st.selectbox("Format", ["from the file extension", "ris", "csv"], key="sfile_fmt")
        label = st.text_input("Where the file came from (database, date of the search)", key="sfile_label")
        declare_f = st.selectbox("Data class of an untagged file", DECLARE, key="sfile_declare")
        go_f = st.form_submit_button("Import the file")
    if go_f:
        if not ui.opt(path):
            st.error("Give the file to import.")
        else:
            ok, out = ui.attempt(fileimport.import_search_file, project, ui.path_of(path), ctx.operator(),
                                 fmt=None if fmt.startswith("from") else fmt, declare=_declared(declare_f),
                                 source_label=ui.opt(label))
            if ok:
                ui.done(f"File import {out['status']}: {out['run']['retrieved_count']} record(s) read; "
                        f"{ui.kv(out['links'])}." + "".join(f" Note: {n}." for n in out["notes"]))

    st.subheader("Search runs")
    rows = runs.list_runs(conn, limit=200)
    ui.table([{"run": r["id"], "started": r["started_at"], "label": runs.run_label(r),
               "executed query": r["executed_query"], "retrieved": r["retrieved_count"],
               "provider total": r["provider_total"], "status": r["status"],
               "completeness": r["completeness_note"]} for r in rows], empty="No search has been run.")
    if rows:
        chosen = st.selectbox("Show the records of a run", [r["id"] for r in rows], key="search_show")
        res = runs.run_results(conn, chosen, limit=50, offset=ui.pager(
            runs.run_results(conn, chosen, limit=1)["total"], "search_results_page"))
        ui.table(res["rows"], ["position", "link_method", "paper_id", "title", "year", "doi", "canonical_id"],
                 empty="The run holds no records.")

    st.subheader("Validate identifiers against Crossref")
    st.caption("Looks up stored DOIs in the Crossref registry (live network lookups; not permitted in a synthetic "
               "project). Validation shows that an identifier is registered and what the registry holds for it. It "
               "does not verify what a workbook says about the paper.")
    limit = st.number_input("At most this many lookups", min_value=1, max_value=500, value=25, key="crossref_limit")
    if st.button("Validate identifiers", key="crossref_go"):
        ok, out = ui.attempt(crossref.validate_identifiers, project, ctx.operator(), limit=int(limit))
        if ok:
            ui.done(f"Crossref lookups on {out['checked_on']}: {out['looked_up']} — validated {out['validated']}, "
                    f"not found {out['not_found']}, possible misassigned identifier "
                    f"{out['possible_misassignment']}, errors {len(out['errors'])}.")


# ------------------------------------------------------------------ duplicates
def _pair_rows(rel: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for side in ("a", "b"):
        p = rel[side]
        out.append({"record": side.upper(), "paper_id": p["id"], "title or citation": ui.paper_label(p),
                    "journal": p.get("journal"), "doi": p.get("doi"), "pmid": p.get("pmid"),
                    "pmcid": p.get("pmcid"), "origin": p.get("origin"),
                    "publication status": p.get("publication_status"), "identity": p.get("identity_status")})
    return out


def duplicates_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Duplicates and related records")
    st.caption("Records are grouped automatically only on a validated identifier match. Everything else is a "
               "candidate that a reviewer confirms or rejects. Grouping never deletes a record and can be reversed.")
    c1, c2 = st.columns(2)
    if c1.button("Run duplicate detection", key="dedup_run"):
        ok, out = ui.attempt(dedup.find_duplicates, conn, ctx.project.config.dedup)
        if ok:
            ui.done("Duplicate detection: " + ui.kv(out))
    if c2.button("Confirm identifier matches whose titles agree", key="dedup_confirm_ids"):
        ok, out = ui.attempt(lambda: dedup.confirm_identifier_candidates(conn, ctx.reviewer()))
        if ok:
            ui.done(f"Confirmed {out['confirmed']}; left for individual review {out['left_for_review']}.")

    status = st.selectbox("Relations with status", ["candidate", "confirmed", "rejected", "all"], key="dedup_status")
    total = dedup.relations(conn, status=None if status == "all" else status, limit=1)["total"]
    res = dedup.relations(conn, status=None if status == "all" else status, limit=25,
                          offset=ui.pager(total, "dedup_page", 25))
    st.write(f"{res['total']} relation(s) with status '{status}'.")
    for rel in res["rows"]:
        title = (f"{rel['relation_type']} — {rel['status']} — method {rel['method']}, score {rel['score']}"
                 + ("  [records already grouped]" if rel["already_grouped"] else ""))
        with st.expander(title):
            ui.table(_pair_rows(rel))
            if rel["score_details"]:
                st.caption("Score details: " + ui.kv(rel["score_details"].get("components") or rel["score_details"]))
            if rel["status"] == "candidate":
                as_type = st.selectbox("Record the relation as", ["(as proposed)", "duplicate", "preprint_published",
                                                                  "same_study_report"], key=f"dd_type_{rel['id']}")
                canonical = st.selectbox("Canonical record when grouping", ["(automatic)", rel["paper_a"],
                                                                            rel["paper_b"]], key=f"dd_can_{rel['id']}")
                note = st.text_input("Note", key=f"dd_note_{rel['id']}")
                b1, b2 = st.columns(2)
                for col, verdict in ((b1, "confirmed"), (b2, "rejected")):
                    if col.button(f"Mark {verdict}", key=f"dd_{verdict}_{rel['id']}"):
                        ok, out = ui.attempt(lambda v=verdict: dedup.decide_relation(
                            conn, rel["id"], ctx.reviewer(), v, note=ui.opt(note),
                            as_type=None if as_type.startswith("(") else as_type,
                            canonical=None if canonical.startswith("(") else canonical))
                        if ok:
                            ui.done(f"Relation {out['relation_id']}: {out['status']}"
                                    + (f"; records grouped ({out['grouping_id']})" if out.get("grouping_id") else ""))

    st.subheader("Groupings")
    groups = dedup.groupings(conn, status=None)
    ui.table(groups, ["id", "status", "member_paper_id", "canonical_paper_id", "basis", "created_by", "created_at",
                      "reversed_by", "reversal_reason"], empty="No records are grouped.")
    active = [g for g in groups if g["status"] == "active"]
    if active:
        with st.form("dedup_ungroup"):
            member = st.selectbox("Reverse the grouping of record", [g["member_paper_id"] for g in active],
                                  key="dedup_ungroup_member")
            reason = st.text_input("Reason", key="dedup_ungroup_reason")
            go = st.form_submit_button("Reverse the grouping")
        if go:
            ok, gid = ui.attempt(lambda: dedup.ungroup(conn, member, ctx.reviewer(), reason))
            if ok:
                ui.done(f"Grouping {gid} reversed; the record stands on its own again.")


# ------------------------------------------------------------------ screening
def _decision_rows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"decided": d["decided_at"], "stage": d["stage"], "decision": d["decision"],
             "reason": d["reason_kind"], "criterion": d["criterion_id"], "by": d["reviewer"],
             "decider": d["decider_kind"], "role": d["decision_role"], "finality": d["finality"],
             "protocol": f"v{d['protocol_version']} ({d['protocol_status_at_decision']})",
             "current": "yes" if d["is_current"] else "superseded",
             "needs re-review": d["rereview_reason"] if d["needs_rereview"] and d["is_current"] else "",
             "reason text": d["reason_text"], "support": d["supporting_text"], "decision_id": d["id"]}
            for d in decisions]


def _support_location(paper: dict[str, Any], field: str, quote: str | None) -> dict[str, Any] | None:
    """Locate a quoted passage in the record's title or abstract (character span of the stored text)."""
    if field.startswith("(") or not quote:
        return None
    start = (paper.get(field) or "").find(quote)
    if start < 0:
        raise GeodesicError(f"The supporting text does not occur in the stored {field} of this record. Quote it "
                            "exactly as stored.")
    return {"kind": "record_field", "field": field, "char_start": start, "char_end": start + len(quote)}


def screening_page(ctx: ui.Ctx) -> None:
    conn = ctx.conn
    st.header("Eligibility screening")
    pv = protocol.current(conn)
    stage = st.selectbox("Stage", list(screening.STAGES), key="screen_stage")
    st.caption("Suggestions are drafts made by transparent rules from the protocol; none of them is a decision. "
               "Automated exclusions run only for rules the confirmed protocol authorises, and are counted "
               "separately from human decisions.")
    c1, c2 = st.columns(2)
    if c1.button("Generate rule-based suggestions", key="screen_suggest"):
        ok, out = ui.attempt(screening.suggest, conn, stage)
        if ok:
            ui.done(f"Suggestions for {out['stage']} (protocol v{out['protocol_version']} {out['protocol_status']}):"
                    f" created {out['created']} ({ui.kv(out['by_suggestion'])}), unchanged {out['unchanged']}, "
                    f"superseded {out['superseded']}; skipped: already decided {out['skipped_already_decided']}, "
                    f"no stored text {out['skipped_no_text']}. Suggestions are drafts, not decisions.")
    if c2.button("Apply the automated exclusions the protocol authorises", key="screen_auto"):
        ok, out = ui.attempt(screening.apply_automation, conn, stage)
        if ok:
            ui.done(f"Automated decisions recorded: {out['applied']}; skipped: already decided "
                    f"{out['skipped_already_decided']}, rule not authorised {out['skipped_rule_not_authorised']}.")

    st.subheader("Queue")
    mine = st.checkbox("Only papers I have not decided", key="screen_mine", value=bool(ctx.reviewer_name.strip()))
    reviewer_name = ctx.reviewer_name.strip() if mine and ctx.reviewer_name.strip() else None
    total = screening.queue(conn, stage, reviewer=reviewer_name, limit=1)["total"]
    res = screening.queue(conn, stage, reviewer=reviewer_name, limit=50, offset=ui.pager(total, "screen_page"))
    st.write(f"{res['total']} paper(s) awaiting a {stage.replace('_', '/')} decision.")
    ui.table([{"paper_id": r["id"], "title or citation": ui.paper_label(r),
               "type": r["publication_type"] or "not reported",
               "suggestion": "; ".join(f"{s['suggested_decision']} ({s['reason_kind']}"
                                       + (f", {s['criterion_id']}" if s["criterion_id"] else "")
                                       + f") — {s['rationale']}" for s in r["suggestions"]),
               "other decisions": r["other_decisions"]} for r in res["rows"]],
             empty="The queue is empty.")

    st.subheader("Decide")
    queue_ids = [r["id"] for r in res["rows"]]
    pick = st.selectbox("Paper from the queue", ["(enter an ID below)"] + queue_ids, key="screen_pick")
    typed = st.text_input("…or paper ID", key="screen_paper_id")
    paper_id = ui.opt(typed) or (None if pick.startswith("(") else pick)
    paper = None
    if paper_id:
        ok, paper = ui.attempt(papers.get_paper, conn, paper_id)
    if paper:
        st.markdown(f"**{ui.paper_label(paper, 300)}**")
        st.caption(f"DOI {paper.get('doi') or 'not recorded'} · type {paper.get('publication_type') or 'not reported'}"
                   f" · origin {paper.get('origin')} · full text {paper.get('fulltext_availability')}")
        st.write(paper.get("abstract") or "_No abstract is stored for this record._")
        hist = screening.history(conn, paper_id)
        if hist["decisions"]:
            st.caption("Decisions recorded for this paper")
            ui.table(_decision_rows(hist["decisions"]))
        open_sug = [s for s in hist["suggestions"] if s["status"] == "open" and s["stage"] == stage]
        for s in open_sug:
            st.info(f"Suggestion (draft): {s['suggested_decision']} — {s['rationale']}"
                    + (f"  “{s['supporting_text']}”" if s["supporting_text"] else ""))
            a1, a2 = st.columns(2)
            if a1.button("Accept this suggestion as my decision", key=f"sug_acc_{s['id']}"):
                ok, out = ui.attempt(lambda s=s: screening.accept_suggestion(conn, s["id"], ctx.reviewer()))
                if ok:
                    ui.done(f"Suggestion accepted as your decision: {out['decision']} ({out['finality']}).")
            if a2.button("Dismiss this suggestion", key=f"sug_dis_{s['id']}"):
                ok, _ = ui.attempt(lambda s=s: screening.dismiss_suggestion(conn, s["id"], ctx.reviewer()))
                if ok:
                    ui.done("Suggestion dismissed.")
        criteria = sorted(screening.known_criterion_ids(pv.protocol, stage))
        with st.form("screen_decide"):
            d1, d2, d3 = st.columns(3)
            decision = d1.selectbox("Decision", list(screening.DECISIONS), key="screen_decision")
            reason_kind = d2.selectbox("Reason", list(screening.REASON_KINDS), key="screen_reason_kind")
            criterion = d3.selectbox("Criterion", ["(none)"] + criteria, key="screen_criterion")
            reason_text = st.text_input("Reason in your words", key="screen_reason_text")
            support = st.text_input("Supporting text quoted from the record (optional)", key="screen_support")
            field = st.selectbox("…quoted from", ["(not located)", "title", "abstract"], key="screen_support_field")
            final = st.checkbox("Refuse to record unless the decision is final (needs a confirmed protocol)",
                                key="screen_final")
            adjudication = st.checkbox("This is an adjudication of a recorded disagreement", key="screen_adjudicate")
            go = st.form_submit_button("Record the decision")
        if go:
            def record():
                fn = screening.adjudicate if adjudication else screening.decide
                return fn(conn, paper_id, stage, decision, ctx.reviewer(), reason_kind=reason_kind,
                          criterion_id=None if criterion.startswith("(") else criterion,
                          reason_text=ui.opt(reason_text), supporting_text=ui.opt(support),
                          supporting_location=_support_location(paper, field, ui.opt(support)), require_final=final)
            ok, out = ui.attempt(record)
            if ok:
                ui.done(f"Recorded {out['decision']} ({out['finality'].upper()}) for {out['paper_id']} at "
                        f"{out['stage']}; current outcome: {out['outcome']}.")

        st.caption("Separate judgements (not eligibility): relevance to the primary question, suitability for "
                   "quantitative comparison, suitability for mathematical modelling")
        ui.table(screening.assessments(conn, paper_id, current_only=False),
                 ["created_at", "dimension", "value", "reviewer", "is_current", "rationale"],
                 empty="No assessment recorded for this paper.")
        with st.form("screen_assess"):
            a1, a2 = st.columns(2)
            dimension = a1.selectbox("Dimension", list(screening.ASSESSMENT_VALUES), key="assess_dim")
            options = sorted({v for vals in screening.ASSESSMENT_VALUES.values() for v in vals})
            value = a2.selectbox("Value", options, key="assess_value",
                                 help="Values allowed per dimension: " + "; ".join(
                                     f"{k}: {', '.join(v)}" for k, v in screening.ASSESSMENT_VALUES.items()))
            rationale = st.text_input("Rationale", key="assess_rationale")
            go_a = st.form_submit_button("Record the assessment")
        if go_a:
            ok, _ = ui.attempt(lambda: screening.assess(conn, paper_id, dimension, value, ctx.reviewer(),
                                                        ui.opt(rationale)))
            if ok:
                ui.done(f"Recorded {dimension} = {value}.")

    st.subheader("Disagreements awaiting adjudication")
    conflicts = screening.conflicts(conn, stage)
    if not conflicts:
        st.caption("No recorded disagreement at this stage.")
    for cf in conflicts:
        with st.expander(f"{cf['paper_id']} — {ui.paper_label(cf)}"):
            ui.table(_decision_rows(cf["decisions"]))
            st.caption("To adjudicate, enter this paper ID under 'Decide' and tick 'adjudication'.")

    st.subheader("Decisions flagged for re-review")
    flagged = screening.rereview_queue(conn, stage)
    ui.table(_decision_rows(flagged), empty="No decision at this stage is flagged for re-review.")
    if flagged:
        with st.form("screen_reaffirm"):
            did = st.selectbox("Decision", [d["id"] for d in flagged], key="reaffirm_id")
            note = st.text_input("Note", key="reaffirm_note")
            go_r = st.form_submit_button("Keep this decision under the current protocol")
        if go_r:
            ok, out = ui.attempt(lambda: screening.reaffirm(conn, did, ctx.reviewer(), note=ui.opt(note)))
            if ok:
                ui.done(f"Decision kept and recorded again under protocol v{out['protocol_version']}.")

    st.subheader("Search and screening flow")
    st.code(format_flow(screening.summary(conn)), language=None)
    st.caption("PRISMA-style layout; no compliance with a reporting guideline is claimed.")
