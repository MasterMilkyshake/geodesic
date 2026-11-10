"""Mapping-driven workbook importer with versioned, idempotent re-import.

``import_workbook`` never writes to the input file. It copies it byte-for-byte
into the project (named by SHA-256), reads the preserved copy, stores every
cell, and links each paper reference and finding to the cells it came from.

Identity across workbook versions is content-based, never row-position-based:
a finding is recognised by its paper, its column role and its normalised
wording, so moving rows changes nothing. See ``docs/WORKBOOK_IMPORT.md``.
"""

from __future__ import annotations

import difflib
import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import audit, findings as F, locations, papers
from ..db import refresh_statistics, transaction
from ..errors import GeodesicError
from ..project import Project, preserve_source_file
from ..provenance import detect_embedded_tag, resolve_import_class
from ..textparse import DEFAULT_QUALIFIER_PHRASES, PARSER_NAME, PARSER_VERSION
from ..util import Actor, SYSTEM, canonical_json, loads, new_id, normalize_text_key, sha256_text, utc_now
from .extract import (EXTERNAL_PROPOSAL_FLAG, EXTERNAL_PROPOSER_METHOD, DraftFinding, Extracted, PaperRef, Proposer,
                      extract)
from .mapping import WorkbookMapping, validate_mapping
from .reader import WorkbookData, read_workbook

log = logging.getLogger("geodesic.workbook.importer")

PAPER_REMATCH_THRESHOLD = 0.85
PAPER_REMATCH_MARGIN = 0.05
FUZZY_BUCKET_LIMIT = 250_000
REPEAT_PAPER_LIMIT = 300

DEFINITIONS = {
    "cells_read": "cells with a value, comment or hyperlink stored from the workbook (all sheets)",
    "evidence_rows": "rows on evidence sheets that have their own content and a paper reference",
    "papers.referenced": "distinct papers referenced by evidence rows of this workbook version",
    "papers.unresolved": "referenced papers without a syntactically valid DOI or PMID (provisional records)",
    "findings.in_workbook": "findings parsed from this workbook version (narrative, measurement, limitation)",
    "findings.added": "findings not matched to any finding of an earlier version",
    "findings.unchanged": "findings matched to an earlier finding with identical content",
    "findings.modified": "findings matched to an earlier finding whose content or stages changed (new version "
                         "stored; earlier reviews marked stale)",
    "findings.absent": "earlier findings not found in this version (kept, marked absent_from_latest)",
    "findings.restored": "findings that were absent in the previous version and are present again",
    "findings.ambiguous_identity": "new findings that resemble more than one earlier finding (or vice versa); "
                                   "stored as new and flagged for review",
    "unparsed_content": "text retained but not interpreted (by reason)",
    "parse_failures": "proposed transcriptions rejected because the claimed source string was not at its span, "
                      "or content that failed schema validation",
    "requiring_review.draft": "ambiguous extractions kept as drafts",
    "requiring_review.flagged": "active findings from this workbook carrying at least one review flag",
    "requiring_review.unsupported": "findings whose excerpt or literals do not resolve to stored text",
    "requiring_review.stale": "findings (including ones absent from this version) whose earlier verification or "
                              "rejection no longer applies because the source changed",
    "requiring_review.unresolved_papers": "referenced papers still without a valid persistent identifier",
    "possible_repeated_finding_pairs": "candidate pairs of repeated findings created by this import (never deleted "
                                       "or merged automatically)",
    "reconciliation": "summary-sheet counts compared with what was imported",
}


@dataclass
class ImportResult:
    run_id: str
    workbook_version_id: str
    version_no: int
    status: str                      # imported | already_imported
    stats: dict[str, Any] = field(default_factory=dict)


def _ratio(a: str, b: str, floor: float) -> float:
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.real_quick_ratio() < floor or sm.quick_ratio() < floor:
        return 0.0
    return sm.ratio()


