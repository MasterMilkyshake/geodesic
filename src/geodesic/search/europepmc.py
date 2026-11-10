"""Europe PMC search connector (default; needs no credentials).

Interface: Europe PMC RESTful Web Service, ``GET /search`` with ``format=json``,
``resultType=core`` and ``cursorMark`` pagination. See DECISIONS.md for what was
read in the provider documentation and when.

Query translation
-----------------
Concept groups from the project configuration are translated as
``(synonym OR "multi word synonym" OR ...) AND (...)``: synonyms within a group
are OR-ed, groups are AND-ed. An optional provider field prefix is applied to
every term. A source-specific query can be supplied instead and is executed
exactly as written. The executed query string is always stored with the run.

This connector retrieves what one provider returns for one query. It makes no
claim that the result set is exhaustive.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import __version__, audit, provenance
from ..config import ConceptGroup, load_local_settings
from ..db import transaction
from ..errors import ConfigError, GeodesicError, ProvenanceError, RetrievalError
from ..project import Project
from ..util import Actor, utc_now
from . import runs
from .http import HttpClient
from .runs import ParsedRecord

PROVIDER = "europepmc"
BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
INTERFACE = "Europe PMC RESTful Web Service GET /search (format=json, resultType=core, cursorMark pagination)"
MAX_PAGE_SIZE = 1000
_PLAIN_TERM = re.compile(r"^[\w\-]+$", re.UNICODE)


class ResponseSchemaError(RetrievalError):
    """The provider answered, but not in the documented response shape."""


# ------------------------------------------------------------------ query translation
def _term(term: str, field: str | None) -> str:
    t = term.strip()
    quoted = t if _PLAIN_TERM.match(t) else '"' + t.replace('"', " ") + '"'
    return f"{field}:{quoted}" if field else quoted


def translate(concepts: list[ConceptGroup], field: str | None = None) -> str:
    """Europe PMC query for the configured concept groups."""
    groups = []
    for g in concepts:
        terms = [_term(t, field) for t in g.terms if t and t.strip()]
        if terms:
            groups.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    return " AND ".join(groups)


def _filter_clause(filters: dict[str, Any]) -> str:
    parts = []
    lo, hi = filters.get("year_from"), filters.get("year_to")
    if lo is not None or hi is not None:
        parts.append(f"PUB_YEAR:[{int(lo) if lo is not None else 1000} TO {int(hi) if hi is not None else 3000}]")
    if filters.get("open_access"):
        parts.append("OPEN_ACCESS:y")
    return " AND ".join(parts)


def build_query(project: Project, query: str | None = None, concepts: list[ConceptGroup] | None = None,
                filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Work out the query that will be executed and how it was derived.

    Precedence: explicit ``query`` argument, then ``search.provider_query_override.europepmc``,
    then the translation of ``search.concepts``.
    """
    cfg = project.config.search
    concepts = concepts if concepts is not None else cfg.concepts
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "", False)}
    translated = translate(concepts, cfg.provider_field.get(PROVIDER)) if concepts else None
    if query and query.strip():
        base, derivation = query.strip(), "explicit source-specific query"
    elif cfg.provider_query_override.get(PROVIDER):
        base, derivation = cfg.provider_query_override[PROVIDER].strip(), "provider_query_override from project.yaml"
    elif translated:
        base, derivation = translated, "translation of configured concept groups"
    else:
        raise ConfigError("No search query. Define search.concepts (or search.provider_query_override.europepmc)"
                          " in project.yaml, or pass a query explicitly.")
    clause = _filter_clause(filters)
    executed = f"({base}) AND {clause}" if clause else base
    return {"concept_query": [g.model_dump() for g in concepts] if concepts else None,
            "translated_query": translated, "executed_query": executed,
            "filters": {**filters, "derivation": derivation}}


