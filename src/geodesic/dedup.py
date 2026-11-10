"""Duplicate detection, identifier conflicts and reversible grouping of paper records.

Rules (see docs/DEDUPLICATION.md)
---------------------------------
* Two records that carry the same normalised DOI, PMID or PMCID are the same
  entity only when the identifier is *validated* on both sides (supplied by a
  bibliographic provider as the record's own identifier, confirmed by a registry
  lookup, or confirmed by a reviewer). They are then grouped automatically under
  the earlier record. Grouping never deletes or rewrites the member record.
* The same identifier on records whose titles clearly differ is an
  ``identifier_conflict`` (possible misassignment) and is flagged for review
  instead of grouped.
* The same identifier with an unvalidated side is a duplicate *candidate*.
* Title/first-author/year similarity only ever produces review candidates. The
  score and its components are stored with the candidate.
* A preprint and a published version are linked (``preprint_published``), never
  grouped as identical. Multiple reports of one study are ``same_study_report``.
* Every grouping is reversible; a reversal is remembered so the pair is not
  grouped again automatically.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from . import audit
from .config import DedupConfig
from .db import transaction
from .errors import NotFoundError, ReviewError, ValidationError
from .papers import canonical_id
from .util import SYSTEM, Actor, canonical_json, loads, new_id, normalize_text_key, utc_now

RELATION_TYPES = ("duplicate", "preprint_published", "same_study_report", "identifier_conflict")
ID_SCHEMES = ("doi", "pmid", "pmcid")
AUTOMATED = Actor("geodesic-dedup", "automated")


# ------------------------------------------------------------------ scoring
def title_similarity(a: str | None, b: str | None) -> float | None:
    """Similarity of two titles in [0, 1], or ``None`` when either is missing.

    ``difflib.SequenceMatcher`` ratio over the *word sequences* of the case-,
    punctuation- and whitespace-normalised titles (2 x matching words / total
    words). Word-level matching keeps unrelated titles near 0, which a
    character-level ratio does not.
    """
    ka, kb = normalize_text_key(a or "").split(), normalize_text_key(b or "").split()
    if not ka or not kb:
        return None
    if ka == kb:
        return 1.0
    return round(SequenceMatcher(None, ka, kb, autojunk=False).ratio(), 4)


def conflicting_identifiers(a: sqlite3.Row | dict, b: sqlite3.Row | dict) -> list[str]:
    """Identifier schemes for which both records carry a value and the values differ."""
    return [s for s in ID_SCHEMES if a[s] and b[s] and a[s] != b[s]]


def score_pair(a: sqlite3.Row | dict, b: sqlite3.Row | dict, cfg: DedupConfig) -> dict[str, Any]:
    """Weighted similarity of two paper records.

    Components: title (sequence ratio), first author (1 same family-name key, 0
    different), year (1 same, 0.5 one year apart, 0 otherwise). A component that
    is missing on either side is left out and the remaining weights are
    renormalised; the components used are reported with the score.
    """
    comps: dict[str, float | None] = {"title": title_similarity(a["title"], b["title"])}
    fa, fb = a["first_author"], b["first_author"]
    comps["first_author"] = None if not fa or not fb else (1.0 if fa == fb else 0.0)
    ya, yb = a["year"], b["year"]
    comps["year"] = None if ya is None or yb is None else (1.0 if ya == yb else 0.5 if abs(ya - yb) == 1 else 0.0)
    weights = {"title": cfg.weight_title, "first_author": cfg.weight_first_author, "year": cfg.weight_year}
    used = {k: w for k, w in weights.items() if comps[k] is not None}
    total = sum(used.values())
    score = None
    if comps["title"] is not None and total > 0:
        score = round(sum(comps[k] * w for k, w in used.items()) / total, 4)
    return {"score": score, "components": comps, "weights_used": used}


# ------------------------------------------------------------------ relations and groupings
def _ordered(conn: sqlite3.Connection, a: str, b: str) -> tuple[str, str]:
    rows = {r["id"]: r["pk"] for r in conn.execute("SELECT id, pk FROM paper WHERE id IN (?, ?)", (a, b))}
    if len(rows) != 2:
        raise NotFoundError(f"Unknown paper in pair ({a}, {b})")
    return (a, b) if rows[a] < rows[b] else (b, a)


def existing_relation(conn: sqlite3.Connection, a: str, b: str, relation_type: str | None = None) -> list[dict]:
    a, b = _ordered(conn, a, b)
    sql = "SELECT * FROM paper_relation WHERE paper_a = ? AND paper_b = ?"
    params: list[Any] = [a, b]
    if relation_type:
        sql += " AND relation_type = ?"
        params.append(relation_type)
    return [dict(r) for r in conn.execute(sql, params)]


def add_relation(conn: sqlite3.Connection, a: str, b: str, relation_type: str, method: str, actor: Actor,
                 status: str = "candidate", score: float | None = None, details: dict | None = None,
                 note: str | None = None) -> str | None:
    """Insert a relation between two paper records; ``None`` when that relation already exists."""
    if relation_type not in RELATION_TYPES:
        raise ValidationError(f"Unsupported relation type {relation_type!r}; use one of {RELATION_TYPES}.")
    if a == b:
        raise ValidationError("A paper cannot be related to itself.")
    a, b = _ordered(conn, a, b)
    if existing_relation(conn, a, b, relation_type):
        return None
    rid = new_id("prl")
    now = utc_now()
    decided = status != "candidate"
    conn.execute(
        "INSERT INTO paper_relation (id, paper_a, paper_b, relation_type, status, method, score, score_details_json,"
        " created_at, created_by, decided_at, decided_by, decision_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, a, b, relation_type, status, method, score, canonical_json(details or {}), now, actor.name,
         now if decided else None, actor.name if decided else None, note))
    return rid


def group(conn: sqlite3.Connection, canonical: str, member: str, actor: Actor, basis: str,
          relation_id: str | None = None) -> str:
    """Group ``member`` under ``canonical`` (reversible). Returns the grouping ID."""
    target = canonical_id(conn, canonical)
    if canonical_id(conn, member) == target:
        raise ValidationError("These records are already grouped under the same canonical record.")
    row = conn.execute("SELECT canonical_id FROM paper WHERE id = ?", (member,)).fetchone()
    if row["canonical_id"]:
        raise ValidationError(f"{member} is already grouped under {row['canonical_id']}; reverse that grouping first.")
    mid = new_id("mrg")
    now = utc_now()
    conn.execute("INSERT INTO paper_merge (id, canonical_paper_id, member_paper_id, basis, relation_id, created_at,"
                 " created_by) VALUES (?,?,?,?,?,?,?)", (mid, target, member, basis, relation_id, now, actor.name))
    conn.execute("UPDATE paper SET canonical_id = ?, updated_at = ? WHERE id = ?", (target, now, member))
    audit.record(conn, actor, "paper.group", "paper", member,
                 {"canonical": target, "basis": basis, "relation_id": relation_id, "grouping_id": mid})
    return mid


def ungroup(conn: sqlite3.Connection, member: str, actor: Actor, reason: str) -> str:
    """Reverse the active grouping of ``member``. The pair is not grouped again automatically."""
    _require_reviewer(conn, actor)
    if not (reason or "").strip():
        raise ReviewError("Reversing a grouping needs a reason.")
    row = conn.execute("SELECT * FROM paper_merge WHERE member_paper_id = ? AND status = 'active'",
                       (member,)).fetchone()
    if row is None:
        raise ReviewError(f"{member} is not currently grouped under another record.")
    now = utc_now()
    with transaction(conn):
        conn.execute("UPDATE paper_merge SET status = 'reversed', reversed_at = ?, reversed_by = ?,"
                     " reversal_reason = ? WHERE id = ?", (now, actor.name, reason.strip(), row["id"]))
        conn.execute("UPDATE paper SET canonical_id = NULL, updated_at = ? WHERE id = ?", (now, member))
        if row["relation_id"]:
            conn.execute("UPDATE paper_relation SET status = 'rejected', decided_at = ?, decided_by = ?,"
                         " decision_note = ? WHERE id = ?",
                         (now, actor.name, f"grouping reversed: {reason.strip()}", row["relation_id"]))
        audit.record(conn, actor, "paper.ungroup", "paper", member,
                     {"canonical": row["canonical_paper_id"], "grouping_id": row["id"], "reason": reason.strip(),
                      "basis": row["basis"]})
    return row["id"]


def _require_reviewer(conn: sqlite3.Connection, actor: Actor) -> None:
    if actor.kind not in ("human", "simulated"):
        raise ReviewError("Duplicate decisions need a named reviewer, not an automated component.")
    if actor.kind == "simulated":
        if conn.execute("SELECT data_class FROM project").fetchone()["data_class"] != "synthetic":
            raise ReviewError("Simulated review actions are only permitted in synthetic projects.")


def decide_relation(conn: sqlite3.Connection, relation_id: str, actor: Actor, status: str,
                    note: str | None = None, as_type: str | None = None, canonical: str | None = None) -> dict:
    """Reviewer decision on a candidate relation.

    * ``confirmed`` on a ``duplicate`` groups the later record under the earlier
      one (or under ``canonical`` when given).
    * ``as_type`` re-classifies the pair, e.g. a duplicate candidate that is
      really a ``preprint_published`` pair or a ``same_study_report``: the
      original candidate is rejected and the new relation confirmed. Linked
      records stay separate.
    * ``identifier_conflict`` confirmed marks both records' identity as ``conflict``.
    """
    _require_reviewer(conn, actor)
    if status not in ("confirmed", "rejected"):
        raise ValidationError("status must be 'confirmed' or 'rejected'.")
    rel = conn.execute("SELECT * FROM paper_relation WHERE id = ?", (relation_id,)).fetchone()
    if rel is None:
        raise NotFoundError(f"Unknown paper relation {relation_id}")
    if as_type and as_type not in RELATION_TYPES:
        raise ValidationError(f"Unsupported relation type {as_type!r}; use one of {RELATION_TYPES}.")
    now = utc_now()
    out: dict[str, Any] = {"relation_id": relation_id, "status": status, "grouping_id": None}
    with transaction(conn):
        if as_type and as_type != rel["relation_type"]:
            if status != "confirmed":
                raise ValidationError("as_type is only used when confirming a different relation type.")
            conn.execute("UPDATE paper_relation SET status = 'rejected', decided_at = ?, decided_by = ?,"
                         " decision_note = ? WHERE id = ?",
                         (now, actor.name, f"re-classified as {as_type}" + (f": {note}" if note else ""), relation_id))
            prior = existing_relation(conn, rel["paper_a"], rel["paper_b"], as_type)
            if prior:
                new_rid = prior[0]["id"]
                conn.execute("UPDATE paper_relation SET status = 'confirmed', decided_at = ?, decided_by = ?,"
                             " decision_note = ? WHERE id = ?", (now, actor.name, note, new_rid))
            else:
                new_rid = add_relation(conn, rel["paper_a"], rel["paper_b"], as_type, "reviewer", actor,
                                       status="confirmed", score=rel["score"], note=note)
            audit.record(conn, actor, "paper.relation_decision", "paper_relation", relation_id,
                         {"status": "rejected", "reclassified_as": as_type, "new_relation_id": new_rid, "note": note})
            rel = conn.execute("SELECT * FROM paper_relation WHERE id = ?", (new_rid,)).fetchone()
            out.update(relation_id=new_rid, reclassified_from=relation_id)
        else:
            conn.execute("UPDATE paper_relation SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?"
                         " WHERE id = ?", (status, now, actor.name, note, relation_id))
        audit.record(conn, actor, "paper.relation_decision", "paper_relation", rel["id"],
                     {"status": status, "relation_type": rel["relation_type"], "paper_a": rel["paper_a"],
                      "paper_b": rel["paper_b"], "note": note})
        if status == "confirmed" and rel["relation_type"] == "duplicate":
            keep = canonical or rel["paper_a"]
            if keep not in (rel["paper_a"], rel["paper_b"]):
                raise ValidationError("canonical must be one of the two related records.")
            member = rel["paper_b"] if keep == rel["paper_a"] else rel["paper_a"]
            if canonical_id(conn, member) != canonical_id(conn, keep):
                out["grouping_id"] = group(conn, keep, canonical_id(conn, member), actor,
                                           "reviewer_confirmed_duplicate", rel["id"])
        elif status == "confirmed" and rel["relation_type"] == "identifier_conflict":
            conn.execute("UPDATE paper SET identity_status = 'conflict', updated_at = ? WHERE id IN (?, ?)",
                         (now, rel["paper_a"], rel["paper_b"]))
    return out


def relate(conn: sqlite3.Connection, a: str, b: str, relation_type: str, actor: Actor,
           note: str | None = None) -> dict:
    """A reviewer links two records directly (e.g. two reports of the same study)."""
    _require_reviewer(conn, actor)
    with transaction(conn):
        rid = add_relation(conn, a, b, relation_type, "reviewer", actor, note=note)
        if rid is None:
            rid = existing_relation(conn, a, b, relation_type)[0]["id"]
    return decide_relation(conn, rid, actor, "confirmed", note=note)


# ------------------------------------------------------------------ detection
def _papers(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["id"]: r for r in conn.execute(
        "SELECT pk, id, canonical_id, title, title_norm, first_author, year, publication_status, record_status,"
        " doi, pmid, pmcid FROM paper WHERE record_status != 'removed'")}


def _relation_type_for(a: sqlite3.Row, b: sqlite3.Row) -> str:
    statuses = {a["publication_status"], b["publication_status"]}
    return "preprint_published" if statuses == {"preprint", "published"} else "duplicate"


def _block_keys(row: sqlite3.Row) -> list[str]:
    keys = []
    tn = row["title_norm"]
    if tn:
        keys.append("t:" + tn[:24])
        tokens = sorted({t for t in tn.split() if len(t) >= 5}, key=lambda t: (-len(t), t))[:3]
        if len(tokens) == 3:
            keys.append("k:" + "|".join(sorted(tokens)))
        if row["first_author"]:
            keys.append(f"a:{row['first_author']}")
    return keys


def find_duplicates(conn: sqlite3.Connection, cfg: DedupConfig, actor: Actor = AUTOMATED,
                    paper_ids: Iterable[str] | None = None) -> dict[str, int]:
    """Detect identifier matches and similarity candidates. Safe to run repeatedly.

    With ``paper_ids`` only pairs that involve one of those records are examined
    (used after an import). Pairs that already have a relation of the same type,
    whatever its status, are left alone, so reviewer decisions are never undone.
    """
    stats = dict(identifier_matches=0, auto_grouped=0, identifier_conflicts=0, candidates_identifier=0,
                 candidates_similarity=0, pairs_scored=0, blocks_skipped_too_large=0)
    rows = _papers(conn)
    focus = set(paper_ids) if paper_ids is not None else None

    def canon(pid: str) -> str:
        seen = set()
        while rows[pid]["canonical_id"] and pid not in seen and rows[pid]["canonical_id"] in rows:
            seen.add(pid)
            pid = rows[pid]["canonical_id"]
        return pid

    with transaction(conn):
        # ---- pass 1: shared persistent identifiers
        by_value: dict[tuple[str, str], dict[str, bool]] = defaultdict(dict)
        for r in conn.execute(
                "SELECT paper_id, scheme, value_norm, validation_status FROM paper_identifier WHERE active = 1"
                " AND scheme IN ('doi','pmid','pmcid') AND validation_status NOT IN ('invalid_syntax','not_found')"):
            if r["paper_id"] in rows:
                holders = by_value[(r["scheme"], r["value_norm"])]
                holders[r["paper_id"]] = holders.get(r["paper_id"], False) or r["validation_status"] == "validated"
        for (scheme, value), holders in by_value.items():
            if len(holders) < 2 or (focus is not None and not focus & set(holders)):
                continue
            ordered = sorted(holders, key=lambda p: rows[p]["pk"])
            anchor = ordered[0]
            for other in ordered[1:]:
                if focus is not None and anchor not in focus and other not in focus:
                    continue
                if canon(anchor) == canon(other):
                    continue
                stats["identifier_matches"] += 1
                sim = title_similarity(rows[anchor]["title"], rows[other]["title"])
                details = {"scheme": scheme, "value": value, "title_similarity": sim,
                           "validated": {anchor: holders[anchor], other: holders[other]}}
                differing = [x for x in conflicting_identifiers(rows[anchor], rows[other]) if x != scheme]
                if differing or (sim is not None and sim < cfg.identifier_title_conflict_below):
                    details["other_identifiers_differ"] = differing
                    rid = add_relation(conn, anchor, other, "identifier_conflict",
                                       "shared_identifier_other_identifiers_differ" if differing
                                       else "shared_identifier_titles_differ", actor, details=details)
                    if rid:
                        stats["identifier_conflicts"] += 1
                        audit.record(conn, actor, "paper.identifier_conflict", "paper", other,
                                     {"other_paper": anchor, **details, "relation_id": rid})
                    continue
                prior = next(iter(existing_relation(conn, anchor, other, "duplicate")), None)
                if prior or existing_relation(conn, anchor, other, "preprint_published"):
                    # A reviewer's decision is never undone. An undecided identifier candidate is settled
                    # once the identifier has been validated on both records.
                    if prior and prior["status"] == "candidate" and holders[anchor] and holders[other] \
                            and prior["method"] == "identifier_match_unvalidated":
                        conn.execute(
                            "UPDATE paper_relation SET status = 'confirmed', score = 1.0, decided_at = ?,"
                            " decided_by = ?, decision_note = ? WHERE id = ?",
                            (utc_now(), actor.name, "identifier since validated on both records", prior["id"]))
                        group(conn, canon(anchor), canon(other), actor, "validated_identifier_match", prior["id"])
                        rows = _papers(conn)
                        stats["auto_grouped"] += 1
                    continue
                if holders[anchor] and holders[other]:
                    rid = add_relation(conn, anchor, other, "duplicate", "validated_identifier_match", actor,
                                       status="confirmed", score=1.0, details=details,
                                       note="same validated identifier on both records")
                    group(conn, canon(anchor), canon(other), actor, "validated_identifier_match", rid)
                    rows = _papers(conn)
                    stats["auto_grouped"] += 1
                else:
                    add_relation(conn, anchor, other, "duplicate", "identifier_match_unvalidated", actor,
                                 details=details)
                    stats["candidates_identifier"] += 1

        # ---- pass 2: title / first author / year similarity (review candidates only)
        blocks: dict[str, list[str]] = defaultdict(list)
        for pid, r in rows.items():
            if r["canonical_id"]:
                continue
            for key in _block_keys(r):
                blocks[key].append(pid)
        seen_pairs: set[tuple[str, str]] = set()
        for key, members in blocks.items():
            if len(members) < 2:
                continue
            if len(members) > cfg.max_block_size:
                stats["blocks_skipped_too_large"] += 1
                continue
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    if focus is not None and a not in focus and b not in focus:
                        continue
                    pair = (a, b) if rows[a]["pk"] < rows[b]["pk"] else (b, a)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    ra, rb = rows[pair[0]], rows[pair[1]]
                    stats["pairs_scored"] += 1
                    s = score_pair(ra, rb, cfg)
                    if s["score"] is None or s["score"] < cfg.candidate_threshold or \
                            (s["components"]["title"] or 0) < cfg.title_floor:
                        continue
                    if existing_relation(conn, pair[0], pair[1]):
                        continue
                    add_relation(conn, pair[0], pair[1], _relation_type_for(ra, rb), "title_author_year_similarity",
                                 actor, score=s["score"],
                                 details={"components": s["components"], "weights_used": s["weights_used"],
                                          "threshold": cfg.candidate_threshold, "title_floor": cfg.title_floor})
                    stats["candidates_similarity"] += 1
        if any(stats[k] for k in ("auto_grouped", "identifier_conflicts", "candidates_identifier",
                                  "candidates_similarity")):
            audit.record(conn, actor, "dedup.run", "project", None, stats)
    return stats


def confirm_identifier_candidates(conn: sqlite3.Connection, actor: Actor, min_title_similarity: float = 0.9,
                                  note: str | None = None) -> dict[str, int]:
    """Reviewer-authorised bulk confirmation of duplicate candidates that share an identifier.

    Only candidates found by an identifier match are touched, and only when both
    titles are known and at least ``min_title_similarity`` alike. Each one is
    recorded as a decision of ``actor``; the rest stay in the review queue.
    """
    _require_reviewer(conn, actor)
    stats = {"confirmed": 0, "left_for_review": 0}
    cands = conn.execute("SELECT * FROM paper_relation WHERE status = 'candidate' AND relation_type = 'duplicate'"
                         " AND method = 'identifier_match_unvalidated' ORDER BY created_at, id").fetchall()
    for rel in cands:
        sim = loads(rel["score_details_json"], {}).get("title_similarity")
        if sim is None or sim < min_title_similarity:
            stats["left_for_review"] += 1
            continue
        decide_relation(conn, rel["id"], actor, "confirmed",
                        note=note or f"bulk confirmation: shared identifier and title similarity >= "
                                     f"{min_title_similarity}")
        stats["confirmed"] += 1
    return stats


# ------------------------------------------------------------------ views
def relations(conn: sqlite3.Connection, status: str | None = "candidate", relation_type: str | None = None,
              limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Relations with both records' citation data, for the duplicate review queue."""
    where, params = [], []
    if status:
        where.append("r.status = ?")
        params.append(status)
    if relation_type:
        where.append("r.relation_type = ?")
        params.append(relation_type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM paper_relation r{clause}", params).fetchone()[0]
    cols = "id, title, authors_json, year, journal, doi, pmid, pmcid, origin, publication_status, citation_text," \
           " canonical_id, identity_status"
    out = []
    for r in conn.execute(f"SELECT r.* FROM paper_relation r{clause} ORDER BY r.score IS NULL, r.score DESC,"
                          f" r.created_at, r.id LIMIT ? OFFSET ?", (*params, limit, offset)):
        item = dict(r)
        item["a"] = dict(conn.execute(f"SELECT {cols} FROM paper WHERE id = ?", (r["paper_a"],)).fetchone())
        item["b"] = dict(conn.execute(f"SELECT {cols} FROM paper WHERE id = ?", (r["paper_b"],)).fetchone())
        item["score_details"] = loads(r["score_details_json"], {})
        item["already_grouped"] = canonical_id(conn, r["paper_a"]) == canonical_id(conn, r["paper_b"])
        out.append(item)
    return {"rows": out, "total": total, "limit": limit, "offset": offset}


def groupings(conn: sqlite3.Connection, status: str | None = "active") -> list[dict]:
    sql = "SELECT * FROM paper_merge"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY created_at, id", params)]


__all__ = ["title_similarity", "score_pair", "conflicting_identifiers", "find_duplicates", "decide_relation",
           "relate", "group", "ungroup", "confirm_identifier_candidates", "relations", "groupings", "add_relation",
           "existing_relation", "RELATION_TYPES", "AUTOMATED", "SYSTEM"]