def _store_cells(conn: sqlite3.Connection, wv_id: str, wb: WorkbookData) -> int:
    rows = [(wv_id, c.sheet, c.cell_ref, c.row, c.col, c.raw_text, c.value_type, c.formula, c.hyperlink, c.comment,
             c.comment_author, c.merged_range, 1 if c.is_merge_anchor else 0, c.fill_color, c.font_flags,
             c.number_format) for sd in wb.sheets for c in sd.cells.values()]
    conn.executemany(
        "INSERT INTO source_cell (workbook_version_id, sheet, cell_ref, row_idx, col_idx, raw_text, value_type,"
        " formula, hyperlink, comment, comment_author, merged_range, is_merge_anchor, fill_color, font_flags,"
        " number_format) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _with_version(locs: list[dict], wv_id: str) -> list[dict]:
    return [{**l, "workbook_version_id": wv_id} for l in locs]


# ---------------------------------------------------------------------- papers
def _sync_papers(conn: sqlite3.Connection, ex: Extracted, key: str, wv_id: str, version_no: int, actor: Actor,
                 stage_ids: dict[str, str], now: str) -> tuple[dict[str, str], dict[str, int]]:
    """Create or match paper records for the workbook's references. Returns (identity_key -> paper_id, stats)."""
    stats = Counter()
    old = {r["identity_key"]: r for r in conn.execute(
        "SELECT * FROM paper WHERE workbook_key = ? AND origin = 'workbook' AND record_status != 'removed'", (key,))}
    ids: dict[str, str] = {}
    unmatched_new = [ref for k, ref in ex.papers.items() if k not in old]
    unmatched_old = {k: r for k, r in old.items() if k not in ex.papers}
    rematch: dict[str, sqlite3.Row] = {}
    if unmatched_new and unmatched_old:
        old_norm = {k: normalize_text_key(r["citation_text"] or r["title"] or "") for k, r in unmatched_old.items()}
        taken: set[str] = set()
        for ref in unmatched_new:
            text = normalize_text_key(ref.citation_text or ref.title or "")
            if not text:
                continue
            scored = sorted(((_ratio(text, o, PAPER_REMATCH_THRESHOLD - 0.1), k) for k, o in old_norm.items()
                             if o and k not in taken), reverse=True)
            if scored and scored[0][0] >= PAPER_REMATCH_THRESHOLD and (
                    len(scored) == 1 or scored[0][0] - scored[1][0] >= PAPER_REMATCH_MARGIN):
                rematch[ref.identity_key] = unmatched_old[scored[0][1]]
                taken.add(scored[0][1])
    source = f"workbook:{key}:v{version_no}"
    events = []
    for k, ref in ex.papers.items():
        row = old.get(k) or rematch.get(k)
        resolved = "resolved" if (ref.doi or ref.pmid) else "unresolved"
        if row is None:
            pid = papers.create_paper(conn, origin="workbook", resolution_status=resolved, identity_key=k,
                                      workbook_key=key, citation_text=ref.citation_text, title=ref.title,
                                      authors=ref.authors, year=ref.year, journal=ref.journal, doi=ref.doi,
                                      pmid=ref.pmid, urls=ref.urls, now=now)
            stats["created"] += 1
        else:
            pid = row["id"]
            stats["matched"] += 1
            sets: dict[str, Any] = {}
            if row["identity_key"] != k:
                sets["identity_key"] = k
                stats["rematched_by_citation_similarity"] += 1
                events.append(("paper.reimport_rematched", "paper", pid,
                               {"before_identity_key": row["identity_key"], "after_identity_key": k,
                                "before_citation": row["citation_text"], "after_citation": ref.citation_text}))
            if ref.citation_text and ref.citation_text != row["citation_text"]:
                sets["citation_text"] = ref.citation_text
                if row["identity_key"] == k:
                    events.append(("paper.citation_changed", "paper", pid,
                                   {"before": row["citation_text"], "after": ref.citation_text}))
            if row["record_status"] == "absent_from_latest":
                sets["record_status"] = "active"
                stats["restored"] += 1
            if resolved == "resolved" and row["resolution_status"] != "resolved":
                sets["resolution_status"] = "resolved"
            if sets:
                sets["updated_at"] = now
                conn.execute(f"UPDATE paper SET {', '.join(c + ' = ?' for c in sets)} WHERE id = ?",
                             (*sets.values(), pid))
            papers.merge_metadata(conn, pid, {"title": ref.title, "authors": ref.authors, "year": ref.year,
                                              "journal": ref.journal, "doi": ref.doi, "pmid": ref.pmid,
                                              "urls": ref.urls}, source, now)
            conn.execute("UPDATE source_location SET is_current = 0 WHERE owner_type = 'paper' AND owner_id = ?"
                         " AND is_current = 1 AND kind = 'workbook_cell'", (pid,))
        ids[k] = pid
        for scheme, raw in ref.identifiers:
            res = papers.add_identifier(conn, pid, scheme, raw, "workbook", now)
            if res["created"] and res["validation_status"] == "invalid_syntax":
                stats["invalid_identifiers"] += 1
        for fld, existing, incoming, where in ref.conflicts:
            # a conflict already recorded from an earlier version of this workbook is not recorded again
            known = conn.execute(
                "SELECT 1 FROM metadata_conflict WHERE paper_id = ? AND field = ? AND incoming_value IS ? AND"
                " incoming_source LIKE ?", (pid, fld, incoming, f"workbook:{key}:%")).fetchone()
            if not known and papers.record_conflict(conn, pid, fld, existing, incoming, f"{source} {where}", now):
                stats["metadata_conflicts"] += 1
                if fld in ("doi", "pmid", "pmcid"):
                    conn.execute("UPDATE paper SET identity_status = 'conflict', updated_at = ? WHERE id = ? AND"
                                 " identity_status = 'unverified'", (now, pid))
                    events.append(("paper.identifier_conflict", "paper", pid,
                                   {"field": fld, "existing": existing, "incoming": incoming, "where": where}))
        conn.executemany(locations.INSERT_SQL, [locations.row_tuple("paper", pid, None, l, True, None, now)
                                                for l in _with_version(ref.locations, wv_id)])
        wanted = {stage_ids[s] for s in ref.stage_keys}
        have = {r["stage_id"]: r for r in conn.execute(
            "SELECT id, stage_id, status FROM paper_stage WHERE paper_id = ? AND assignment_source = 'imported'",
            (pid,))}
        for sid, r in have.items():
            target = "active" if sid in wanted else "removed"
            if r["status"] != target:
                conn.execute("UPDATE paper_stage SET status = ?, updated_at = ? WHERE id = ?", (target, now, r["id"]))
        papers.set_paper_stages(conn, pid, wanted - set(have), "imported", actor, now)
        stats[resolved] += 1
    matched_ids = set(ids.values())
    for k, row in old.items():
        if row["id"] not in matched_ids and row["record_status"] == "active":
            conn.execute("UPDATE paper SET record_status = 'absent_from_latest', updated_at = ? WHERE id = ?",
                         (now, row["id"]))
            events.append(("paper.absent_from_latest", "paper", row["id"], {"workbook_version": version_no}))
            stats["absent_from_latest"] += 1
    audit.record_many(conn, actor, events)
    stats["referenced"] = len(ex.papers)
    return ids, dict(stats)


# ---------------------------------------------------------------------- findings
def _build(ex: Extracted, paper_ids: dict[str, str], key: str, wv_id: str, cell_text: dict[tuple[str, str], str],
           failures: list[dict]) -> list[F.NewFinding]:
    """Draft findings -> storable findings with match keys, occurrences and checked grounding."""
    items: list[F.NewFinding] = []
    seen: Counter[str] = Counter()
    for d in ex.findings:
        pid = paper_ids[d.paper_key]
        h1 = sha256_text(f"{pid}|{d.role_code}|{d.seg_norm}|{d.qidx}")[:16]
        h2 = sha256_text(",".join(sorted(d.stage_keys)))[:8]
        mk = f"{d.role_code}{d.qidx}:{h1}:{h2}"
        seen[mk] += 1
        locs = _with_version(d.locations, wv_id)
        texts = {(l.get("role") or F.PRIMARY_ROLE): cell_text.get((l["sheet"], l["cell_ref"])) for l in locs}
        problems = F.grounding_problems(d.content, d.spans, texts, locs)
        flags = list(d.review_flags)
        grounding = "grounded"
        if problems:
            grounding = "unsupported"
            flags.append(F.UNSUPPORTED_FLAG)
            failures.extend({"sheet": d.sheet, "cell_ref": d.cell_ref, "char_start": p.get("char_start"),
                             "char_end": p.get("char_end"), "field": p.get("field"),
                             "claimed_literal": p.get("claimed_literal"), "reason": p["reason"]} for p in problems)
        items.append(F.NewFinding(
            paper_id=pid, origin="workbook_import",
            extraction_method=(EXTERNAL_PROPOSER_METHOD if EXTERNAL_PROPOSAL_FLAG in flags
                               else f"{PARSER_NAME}/{PARSER_VERSION}"),
            content=d.content, locations=locs, spans=d.spans, stage_keys=d.stage_keys, stage_source="imported",
            review_status="draft" if set(flags) & F.DRAFT_FLAGS else "unreviewed", review_flags=flags,
            grounding_status=grounding, source_scope="workbook_summary", workbook_key=key, match_key=mk,
            occurrence=seen[mk],
            meta={"segment": (d.sheet, d.cell_ref, d.seg_start), "seg_norm": d.seg_norm,
                  "bucket": (pid, f"{d.role_code}{d.qidx}"), "limitation_cell": d.limitation_cell,
                  "is_limitation": d.role_code == "L", "hash": F.content_hash(d.content, d.stage_keys)}))
    return items


def _match(old: list[sqlite3.Row], new: list[F.NewFinding], threshold: float, margin: float
           ) -> tuple[list[tuple[sqlite3.Row, F.NewFinding]], list[F.NewFinding], list[sqlite3.Row],
                      list[tuple[F.NewFinding, list[tuple[sqlite3.Row, float]]]]]:
    """Pair earlier findings with this version's findings. Returns (pairs, added, absent, ambiguous)."""
    pairs: list[tuple[sqlite3.Row, F.NewFinding]] = []
    by_exact = {(r["match_key"], r["occurrence"]): r for r in old}
    used: set[str] = set()
    rest_new: list[F.NewFinding] = []
    for n in new:
        r = by_exact.get((n.match_key, n.occurrence))
        if r is not None and r["id"] not in used:
            used.add(r["id"])
            pairs.append((r, n))
        else:
            rest_new.append(n)
    rest_old = [r for r in old if r["id"] not in used]
    # pass 2: same paper, role and wording; only the stage assignment (or the occurrence count) differs
    if rest_new and rest_old:
        grp_old: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rest_old:
            grp_old[(r["match_key"] or "").rsplit(":", 1)[0]].append(r)
        still: list[F.NewFinding] = []
        for n in rest_new:
            cands = grp_old.get(n.match_key.rsplit(":", 1)[0])
            if cands:
                r = cands.pop(0)
                used.add(r["id"])
                pairs.append((r, n))
            else:
                still.append(n)
        rest_new = still
        rest_old = [r for r in rest_old if r["id"] not in used]
    # pass 3: reworded text, matched within (paper, role, quantity index) by similarity
    ambiguous: list[tuple[F.NewFinding, list[tuple[sqlite3.Row, float]]]] = []
    if rest_new and rest_old:
        bucket_old: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
        for r in rest_old:
            bucket_old[(r["paper_id"], (r["match_key"] or "").split(":", 1)[0])].append(r)
        bucket_new: dict[tuple, list[F.NewFinding]] = defaultdict(list)
        for n in rest_new:
            bucket_new[n.meta["bucket"]].append(n)
        added: list[F.NewFinding] = []
        for b, news in bucket_new.items():
            olds = bucket_old.get(b, [])
            if not olds or len(olds) * len(news) > FUZZY_BUCKET_LIMIT:
                added.extend(news)
                continue
            old_norm = [normalize_text_key(r["source_wording"]) for r in olds]
            score = [[_ratio(n.meta["seg_norm"], o, threshold - margin) for o in old_norm] for n in news]
            for i, n in enumerate(news):
                ranked = sorted(((s, j) for j, s in enumerate(score[i])), reverse=True)
                best, j = ranked[0]
                if best < threshold:
                    added.append(n)
                    continue
                second = ranked[1][0] if len(ranked) > 1 else 0.0
                col = sorted((score[k][j] for k in range(len(news)) if k != i), reverse=True)
                rival = col[0] if col else 0.0
                if best - second >= margin and best - rival >= margin and olds[j]["id"] not in used:
                    used.add(olds[j]["id"])
                    pairs.append((olds[j], n))
                else:
                    ambiguous.append((n, [(olds[jj], s) for s, jj in ranked[:3] if s >= threshold - margin]))
        rest_new = added
        rest_old = [r for r in rest_old if r["id"] not in used]
    return pairs, rest_new, rest_old, ambiguous


def _sync_findings(conn: sqlite3.Connection, items: list[F.NewFinding], key: str, wv_id: str, version_no: int,
                   same_version: bool, actor: Actor, stage_ids: dict[str, str], cfg, now: str
                   ) -> tuple[dict[str, Any], list[F.NewFinding]]:
    old = list(conn.execute(
        "SELECT id, paper_id, match_key, occurrence, record_status, review_status, source_wording,"
        " current_version, review_flags_json FROM finding WHERE workbook_key = ? AND origin = 'workbook_import'"
        " AND record_status != 'removed'", (key,)))
    import_hash = {r[0]: r[1] for r in conn.execute(
        "SELECT finding_id, content_sha256, MAX(version_no) FROM finding_version WHERE change_kind IN"
        " ('imported','reimport_modified') AND finding_id IN (SELECT id FROM finding WHERE workbook_key = ?)"
        " GROUP BY finding_id", (key,))}
    edited = {r[0] for r in conn.execute(
        "SELECT fv.finding_id FROM finding_version fv JOIN finding f ON f.id = fv.finding_id AND"
        " f.current_version = fv.version_no WHERE f.workbook_key = ? AND fv.change_kind = 'review_edit'", (key,))}
    pairs, added, absent, ambiguous = _match(old, items, cfg.reimport_similarity_threshold,
                                             cfg.reimport_ambiguity_margin)
    st = Counter()
    events: list[tuple] = []
    reason = f"source workbook changed (workbook '{key}' version {version_no})"
    touched: list[F.NewFinding] = []
    loc_rows: list[tuple] = []
    repoint: list[tuple] = []
    for r, n in pairs:
        n.id = r["id"]
        restored = r["record_status"] == "absent_from_latest"
        if import_hash.get(r["id"]) == n.meta["hash"]:
            st["unchanged"] += 1
            st["restored"] += restored
            if not same_version or restored or r["match_key"] != n.match_key or r["occurrence"] != n.occurrence:
                repoint.append((n.match_key, n.occurrence, None if r["id"] in edited else canonical_json(n.spans),
                                now, r["id"]))
                loc_rows.extend(locations.row_tuple("finding", r["id"], r["current_version"], l, True, None, now)
                                for l in n.locations)
            if restored:
                events.append(("finding.restored", "finding", r["id"], {"workbook_version": version_no}))
            continue
        st["modified"] += 1
        st["restored"] += restored
        flags = list(n.review_flags)
        if r["id"] in edited:
            flags.append("reviewer_edit_superseded_by_source_change")
        status = "stale" if r["review_status"] in ("verified", "rejected", "stale") else n.review_status
        stale = F.mark_stale(conn, r["id"], reason, now)
        st["reviews_marked_stale"] += stale["reviews"]
        st["derived_values_marked_stale"] += stale["derived_values"]
        v = F.write_new_version(conn, r["id"], content=n.content, spans=n.spans, locs=n.locations,
                                stage_keys=n.stage_keys, actor=actor, change_kind="reimport_modified",
                                review_status=status, review_flags=flags, grounding_status=n.grounding_status,
                                note=reason, extra_columns={"match_key": n.match_key, "occurrence": n.occurrence,
                                                            "record_status": "active"})
        F.set_stage_keys(conn, r["id"], n.stage_keys, "imported", stage_ids, actor, now)
        events.append(("finding.reimport_modified", "finding", r["id"],
                       {"to_version": v, "workbook_version": version_no, "before_status": r["review_status"],
                        "after_status": status, "reviews_marked_stale": stale["reviews"]}))
        touched.append(n)
    if repoint:
        ids = [(x[4],) for x in repoint]
        conn.executemany("UPDATE source_location SET is_current = 0 WHERE owner_type = 'finding' AND owner_id = ?"
                         " AND is_current = 1", ids)
        conn.executemany(locations.INSERT_SQL, loc_rows)
        conn.executemany("UPDATE finding SET match_key = ?, occurrence = ?, spans_json = COALESCE(?, spans_json),"
                         " record_status = 'active', updated_at = ? WHERE id = ?", repoint)
        # reviewer-edited findings keep their own spans; re-check them against the new cell text
        for fid in (x[4] for x in repoint if x[4] in edited):
            row = conn.execute("SELECT * FROM finding WHERE id = ?", (fid,)).fetchone()
            locs = locations.current_locations(conn, "finding", fid)
            if F.grounding_problems(F.content_from_row(row), loads(row["spans_json"], {}),
                                    F.stored_unit_texts(conn, locs), locs):
                flags = sorted({*loads(row["review_flags_json"], []), F.UNSUPPORTED_FLAG})
                F.mark_stale(conn, fid, reason + "; reviewer-entered spans no longer resolve", now)
                conn.execute("UPDATE finding SET grounding_status = 'unsupported', review_flags_json = ?,"
                             " review_status = CASE WHEN review_status IN ('verified','rejected') THEN 'stale'"
                             " ELSE review_status END WHERE id = ?", (canonical_json(flags), fid))
                events.append(("finding.grounding_lost", "finding", fid, {"workbook_version": version_no}))
                st["grounding_lost_after_source_change"] += 1
    amb_rel: list[tuple] = []
    for n, cands in ambiguous:
        n.review_flags = [*n.review_flags, "ambiguous_identity_match"]
        n.review_status = "draft"
        added.append(n)
        st["ambiguous_identity"] += 1
        amb_rel.extend((n.id, r["id"], s) for r, s in cands)
    F.insert_findings(conn, added, actor, "imported", stage_ids)
    st["added"] = len(added)
    touched.extend(added)
    conn.executemany(
        "INSERT OR IGNORE INTO finding_relation (id, finding_a, finding_b, relation_type, status, method, score,"
        " details_json, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(new_id("frl"), a, b, "ambiguous_identity", "candidate", "reimport_wording_similarity", s,
          canonical_json({"workbook_version": version_no}), now, actor.name) for a, b, s in amb_rel])
    for r in absent:
        if r["record_status"] != "active":
            continue
        stale = F.mark_stale(conn, r["id"], f"finding absent from workbook '{key}' version {version_no}", now)
        st["reviews_marked_stale"] += stale["reviews"]
        conn.execute("UPDATE finding SET record_status = 'absent_from_latest', review_status = CASE WHEN"
                     " review_status IN ('verified','rejected') THEN 'stale' ELSE review_status END, updated_at = ?"
                     " WHERE id = ?", (now, r["id"]))
        events.append(("finding.absent_from_latest", "finding", r["id"], {"workbook_version": version_no}))
        st["absent"] += 1
    audit.record_many(conn, actor, events)
    return dict(st), touched


