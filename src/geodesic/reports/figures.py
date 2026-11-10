"""Static figures: evidence-map heatmap, missing-information bars and the search/screening flow diagram.

Every figure is drawn from the dictionaries the CLI prints
(:func:`geodesic.reports.maps.evidence_map`,
:func:`geodesic.reports.missing.missing_information`,
:func:`geodesic.screening.summary`), so a figure can never show other numbers
than the tables. Figures are written as SVG, PDF and PNG (300 DPI by default)
and carry the project's data-class label inside the image, so a figure of a
synthetic project stays labelled when it is copied out of its report.

Drawing conventions (documented in docs/REPORTS.md)
---------------------------------------------------
* Layout is computed in CSS pixels (96 per inch) on one full-figure canvas; the
  bundled DejaVu Sans face is used so that output does not depend on installed fonts.
* Magnitude uses one hue, light to dark (heatmap). The six missing-information
  states are nominal categories and use six fixed categorical colours in a fixed
  order; the order was checked for colour-vision-deficiency separation, and three of
  the fills are below 3:1 contrast on the surface, which is why counts are printed on
  the segments where they fit and the same numbers are always written as a table.
* Text is drawn in ink colours, never in a data colour. Fills are separated by a
  2 px gap in the surface colour, not by outlines.
* Nothing is pooled, averaged, ranked or modelled in a figure: all values are counts
  of records in the project.
"""

from __future__ import annotations

import io
import math
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MPath
from matplotlib.textpath import TextPath

from .. import __version__, query
from ..errors import ValidationError
from . import maps as M
from . import missing as MI

PX_PER_INCH = 96
FONT = "DejaVu Sans"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
EMPTY = "#f0efec"
#: one hue, light -> dark (sequential)
SEQUENTIAL = ("#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf",
              "#1c5cab", "#184f95", "#104281", "#0d366b")
#: fixed categorical order (state -> fill, ink used on the fill)
STATE_COLOURS = {
    "recorded": ("#2a78d6", "#ffffff"),
    "not_reported": ("#eb6834", "#ffffff"),
    "not_applicable": ("#1baf7a", "#ffffff"),
    "not_yet_extracted": ("#eda100", "#0b0b0b"),
    "could_not_be_parsed": ("#e87ba4", "#ffffff"),
    "no_status_recorded": ("#008300", "#ffffff"),
}
STATE_LABELS = {
    "recorded": "recorded", "not_reported": "not reported by the source", "not_applicable": "not applicable",
    "not_yet_extracted": "not yet extracted", "could_not_be_parsed": "could not be parsed",
    "no_status_recorded": "empty, no status recorded",
}
FORMATS = ("svg", "pdf", "png")
MARGIN = 24


# ------------------------------------------------------------------ text measurement
@lru_cache(maxsize=20000)
def _text_width(text: str, size: float, bold: bool) -> float:
    """Width of ``text`` in px at ``size`` px."""
    if not text:
        return 0.0
    prop = FontProperties(family=FONT, weight="bold" if bold else "normal")
    tp = TextPath((0, 0), text, size=size * 72 / PX_PER_INCH, prop=prop)
    return float(tp.get_extents().width) * PX_PER_INCH / 72


def text_width(text: str, size: float = 12, bold: bool = False) -> float:
    return _text_width(str(text).replace("$", "\\$"), float(size), bool(bold))


