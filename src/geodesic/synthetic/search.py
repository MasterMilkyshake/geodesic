"""Synthetic search fixtures: provider-shaped response pages, an RIS file and a CSV file.

Every record is invented (inert tracer in engineered reservoirs; DOI prefix
``10.5555`` is reserved for examples). Each file carries the synthetic tag so it
can only be imported into a synthetic project. The pages follow the response
*shape* of the Europe PMC search service; they are not provider data.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from ..provenance import TAG_TEXT
from ..util import normalize_text_key

_ABS = {
    "S2": "A dye tracer pulse was injected into a baffled conduit linking two reservoirs. Transit time and"
          " dispersion were measured at three flow rates.",
    "S5": "This review summarises inert tracer methods used in engineered reservoirs and tanks.",
    "S6": "Washout of a dye tracer from a stirred tank was followed for 12 h.",
    "S7": "Decay of a reactive tracer was followed in a weir pool downstream of a holding reservoir.",
    "S9": "Purely computational study of conduit flow between tanks; no tracer measurements were made.",
}


def _rec(key: str, title: str, authors: list[str], year: int, *, source: str = "MED", doi: str | None = None,
         pmid: str | None = None, pmcid: str | None = None, types: tuple[str, ...] = ("research-article",),
         oa: str = "N", license_: str | None = None, journal: str = "Journal of Synthetic Hydraulics",
         abstract: str | None = None, cited: int = 0) -> dict:
    rec: dict = {"id": pmid or f"SYN{key[1:].zfill(6)}", "source": source, "title": title,
                 "authorString": ", ".join(authors) + ".",
                 "authorList": {"author": [{"fullName": a} for a in authors]},
                 "journalInfo": {"journal": {"title": journal}}, "pubYear": str(year),
                 "pubTypeList": {"pubType": list(types)}, "isOpenAccess": oa, "language": "eng",
                 "citedByCount": cited}
    if doi:
        rec["doi"] = doi
    if pmid:
        rec["pmid"] = pmid
    if pmcid:
        rec["pmcid"] = pmcid
    if license_:
        rec["license"] = license_
    if abstract:
        rec["abstractText"] = abstract
    if oa == "Y" and pmcid:
        rec["fullTextUrlList"] = {"fullTextUrl": [{"availability": "Open access", "documentStyle": "html",
                                                   "site": "Europe_PMC",
                                                   "url": f"https://example.invalid/synthetic/{pmcid}"}]}
    return rec


def records(variant: int = 1) -> list[dict]:
    """Synthetic records. ``variant=2`` is a later re-run: one record unchanged, one changed, one new."""
    s2 = _rec("S2", "Dye pulse dispersion in a baffled conduit", ["Norcross V", "Oakes W"], 2022,
              doi="10.5555/geodesic.synth.101", pmid="90000002", pmcid="PMC9000002", oa="Y", license_="cc by",
              abstract=_ABS["S2"])
    s5 = _rec("S5", "A review of inert tracer methods for engineered reservoirs", ["Quillon B"], 2020,
              doi="10.5555/geodesic.synth.105", pmid="90000005", types=("review", "journal article"),
              abstract=_ABS["S5"], cited=3 if variant == 1 else 9)
    if variant == 2:
        s11 = _rec("S11", "Tracer breakthrough at a reservoir outlet weir", ["Underhill C", "Vance D"], 2024,
                   doi="10.5555/geodesic.synth.111", pmid="90000011", abstract="A dye tracer was released into a"
                   " reservoir and breakthrough at the outlet weir was recorded.")
        return [s2, s5, s11]
    return [
        _rec("S1", "Tracer release from a stirred source tank", ["Arden Q", "Belmont T"], 2019,
             doi="10.5555/geodesic.synth.001", pmid="90000001",
             abstract="Release of an inert tracer from a stirred source tank into a conduit was measured."),
        s2,
        _rec("S3", "Dye pulse dispersion in a baffled conduit", ["Norcross V", "Oakes W"], 2021, source="PPR",
             doi="10.5555/geodesic.synth.preprint.101", types=("preprint",), journal="Synthetic Preprint Server",
             abstract=_ABS["S2"]),
        _rec("S4", "Dye-pulse dispersion in a baffled conduit.", ["Norcross V", "Oakes W"], 2022, source="AGR",
             abstract=_ABS["S2"]),
        s5,
        _rec("S6", "Tracer washout from a stirred tank", ["Rowntree E", "Sable F"], 2012,
             doi="10.5555/geodesic.synth.106", pmid="90000006", abstract=_ABS["S6"]),
        _rec("S7", "Reactive tracer decay in a weir pool", ["Thackeray G"], 2021,
             doi="10.5555/geodesic.synth.107", pmid="90000007", abstract=_ABS["S7"]),
        _rec("S8", "Thermal stratification of a storage pond", ["Ullswater H"], 2018,
             doi="10.5555/geodesic.synth.101", pmid="90000008",
             abstract="Temperature profiles of a storage pond were logged over one season."),
        _rec("S9", "Numerical study of conduit flow between tanks", ["Varley I"], 2023,
             doi="10.5555/geodesic.synth.109", pmid="90000009", abstract=_ABS["S9"]),
        _rec("S10", "Sediment sampling protocol", ["Wexcombe J"], 2022, doi="10.5555/geodesic.synth.110",
             pmid="90000010"),
    ]


def europepmc_pages(page_size: int = 4, variant: int = 1, hit_count: int | None = None) -> list[bytes]:
    """Response pages in the Europe PMC search response shape, tagged synthetic."""
    recs = records(variant)
    total = len(recs) if hit_count is None else hit_count
    pages = []
    chunks = [recs[i:i + page_size] for i in range(0, len(recs), page_size)] or [[]]
    for i, chunk in enumerate(chunks):
        body = {"geodesicFixture": TAG_TEXT, "version": "synthetic-fixture", "hitCount": total,
                "nextCursorMark": f"SYNCURSOR{i + 1:03d}", "nextPageUrl": None,
                "request": {"queryString": "synthetic", "resultType": "core", "cursorMark": "*" if i == 0
                            else f"SYNCURSOR{i:03d}", "pageSize": page_size},
                "resultList": {"result": chunk}}
        pages.append(json.dumps(body, indent=1).encode("utf-8"))
    return pages


def write_europepmc_fixture(directory: Path, page_size: int = 4, variant: int = 1) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(europepmc_pages(page_size, variant)):
        (directory / f"page_{i + 1:03d}.json").write_bytes(page)
    return directory


RIS_TEXT = f"""TY  - JOUR
N1  - {TAG_TEXT}
TI  - Dye pulse dispersion in a baffled conduit
AU  - Norcross, V.
AU  - Oakes, W.
PY  - 2022/03/14/
JO  - Journal of Synthetic Hydraulics
DO  - https://doi.org/10.5555/GEODESIC.SYNTH.101
AB  - A dye tracer pulse was injected into a baffled conduit linking two reservoirs.
  Transit time and dispersion were measured at three flow rates.
