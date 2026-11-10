"""Search runs: stored raw responses, parsed records and their links to paper records.

A search run is a snapshot. What was retrieved is stored byte-for-byte with its
SHA-256, and "reproducing" a run means re-reading that snapshot
(:mod:`geodesic.search.snapshot`). Running the same query again later is a new
run and may legitimately return different records.

Linking a retrieved record to a paper record
--------------------------------------------
1. ``exact_duplicate_record``: the identical provider record was already imported
   (same SHA-256 of the record as supplied) -> the same paper.
2. ``validated_identifier_match``: the record's own identifier (validated because
   the provider supplied it) equals a *validated* identifier of exactly one
   existing paper and the titles do not clearly differ -> the same paper. The
   source record is kept in ``search_result``; differing metadata is recorded as
   a conflict, never overwritten.
3. ``new_paper`` otherwise. Possible duplicates are then proposed by
   :func:`geodesic.dedup.find_duplicates` for review.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import audit, dedup, papers
from ..db import refresh_statistics, transaction
from ..errors import NotFoundError
from ..identifiers import normalize_identifier
from ..project import Project
from ..util import Actor, canonical_json, loads, new_id, sha256_bytes, sha256_text, utc_now

ID_SCHEMES = ("doi", "pmid", "pmcid")


class ParsedRecord(BaseModel):
    """Provider- or file-supplied bibliographic record in a common shape. Values are as supplied."""

    model_config = ConfigDict(extra="forbid")

    provider_record_id: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    publication_type: str | None = None          # original term(s) from the source
    language: str | None = None
    abstract: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    urls: list[str] = Field(default_factory=list)
    publication_status: Literal["preprint", "published", "unknown"] = "unknown"
    open_access: bool | None = None               # as reported by the source; None = not reported
    license: str | None = None                    # as reported by the source
    other_ids: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------ run records
def start_run(project: Project, *, provider: str, interface: str, mode: str, executed_query: str, actor: Actor,
              declared_data_class: str, concept_query: Any = None, translated_query: str | None = None,
              filters: dict | None = None, page_size: int | None = None, max_records: int | None = None,
              client_ident: str | None = None, source_file_name: str | None = None,
              source_file_sha256: str | None = None) -> str:
    """Create the run record. The protocol version in force is recorded with it.

    A run started while the protocol is provisional is labelled exploratory.
    """
    from .. import protocol as P

    conn = project.conn
    pv = P.current(conn)
    rid = new_id("srn")
    with transaction(conn):
        conn.execute(
            "INSERT INTO search_run (id, provider, interface, mode, concept_query_json, translated_query,"
            " executed_query, filters_json, started_at, page_size, max_records, status, protocol_version_id,"
            " protocol_status_at_run, exploratory, client_ident, source_file_name, source_file_sha256,"
            " declared_data_class, actor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, provider, interface, mode, canonical_json(concept_query) if concept_query is not None else None,
             translated_query, executed_query, canonical_json(filters or {}), utc_now(), page_size, max_records,
             "running", pv.id, pv.status, 0 if pv.is_confirmed else 1, client_ident, source_file_name,
             source_file_sha256, declared_data_class, actor.name))
        audit.record(conn, actor, "search.start", "search_run", rid,
                     {"provider": provider, "mode": mode, "executed_query": executed_query,
                      "protocol_version": pv.version, "protocol_status": pv.status,
                      "exploratory": not pv.is_confirmed, "max_records": max_records, "page_size": page_size})
    return rid


def store_raw_response(project: Project, run_id: str, seq: int, content: bytes, *, request_url: str | None,
                       request_params: dict | None, http_status: int | None, is_fixture: bool,
                       suffix: str = ".json", stored_path: Path | None = None) -> str:
    """Write a raw response under ``<project>/raw/search/<run>/`` and record its hash.

    ``request_url`` and ``request_params`` must already be redacted; credentials,
    contact addresses and request headers are never passed to this function.
    With ``stored_path`` the bytes are already preserved elsewhere in the project
    (imported files) and only the record is written.
    """
    if stored_path is None:
        directory = project.raw_dir / "search" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        stored_path = directory / f"{seq:05d}{suffix}"
        stored_path.write_bytes(content)
        stored_path.chmod(0o444)
    raw_id = new_id("raw")
    project.conn.execute(
        "INSERT INTO search_raw_response (id, search_run_id, seq, request_url, request_params_json, http_status,"
        " stored_path, sha256, size_bytes, retrieved_at, is_fixture) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (raw_id, run_id, seq, request_url, canonical_json(request_params or {}), http_status,
         project.rel(stored_path), sha256_bytes(content), len(content), utc_now(), 1 if is_fixture else 0))
    return raw_id


def record_hash(provider: str, raw: Any) -> str:
    """SHA-256 identifying one record exactly as supplied by one source."""
    return sha256_text(provider + "\n" + (raw if isinstance(raw, str) else canonical_json(raw)))


def _valid_ids(rec: ParsedRecord) -> dict[str, str]:
    out = {}
    for scheme in ID_SCHEMES:
        raw = getattr(rec, scheme)
        norm = normalize_identifier(scheme, raw) if raw else None
        if norm:
            out[scheme] = norm
    return out


def _incoming(rec: ParsedRecord, ids: dict[str, str]) -> dict[str, Any]:
    return {"title": rec.title, "authors": rec.authors, "year": rec.year, "journal": rec.journal,
            "publication_type": rec.publication_type, "language": rec.language, "abstract": rec.abstract,
            "publication_status": rec.publication_status, "urls": rec.urls, "other_ids": rec.other_ids, **ids}


def link_record(conn: sqlite3.Connection, rec: ParsedRecord, rec_sha: str, *, provider: str, id_source: str,
                ids_validated: bool, origin: str, conflict_below: float, now: str) -> tuple[str, str]:
    """Find or create the paper record for one retrieved record. Returns ``(paper_id, link_method)``."""
    seen = conn.execute("SELECT paper_id FROM search_result WHERE record_sha256 = ? AND paper_id IS NOT NULL"
                        " ORDER BY created_at, id LIMIT 1", (rec_sha,)).fetchone()
    if seen:
        return seen["paper_id"], "exact_duplicate_record"
    ids = _valid_ids(rec)
    source = f"{id_source}"
    if ids_validated and ids:
        holders: dict[str, bool] = {}
        for scheme, norm in ids.items():
            for r in conn.execute(
                    "SELECT pi.paper_id, pi.validation_status FROM paper_identifier pi JOIN paper p ON p.id ="
                    " pi.paper_id WHERE pi.scheme = ? AND pi.value_norm = ? AND pi.active = 1 AND"
                    " p.record_status != 'removed' ORDER BY p.pk", (scheme, norm)):
                holders[r["paper_id"]] = holders.get(r["paper_id"], True) and r["validation_status"] == "validated"
        canon = {papers.canonical_id(conn, pid) for pid in holders}
        if len(canon) == 1 and all(holders.values()):
            target = next(iter(holders))
            existing = conn.execute("SELECT title, doi, pmid, pmcid FROM paper WHERE id = ?", (target,)).fetchone()
            sim = dedup.title_similarity(existing["title"], rec.title)
            differing = dedup.conflicting_identifiers(existing, {s: ids.get(s) for s in ID_SCHEMES})
            if not differing and (sim is None or sim >= conflict_below):
                papers.merge_metadata(conn, target, _incoming(rec, ids), source, now)
                _add_ids(conn, target, rec, provider, id_source, ids_validated, now)
                if rec.open_access:
                    conn.execute("UPDATE paper SET fulltext_availability = 'open_access_reported', updated_at = ?"
                                 " WHERE id = ? AND fulltext_availability = 'not_checked'", (now, target))
                return target, "validated_identifier_match"
    first = next(iter(ids.items()), None)
    identity_key = f"{first[0]}:{first[1]}" if first else (
        f"{provider}:{rec.provider_record_id}" if rec.provider_record_id else None)
    pid = papers.create_paper(
        conn, origin=origin, resolution_status="resolved" if ids else "unresolved", identity_key=identity_key,
        title=rec.title, authors=rec.authors, year=rec.year, journal=rec.journal,
        publication_type=rec.publication_type, language=rec.language, abstract=rec.abstract, doi=ids.get("doi"),
        pmid=ids.get("pmid"), pmcid=ids.get("pmcid"), urls=rec.urls, other_ids=rec.other_ids,
        publication_status=rec.publication_status,
        fulltext_availability="open_access_reported" if rec.open_access else "not_checked", now=now)
    if ids_validated and ids:
        conn.execute("UPDATE paper SET identity_status = 'identifier_validated' WHERE id = ?", (pid,))
    _add_ids(conn, pid, rec, provider, id_source, ids_validated, now)
    return pid, "new_paper"


def _add_ids(conn: sqlite3.Connection, pid: str, rec: ParsedRecord, provider: str, id_source: str,
             ids_validated: bool, now: str) -> None:
    for scheme in ID_SCHEMES:
        raw = getattr(rec, scheme)
        if not raw:
            continue
        res = papers.add_identifier(conn, pid, scheme, raw, id_source, now)
        if ids_validated and res["validation_status"] == "unvalidated":
            conn.execute(
                "UPDATE paper_identifier SET validation_status = 'validated', validated_via = ?, validated_at = ?"
                " WHERE paper_id = ? AND scheme = ? AND value_norm = ?",
                (f"provider_record:{provider}", now, pid, scheme, res["value_norm"]))
    if rec.provider_record_id:
        papers.add_identifier(conn, pid, f"provider:{provider}", rec.provider_record_id, id_source, now)


def ingest_records(project: Project, run_id: str, raw_id: str | None, items: list[tuple[Any, ParsedRecord]], *,
                   provider: str, start_position: int, id_source: str, ids_validated: bool, origin: str) -> dict:
    """Store retrieved records (raw and parsed) and link each to a paper record.

    Returns counts by link method plus the IDs of newly created papers.
    """
    conn = project.conn
    below = project.config.dedup.identifier_title_conflict_below
    now = utc_now()
    stats: dict[str, Any] = {"records": 0, "new_paper": 0, "exact_duplicate_record": 0,
                             "validated_identifier_match": 0, "new_paper_ids": []}
    for offset, (raw, rec) in enumerate(items):
        sha = record_hash(provider, raw)
        pid, method = link_record(conn, rec, sha, provider=provider, id_source=id_source,
                                  ids_validated=ids_validated, origin=origin, conflict_below=below, now=now)
        conn.execute(
            "INSERT INTO search_result (id, search_run_id, raw_response_id, position, provider_record_id,"
            " record_sha256, raw_json, parsed_json, paper_id, link_method, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("srs"), run_id, raw_id, start_position + offset, rec.provider_record_id, sha,
             raw if isinstance(raw, str) else canonical_json(raw), canonical_json(rec.model_dump(mode="json")),
             pid, method, now))
        stats["records"] += 1
        stats[method] += 1
        if method == "new_paper":
            stats["new_paper_ids"].append(pid)
    return stats


def finish_run(project: Project, run_id: str, actor: Actor, *, status: str, note: str | None = None,
               errors: list | None = None, resume_cursor: str | None = None,
               new_paper_ids: list[str] | None = None, link_counts: dict | None = None) -> dict:
    """Close a run, look for duplicates among the records it added and write the audit event."""
    conn = project.conn
    with transaction(conn):
        conn.execute("UPDATE search_run SET status = ?, completeness_note = ?, errors_json = ?, resume_cursor = ?,"
                     " finished_at = ? WHERE id = ?",
                     (status, note, canonical_json(errors or []), resume_cursor, utc_now(), run_id))
        run = get_run(conn, run_id)
        audit.record(conn, actor, "search.finish", "search_run", run_id,
                     {"status": status, "retrieved_count": run["retrieved_count"],
                      "provider_total": run["provider_total"], "pages_retrieved": run["pages_retrieved"],
                      "note": note, "errors": len(errors or []), "links": link_counts or {}})
    refresh_statistics(conn)
    dup = dedup.find_duplicates(conn, project.config.dedup, paper_ids=new_paper_ids) if new_paper_ids else {}
    return {"run": get_run(conn, run_id), "dedup": dup}


# ------------------------------------------------------------------ views
def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM search_run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown search run {run_id}")
    out = dict(row)
    out["concept_query"] = loads(row["concept_query_json"])
    out["filters"] = loads(row["filters_json"], {})
    out["errors"] = loads(row["errors_json"], [])
    out["link_counts"] = {r["link_method"]: r["n"] for r in conn.execute(
        "SELECT link_method, COUNT(*) AS n FROM search_result WHERE search_run_id = ? GROUP BY link_method",
        (run_id,))}
    out["raw_responses"] = conn.execute("SELECT COUNT(*) FROM search_raw_response WHERE search_run_id = ?",
                                        (run_id,)).fetchone()[0]
    out["label"] = run_label(out)
    return out


def run_label(run: dict | sqlite3.Row) -> str:
    """One-line description that always states mode, completeness and protocol status."""
    mode = {"live": "LIVE", "fixture": "FIXTURE (synthetic responses)", "file_import": "FILE IMPORT"}[run["mode"]]
    total = "unknown" if run["provider_total"] is None else str(run["provider_total"])
    explor = " · exploratory (protocol provisional)" if run["exploratory"] else ""
    return (f"{run['provider']} · {mode} · {run['status']} · retrieved {run['retrieved_count']}"
            f" of {total} reported{explor}")


def list_runs(conn: sqlite3.Connection, limit: int = 50, offset: int = 0) -> list[dict]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM search_run ORDER BY started_at DESC, id LIMIT ? OFFSET ?",
                                         (limit, offset))]
    return [get_run(conn, i) for i in ids]


def run_results(conn: sqlite3.Connection, run_id: str, limit: int = 50, offset: int = 0) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM search_result WHERE search_run_id = ?", (run_id,)).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT sr.position, sr.provider_record_id, sr.link_method, sr.paper_id, sr.record_sha256, p.title, p.year,"
        " p.doi, p.canonical_id FROM search_result sr LEFT JOIN paper p ON p.id = sr.paper_id"
        " WHERE sr.search_run_id = ? ORDER BY sr.position LIMIT ? OFFSET ?", (run_id, limit, offset))]
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}