def clip_text(text: str, size: float, max_width: float, bold: bool = False) -> str:
    """``text`` shortened with an ellipsis so that it fits ``max_width`` px."""
    text = str(text)
    if text_width(text, size, bold) <= max_width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_width(text[:mid].rstrip() + "…", size, bold) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def wrap_text(text: str, size: float, max_width: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in str(text).split():
        trial = f"{line} {word}".strip()
        if line and text_width(trial, size, bold) > max_width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _luminance(hex_colour: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ink_on(fill: str) -> str:
    """White or near-black, whichever reads better on ``fill``."""
    return "#ffffff" if _luminance(fill) < 0.28 else INK


def sequential_colour(value: int, vmax: int) -> str:
    """Fill for a count between 1 and ``vmax`` (more is darker)."""
    if vmax <= 1:
        return SEQUENTIAL[6]
    idx = round((value - 1) / (vmax - 1) * (len(SEQUENTIAL) - 1))
    return SEQUENTIAL[max(0, min(len(SEQUENTIAL) - 1, idx))]


# ------------------------------------------------------------------ canvas
class Canvas:
    """A fixed-size drawing surface in px with the origin at the top left."""

    def __init__(self, width: float, height: float, title: str, description: str) -> None:
        self.width, self.height, self.title, self.description = width, height, title, description
        self.fig = Figure(figsize=(width / PX_PER_INCH, height / PX_PER_INCH), dpi=PX_PER_INCH, facecolor=SURFACE)
        FigureCanvasAgg(self.fig)
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ax.set_xlim(0, width)
        self.ax.set_ylim(height, 0)
        self.ax.axis("off")
        #: every string drawn, in drawing order (lets tests and the HTML report check what a figure states)
        self.texts: list[str] = []

    def text(self, x: float, y: float, s: str, *, size: float = 12, colour: str = INK, ha: str = "left",
             va: str = "center", bold: bool = False) -> None:
        self.texts.append(str(s))
        self.ax.text(x, y, str(s).replace("$", "\\$"), fontsize=size * 72 / PX_PER_INCH, color=colour, ha=ha, va=va,
                     fontfamily=FONT, fontweight="bold" if bold else "normal")

    def box(self, x: float, y: float, w: float, h: float, fill: str | None, *, radii=(0, 0, 0, 0),
            edge: str | None = None, lw: float = 1.0) -> None:
        """Rectangle with per-corner radii ``(top-left, top-right, bottom-right, bottom-left)``."""
        if w <= 0 or h <= 0:
            return
        tl, tr, br, bl = (min(r, w / 2, h / 2) for r in radii)
        k = 0.5523
        v: list[tuple[float, float]] = [(x + tl, y)]
        c: list[int] = [MPath.MOVETO]

        def line(px: float, py: float) -> None:
            v.append((px, py))
            c.append(MPath.LINETO)

        def curve(c1, c2, end) -> None:
            v.extend([c1, c2, end])
            c.extend([MPath.CURVE4] * 3)

        line(x + w - tr, y)
        if tr:
            curve((x + w - tr + k * tr, y), (x + w, y + tr - k * tr), (x + w, y + tr))
        line(x + w, y + h - br)
        if br:
            curve((x + w, y + h - br + k * br), (x + w - br + k * br, y + h), (x + w - br, y + h))
        line(x + bl, y + h)
        if bl:
            curve((x + bl - k * bl, y + h), (x, y + h - bl + k * bl), (x, y + h - bl))
        line(x, y + tl)
        if tl:
            curve((x, y + tl - k * tl), (x + tl - k * tl, y), (x + tl, y))
        v.append((x + tl, y))
        c.append(MPath.CLOSEPOLY)
        self.ax.add_patch(PathPatch(MPath(v, c), facecolor=fill or "none", edgecolor=edge or "none",
                                    linewidth=(lw * 72 / PX_PER_INCH) if edge else 0))

    def line(self, x0: float, y0: float, x1: float, y1: float, colour: str = GRID, lw: float = 1.0) -> None:
        """Hairline drawn beneath fills and text (grid lines and axes never cross a mark)."""
        self.ax.plot([x0, x1], [y0, y1], color=colour, linewidth=lw * 72 / PX_PER_INCH, solid_capstyle="butt",
                     zorder=0.5)

    def arrow(self, x0: float, y0: float, x1: float, y1: float, colour: str = MUTED) -> None:
        self.ax.annotate("", xy=(x1, y1), xytext=(x0, y0), arrowprops={
            "arrowstyle": "-|>", "color": colour, "linewidth": 1.0 * 72 / PX_PER_INCH, "shrinkA": 0, "shrinkB": 0,
            "mutation_scale": 9})

    # -------------------------------------------------------------- output
    def _meta(self, fmt: str) -> dict[str, Any]:
        creator = f"GEODESIC {__version__}"
        if fmt == "svg":
            return {"Title": self.title, "Description": self.description, "Creator": creator, "Date": None}
        if fmt == "pdf":
            return {"Title": self.title, "Subject": self.description, "Creator": creator, "CreationDate": None}
        return {"Title": self.title, "Description": self.description, "Software": creator}

    def render(self, fmt: str, dpi: int = 300) -> bytes:
        if fmt not in FORMATS:
            raise ValidationError(f"Figure format must be one of {', '.join(FORMATS)}; got {fmt!r}")
        if fmt == "png" and dpi < 300:
            raise ValidationError("PNG figures are written at 300 DPI or more.")
        buf = io.BytesIO()
        extra: dict[str, Any] = {}
        if fmt == "png":
            # PNG stores resolution as whole pixels per metre; round up so the recorded value is never below ``dpi``
            recorded = math.ceil(dpi / 0.0254) * 0.0254 + 1e-6
            extra["pil_kwargs"] = {"dpi": (recorded, recorded)}
        with matplotlib.rc_context({"svg.hashsalt": "geodesic", "svg.fonttype": "path", "pdf.fonttype": 42}):
            self.fig.savefig(buf, format=fmt, dpi=dpi if fmt == "png" else PX_PER_INCH, facecolor=SURFACE,
                             metadata=self._meta(fmt), **extra)
        return buf.getvalue()

    def svg(self) -> str:
        return self.render("svg").decode("utf-8")

    def save(self, base: Path | str, formats: Iterable[str] = FORMATS, dpi: int = 300) -> list[Path]:
        """Write ``<base>.<fmt>`` for each format; returns the paths."""
        base = Path(base)
        base.parent.mkdir(parents=True, exist_ok=True)
        out = []
        for fmt in formats:
            path = base.with_name(base.name + f".{fmt}")
            path.write_bytes(self.render(fmt, dpi))
            out.append(path)
        return out


def _header(c: Canvas, title: str, subtitle_lines: list[str], banner: str | None) -> float:
    """Draw banner, title and subtitle; return the y where the plot can start."""
    y = MARGIN
    if banner:
        c.text(MARGIN, y + 6, banner, size=11, colour=INK, bold=True)
        y += 24
    c.text(MARGIN, y + 9, title, size=16, bold=True)
    y += 28
    for line in subtitle_lines:
        c.text(MARGIN, y + 7, line, size=12, colour=INK_2)
        y += 18
    return y + 10


def _header_height(subtitle_lines: list[str], banner: str | None) -> float:
    return MARGIN + (24 if banner else 0) + 28 + 18 * len(subtitle_lines) + 10


def _footer(c: Canvas, y: float, lines: list[str]) -> None:
    for line in lines:
        c.text(MARGIN, y + 6, line, size=10.5, colour=MUTED)
        y += 15


# ------------------------------------------------------------------ evidence map
def evidence_map_figure(emap: dict[str, Any], metric: str = "findings", *, banner: str | None = None) -> Canvas:
    """Heatmap of one metric: one row per term, one column per stage; empty cells stay empty."""
    if metric not in M.METRICS:
        raise ValidationError(f"metric must be one of {', '.join(M.METRICS)}")
    stages, terms = emap["rows"], emap["columns"]
    label_size, cell_h, gap = 12, 28, 2
    row_labels = []
    for t in terms:
        label = t["label"]
        if t["kind"] == "mapped":
            label += f" ({len(t['original_terms'])} terms grouped by reviewer)"
        row_labels.append(clip_text(label, label_size, 300))
    label_w = max([text_width(s, label_size) for s in row_labels] + [text_width("all terms", label_size), 80])
    cell_w = max([text_width(s["label"], 11.5) for s in stages] + [64]) + 14
    cell_w = min(cell_w, 150)
    total_w = 86
    grid_w = len(stages) * cell_w
    min_width = 620
    width = max(MARGIN + label_w + 12 + grid_w + 12 + total_w + MARGIN, min_width)
    inner = width - 2 * MARGIN
    subtitle = wrap_text(f"Number of {M.METRIC_LABELS[metric]}, by {emap['field_title']} (rows) and stage (columns). "
                         "Darker means more. Empty cells: nothing recorded in this project.", 12, inner)
    notes: list[str] = []
    for n in ("A finding assigned to several stages is counted in each stage column; 'all stages' counts it once.",
              "Rows are terms as stored; terms share a row only where a reviewer mapped them to one normalized term.",
              "Counts describe records in this project, not the literature as a whole."):
        notes += wrap_text(n, 10.5, inner)
    top = _header_height(subtitle, banner)
    body_h = 26 + len(terms) * cell_h + 8 + 24
    legend_h = 44
    height = top + body_h + legend_h + 15 * len(notes) + MARGIN
    c = Canvas(width, height, emap["title"], f"{emap['title']}: {M.METRIC_LABELS[metric]}. "
               + (banner or ""))
    y = _header(c, emap["title"], subtitle, banner)
    x0 = MARGIN + label_w + 12
    # column headers
    for j, s in enumerate(stages):
        c.text(x0 + j * cell_w + cell_w / 2, y + 10, clip_text(s["label"], 11.5, cell_w - 8), size=11.5, colour=INK_2,
               ha="center")
    c.text(x0 + grid_w + 12 + total_w, y + 10, "all stages", size=11.5, colour=MUTED, ha="right")
    y += 26
    vmax = max([M.cell(emap, s["key"], t["key"], metric) for s in stages for t in terms] + [0])
    for i, t in enumerate(terms):
        cy = y + i * cell_h
        c.text(x0 - 12, cy + cell_h / 2, row_labels[i], size=label_size,
               colour=INK_2 if t["kind"] in ("term", "mapped") else MUTED, ha="right")
        for j, s in enumerate(stages):
            v = M.cell(emap, s["key"], t["key"], metric)
            fill = sequential_colour(v, vmax) if v else EMPTY
            c.box(x0 + j * cell_w + gap / 2, cy + gap / 2, cell_w - gap, cell_h - gap, fill, radii=(2, 2, 2, 2))
            if v:
                c.text(x0 + j * cell_w + cell_w / 2, cy + cell_h / 2, str(v), size=11.5, colour=ink_on(fill),
                       ha="center")
        c.text(x0 + grid_w + 12 + total_w, cy + cell_h / 2, str(t[metric]), size=11.5, colour=INK_2, ha="right")
    y += len(terms) * cell_h + 8
    c.line(x0, y, x0 + grid_w + 12 + total_w, y, AXIS)
    c.text(x0 - 12, y + 13, "all terms", size=label_size, colour=MUTED, ha="right")
    for j, s in enumerate(stages):
        c.text(x0 + j * cell_w + cell_w / 2, y + 13, str(s[metric]), size=11.5, colour=INK_2, ha="center")
    c.text(x0 + grid_w + 12 + total_w, y + 13, str(emap["total"][metric]), size=11.5, colour=INK, ha="right", bold=True)
    y += 24
    # scale legend
    ly = y + 14
    c.text(MARGIN, ly + 7, "fewer", size=10.5, colour=MUTED)
    lx = MARGIN + text_width("fewer", 10.5) + 8
    steps = SEQUENTIAL[::3]
    for k, colour in enumerate(steps):
        c.box(lx + k * 22 + 1, ly, 20, 14, colour, radii=(2, 2, 2, 2))
    lx += len(steps) * 22 + 8
    c.text(lx, ly + 7, f"more (1 to {vmax})" if vmax > 1 else "more", size=10.5, colour=MUTED)
    lx += text_width(f"more (1 to {vmax})" if vmax > 1 else "more", 10.5) + 24
    c.box(lx, ly, 20, 14, EMPTY, radii=(2, 2, 2, 2))
    c.text(lx + 28, ly + 7, "none recorded", size=10.5, colour=MUTED)
    _footer(c, y + legend_h, notes)
    return c


# ------------------------------------------------------------------ missing information
def _nice_ticks(vmax: int, target: int = 5) -> list[int]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / target
    mag = 10 ** max(0, len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next((m * mag for m in (1, 2, 5, 10) if m * mag >= raw), 10 * mag)
    step = max(1, int(step))
    top = ((vmax + step - 1) // step) * step
    return list(range(0, top + 1, step))


def missing_figure(summary: dict[str, Any], stage_key: str = MI.ALL_STAGES, *, banner: str | None = None) -> Canvas:
    """Stacked horizontal bars: per field, how many measurement findings record it and why the others do not."""
    rows = [r for r in summary["rows"] if r["stage_key"] == stage_key]
    stage_name = rows[0]["stage"] if rows else stage_key
    title = "Missing information by field" + ("" if stage_key == MI.ALL_STAGES else f" — {stage_name}")
    width = 760
    inner = width - 2 * MARGIN
    subtitle = wrap_text("Measurement findings per field: recorded, or the reason the field is empty. 'Not reported' "
                         "is a recorded statement about the source; 'no status recorded' means nobody has said why.",
                         12, inner)
    notes: list[str] = []
    for n in ("Unit and uncertainty are counted for findings stating a number, range or bound; the reference "
              "definition for relative quantities only, so bars differ in length.",
              "Counts describe records in this project. The same numbers are written as a table with this figure."):
        notes += wrap_text(n, 10.5, inner)
    # legend rows
    states = list(MI.STATES)
    legend: list[list[str]] = [[]]
    used = 0.0
    for s in states:
        w = 12 + 6 + text_width(STATE_LABELS[s], 11.5) + 18
        if used + w > inner and legend[-1]:
            legend.append([])
            used = 0.0
        legend[-1].append(s)
        used += w
    band, bar_h, gap = 30, 16, 2
    top = _header_height(subtitle, banner)
    height = top + 22 * len(legend) + 10 + max(len(rows), 1) * band + 44 + 15 * len(notes) + MARGIN
    c = Canvas(width, height, title, title + ". " + (banner or ""))
    y = _header(c, title, subtitle, banner)
    for line in legend:
        x = MARGIN
        for s in line:
            c.box(x, y + 4, 12, 12, STATE_COLOURS[s][0], radii=(2, 2, 2, 2))
            c.text(x + 18, y + 10, STATE_LABELS[s], size=11.5, colour=INK_2)
            x += 12 + 6 + text_width(STATE_LABELS[s], 11.5) + 18
        y += 22
    y += 10
    label_w = max([text_width(r["field"].replace("_", " "), 12) for r in rows] + [60])
    x0 = MARGIN + label_w + 12
    plot_w = width - MARGIN - x0 - 44
    vmax = max([r["findings"] for r in rows] + [1])
    ticks = _nice_ticks(vmax)
    scale = plot_w / ticks[-1]
    y_end = y + max(len(rows), 1) * band
    for t in ticks:
        c.line(x0 + t * scale, y, x0 + t * scale, y_end, GRID if t else AXIS)
        c.text(x0 + t * scale, y_end + 12, str(t), size=11, colour=MUTED, ha="center")
    c.text(x0 + plot_w / 2, y_end + 30, "measurement findings", size=11, colour=MUTED, ha="center")
    if not rows:
        c.text(x0 + 8, y + band / 2, "No measurement findings.", size=12, colour=MUTED)
    for i, r in enumerate(rows):
        cy = y + i * band + (band - bar_h) / 2
        c.text(x0 - 12, cy + bar_h / 2, r["field"].replace("_", " "), size=12, colour=INK_2, ha="right")
        present = [s for s in states if r[s]]
        x = x0
        for s in present:
            w = r[s] * scale
            last = s == present[-1]
            fill, ink = STATE_COLOURS[s]
            draw_w = w - (0 if last else gap)
            c.box(x, cy, draw_w, bar_h, fill, radii=(0, 4, 4, 0) if last else (0, 0, 0, 0))
            label = str(r[s])
            if text_width(label, 10.5) + 10 <= draw_w:
                c.text(x + draw_w / 2, cy + bar_h / 2, label, size=10.5, colour=ink, ha="center")
            x += w
        c.text(x + 6, cy + bar_h / 2, str(r["findings"]), size=11, colour=INK_2)
    _footer(c, y_end + 44, notes)
    return c


# ------------------------------------------------------------------ flow diagram
def _kv(d: dict[str, int]) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in d.items()) or "none"


def flow_figure(summary: dict[str, Any], *, banner: str | None = None) -> Canvas:
    """Search and screening flow as boxes and arrows (counts from :func:`geodesic.screening.summary`)."""
    ident = summary["identification"]
    width, main_w, side_w, gap_x = 820, 400, 300, 48
    inner = width - 2 * MARGIN
    title = "Search and screening flow"
    subtitle = wrap_text(f"Protocol: {summary['protocol']['label']}. Records follow the arrows downward; boxes on "
                         "the right leave the flow. The layout is PRISMA-style; no compliance with a reporting "
                         "guideline is claimed.", 12, inner)
    stage_titles = {"title_abstract": "Title/abstract screening", "full_text": "Full-text screening"}
    steps: list[tuple[str, list[str], tuple[str, list[str]] | None]] = [
        (f"Records retrieved by searches: {sum(ident['records_retrieved'].values())}", [f"by mode: {_kv(ident['records_retrieved'])}",
                                           f"search runs: {_kv(ident['search_runs'])}; truncated or incomplete: "
                                           f"{ident['runs_not_complete']}"], None),
        (f"Paper records in the project: {ident['paper_records']}",
         [f"by origin: {_kv(ident['paper_records_by_origin'])}"],
         (f"Grouped as duplicates of another record: {ident['grouped_as_duplicates']}",
          [f"duplicate candidates awaiting review: {ident['duplicate_candidates_awaiting_review']}"])),
        (f"Distinct records after grouping: {ident['distinct_records_after_grouping']}", [], None),
    ]
    for key, st in summary["stages"].items():
        o, kinds, fin = st["outcomes"], st["current_decisions_by_decider"], st["papers_by_finality"]
        steps.append((
            f"{stage_titles.get(key, key)} — records eligible: {st['eligible_for_stage']}",
            [f"with a current decision: {st['screened']}; awaiting a decision: {st['awaiting_decision']}",
             f"include {o['include']}, uncertain {o['uncertain']}, conflicting decisions {o['conflict']}",
             f"papers with final decisions {fin['final']}, provisional {fin['provisional']}",
             f"decisions — human {kinds['human']}, automated {kinds['automated']}, simulated {kinds['simulated']}"],
            (f"Excluded: {o['exclude']}",
             [(f"{r['reason_kind'].replace('_', ' ')}" + (f" [{r['criterion_id']}]" if r["criterion_id"] else "")
               + f", {r['decider_kind']}: {r['papers']}") for r in st["exclusion_reasons"][:5]]
             + ([f"… and {len(st['exclusion_reasons']) - 5} more reason(s), see the table"]
                if len(st["exclusion_reasons"]) > 5 else []))))
    last = summary["stages"].get("full_text")
    if last:
        steps.append((f"Included after full-text screening: {last['outcomes']['include']}",
                      ["An inclusion is a screening decision, not a statement about the quality of the evidence."],
                      None))
    notes: list[str] = []
    for n in summary["notes"]:
        notes += wrap_text(n, 10.5, inner)

    def box_lines(head: str, body: list[str], w: float) -> tuple[list[str], list[str]]:
        heads = wrap_text(head, 12, w - 24, bold=True)
        bodies = [ln for b in body for ln in wrap_text(b, 11, w - 24)]
        return heads, bodies

    def box_height(heads: list[str], bodies: list[str]) -> float:
        return 12 + 17 * len(heads) + 15 * len(bodies) + (4 if bodies else 0) + 10

    laid = []
    for head, body, side in steps:
        mh = box_lines(head, body, main_w)
        sh = box_lines(side[0], side[1], side_w) if side else None
        h = max(box_height(*mh), box_height(*sh) if sh else 0)
        laid.append((mh, sh, h))
    arrow_h = 26
    top = _header_height(subtitle, banner)
    height = top + sum(h for _, _, h in laid) + arrow_h * (len(laid) - 1) + 20 + 15 * len(notes) + MARGIN
    c = Canvas(width, height, title, title + ". " + (banner or ""))
    y = _header(c, title, subtitle, banner)
    x_main = MARGIN
    x_side = MARGIN + main_w + gap_x

    def draw_box(x: float, yy: float, w: float, h: float, heads: list[str], bodies: list[str]) -> None:
        c.box(x, yy, w, h, SURFACE, radii=(4, 4, 4, 4), edge=AXIS)
        ty = yy + 12
        for ln in heads:
            c.text(x + 12, ty + 7, ln, size=12, colour=INK, bold=True)
            ty += 17
        ty += 4 if bodies else 0
        for ln in bodies:
            c.text(x + 12, ty + 6, ln, size=11, colour=INK_2)
            ty += 15

    for i, (mh, sh, h) in enumerate(laid):
        draw_box(x_main, y, main_w, h, *mh)
        if sh:
            own = box_height(*sh)
            draw_box(x_side, y + (h - own) / 2, side_w, own, *sh)
            c.arrow(x_main + main_w, y + h / 2, x_side, y + h / 2)
        if i < len(laid) - 1:
            c.arrow(x_main + main_w / 2, y + h, x_main + main_w / 2, y + h + arrow_h)
        y += h + arrow_h
    _footer(c, y - arrow_h + 20, notes)
    return c


# ------------------------------------------------------------------ the standard set
STANDARD_FIGURES = ("evidence_map_system_context", "evidence_map_outcome", "missing_information", "search_screening_flow")


def standard_figures(conn: sqlite3.Connection, *, flt: query.FindingFilter | None = None, banner: str | None = None,
                     metric: str = "findings", max_columns: int = 12) -> dict[str, Canvas]:
    """The figures every report carries, drawn from the same functions the CLI prints.

    ``flt`` narrows the findings counted in the evidence maps and the missing-information figure; the
    search and screening flow always describes the whole project.
    """
    from .. import screening

    return {
        "evidence_map_system_context": evidence_map_figure(
            M.evidence_map(conn, "system_context", flt=flt, max_columns=max_columns), metric, banner=banner),
        "evidence_map_outcome": evidence_map_figure(
            M.evidence_map(conn, "outcome", flt=flt, max_columns=max_columns), metric, banner=banner),
        "missing_information": missing_figure(MI.missing_information(conn, flt), banner=banner),
        "search_screening_flow": flow_figure(screening.summary(conn), banner=banner),
    }
