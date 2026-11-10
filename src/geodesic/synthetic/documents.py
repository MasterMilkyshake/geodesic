"""Synthetic document fixtures: generated PDF and XML files (no real publication is used).

The documents describe invented experiments with an inert tracer in engineered
reservoirs and match the synthetic bibliographic records of
:mod:`geodesic.synthetic.search` and the fixture workbook. Every file carries the
synthetic tag (PDF: document information ``Keywords``; XML: a leading comment),
so it can only be attached in a synthetic project.

The PDF writer below produces small, valid, deterministic PDF 1.4 files with the
standard Helvetica font; it exists only to make fixtures without a third-party
PDF generator. ``image_only`` pages contain a bitmap and no text, like a scan.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from ..provenance import TAG_TEXT

# ---------------------------------------------------------------------------------------------
# Minimal PDF writer
# ---------------------------------------------------------------------------------------------
def _pdf_string(text: str) -> bytes:
    out = bytearray(b"(")
    for b in text.encode("cp1252"):
        if b in b"()\\":
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += b"\\%03o" % b
        else:
            out.append(b)
    return bytes(out + b")")


def build_pdf(pages: list[list[str] | None], *, title: str, keywords: str | None = TAG_TEXT) -> bytes:
    """A PDF with one text page per list of lines; ``None`` makes an image-only page (no text)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")           # 1, filled in below
    pages_obj = add(b"")         # 2
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    info = b"<< /Title " + _pdf_string(title) + b" /Producer (geodesic synthetic fixture writer)"
    if keywords:
        info += b" /Keywords " + _pdf_string(keywords)
    info_obj = add(info + b" >>")
    pixels = bytes((x * 16 + y * 16) % 256 for y in range(8) for x in range(8))
    image = add(b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray"
                b" /BitsPerComponent 8 /Length " + str(len(pixels)).encode() + b" >>\nstream\n" + pixels
                + b"\nendstream")
    kids: list[int] = []
    for lines in pages:
        if lines is None:
            stream = b"q 400 0 0 500 100 150 cm /Im1 Do Q"
            resources = b"<< /XObject << /Im1 " + str(image).encode() + b" 0 R >> >>"
        else:
            parts = [b"BT /F1 10 Tf 14 TL 72 770 Td"]
            for i, line in enumerate(lines):
                parts.append((b"" if i == 0 else b"T* ") + _pdf_string(line) + b" Tj")
            parts.append(b"ET")
            stream = b"\n".join(parts)
            resources = b"<< /Font << /F1 " + str(font).encode() + b" 0 R >> >>"
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent " + str(pages_obj).encode() + b" 0 R /MediaBox [0 0 612 792]"
                        b" /Resources " + resources + b" /Contents " + str(content).encode() + b" 0 R >>"))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages " + str(pages_obj).encode() + b" 0 R >>"
    objects[pages_obj - 1] = (b"<< /Type /Pages /Count " + str(len(kids)).encode() + b" /Kids ["
                              + b" ".join(str(k).encode() + b" 0 R" for k in kids) + b"] >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root " + str(catalog).encode()
            + b" 0 R /Info " + str(info_obj).encode() + b" 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def _write(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------------------------
# PDF fixtures
# ---------------------------------------------------------------------------------------------
PDF_TITLE = "Tracer release from a stirred source tank"
PDF_DOI = "10.5555/geodesic.synth.001"
PDF_PAGES: list[list[str]] = [
    [
        "Tracer release from a stirred source tank",
        "Arden Q, Belmont T. Journal of Synthetic Hydraulics (2019).",
        f"doi: {PDF_DOI}",
        "",
        "Abstract",
        "Release of an inert tracer from a stirred source tank into a conduit was measured.",
        "",
        "1 Methods",
        "The source tank held 250 L of water and was stirred at 120 rpm.",
        "A dye tracer (5 g) was added at the start of each run (n = 4 runs).",
    ],
    [
        "2 Results",
        "Tracer release reached 62 ± 5 % of the loaded dose after 30 min.",
        "The release rate constant was approximately 0.04 1/min.",
        "Between 85 and 95 % of the loaded dose had left the tank after 2 h.",
        "Less than 2 % of the loaded dose remained in the tank at 6 h.",
        "The outflow concentration peaked at 18.5 mg/L.",
    ],
    [
        "3 Limitations",
        "Release was measured in a single tank geometry; other geometries were not tested.",
        "Wall adsorption of the dye was not measured.",
    ],
]


def write_pdf_fixture(path: Path) -> Path:
    """Three text pages (title/methods, results, limitations)."""
    return _write(path, build_pdf(PDF_PAGES, title=PDF_TITLE))


def write_scanned_pdf_fixture(path: Path) -> Path:
    """Two image-only pages: a 'scan' with no extractable text."""
    return _write(path, build_pdf([None, None], title="Scanned synthetic report"))


def write_mixed_pdf_fixture(path: Path) -> Path:
    """One text page followed by one image-only page."""
    return _write(path, build_pdf([PDF_PAGES[1], None], title="Partly scanned synthetic report"))


def write_untagged_pdf_fixture(path: Path) -> Path:
    """A text PDF without the synthetic tag (its provenance must be declared)."""
    return _write(path, build_pdf([["An untagged synthetic page with 3 mg of tracer."]], title="Untagged",
                                  keywords=None))


def write_corrupt_pdf_fixture(path: Path) -> Path:
    """Bytes that are not a readable PDF (tagged in plain text so the provenance rules can be tested)."""
    return _write(path, b"%PDF-1.4\n% " + TAG_TEXT.encode() + b"\nthis is not a valid PDF body\n")


SUPPLEMENT_PAGES = [[
    "Supplementary material: Tracer release from a stirred source tank",
    "Table S1. Release at additional stirring speeds.",
    "At 60 rpm the release was 41 % of the loaded dose after 30 min.",
    "At 240 rpm the release was 78 % of the loaded dose after 30 min.",
]]


def write_supplement_pdf_fixture(path: Path) -> Path:
    return _write(path, build_pdf(SUPPLEMENT_PAGES, title="Supplementary material (synthetic)"))


# ---------------------------------------------------------------------------------------------
# XML fixture (article markup in the JATS style)
# ---------------------------------------------------------------------------------------------
XML_DOI = "10.5555/geodesic.synth.101"
XML_PMCID = "PMC9000002"
XML_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


def xml_article(doi: str = XML_DOI, pmcid: str = XML_PMCID, tagged: bool = True) -> str:
    tag = f"<!-- {TAG_TEXT} -->\n" if tagged else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{tag}<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">
  <front>
    <journal-meta><journal-title-group><journal-title>Journal of Synthetic Hydraulics</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">{doi}</article-id>
      <article-id pub-id-type="pmcid">{pmcid}</article-id>
      <title-group><article-title>Dye pulse dispersion in a baffled conduit</article-title></title-group>
      <contrib-group><contrib><name><surname>Norcross</surname><given-names>V</given-names></name></contrib></contrib-group>
      <pub-date><year>2022</year></pub-date>
      <permissions>
        <license license-type="open-access" xlink:href="{XML_LICENSE_URL}">
          <license-p>Synthetic fixture text, marked as distributed under CC BY 4.0 for testing.</license-p>
        </license>
      </permissions>
      <abstract>
        <p>A dye tracer pulse was injected into a baffled conduit linking two reservoirs. Transit time and
           dispersion were measured at three flow rates.</p>
      </abstract>
    </article-meta>
  </front>
  <body>
    <sec id="s1">
      <title>Methods</title>
      <p>The conduit was 12 m long and held 40 L of water.</p>
      <sec id="s1.1">
        <title>Tracer injection</title>
        <p>A pulse of 2 g of dye tracer was injected at the inlet in each run (n = 3 runs per flow rate).</p>
      </sec>
    </sec>
    <sec id="s2">
      <title>Results</title>
      <p>Mean transit time was 42 ± 3 min at a flow rate of 5 L/min. Recovery at the outlet was about
         91 % of the injected dose after 2 h.</p>
      <p>The dispersion coefficient ranged from 0.8 to 1.4 m<sup>2</sup>/min across the three flow rates, and
         more than 95 % of the recovered tracer passed the outlet within 90 min.</p>
      <table-wrap id="t1">
        <label>Table 1</label>
        <caption><p>Transit time and recovery by flow rate.</p></caption>
        <table>
          <thead>
            <tr><th>Flow rate (L/min)</th><th>Transit time (min)</th><th>Recovery (% of injected dose)</th></tr>
          </thead>
          <tbody>
            <tr><td>5</td><td>42 ± 3</td><td>91</td></tr>
            <tr><td>10</td><td>23 ± 2</td><td>88</td></tr>
            <tr><td>20</td><td>12 ± 1</td><td>not reported</td></tr>
          </tbody>
        </table>
      </table-wrap>
    </sec>
    <sec id="s3">
      <title>Limitations</title>
      <p>Only one conduit geometry was tested. Tracer loss to the conduit wall was not measured.</p>
    </sec>
  </body>
  <back>
    <ref-list>
      <ref id="r1"><mixed-citation>Arden Q, Belmont T. Tracer release from a stirred source tank. 2019;12:100-110.</mixed-citation></ref>
    </ref-list>
  </back>
</article>
"""


def write_xml_fixture(path: Path, **kwargs) -> Path:
    return _write(path, xml_article(**kwargs).encode("utf-8"))


def write_malformed_xml_fixture(path: Path) -> Path:
    return _write(path, f"<?xml version='1.0'?>\n<!-- {TAG_TEXT} -->\n<article><body><p>unclosed".encode("utf-8"))


def write_entity_xml_fixture(path: Path) -> Path:
    """XML declaring an entity (refused by the hardened parser; stored as a failed snapshot)."""
    return _write(path, (f"<?xml version='1.0'?>\n<!-- {TAG_TEXT} -->\n<!DOCTYPE article [<!ENTITY e \"expanded\">]>\n"
                         "<article><body><p>&e;</p></body></article>").encode("utf-8"))


# ---------------------------------------------------------------------------------------------
# Offline stand-in for the open-access full-text service
# ---------------------------------------------------------------------------------------------
def fulltext_transport(available: dict[str, bytes] | None = None, requests: list | None = None) -> httpx.MockTransport:
    """Serves ``/{PMCID}/fullTextXML`` for the synthetic open-access record; other IDs answer 404."""
    available = {XML_PMCID: xml_article().encode("utf-8")} if available is None else available

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        parts = request.url.path.rstrip("/").split("/")
        if parts[-1] == "fullTextXML" and parts[-2] in available:
            return httpx.Response(200, content=available[parts[-2]], headers={"content-type": "text/xml"})
        return httpx.Response(404, content=json.dumps({"error": "not found"}).encode())

    return httpx.MockTransport(handler)
