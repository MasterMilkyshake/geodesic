"""Credentials and contact details never reach raw-response archives, the database, exports or logs.

The live request path is exercised without a network: a project declared
``real`` receives generated responses from a mock transport with ``mode='live'``.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import httpx
import pytest

from conftest import OPERATOR
from geodesic import papers
from geodesic.export import export_tables
from geodesic.logging_utils import REDACTED, redact, redact_headers, redact_params
from geodesic.search import crossref, europepmc, snapshot
from geodesic.synthetic import search as SS

EMAIL = "contact.person@example.invalid"
TOKEN = "plus-token-0123456789abcdef"
ENCODED = "contact.person%40example.invalid"


def _untagged_pages(page_size=4):
    """Generated provider-shaped pages without the fixture tag (what a live answer looks like)."""
    out = []
    for page in SS.europepmc_pages(page_size):
        body = json.loads(page)
        body.pop("geodesicFixture")
        out.append(json.dumps(body).encode("utf-8"))
    return out


def _leaks(root: Path, needles: list[str]) -> list[str]:
    """Files under ``root`` whose content (or zip members' content) contains one of ``needles``."""
    found = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        blobs = [path.read_bytes()]
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                blobs += [z.read(n) for n in z.namelist()]
        for needle in needles:
            if any(needle.encode() in b for b in blobs):
                found.append(f"{path.relative_to(root)}: {needle}")
    return found


def _db_text(conn) -> str:
    return "\n".join(conn.iterdump())


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("GEODESIC_CONTACT_EMAIL", EMAIL)
    monkeypatch.setenv("GEODESIC_CROSSREF_PLUS_TOKEN", TOKEN)
    return [EMAIL, ENCODED, TOKEN]


def test_redaction_helpers():
    assert redact_params({"query": "x", "email": EMAIL, "mailto": EMAIL, "api_key": "abc"}) == \
        {"query": "x", "email": REDACTED, "mailto": REDACTED, "api_key": REDACTED}
    heads = redact_headers({"Accept": "application/json", "crossref-api-key": f"Bearer {TOKEN}",
                            "Authorization": "Bearer zzz", "User-Agent": "tool/1 (mailto:a@b.invalid)"})
    assert heads == {"Accept": "application/json", "crossref-api-key": REDACTED, "Authorization": REDACTED,
                     "User-Agent": REDACTED}
    text = redact(f"GET https://example.invalid/works?mailto={ENCODED}&rows=3 crossref-api-key: Bearer {TOKEN}")
    assert EMAIL not in text and ENCODED not in text and TOKEN not in text and "rows=3" in text


def test_live_search_sends_contact_but_stores_it_nowhere(real_project, secrets, tmp_path):
    seen: list[httpx.Request] = []
    pages = iter(_untagged_pages())
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, text=f"busy; echo of the request: {request.url}")
        if state["n"] == 2:
            raise httpx.ConnectError(f"connection reset while requesting {request.url}")
        return httpx.Response(200, content=next(pages), headers={"content-type": "application/json"})

    out = europepmc.run_search(real_project, OPERATOR, query="generated", page_size=4, mode="live",
                               transport=httpx.MockTransport(handler), sleep=lambda s: None)
    run = out["run"]
    assert run["status"] == "complete" and run["retrieved_count"] == 10 and run["mode"] == "live"
    # the contact address really was sent with every request ...
    assert len(seen) == 5 and all(r.url.params.get("email") == EMAIL for r in seen)
    # ... and the run record only says that one was supplied
    assert "contact address supplied: yes" in json.dumps(run)
    kinds = [e["kind"] for e in run["errors"]]
    assert kinds == ["retried_request", "retried_request"]
    # the transport error quoted the full request URL; the stored problem text has the address masked
    assert "connection reset while requesting" in json.dumps(run["errors"]) and REDACTED in json.dumps(run["errors"])
    assert EMAIL not in json.dumps(run) and ENCODED not in json.dumps(run)
    assert snapshot.verify_snapshot(real_project, run["id"])["intact"]

    # a run that fails outright keeps a redacted error
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    failed = europepmc.run_search(real_project, OPERATOR, query="generated again", page_size=4, mode="live",
                                  transport=httpx.MockTransport(refuse), sleep=lambda s: None)["run"]
    assert failed["status"] in ("failed", "incomplete") and failed["errors"]
    assert EMAIL not in json.dumps(failed) and ENCODED not in json.dumps(failed)

    exported = export_tables(real_project, tmp_path / "exported", formats=("xlsx", "csv", "json"))
    assert exported["files"]
    for handler_ in logging.getLogger("geodesic").handlers:
        handler_.flush()
    assert (real_project.logs_dir / "geodesic.log").stat().st_size > 0
    real_project.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert _leaks(real_project.root, secrets) == []
    assert _leaks(tmp_path / "exported", secrets) == []
    dump = _db_text(real_project.conn)
    assert all(s not in dump for s in secrets)