UR  - https://example.invalid/synthetic/ris/101
LA  - eng
ER  -

TY  - CONF
TI  - Holding reservoir retention of an inert tracer: a conference summary
AU  - Ingleby, K.
AU  - Jarrow, M.
PY  - 2022
T2  - Proceedings of the Synthetic Flow Conference
AN  - PMID: 90000042
ER  -

TY  - RPRT
TI  - Outflow tracer sampling at a discharge weir
A1  - Yarrow, L.
Y1  - 2019//
DO  - 10.5555/not a doi
ZZ  - an unknown tag is kept with the stored record
ER  -

TY  - JOUR
TI  - A record without the end-of-record tag
AU  - Zennor, M.
PY  - 2020
"""

CSV_TEXT = f"""# {TAG_TEXT}
Title,Authors,Year,Journal,DOI,PMID,Abstract,Publication type,Extra column
Tracer breakthrough at a reservoir outlet weir,"Underhill, C.; Vance, D.",2024,Journal of Synthetic Hydraulics,10.5555/geodesic.synth.111,90000011,"A dye tracer was released into a reservoir.",journal article,kept
"Dye pulse dispersion in a baffled conduit","Norcross, V.; Oakes, W.",2022,Journal of Synthetic Hydraulics,doi:10.5555/geodesic.synth.101,,,journal article,
Untitled synthetic entry,,n.d.,,,,,,
"""


def write_ris_fixture(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RIS_TEXT, encoding="utf-8")
    return path


def write_csv_fixture(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CSV_TEXT, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------------
# Synthetic Crossref-shaped responses (identifier validation and enrichment)
# ---------------------------------------------------------------------------------------------
def _work(doi: str, title: str, authors: list[tuple[str, str]], year: int, journal: str, **extra: Any) -> dict:
    return {"DOI": doi, "type": "journal-article", "title": [title], "container-title": [journal],
            "author": [{"family": f, "given": g} for f, g in authors], "issued": {"date-parts": [[year]]},
            "language": "en", **extra}


def crossref_works() -> dict[str, dict]:
    """Synthetic registry records keyed by DOI. DOIs not listed here answer 404 (not found).

    ``...001`` and ``...008`` agree with the fixture workbook; ``...002`` carries a
    correction notice; ``...004`` has an unrelated title (possible misassigned
    identifier); ``...007`` and ``...060`` are absent.
    """
    return {
        "10.5555/geodesic.synth.001": _work("10.5555/geodesic.synth.001", "Tracer release from a stirred source tank",
                                            [("Arden", "Q"), ("Belmont", "T")], 2019, "J Synth Hydraul"),
        "10.5555/geodesic.synth.002": _work(
            "10.5555/geodesic.synth.002", "Liner binding of an inert tracer",
            [("Castellan", "R"), ("Dunmore", "L"), ("Eastlake", "P")], 2020, "Synth Reserv Eng",
            **{"updated-by": [{"type": "correction", "label": "Correction", "DOI": "10.5555/geodesic.synth.902",
                               "updated": {"date-parts": [[2021, 3, 1]], "date-time": "2021-03-01T00:00:00Z"}}]}),
        "10.5555/geodesic.synth.004": _work("10.5555/geodesic.synth.004",
                                            "Quarterly maintenance schedule for pump bearings",
                                            [("Orrin", "V")], 2021, "Synth Plant Notes"),
        "10.5555/geodesic.synth.008": _work("10.5555/geodesic.synth.008", "Discharge clearance of an inert tracer",
                                            [("Lorimer", "S"), ("Marlow", "D")], 2023, "Synth Reserv Eng"),
        "10.5555/geodesic.synth.005": _work("10.5555/geodesic.synth.005", "Minimum flow for complete mixing",
                                            [("Hallam", "J")], 2018, "Proc Synth Flow Conf"),
    }


def crossref_transport(works: dict[str, dict] | None = None) -> httpx.MockTransport:
    """Offline stand-in for the Crossref REST API serving :func:`crossref_works`. Responses carry the synthetic tag."""
    works = crossref_works() if works is None else works

    def body(message: Any) -> bytes:
        return json.dumps({"geodesicFixture": TAG_TEXT, "status": "ok", "message": message}).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = urllib.parse.unquote(request.url.path)
        if "/works/" in path:
            work = works.get(path.split("/works/", 1)[1].lower())
            if work is None:
                return httpx.Response(404, content=b"Resource not found.")
            return httpx.Response(200, content=body(work), headers={"content-type": "application/json"})
        if path.endswith("/works"):
            wanted = set(normalize_text_key(request.url.params.get("query.bibliographic", "")).split())
            scored = sorted(works.values(), key=lambda w: -sum(
                t in wanted for t in normalize_text_key(w["title"][0]).split()))
            rows = int(request.url.params.get("rows", 3))
            return httpx.Response(200, content=body({"total-results": len(scored), "items": scored[:rows]}),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404, content=b"Resource not found.")

    return httpx.MockTransport(handler)
