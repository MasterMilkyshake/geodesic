"""Crossref metadata lookups: identifier validation and enrichment (not a search source).

What a lookup establishes
-------------------------
* ``GET /works/{doi}`` answering 200 shows that the DOI is registered with
  Crossref and returns the registry's metadata. The identifier is then marked
  ``validated`` (via ``crossref``), empty metadata fields are filled and differing
  values are recorded as conflicts. A clearly different registry title is flagged
  as a possible misassigned identifier and nothing is filled.
* A 404 marks the identifier ``not_found`` in Crossref on that date. It may be
  registered elsewhere; the paper record is left as it is.
* The ``updated-by`` field of the work record is recorded as an integrity check:
  either the notices it lists, or "none found in Crossref on <date>".
* Free-text citations can be sent to ``/works?query.bibliographic=``; the answers
  are stored as *proposed* candidates that only a reviewer can accept.

Validation says the identifier exists and what the registry holds for it. It
does not verify anything a workbook says about the paper's content.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from typing import Any, Callable

import httpx

from .. import __version__, audit, dedup, papers, provenance
from ..config import ENV_CROSSREF_PLUS_TOKEN, load_local_settings, read_credential
from ..db import transaction
from ..errors import NotFoundError, RetrievalError, ReviewError, ValidationError
from ..identifiers import normalize_doi
from ..logging_utils import register_secret
from ..project import Project
from ..util import (Actor, canonical_json, loads, new_id, normalize_text_key, sha256_bytes, utc_now,
                    utc_today)
from .http import HttpClient

PROVIDER = "crossref"
BASE_URL = "https://api.crossref.org/v1"
SOURCE = f"metadata_lookup:{PROVIDER}"
_NOTICE_TYPES = {"retraction": "retraction", "withdrawal": "retraction", "removal": "retraction",
                 "correction": "correction", "erratum": "correction", "corrigendum": "correction",
                 "addendum": "correction", "expression_of_concern": "expression_of_concern",
                 "expression-of-concern": "expression_of_concern"}


def make_client(project: Project, transport: httpx.BaseTransport | None = None,
                sleep: Callable[[float], None] = time.sleep, live: bool | None = None) -> HttpClient:
    """Client with the configured identification. Contact address and token are never stored or logged.

    ``live`` defaults to "no transport supplied". A supplied transport serves
    fixture responses unless ``live=True`` is stated (used to test the live
    request path without a network).
    """
    live = transport is None if live is None else live
    if live:
        provenance.require_live_allowed(project.data_class, "A Crossref lookup")
    cfg = project.config.search
    contact = load_local_settings(project.root).contact_email if live else None
    token = read_credential(ENV_CROSSREF_PLUS_TOKEN, project.root) if live else None
    headers = {"User-Agent": f"{cfg.tool_name}/{__version__}", "Accept": "application/json"}
    if token:
        register_secret(token)
        headers["crossref-api-key"] = f"Bearer {token}"
    return HttpClient(min_interval_seconds=cfg.min_interval_seconds if live else 0.0, max_retries=cfg.max_retries,
                      backoff_seconds=cfg.backoff_seconds, timeout_seconds=cfg.timeout_seconds, headers=headers,
                      secret_params={"mailto": contact} if contact else None, transport=transport, sleep=sleep)


def parse_work(msg: dict) -> dict[str, Any]:
    """Metadata of a Crossref work record in the paper-metadata shape (values as supplied)."""
    def first(key: str) -> str | None:
        v = msg.get(key)
        v = v[0] if isinstance(v, list) and v else v
        return str(v).strip() if isinstance(v, str) and v.strip() else None

    authors = []
    for a in msg.get("author") or []:
        name = ", ".join(x for x in (a.get("family"), a.get("given")) if x) or a.get("name")
        if name:
            authors.append(name)
    year = None
    for key in ("published", "issued", "published-print", "published-online"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    kind = msg.get("type")
    preprint = kind == "posted-content" and msg.get("subtype") in (None, "preprint")
    notices = []
    for u in msg.get("updated-by") or []:
        notices.append({"type": str(u.get("type") or "").lower(), "doi": u.get("DOI"), "label": u.get("label"),
                        "date": ((u.get("updated") or {}).get("date-time") or "")[:10] or None})
    return {"title": first("title"), "authors": authors, "year": year, "journal": first("container-title"),
            "publication_type": kind, "language": first("language"),
            "publication_status": "preprint" if preprint else "published" if kind else "unknown",
            "doi": normalize_doi(msg.get("DOI")), "notices": notices,
            "relations": {k: [r.get("id") for r in v if isinstance(r, dict)]
                          for k, v in (msg.get("relation") or {}).items() if isinstance(v, list)}}


def title_agreement(paper: sqlite3.Row | dict, registry_title: str | None) -> float | None:
    """How well a registry title agrees with what the project holds for the paper (0..1, None = cannot tell).

    With a stored title: word-level title similarity. With only a free-text
    citation: the share of the registry title's words that occur in the citation.
    """
    if paper["title"]:
        return dedup.title_similarity(paper["title"], registry_title)
    words = normalize_text_key(registry_title or "").split()
    cited = set(normalize_text_key(paper["citation_text"] or "").split())
    if not words or not cited:
        return None
    return round(sum(w in cited for w in words) / len(words), 4)


def _store_raw(project: Project, content: bytes) -> str:
    sha = sha256_bytes(content)
    directory = project.raw_dir / PROVIDER
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.json"
    if not path.exists():
        path.write_bytes(content)
        path.chmod(0o444)
    return sha


def _fixture_check(project: Project, content: bytes, live: bool) -> None:
    if not live:
        tag = "synthetic" if provenance.TAG_TEXT.encode() in content[:4096] else None
        provenance.resolve_import_class(project.data_class, None, tag, "The fixture Crossref response")


def _candidate(conn: sqlite3.Connection, paper_id: str, query: str, basis: str, candidate: dict, doi: str | None,
               score: float | None, status: str, sha: str | None, actor: Actor | None = None) -> str:
    cid = new_id("cit")
    now = utc_now()
    decided = status in ("auto_attached", "not_found")
    conn.execute(
        "INSERT INTO citation_candidate (id, paper_id, provider, looked_up_at, query, match_basis, candidate_json,"
        " candidate_doi, score, status, decided_by, decided_at, response_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, paper_id, PROVIDER, now, query, basis, canonical_json(candidate), doi, score, status,
         actor.name if actor and decided else None, now if decided else None, sha))
    return cid


def validate_paper_doi(project: Project, paper_id: str, actor: Actor, client: HttpClient, *,
                       live: bool) -> dict[str, Any]:
    """Look up the paper's DOI in Crossref and record the outcome. Returns a small result dict.

    ``live`` is False when ``client`` serves fixture responses; those must carry the synthetic tag.
    """
    conn = project.conn
    row = conn.execute("SELECT * FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    if not row["doi"]:
        return {"paper_id": paper_id, "outcome": "no_doi"}
    doi = row["doi"]
    url = f"{BASE_URL}/works/{urllib.parse.quote(doi, safe='/')}"
    now, today = utc_now(), utc_today()
    try:
        resp = client.get(url)
    except RetrievalError as exc:
        if "HTTP 404" not in str(exc):
            raise
        with transaction(conn):
            conn.execute("UPDATE paper_identifier SET validation_status = 'not_found', validated_via = ?,"
                         " validated_at = ? WHERE paper_id = ? AND scheme = 'doi' AND value_norm = ? AND"
                         " validation_status = 'unvalidated'", (PROVIDER, now, paper_id, doi))
            _candidate(conn, paper_id, doi, "exact_identifier", {}, doi, None, "not_found", None, actor)
            audit.record(conn, actor, "paper.identifier_lookup", "paper", paper_id,
                         {"provider": PROVIDER, "doi": doi, "outcome": "not_found", "checked_on": today})
        return {"paper_id": paper_id, "outcome": "not_found", "doi": doi}
    _fixture_check(project, resp.content, live)
    try:
        msg = json.loads(resp.content.decode("utf-8"))["message"]
        work = parse_work(msg)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RetrievalError(f"Crossref answered for {doi} but not in the documented response shape ({exc}).") from exc
    sha = _store_raw(project, resp.content)
    sim = title_agreement(row, work["title"])
    mismatch = (work["doi"] is not None and work["doi"] != doi) or \
        (sim is not None and sim < project.config.dedup.identifier_title_conflict_below)
    with transaction(conn):
        if mismatch:
            papers.record_conflict(conn, paper_id, "title", row["title"], work["title"], SOURCE, now)
            conn.execute("UPDATE paper SET identity_status = 'conflict', updated_at = ? WHERE id = ?", (now, paper_id))
            conn.execute("UPDATE paper_identifier SET validation_status = 'conflict', validated_via = ?,"
                         " validated_at = ? WHERE paper_id = ? AND scheme = 'doi' AND value_norm = ? AND"
                         " validation_status = 'unvalidated'", (PROVIDER, now, paper_id, doi))
            _candidate(conn, paper_id, doi, "exact_identifier", work, work["doi"], sim, "proposed", sha)
            audit.record(conn, actor, "paper.identifier_conflict", "paper", paper_id,
                         {"provider": PROVIDER, "doi": doi, "title_similarity": sim,
                          "registry_title": work["title"], "checked_on": today})
            return {"paper_id": paper_id, "outcome": "possible_misassignment", "doi": doi, "title_similarity": sim}
        conn.execute("UPDATE paper_identifier SET validation_status = 'validated', validated_via = ?,"
                     " validated_at = ? WHERE paper_id = ? AND scheme = 'doi' AND value_norm = ? AND"
                     " validation_status IN ('unvalidated', 'not_found')", (PROVIDER, now, paper_id, doi))
        merged = papers.merge_metadata(conn, paper_id, {k: work[k] for k in (
            "title", "authors", "year", "journal", "publication_type", "language", "publication_status")}, SOURCE, now)
        conn.execute("UPDATE paper SET identity_status = 'identifier_validated', updated_at = ? WHERE id = ? AND"
                     " identity_status = 'unverified'", (now, paper_id))
        _candidate(conn, paper_id, doi, "exact_identifier", work, work["doi"], sim, "auto_attached", sha, actor)
        audit.record(conn, actor, "paper.identifier_lookup", "paper", paper_id,
                     {"provider": PROVIDER, "doi": doi, "outcome": "validated", "filled": merged["filled"],
                      "conflicts": merged["conflicts"], "checked_on": today, "response_sha256": sha})
        results = []
        for n in work["notices"]:
            result = _NOTICE_TYPES.get(n["type"], "other_notice")
            papers.record_integrity_check(
                conn, paper_id, "Crossref (updated-by field of the work record)", result, actor,
                details=f"{n['type'] or 'notice'}: {n.get('label') or ''} {n.get('date') or ''}".strip(),
                notice_url=f"https://doi.org/{n['doi']}" if n.get("doi") else None, checked_on=today)
            results.append(result)
        if not work["notices"]:
            papers.record_integrity_check(conn, paper_id, "Crossref (updated-by field of the work record)",
                                          "none_found", actor, checked_on=today)
    return {"paper_id": paper_id, "outcome": "validated", "doi": doi, "filled": merged["filled"],
            "conflicts": merged["conflicts"], "notices": results}


def validate_identifiers(project: Project, actor: Actor, *, paper_ids: list[str] | None = None, limit: int = 50,
                         transport: httpx.BaseTransport | None = None,
                         sleep: Callable[[float], None] = time.sleep, live: bool | None = None) -> dict[str, Any]:
    """Validate unvalidated DOIs (at most ``limit`` lookups), then re-run duplicate detection.

    Errors on single lookups are counted and reported; they do not change any record.
    """
    conn = project.conn
    live = transport is None if live is None else live
    client = make_client(project, transport, sleep, live)
    if paper_ids is None:
        paper_ids = [r["paper_id"] for r in conn.execute(
            "SELECT DISTINCT pi.paper_id FROM paper_identifier pi JOIN paper p ON p.id = pi.paper_id"
            " WHERE pi.scheme = 'doi' AND pi.validation_status = 'unvalidated' AND pi.active = 1 AND"
            " p.record_status != 'removed' AND p.doi = pi.value_norm ORDER BY p.pk LIMIT ?", (limit,))]
    stats: dict[str, Any] = {"looked_up": 0, "validated": 0, "not_found": 0, "possible_misassignment": 0,
                             "no_doi": 0, "errors": [], "provider": PROVIDER, "checked_on": utc_today()}
    try:
        for pid in paper_ids[:limit]:
            try:
                out = validate_paper_doi(project, pid, actor, client, live=live)
            except RetrievalError as exc:
                stats["errors"].append({"paper_id": pid, "message": str(exc)})
                continue
            stats["looked_up"] += out["outcome"] != "no_doi"
            stats[out["outcome"]] += 1
    finally:
        client.close()
    stats["dedup"] = dedup.find_duplicates(conn, project.config.dedup) if stats["validated"] else {}
    return stats


def propose_for_citation(project: Project, paper_id: str, actor: Actor, *, rows: int = 3,
                         transport: httpx.BaseTransport | None = None,
                         sleep: Callable[[float], None] = time.sleep, live: bool | None = None) -> list[dict]:
    """Ask Crossref for records resembling an unresolved citation; store them as proposals for review."""
    conn = project.conn
    row = conn.execute("SELECT * FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    text = row["citation_text"] or row["title"]
    if not text:
        raise ValidationError("This paper record has no citation text or title to look up.")
    live = transport is None if live is None else live
    client = make_client(project, transport, sleep, live)
    try:
        resp = client.get(f"{BASE_URL}/works", {"query.bibliographic": text, "rows": rows})
    finally:
        client.close()
    _fixture_check(project, resp.content, live)
    try:
        items = json.loads(resp.content.decode("utf-8"))["message"]["items"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RetrievalError(f"Crossref answered but not in the documented response shape ({exc}).") from exc
    sha = _store_raw(project, resp.content)
    out = []
    with transaction(conn):
        for item in items[:rows]:
            work = parse_work(item)
            sim = title_agreement(row, work["title"])
            cid = _candidate(conn, paper_id, text, "bibliographic_similarity", work, work["doi"], sim, "proposed", sha)
            out.append({"candidate_id": cid, "doi": work["doi"], "title": work["title"], "year": work["year"],
                        "title_similarity": sim})
        audit.record(conn, actor, "paper.citation_lookup", "paper", paper_id,
                     {"provider": PROVIDER, "proposals": len(out), "response_sha256": sha})
    return out


def decide_candidate(conn: sqlite3.Connection, candidate_id: str, actor: Actor, status: str) -> dict[str, Any]:
    """A reviewer accepts or rejects a proposed registry record for a paper."""
    if actor.kind not in ("human", "simulated"):
        raise ReviewError("Only a named reviewer can accept or reject a proposed citation match.")
    if actor.kind == "simulated" and \
            conn.execute("SELECT data_class FROM project").fetchone()["data_class"] != "synthetic":
        raise ReviewError("Simulated review actions are only permitted in synthetic projects.")
    if status not in ("accepted", "rejected"):
        raise ValidationError("status must be 'accepted' or 'rejected'.")
    cand = conn.execute("SELECT * FROM citation_candidate WHERE id = ?", (candidate_id,)).fetchone()
    if cand is None:
        raise NotFoundError(f"Unknown citation candidate {candidate_id}")
    if cand["status"] != "proposed":
        raise ReviewError(f"This candidate is already '{cand['status']}'.")
    now = utc_now()
    work = loads(cand["candidate_json"], {})
    filled: list[str] = []
    with transaction(conn):
        conn.execute("UPDATE citation_candidate SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                     (status, actor.name, now, candidate_id))
        if status == "accepted":
            if cand["candidate_doi"]:
                papers.add_identifier(conn, cand["paper_id"], "doi", cand["candidate_doi"], SOURCE, now)
                conn.execute("UPDATE paper_identifier SET validation_status = 'validated', validated_via = ?,"
                             " validated_at = ? WHERE paper_id = ? AND scheme = 'doi' AND value_norm = ?",
                             (f"{PROVIDER}+reviewer", now, cand["paper_id"], cand["candidate_doi"]))
            merged = papers.merge_metadata(conn, cand["paper_id"], {**{k: work.get(k) for k in (
                "title", "authors", "year", "journal", "publication_type", "language", "publication_status")},
                "doi": cand["candidate_doi"]}, SOURCE, now)
            filled = merged["filled"]
            conn.execute("UPDATE paper SET resolution_status = 'resolved', identity_status = 'reviewer_verified',"
                         " updated_at = ? WHERE id = ? AND doi IS NOT NULL", (now, cand["paper_id"]))
        audit.record(conn, actor, "paper.citation_candidate", "paper", cand["paper_id"],
                     {"candidate_id": candidate_id, "status": status, "doi": cand["candidate_doi"], "filled": filled})
    return {"candidate_id": candidate_id, "status": status, "filled": filled}