# ------------------------------------------------------------------ response parsing
@dataclass
class ParsedPage:
    hit_count: int | None
    next_cursor: str | None
    items: list[tuple[dict, ParsedRecord]]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_record(raw: dict) -> ParsedRecord:
    """Map one ``resultList.result`` entry to the common record shape (values as supplied)."""
    authors = [a.get("fullName") for a in (raw.get("authorList") or {}).get("author") or []
               if isinstance(a, dict) and a.get("fullName")]
    if not authors and raw.get("authorString"):
        authors = [a.strip().rstrip(".") for a in str(raw["authorString"]).split(",") if a.strip()]
    types = [str(t) for t in (raw.get("pubTypeList") or {}).get("pubType") or []]
    source = _text(raw.get("source"))
    year = None
    try:
        year = int(str(raw.get("pubYear"))[:4]) if raw.get("pubYear") else None
    except ValueError:
        year = None
    journal = _text(((raw.get("journalInfo") or {}).get("journal") or {}).get("title")) or \
        _text((raw.get("bookOrReportDetails") or {}).get("publisher"))
    urls = [u.get("url") for u in (raw.get("fullTextUrlList") or {}).get("fullTextUrl") or []
            if isinstance(u, dict) and u.get("url")]
    is_preprint = source == "PPR" or any(t.casefold() == "preprint" for t in types)
    oa = raw.get("isOpenAccess")
    rid = f"{source}:{raw.get('id')}" if source and raw.get("id") else _text(raw.get("id"))
    return ParsedRecord(
        provider_record_id=rid, title=_text(raw.get("title")), authors=authors, year=year, journal=journal,
        publication_type="; ".join(types) or None, language=_text(raw.get("language")),
        abstract=_text(raw.get("abstractText")), doi=_text(raw.get("doi")), pmid=_text(raw.get("pmid")),
        pmcid=_text(raw.get("pmcid")), urls=urls,
        publication_status="preprint" if is_preprint else "published" if source else "unknown",
        open_access=None if oa is None else str(oa).upper() == "Y", license=_text(raw.get("license")),
        other_ids={"europepmc_source": source} if source else {})


def parse_page(content: bytes) -> ParsedPage:
    """Parse one stored response page. Raises :class:`ResponseSchemaError` on an unrecognised shape."""
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResponseSchemaError(f"Europe PMC response is not valid JSON ({exc}).") from exc
    if not isinstance(data, dict) or "hitCount" not in data:
        raise ResponseSchemaError("Europe PMC response has no 'hitCount'; the response schema was not recognised.")
    results = (data.get("resultList") or {}).get("result")
    if results is None:
        results = []
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise ResponseSchemaError("Europe PMC 'resultList.result' is not a list of records.")
    try:
        hit = int(data["hitCount"])
    except (TypeError, ValueError) as exc:
        raise ResponseSchemaError("Europe PMC 'hitCount' is not a number.") from exc
    return ParsedPage(hit, _text(data.get("nextCursorMark")), [(r, parse_record(r)) for r in results])