def test_live_crossref_sends_token_and_contact_but_stores_neither(real_project, secrets, tmp_path):
    conn = real_project.conn
    works = SS.crossref_works()
    doi = "10.5555/geodesic.synth.001"
    pid = papers.create_paper(conn, origin="manual", resolution_status="resolved", doi=doi,
                              citation_text="Arden Q, Belmont T. Tracer release from a stirred source tank. 2019.")
    papers.add_identifier(conn, pid, "doi", doi, "manual")
    gone = papers.create_paper(conn, origin="manual", resolution_status="resolved", doi="10.5555/geodesic.synth.777",
                               citation_text="Generated citation with an unregistered identifier")
    papers.add_identifier(conn, gone, "doi", "10.5555/geodesic.synth.777", "manual")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        work = works.get(request.url.path.split("/works/", 1)[-1])
        if request.url.path.endswith("/works"):
            body = {"status": "ok", "message": {"items": list(works.values())[:2]}}
        elif work is None:
            return httpx.Response(404, text=f"not found: {request.url}")
        else:
            body = {"status": "ok", "message": work}
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    transport = httpx.MockTransport(handler)
    stats = crossref.validate_identifiers(real_project, OPERATOR, transport=transport, live=True,
                                          sleep=lambda s: None)
    assert (stats["validated"], stats["not_found"], stats["errors"]) == (1, 1, [])
    crossref.propose_for_citation(real_project, gone, OPERATOR, transport=transport, live=True, sleep=lambda s: None)
    assert len(seen) == 3
    assert all(r.headers["crossref-api-key"] == f"Bearer {TOKEN}" for r in seen)
    assert all(r.url.params.get("mailto") == EMAIL for r in seen)
    assert all(EMAIL not in r.headers["user-agent"] for r in seen)

    export_tables(real_project, tmp_path / "exported", formats=("xlsx", "csv", "json"))
    for handler_ in logging.getLogger("geodesic").handlers:
        handler_.flush()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert _leaks(real_project.root, secrets) == []
    assert _leaks(tmp_path / "exported", secrets) == []
    dump = _db_text(conn)
    assert all(s not in dump for s in secrets)
    assert list((real_project.raw_dir / "crossref").glob("*.json"))


def test_credentials_file_and_local_settings_are_read_but_not_copied(real_project, monkeypatch, tmp_path):
    (real_project.root / "credentials.env").write_text(f"GEODESIC_CROSSREF_PLUS_TOKEN={TOKEN}\n", encoding="utf-8")
    (real_project.root / "local.yaml").write_text(f"reviewer: Synthetic Operator\ncontact_email: {EMAIL}\n",
                                                  encoding="utf-8")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, text="not found")

    client = crossref.make_client(real_project, httpx.MockTransport(handler), live=True)
    with pytest.raises(Exception) as err:
        client.get(f"{crossref.BASE_URL}/works/10.5555/none")
    client.close()
    assert seen[0].headers["crossref-api-key"] == f"Bearer {TOKEN}" and seen[0].url.params["mailto"] == EMAIL
    assert all(s not in str(err.value) for s in (EMAIL, ENCODED, TOKEN))
    assert client.described_headers()["crossref-api-key"] == REDACTED
    # only the two local files the user wrote hold the values
    real_project.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for handler_ in logging.getLogger("geodesic").handlers:
        handler_.flush()
    holders = {leak.split(":")[0] for leak in _leaks(real_project.root, [EMAIL, TOKEN])}
    assert holders == {"credentials.env", "local.yaml"}
    # and both names are excluded from source control by the shipped ignore rules
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "credentials.env" in ignore and "local.yaml" in ignore


def test_fixture_mode_never_reads_contact_or_token(project, secrets):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return SS.crossref_transport().handle_request(request)

    client = crossref.make_client(project, httpx.MockTransport(handler))
    with pytest.raises(Exception):
        client.get(f"{crossref.BASE_URL}/works/10.5555/none")
    client.close()
    assert "crossref-api-key" not in seen[0].headers and "mailto" not in seen[0].url.params