def _link_limitations(conn: sqlite3.Connection, items: list[F.NewFinding], actor: Actor, now: str) -> int:
    lim: dict[tuple, list[str]] = defaultdict(list)
    for n in items:
        if n.meta["is_limitation"]:
            lim[(n.meta["segment"][0], n.meta["segment"][1])].append(n.id)
    rows = [(new_id("frl"), n.id, lid, "limited_by", "confirmed", "same_workbook_row", None, "{}", now, actor.name)
            for n in items if not n.meta["is_limitation"] and n.meta["limitation_cell"]
            for lid in lim.get(n.meta["limitation_cell"], [])]
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO finding_relation (id, finding_a, finding_b, relation_type, status, method, score,"
        " details_json, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return conn.total_changes - before


def _repeats(conn: sqlite3.Connection, items: list[F.NewFinding], touched: list[F.NewFinding], threshold: float,
             actor: Actor, now: str) -> int:
    """Flag possible repeated findings within a paper. Candidates only: nothing is deleted or merged."""
    touched_ids = {n.id for n in touched}
    if not touched_ids:
        return 0
    by_paper: dict[str, list[F.NewFinding]] = defaultdict(list)
    for n in items:
        if not n.meta["is_limitation"]:
            by_paper[n.paper_id].append(n)
    rows = []
    for group in by_paper.values():
        if len(group) < 2 or len(group) > REPEAT_PAPER_LIMIT or not any(n.id in touched_ids for n in group):
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a.id not in touched_ids and b.id not in touched_ids:
                    continue
                if a.meta["segment"] == b.meta["segment"]:
                    continue
                ca, cb = a.content, b.content
                method, score = None, None
                if a.meta["seg_norm"] == b.meta["seg_norm"] and ca["kind"] == cb["kind"] and \
                        ca.get("value_literal") == cb.get("value_literal"):
                    method, score = "identical_normalized_wording", 1.0
                elif ca["kind"] == cb["kind"] == "measurement" and ca["value_literal"] == cb["value_literal"] and \
                        ca.get("unit_original") == cb.get("unit_original") and \
                        ca.get("timepoint_literal") == cb.get("timepoint_literal") and \
                        (ca.get("outcome") or "") == (cb.get("outcome") or ""):
                    method, score = "same_value_unit_timepoint", None
                elif ca["kind"] == cb["kind"]:
                    s = _ratio(a.meta["seg_norm"], b.meta["seg_norm"], threshold)
                    if s >= threshold and ca.get("value_literal") == cb.get("value_literal"):
                        method, score = "wording_similarity", round(s, 4)
                if method:
                    x, y = sorted((a.id, b.id))
                    rows.append((new_id("frl"), x, y, "possible_duplicate", "candidate", method, score,
                                 canonical_json({"threshold": threshold}), now, actor.name))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO finding_relation (id, finding_a, finding_b, relation_type, status, method, score,"
        " details_json, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return conn.total_changes - before


# ---------------------------------------------------------------------- reconciliation
def _reconcile(mapping: WorkbookMapping, wb: WorkbookData, ex: Extracted, paper_ids: dict[str, str]) -> list[dict]:
    """Compare counts written on summary sheets with what was imported."""
    out = []
    for sc in mapping.summary_counts:
        sd = wb.sheet(sc.sheet)
        cell = None
        if sd is not None:
            cell = next((c for c in sd.cells.values() if c.cell_ref == sc.cell.upper()), None)
        rows = [r for r in ex.rows if sc.stage_key is None or sc.stage_key in r["stage_keys"]]
        imported = len(rows) if sc.measures == "evidence_rows" else len({paper_ids[r["paper_key"]] for r in rows})
        raw = cell.raw_text if cell else None
        try:
            stated = int(float(raw)) if raw is not None and float(raw) == int(float(raw)) else None
        except (TypeError, ValueError):
            stated = None
        status = "unreadable" if stated is None else "match" if stated == imported else "mismatch"
        out.append({"sheet": sc.sheet, "cell": sc.cell.upper(), "label": sc.label, "measures": sc.measures,
                    "stage_key": sc.stage_key, "workbook_value_text": raw, "workbook_count": stated,
                    "imported_count": imported, "status": status,
                    "difference": None if stated is None else imported - stated})
    return out


def import_workbook(project: Project, workbook_path: Path | str, mapping: WorkbookMapping,
                    declare: str | None = None, actor: Actor = SYSTEM,
                    proposer: Proposer | None = None) -> ImportResult:
    """Import (or re-import) a workbook through a confirmed mapping.

    Raises :class:`~geodesic.errors.ProvenanceError` when the declared data class
    conflicts with the project, :class:`~geodesic.errors.UnconfirmedMappingError`
    while required mapping entries are unconfirmed, and
    :class:`~geodesic.errors.MappingError` for a mapping that does not fit the workbook.
    """
    conn = project.conn
    src = Path(workbook_path)
    if not src.is_file():
        raise GeodesicError(f"Workbook not found: {src}")
    tag = detect_embedded_tag(src)
    data_class = resolve_import_class(project.data_class, declare, tag, f"Workbook '{src.name}'")
    stored, sha, size = preserve_source_file(src, project.workbooks_dir)
    wb = read_workbook(stored)
    stage_ids = {s["key"]: s["id"] for s in conn.execute("SELECT key, id FROM stage WHERE active = 1")}
    validate_mapping(mapping, [s.name for s in wb.sheets], list(stage_ids))
    key = mapping.workbook_key
    map_sha = mapping.content_sha256()
    latest = conn.execute("SELECT * FROM workbook_version WHERE workbook_key = ? ORDER BY version_no DESC LIMIT 1",
                          (key,)).fetchone()
    existing = conn.execute("SELECT * FROM workbook_version WHERE workbook_key = ? AND sha256 = ?",
                            (key, sha)).fetchone()
    if existing is not None:
        if existing["id"] != latest["id"]:
            raise GeodesicError(
                f"This file is version {existing['version_no']} of workbook '{key}', but version "
                f"{latest['version_no']} has since been imported. Re-importing an older version is not supported; "
                "import it under a different workbook_key if it is a different workbook.")
        last = conn.execute("SELECT * FROM import_run WHERE workbook_version_id = ? AND status = 'complete'"
                            " ORDER BY started_at DESC LIMIT 1", (existing["id"],)).fetchone()
        if last is not None and last["mapping_sha256"] == map_sha and proposer is None:
            log.info("workbook already imported; nothing to do", extra={"workbook_version_id": existing["id"]})
            return ImportResult(last["id"], existing["id"], existing["version_no"], "already_imported",
                                loads(last["stats_json"], {}))
    cfg = project.config.parser
    phrases = tuple(dict.fromkeys([*DEFAULT_QUALIFIER_PHRASES, *cfg.extra_qualifier_phrases]))
    ex = extract(wb, mapping, phrases, cfg.segment_on_semicolon, proposer)
    cell_text = {(c.sheet, c.cell_ref): (c.raw_text or "") for sd in wb.sheets for c in sd.cells.values()}
    run_id = new_id("imp")
    now = utc_now()
    with transaction(conn):
        if existing is None:
            wv_id = new_id("wbv")
            version_no = (latest["version_no"] + 1) if latest else 1
            conn.execute(
                "INSERT INTO workbook_version (id, workbook_key, version_no, original_filename, sha256, size_bytes,"
                " stored_path, declared_data_class, embedded_tag, imported_at, imported_by, previous_version_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (wv_id, key, version_no, src.name, sha, size, project.rel(stored), data_class, tag, now, actor.name,
                 latest["id"] if latest else None))
            _store_cells(conn, wv_id, wb)
        else:
            wv_id, version_no = existing["id"], existing["version_no"]
        conn.execute(
            "INSERT INTO import_run (id, kind, started_at, status, actor, workbook_version_id, mapping_sha256,"
            " mapping_json, parser_version) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, "workbook", now, "running", actor.name, wv_id, map_sha,
             canonical_json(mapping.model_dump(mode="json")), f"{PARSER_NAME}/{PARSER_VERSION}"))
        paper_ids, pstats = _sync_papers(conn, ex, key, wv_id, version_no, actor, stage_ids, now)
        failures = list(ex.parse_failures)
        items = _build(ex, paper_ids, key, wv_id, cell_text, failures)
        fstats, touched = _sync_findings(conn, items, key, wv_id, version_no, existing is not None, actor,
                                         stage_ids, cfg, now)
        n_lim = _link_limitations(conn, items, actor, now)
        n_rep = _repeats(conn, items, touched, cfg.repeat_similarity_threshold, actor, now)
        conn.executemany(
            "INSERT INTO workbook_row (workbook_version_id, sheet, row_idx, paper_id, stage_keys_json,"
            " unknown_fields_json) VALUES (?,?,?,?,?,?) ON CONFLICT (workbook_version_id, sheet, row_idx) DO UPDATE"
            " SET paper_id = excluded.paper_id, stage_keys_json = excluded.stage_keys_json,"
            " unknown_fields_json = excluded.unknown_fields_json",
            [(wv_id, r["sheet"], r["row"], paper_ids[r["paper_key"]], canonical_json(r["stage_keys"]),
              canonical_json(r["unknown_fields"])) for r in ex.rows])
        conn.executemany(
            "INSERT INTO unparsed_content (import_run_id, workbook_version_id, sheet, cell_ref, char_start,"
            " char_end, text, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(run_id, wv_id, u["sheet"], u["cell_ref"], u["char_start"], u["char_end"], u["text"], u["reason"], now)
             for u in ex.unparsed])
        conn.executemany(
            "INSERT INTO parse_failure (import_run_id, context, workbook_version_id, sheet, cell_ref, char_start,"
            " char_end, field, claimed_literal, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, "workbook_import", wv_id, p["sheet"], p["cell_ref"], p["char_start"], p["char_end"],
              p["field"], p["claimed_literal"], p["reason"], now) for p in failures])
        review = conn.execute(
            "SELECT SUM(record_status = 'active' AND review_status = 'draft') AS draft,"
            " SUM(record_status = 'active' AND review_flags_json != '[]') AS flagged,"
            " SUM(record_status = 'active' AND grounding_status = 'unsupported') AS unsupported,"
            " SUM(review_status = 'stale') AS stale FROM finding WHERE workbook_key = ?", (key,)).fetchone()
        stats: dict[str, Any] = {
            "workbook": {"file_name": src.name, "sha256": sha, "size_bytes": size, "workbook_key": key,
                         "version_no": version_no, "new_version": existing is None, "data_class": data_class,
                         "preserved_copy": project.rel(stored)},
            "cells_read": wb.n_cells,
            "evidence_rows": sum(ex.evidence_rows_by_sheet.values()),
            "evidence_rows_by_sheet": ex.evidence_rows_by_sheet,
            "papers": {k: pstats.get(k, 0) for k in
                       ("referenced", "created", "matched", "rematched_by_citation_similarity", "restored",
                        "absent_from_latest", "resolved", "unresolved", "invalid_identifiers",
                        "metadata_conflicts")},
            "findings": {"in_workbook": len(items),
                         "by_kind": dict(Counter(n.content["kind"] for n in items)),
                         "by_statement_type": dict(Counter(n.content["statement_type"] for n in items)),
                         **{k: fstats.get(k, 0) for k in ("added", "unchanged", "modified", "absent", "restored",
                                                          "ambiguous_identity")}},
            "reviews_marked_stale": fstats.get("reviews_marked_stale", 0),
            "derived_values_marked_stale": fstats.get("derived_values_marked_stale", 0),
            "grounding_lost_after_source_change": fstats.get("grounding_lost_after_source_change", 0),
            "unparsed_content": {"total": len(ex.unparsed),
                                 "by_reason": dict(Counter(u["reason"] for u in ex.unparsed))},
            "parse_failures": len(failures),
            "requiring_review": {"draft": review["draft"] or 0, "flagged": review["flagged"] or 0,
                                 "unsupported": review["unsupported"] or 0, "stale": review["stale"] or 0,
                                 "unresolved_papers": pstats.get("unresolved", 0)},
            "limitation_links_created": n_lim,
            "possible_repeated_finding_pairs": n_rep,
            "reconciliation": _reconcile(mapping, wb, ex, paper_ids),
            "definitions": DEFINITIONS,
        }
        conn.execute("UPDATE import_run SET status = 'complete', finished_at = ?, stats_json = ? WHERE id = ?",
                     (utc_now(), canonical_json(stats), run_id))
        audit.record(conn, actor, "workbook.import", "import_run", run_id,
                     {k: v for k, v in stats.items() if k != "definitions"})
    refresh_statistics(conn)
    log.info("workbook imported", extra={"run_id": run_id, "workbook_version": version_no,
                                         "findings": len(items), "papers": len(paper_ids)})
    return ImportResult(run_id, wv_id, version_no, "imported", stats)