# ------------------------------------------------------------------ fixtures
def fixture_transport(directory: Path) -> httpx.MockTransport:
    """Serve the JSON files of ``directory`` as response pages, selected by the request's ``cursorMark``.

    The first file (sorted by name) answers ``cursorMark=*``; each later file
    answers the ``nextCursorMark`` of the file before it. An unknown cursor is
    answered with 404, as is any request once the pages are exhausted.
    """
    files = sorted(p for p in Path(directory).glob("*.json"))
    if not files:
        raise GeodesicError(f"No fixture response pages (*.json) found in {directory}.")
    by_cursor: dict[str, Path] = {}
    cursor: str | None = "*"
    for f in files:
        if cursor is None:
            break
        by_cursor.setdefault(cursor, f)
        try:
            body = json.loads(f.read_bytes())
            cursor = body.get("nextCursorMark") if isinstance(body, dict) else None
        except ValueError:
            cursor = None

    def handler(request: httpx.Request) -> httpx.Response:
        f = by_cursor.get(request.url.params.get("cursorMark", "*"))
        if f is None:
            return httpx.Response(404, text="no fixture page for this cursor")
        return httpx.Response(200, content=f.read_bytes(), headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def _client(project: Project, transport: httpx.BaseTransport | None, sleep: Callable[[float], None],
            live: bool) -> tuple[HttpClient, str]:
    cfg = project.config.search
    contact = load_local_settings(project.root).contact_email if live else None
    client = HttpClient(
        min_interval_seconds=cfg.min_interval_seconds if live else 0.0, max_retries=cfg.max_retries,
        backoff_seconds=cfg.backoff_seconds, timeout_seconds=cfg.timeout_seconds,
        headers={"User-Agent": f"{cfg.tool_name}/{__version__}", "Accept": "application/json"},
        secret_params={"email": contact} if contact else None, transport=transport, sleep=sleep)
    ident = f"{cfg.tool_name}/{__version__}; contact address supplied: {'yes' if contact else 'no'}"
    return client, ident


# ------------------------------------------------------------------ running a search
def run_search(project: Project, actor: Actor, *, query: str | None = None,
               concepts: list[ConceptGroup] | None = None, filters: dict[str, Any] | None = None,
               max_records: int | None = None, page_size: int | None = None, mode: str | None = None,
               fixture_dir: Path | None = None, transport: httpx.BaseTransport | None = None,
               declare: str | None = None, resume_run_id: str | None = None,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Execute (or resume) one Europe PMC search and import what it returns.

    ``mode='live'`` contacts the provider and is refused in synthetic projects.
    ``mode='fixture'`` replays stored responses (``fixture_dir``) or a supplied
    transport; fixture responses must carry the synthetic tag or be declared.

    Returns ``{"run": <run record>, "dedup": <duplicate-detection counts>, "links": <counts by link method>}``.
    """
    conn = project.conn
    cfg = project.config.search
    mode = mode or ("fixture" if fixture_dir is not None else "live")
    if mode not in ("live", "fixture"):
        raise ConfigError("mode must be 'live' or 'fixture'.")
    if mode == "live":
        provenance.require_live_allowed(project.data_class, "A live Europe PMC search")
        if fixture_dir is not None:
            raise ConfigError("fixture_dir cannot be combined with mode='live'.")
        data_class = "real"
    else:
        if fixture_dir is not None:
            tags = {provenance.detect_embedded_tag(p) for p in sorted(Path(fixture_dir).glob("*.json"))}
            tag = "synthetic" if tags == {"synthetic"} else None
            data_class = provenance.resolve_import_class(project.data_class, declare, tag,
                                                         f"Fixture response directory {Path(fixture_dir).name}")
            transport = fixture_transport(Path(fixture_dir))
        elif transport is None:
            raise ConfigError("mode='fixture' needs fixture_dir (or a transport).")
        else:
            data_class = project.data_class        # checked against the first response below

    page_size = min(int(page_size or cfg.page_size), MAX_PAGE_SIZE)
    if page_size < 1:
        raise ConfigError("page_size must be at least 1.")

    if resume_run_id:
        run = runs.get_run(conn, resume_run_id)
        if run["provider"] != PROVIDER or run["mode"] != mode:
            raise GeodesicError(f"Run {resume_run_id} is a {run['provider']} {run['mode']} run and cannot be"
                                f" resumed as a {PROVIDER} {mode} run.")
        if run["status"] not in ("incomplete", "truncated") or not run["resume_cursor"]:
            raise GeodesicError(f"Run {resume_run_id} has status '{run['status']}' and no stored cursor;"
                                " there is nothing to resume. Start a new run instead.")
        run_id, executed, cursor = run["id"], run["executed_query"], run["resume_cursor"]
        page_size = run["page_size"] or page_size
        max_records = int(max_records or run["max_records"] or cfg.max_records)
        retrieved, seq, provider_total = run["retrieved_count"], run["raw_responses"], run["provider_total"]
        errors = list(run["errors"])
        seen = {r[0] for r in conn.execute("SELECT record_sha256 FROM search_result WHERE search_run_id = ?",
                                           (run_id,))}
        client, _ = _client(project, transport, sleep, mode == "live")
        with transaction(conn):
            conn.execute("UPDATE search_run SET status = 'running', max_records = ? WHERE id = ?",
                         (max_records, run_id))
            audit.record(conn, actor, "search.resume", "search_run", run_id,
                         {"from_retrieved": retrieved, "max_records": max_records})
    else:
        q = build_query(project, query, concepts, filters)
        executed, cursor = q["executed_query"], "*"
        max_records = int(max_records or cfg.max_records)
        client, ident = _client(project, transport, sleep, mode == "live")
        run_id = runs.start_run(
            project, provider=PROVIDER, interface=INTERFACE, mode=mode, executed_query=executed, actor=actor,
            declared_data_class=data_class, concept_query=q["concept_query"], translated_query=q["translated_query"],
            filters=q["filters"], page_size=page_size, max_records=max_records, client_ident=ident)
        retrieved, seq, provider_total, errors = 0, 0, None, []
        seen = set()

    status, note, resume_cursor = "complete", None, None
    new_ids: list[str] = []
    links = {"new_paper": 0, "exact_duplicate_record": 0, "validated_identifier_match": 0}
    notes: list[str] = []
    try:
        while True:
            if retrieved >= max_records:
                status, resume_cursor = "truncated", cursor
                notes.append(f"stopped at the retrieval limit of {max_records} records")
                break
            params = {"query": executed, "resultType": "core", "format": "json", "pageSize": page_size,
                      "cursorMark": cursor}
            try:
                resp = client.get(BASE_URL, params)
            except RetrievalError as exc:
                status, resume_cursor = "incomplete", cursor
                errors.append({"at": utc_now(), "cursor": cursor, "kind": "retrieval_failed", "message": str(exc)})
                notes.append("retrieval stopped after a request failed; the run can be resumed")
                break
            for r in resp.retries:
                errors.append({"at": utc_now(), "cursor": cursor, "kind": "retried_request", **r})
            if mode == "fixture" and fixture_dir is None and seq == 0 and not resume_run_id:
                tag = "synthetic" if provenance.TAG_TEXT.encode() in resp.content[:4096] else None
                provenance.resolve_import_class(project.data_class, declare, tag, "The fixture response")
            try:
                page = parse_page(resp.content)
            except ResponseSchemaError as exc:
                with transaction(conn):
                    runs.store_raw_response(project, run_id, seq, resp.content, request_url=resp.request_url,
                                            request_params=resp.request_params, http_status=resp.status,
                                            is_fixture=mode == "fixture")
                status, resume_cursor = "incomplete", cursor
                errors.append({"at": utc_now(), "cursor": cursor, "kind": "unrecognised_response",
                               "message": str(exc)})
                notes.append("a response did not have the documented shape; it was stored but not imported")
                break
            # a resumed run may re-read a partly imported page: records already in the run are not imported twice
            items = [it for it in page.items if runs.record_hash(PROVIDER, it[0]) not in seen] if seen \
                else page.items
            take = items[: max_records - retrieved]
            with transaction(conn):
                raw_id = runs.store_raw_response(project, run_id, seq, resp.content, request_url=resp.request_url,
                                                 request_params=resp.request_params, http_status=resp.status,
                                                 is_fixture=mode == "fixture")
                st = runs.ingest_records(project, run_id, raw_id, take, provider=PROVIDER, start_position=retrieved,
                                         id_source=f"search_provider:{PROVIDER}", ids_validated=True,
                                         origin="search")
                if provider_total is not None and page.hit_count != provider_total:
                    notes.append(f"provider-reported total changed during retrieval ({provider_total} ->"
                                 f" {page.hit_count})")
                provider_total = page.hit_count
                retrieved += len(take)
                seq += 1
                conn.execute("UPDATE search_run SET pages_retrieved = ?, retrieved_count = ?, provider_total = ?,"
                             " resume_cursor = ? WHERE id = ?",
                             (seq, retrieved, provider_total, page.next_cursor, run_id))
            new_ids += st["new_paper_ids"]
            for k in links:
                links[k] += st[k]
            if len(take) < len(items):
                status, resume_cursor = "truncated", cursor
                notes.append(f"stopped at the retrieval limit of {max_records} records; {len(items) - len(take)}"
                             " further record(s) of the last stored response were not imported")
                break
            more_pages = bool(page.items) and page.next_cursor and page.next_cursor != cursor
            if provider_total is not None and retrieved >= provider_total:
                break
            if not more_pages:
                if provider_total is not None and retrieved < provider_total:
                    status = "incomplete"
                    notes.append(f"provider reported {provider_total} records but pagination ended after {retrieved}")
                break
            cursor = page.next_cursor
        if status == "truncated" and provider_total is not None and retrieved >= provider_total:
            status, resume_cursor = "complete", None
    except ProvenanceError as exc:
        runs.finish_run(project, run_id, actor, status="failed", note=str(exc),
                        errors=errors + [{"at": utc_now(), "kind": "provenance", "message": str(exc)}])
        raise
    finally:
        client.close()
    if status == "truncated" and provider_total is not None:
        notes.append(f"{provider_total - retrieved} record(s) reported by the provider were not retrieved")
    out = runs.finish_run(project, run_id, actor, status=status, note="; ".join(notes) or None, errors=errors,
                          resume_cursor=resume_cursor if status != "complete" else None, new_paper_ids=new_ids,
                          link_counts=links)
    out["links"] = links
    return out
