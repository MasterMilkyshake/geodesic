"""Automatic full-text retrieval, limited to open-access records and a documented API.

Only one route is implemented: the Europe PMC RESTful service
``GET /europepmc/webservices/rest/{PMCID}/fullTextXML``, which serves full-text
XML for articles of the open-access subset. A retrieval is attempted only when

* a search source reported the record as open access, and
* the record carries a PMCID.

Nothing else is tried: no publisher sites, no link resolvers, no HTML scraping.
A failed retrieval is recorded as ``retrieval_failed`` (with the reason) and no
document is attached; it is never presented as "no full text exists".

The licence stated inside the retrieved XML is recorded with the document,
together with the licence the search source reported.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import __version__, audit, provenance
from ..config import load_local_settings
from ..db import transaction
from ..errors import GeodesicError, NotFoundError, RetrievalError, ValidationError
from ..project import Project
from ..search.http import HttpClient
from ..util import Actor, sha256_bytes, utc_now
from . import parsers, store

PROVIDER = "europepmc"
SOURCE = f"open_access_api:{PROVIDER}"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"


def reported_open_access(project: Project, paper_id: str) -> dict[str, Any]:
    """What search sources reported about open access for the paper (and records grouped with it)."""
    conn = project.conn
    ids = store.group_paper_ids(conn, paper_id)
    marks = ",".join("?" * len(ids))
    reported, licenses = False, []
    for r in conn.execute(f"SELECT parsed_json FROM search_result WHERE paper_id IN ({marks}) ORDER BY created_at", ids):
        rec = json.loads(r["parsed_json"])
        if rec.get("open_access") is True:
            reported = True
            if rec.get("license") and rec["license"] not in licenses:
                licenses.append(rec["license"])
    pmcids = sorted(store.known_identifiers(conn, paper_id)["pmcid"])
    return {"reported_open_access": reported, "licenses_reported": licenses, "pmcids": pmcids}


def _failed(project: Project, paper_id: str, actor: Actor, reason: str, url: str | None) -> dict[str, Any]:
    conn = project.conn
    now = utc_now()
    with transaction(conn):
        conn.execute("UPDATE paper SET fulltext_availability = 'retrieval_failed', updated_at = ? WHERE id = ? AND"
                     " fulltext_availability != 'document_attached'", (now, paper_id))
        audit.record(conn, actor, "document.retrieval_failed", "paper", paper_id,
                     {"provider": PROVIDER, "url": url, "reason": reason})
    return {"paper_id": paper_id, "outcome": "retrieval_failed", "reason": reason, "document_id": None}


def fetch_open_access(project: Project, paper_id: str, actor: Actor, *,
                      transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                      live: bool | None = None) -> dict[str, Any]:
    """Retrieve open-access full-text XML for one paper and attach it. Returns the outcome.

    ``transport`` serves fixture responses (which must carry the synthetic tag)
    unless ``live=True``. Live retrieval is refused in synthetic projects.
    """
    conn = project.conn
    if conn.execute("SELECT 1 FROM paper WHERE id = ?", (paper_id,)).fetchone() is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    live = transport is None if live is None else live
    if live:
        provenance.require_live_allowed(project.data_class, "Open-access full-text retrieval")
    info = reported_open_access(project, paper_id)
    if not info["reported_open_access"]:
        raise ValidationError(
            "No search source reported this record as open access. Automatic retrieval is limited to records "
            "reported as open access; attach a file you are entitled to use instead (documents add).")
    if not info["pmcids"]:
        raise ValidationError("This record has no PMCID; the open-access full-text service is addressed by PMCID.")
    pmcid = info["pmcids"][0]
    url = FULLTEXT_URL.format(pmcid=pmcid)
    cfg = project.config.search
    contact = load_local_settings(project.root).contact_email if live else None
    client = HttpClient(min_interval_seconds=cfg.min_interval_seconds if live else 0.0, max_retries=cfg.max_retries,
                        backoff_seconds=cfg.backoff_seconds, timeout_seconds=cfg.timeout_seconds,
                        headers={"User-Agent": f"{cfg.tool_name}/{__version__}", "Accept": "application/xml"},
                        secret_params={"email": contact} if contact else None, transport=transport, sleep=sleep)
    try:
        resp = client.get(url)
    except RetrievalError as exc:
        reason = "the service answered HTTP 404 (no open-access full text under this PMCID)" \
            if "HTTP 404" in str(exc) else str(exc)
        return _failed(project, paper_id, actor, reason, url)
    finally:
        client.close()
    content = resp.content
    tmp_dir = Path(tempfile.mkdtemp(prefix="geodesic-oa-"))
    tmp = tmp_dir / f"{pmcid}.xml"
    try:
        tmp.write_bytes(content)
        if live:
            declare = "real"
        else:
            if provenance.detect_embedded_tag(tmp) != "synthetic":
                raise provenance.ProvenanceError(
                    "The fixture full-text response does not carry the synthetic tag; fixture responses must be "
                    "tagged synthetic.")
            declare = None
        parsed = parsers.parse_xml(tmp)
        if parsed.status == "failed":
            return _failed(project, paper_id, actor,
                           f"the response was not usable full-text XML ({parsed.failure_reason})", url)
        try:
            out = store.add_document(project, paper_id, tmp, actor, declare=declare, source=SOURCE, source_url=url,
                                     license=parsed.license or (info["licenses_reported"] or [None])[0],
                                     original_filename=f"{pmcid}.xml")
        except ValidationError as exc:
            return _failed(project, paper_id, actor, str(exc), url)
    finally:
        tmp.unlink(missing_ok=True)
        tmp_dir.rmdir()
    with transaction(conn):
        audit.record(conn, actor, "document.retrieved", "document", out["document_id"],
                     {"provider": PROVIDER, "url": url, "pmcid": pmcid, "live": live, "http_status": resp.status,
                      "response_sha256": sha256_bytes(content), "bytes": len(content),
                      "license_in_document": parsed.license, "licenses_reported_by_search": info["licenses_reported"]})
    return {"paper_id": paper_id, "outcome": "already_present" if out["already_present"] else "attached",
            "document_id": out["document_id"], "pmcid": pmcid, "url": url, "live": live,
            "license": out.get("license", parsed.license), "license_in_document": parsed.license,
            "licenses_reported_by_search": info["licenses_reported"], "snapshot": out["snapshot"],
            "sha256": sha256_bytes(content), "bytes": len(content)}


def fetch_candidates(project: Project, limit: int = 50) -> list[str]:
    """Papers reported open access with a PMCID and no usable document yet."""
    conn = project.conn
    out = []
    for r in conn.execute("SELECT id FROM paper WHERE fulltext_availability IN ('open_access_reported',"
                          " 'retrieval_failed') AND record_status != 'removed' AND canonical_id IS NULL ORDER BY pk"):
        info = reported_open_access(project, r["id"])
        if info["reported_open_access"] and info["pmcids"]:
            out.append(r["id"])
            if len(out) >= limit:
                break
    return out


__all__ = ["PROVIDER", "SOURCE", "FULLTEXT_URL", "reported_open_access", "fetch_open_access", "fetch_candidates",
           "GeodesicError"]