def format_reconciliation(stats: dict[str, Any], with_definitions: bool = False) -> str:
    """Plain-text import report printed by the CLI and shown in the dashboard."""
    w, p, f, r = stats["workbook"], stats["papers"], stats["findings"], stats["requiring_review"]
    lines = [
        f"Workbook '{w['workbook_key']}' version {w['version_no']} ({w['file_name']}, sha256 {w['sha256'][:16]}…,"
        f" {w['data_class']})",
        f"  cells read:                 {stats['cells_read']}",
        f"  evidence rows:              {stats['evidence_rows']}  "
        + ", ".join(f"{k}: {v}" for k, v in stats["evidence_rows_by_sheet"].items()),
        f"  papers referenced:          {p['referenced']}  (created {p['created']}, matched {p['matched']}, "
        f"resolved {p['resolved']}, unresolved {p['unresolved']}, absent from this version "
        f"{p['absent_from_latest']})",
        f"  invalid identifiers:        {p['invalid_identifiers']};  metadata conflicts: {p['metadata_conflicts']}",
        f"  findings in workbook:       {f['in_workbook']}  ("
        + ", ".join(f"{k}: {v}" for k, v in sorted(f["by_kind"].items())) + ")",
        "  by statement type:          " + ", ".join(f"{k}: {v}" for k, v in sorted(f["by_statement_type"].items())),
        f"  change classification:      added {f['added']}, unchanged {f['unchanged']}, modified {f['modified']}, "
        f"absent {f['absent']}, restored {f['restored']}, ambiguous identity {f['ambiguous_identity']}",
        f"  reviews marked stale:       {stats['reviews_marked_stale']}",
        f"  unparsed content:           {stats['unparsed_content']['total']}  "
        + ", ".join(f"{k}: {v}" for k, v in sorted(stats["unparsed_content"]["by_reason"].items())),
        f"  parse failures:             {stats['parse_failures']}",
        f"  requiring review:           drafts {r['draft']}, flagged {r['flagged']}, unsupported "
        f"{r['unsupported']}, stale {r['stale']}, unresolved papers {r['unresolved_papers']}",
        f"  possible repeated findings: {stats['possible_repeated_finding_pairs']} candidate pair(s)",
        "  reconciliation with workbook summary counts:",
    ]
    if not stats["reconciliation"]:
        lines.append("    (no summary_counts entries in the mapping)")
    for rc in stats["reconciliation"]:
        scope = rc["stage_key"] or "whole workbook"
        lines.append(f"    {rc['sheet']}!{rc['cell']} {rc['measures']} [{scope}]: workbook says "
                     f"{rc['workbook_count'] if rc['workbook_count'] is not None else repr(rc['workbook_value_text'])}"
                     f", imported {rc['imported_count']} -> {rc['status'].upper()}")
    if with_definitions:
        lines.append("Definitions:")
        lines += [f"  {k}: {v}" for k, v in DEFINITIONS.items()]
    return "\n".join(lines)
