"""Deterministic, domain-neutral parser for quantities in free text.

No model service is involved. The parser recognises numbers, units from the
explicit unit table, approximation and bound qualifiers, ranges, ``±``
expressions, confidence intervals, sample sizes and timepoints, and it records
the character span of every literal it transcribes.

What it deliberately does not do
--------------------------------
* It never turns a range or a bound into a point value.
* It never decides that ``±`` means SD or SE unless the text says so.
* It never guesses the reference of a percentage or the baseline of a fold change.
* It never decides whether an observation is a direct measurement or a proxy.

Spans are 0-based, half-open code-point offsets into the text that was passed in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

from .quantities.units import RELATIVE_DIMENSIONS, TIME_DIMENSION, match_unit_at

PARSER_NAME = "geodesic-deterministic-parser"
PARSER_VERSION = "1.0"

# ------------------------------------------------------------------ vocabulary
DEFAULT_QUALIFIER_PHRASES: tuple[str, ...] = (
    "not independently verified", "not verified", "unverified", "denominator unclear", "unclear denominator",
    "denominator not stated", "units unclear", "unit unclear", "estimated from figure", "read from figure",
    "estimated from graph", "read from graph", "digitized", "digitised", "extrapolated", "interpolated",
    "preliminary", "unpublished", "personal communication", "data not shown", "author-reported",
    "as reported", "secondary source", "not reported", "not stated", "unclear", "inconsistent",
    "single run", "single replicate", "no replicates", "pilot",
)

DEFAULT_LABEL_WORDS: tuple[str, ...] = (
    "fig", "figs", "figure", "figures", "table", "tables", "tab", "eq", "eqs", "equation", "ref", "refs",
    "reference", "section", "sec", "stage", "step", "phase", "run", "no", "group", "sample", "site", "batch",
    "version", "chapter", "part", "appendix", "panel", "experiment", "study", "trial", "arm", "lot", "item",
    "level", "type", "class", "series", "set", "test", "case", "scenario", "condition", "replicate", "id",
)

_APPROX = ("approximately", "approx.", "approx", "about", "around", "roughly", "circa", "ca.", "nearly",
           "almost", "~", "≈", "∼", "≃")
_BOUNDS: dict[str, tuple[str, ...]] = {
    "ge": ("≥", ">=", "≧", "at least", "no less than", "not less than", "a minimum of", "minimum of"),
    "le": ("≤", "<=", "≦", "at most", "up to", "no more than", "not more than", "a maximum of", "maximum of",
           "not exceeding"),
    "gt": (">", "greater than", "more than", "exceeding", "in excess of", "above", "over"),
    "lt": ("<", "less than", "fewer than", "below", "under"),
}
_RANGE_OPENERS = ("between", "ranging from", "ranged from", "ranges from", "from")
_TIME_PREPS = ("at", "after", "by", "within", "over", "for", "during", "until", "following", "post", "since", "@")

_NUM_CORE = (r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
             r"(?:[eE][-−+]?\d+|\s?[×x]\s?10(?:\^[-−+]?\d+|[-−+]\d+|[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+))?")
NUM_RE = re.compile(rf"(?<![\w.,]){_NUM_CORE}(?!\d|[,.]\d)")
_DECIMAL_COMMA_RE = re.compile(r"(?<![\w.,])\d+,\d{1,2}(?![\d,])")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+")
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"]+")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b")
_N_RE = re.compile(
    r"\b[nN]\s?=\s?(?P<n>\d+)(?:\s+(?P<rep>(?:independent\s+|technical\s+|repeat\s+)?"
    r"(?:replicates?|runs?|samples?|experiments?|batches|trials|repeats|measurements|observations|units)"
    r"(?:\s+per\s+[A-Za-z]+)?))?")
_COUNT_NOUN_RE = re.compile(
    r"(?<![\w.,])(?P<n>\d+)\s+(?P<rep>(?:independent\s+|technical\s+|repeat\s+|replicate\s+)?"
    r"(?:replicates?|runs?|samples?|experiments?|batches|trials|repeats|measurements|observations))\b")
_CAP_WORD_BEFORE_RE = re.compile(r"(?<![A-Za-z])[A-Z][a-z]{2,}\s$")
_P_RE = re.compile(rf"\b[pP]\s?(?:[<>=≤≥]|<=|>=)\s?{_NUM_CORE}")
_CI_RE = re.compile(
    rf"(?P<level>\d{{2}}(?:\.\d+)?)\s?%\s?(?:CI|C\.I\.|confidence interval)[:,]?\s*\[?(?P<lo>[-−]?{_NUM_CORE})"
    rf"\s*(?:[–—−-]|to|,)\s*(?P<hi>[-−]?{_NUM_CORE})\]?")
_SPREAD_RE = re.compile(
    rf"\b(?P<kind>IQR|interquartile range|range)[:,]?\s*(?P<lo>[-−]?{_NUM_CORE})\s*(?:[–—−-]|to)\s*"
    rf"(?P<hi>[-−]?{_NUM_CORE})")
_TIMEWORD_BEFORE_RE = re.compile(
    r"\b(?P<word>days?|weeks?|months?|hours?|minutes?|years?)\s(?P<num>\d+(?:\.\d+)?)(?!\w|[.,]\d)", re.IGNORECASE)
_TIMEWORD_UNIT = {"day": "d", "week": "wk", "month": "mo", "hour": "h", "minute": "min", "year": "yr"}
_SD_RE = re.compile(r"(?<![A-Za-z])(?:SD|S\.D\.|s\.d\.|std\.? ?dev\.?|standard deviation)(?![A-Za-z])")
_SE_RE = re.compile(r"(?<![A-Za-z])(?:SEM|SE|S\.E\.M\.|S\.E\.|s\.e\.m\.|s\.e\.|standard error(?: of the mean)?)(?![A-Za-z])")
_STAT_RE = re.compile(r"\b(geometric mean|mean|median|average)\b", re.IGNORECASE)
_PM_RE = re.compile(r"\s*(?:±|\+/-|\+/−|\+-)\s*")
_RANGE_SEP_RE = re.compile(r"\s*(?P<sep>[–—−-]|to|and)\s*")
_PCT_OF_RE = re.compile(
    r"\s+of\s+(?:the\s+)?(?P<ref>[^,;.()\n]{2,60}?)"
    r"(?=\s+(?:at|after|by|within|in|per|was|were|is|are|and|with|over|during|when|remained|remaining|had|has)\b"
    r"|\s*[,;.()\n]|\s*$)")
_PER_MASS_VOL_RE = re.compile(r"\s+per\s+(?:g|gram|mg|kg|mL|ml|L|litre|liter|unit mass|unit volume)\b")
_BASELINE_RE = re.compile(
    r"\s*(?:(?:higher|lower|greater|more|less|increase|decrease|reduction|rise|drop|change)\s+)?"
    r"(?:than|relative to|compared (?:with|to)|versus|vs\.?|over|above)\s+(?:the\s+)?(?P<ref>[^,;.()\n]{2,60}?)"
    r"(?=\s+(?:at|after|by|within|in|was|were|is|are|and|with|during|when)\b|\s*[,;.()\n]|\s*$)")
_NORMALIZED_RE = re.compile(r"\bnormali[sz]ed\s+(?:to|by|per)\s+(?:the\s+)?(?P<ref>[^,;.()\n]{2,60})", re.IGNORECASE)
_COUNTED_RE = re.compile(r"\b(?:counted|enumerated|tallied)\b", re.IGNORECASE)
_TIME_POST_RE = re.compile(r"\s*(?:after|post|later|following|into|time ?point)\b", re.IGNORECASE)

_ABBREVIATIONS = ("approx", "ca", "fig", "figs", "eq", "eqs", "no", "vs", "al", "e.g", "i.e", "resp", "ref",
                  "refs", "tab", "vol", "sec", "cf", "etc", "est", "max", "min", "avg", "std", "dev", "s.d",
                  "s.e", "s.e.m", "a.u", "arb")


def _prefix_regex(phrases: Iterable[str]) -> re.Pattern[str]:
    parts = []
    for ph in sorted(phrases, key=len, reverse=True):
        esc = re.escape(ph)
        parts.append(rf"(?<![A-Za-z]){esc}" if ph[0].isalpha() else esc)
    return re.compile(r"(?:" + "|".join(parts) + r")\s*$", re.IGNORECASE)


_APPROX_RE = _prefix_regex(_APPROX)
_BOUND_RES = {bt: _prefix_regex(words) for bt, words in _BOUNDS.items()}
_OPENER_RE = _prefix_regex(_RANGE_OPENERS)
_TIME_PREP_RE = re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(p) for p in _TIME_PREPS) + r"|t\s?=|time\s?=)\s*$",
                           re.IGNORECASE)


# ------------------------------------------------------------------ data classes
@dataclass
class Segment:
    """One fact-sized piece of a narrative cell, with its span in the cell text."""

    start: int
    end: int
    text: str


@dataclass
class Quantity:
    """A parsed quantity expression. All spans are relative to the parsed text."""

    expr_start: int
    expr_end: int
    value_literal: str
    value_start: int
    value_end: int
    statement_type: str                      # exact | approximate | range | bound
    value_num: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    bound_type: str | None = None
    unit_literal: str | None = None
    unit_start: int | None = None
    unit_end: int | None = None
    unit_dimension: str | None = None
    unit_ambiguous: bool = False
    uncertainty_type: str | None = None
    uncertainty_literal: str | None = None
    uncertainty_start: int | None = None
    uncertainty_end: int | None = None
    uncertainty_value: float | None = None
    uncertainty_low: float | None = None
    uncertainty_high: float | None = None
    confidence_level: float | None = None
    qualifiers: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    role: str = "measurement"                # measurement | timepoint
    relative_semantics: str | None = None
    reference_definition: str | None = None
    reference_start: int | None = None
    reference_end: int | None = None

    @property
    def is_time(self) -> bool:
        return self.unit_dimension == TIME_DIMENSION

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], False)}


@dataclass
class SampleSize:
    start: int
    end: int
    literal: str
    value: int
    replicate_type: str | None
    num_start: int
    num_end: int
    from_count_phrase: bool = False     # "4 runs" rather than "n = 4": needs review as a sample size


@dataclass
class TextParse:
    """Everything the parser found in one piece of text."""

    quantities: list[Quantity] = field(default_factory=list)   # measurements
    timepoints: list[Quantity] = field(default_factory=list)
    sample_sizes: list[SampleSize] = field(default_factory=list)
    qualifiers: list[dict] = field(default_factory=list)       # generic phrases anywhere in the text
    summary_statistic: dict | None = None
    normalization: dict | None = None
    unparsed: list[dict] = field(default_factory=list)         # {start, end, text, reason}


# ------------------------------------------------------------------ number helpers
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")


def number_from_literal(literal: str) -> float:
    """Numeric value of a transcribed literal. The literal itself is stored unchanged elsewhere."""
    s = literal.strip().replace("−", "-").replace(",", "").translate(_SUPERSCRIPTS)
    m = re.match(r"^([-+]?\d+(?:\.\d+)?)\s?[×x]\s?10\^?([-+]?\d+)$", s)
    if m:
        return float(m.group(1)) * (10.0 ** int(m.group(2)))
    return float(s)


def _mask(chars: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


# ------------------------------------------------------------------ segmentation
_BULLET_RE = re.compile(r"^\s*(?:[•●▪◦*·]|[-–—](?=\s)|\(?\d{1,2}[.)](?=\s)|\(?[a-z][.)](?=\s))\s*")


def split_segments(text: str, on_semicolon: bool = True) -> list[Segment]:
    """Split narrative text into fact-sized segments, keeping exact spans.

    Boundaries: line breaks, semicolons (optional) and sentence ends (``.``/``!``/``?``
    followed by whitespace and an upper-case letter or digit, except after common
    abbreviations). Leading bullet or list markers are excluded from the segment span.
    """
    if not text:
        return []
    cuts: list[tuple[int, int]] = []   # (end_of_segment, start_of_next)
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "\n":
            cuts.append((i, i + 1))
            depth = 0
        elif ch == ";" and on_semicolon and depth == 0:
            cuts.append((i, i + 1))
        elif ch in ".!?" and depth == 0:
            m = re.match(r"\s+(?=[A-Z0-9(\[\"'“~≈<>≥≤])", text[i + 1:i + 6]) if i + 1 < len(text) else None
            if m:
                before = re.search(r"([A-Za-z.]+)$", text[max(0, i - 8):i])
                word = before.group(1).lower() if before else ""
                # a unit written straight after a number ("48 h.", "30 min.") ends a sentence;
                # initials and abbreviations ("J. Smith", "approx. 5") do not
                after_number = re.search(r"\d\s?(?:[A-Za-zµμ%]{1,6}[/·])*" + re.escape(before.group(1)) + r"$",
                                         text[max(0, i - 20):i]) if before else None
                unit_end = bool(after_number) and re.match(r"\s+[A-Z]", text[i + 1:i + 6]) is not None
                if ch == "." and not unit_end and (
                        word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())):
                    continue
                cuts.append((i + 1, i + 1 + m.end()))
    segments: list[Segment] = []
    pos = 0
    for end, nxt in [*cuts, (len(text), len(text))]:
        if end > pos:
            raw = text[pos:end]
            lead = _BULLET_RE.match(raw)
            s = pos + (lead.end() if lead else 0)
            s += len(text[s:end]) - len(text[s:end].lstrip())
            e = s + len(text[s:end].rstrip())
            # drop a trailing sentence period from the span only when it is clearly terminal punctuation
            if e > s:
                segments.append(Segment(s, e, text[s:e]))
        pos = max(pos, nxt)
    return segments


# ------------------------------------------------------------------ the parser
@lru_cache(maxsize=16)
def _qualifier_regex(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    uniq = sorted({p.strip().lower() for p in phrases if p and p.strip()}, key=len, reverse=True)
    if not uniq:
        return None
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(p) for p in uniq) + r")(?![A-Za-z])", re.IGNORECASE)


@lru_cache(maxsize=16)
def _label_regex(label_words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(
        r"(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in sorted(set(label_words), key=len, reverse=True))
        + r")s?\.?\s?#?(?P<num>\d+(?:\.\d+)*[A-Za-z]?)(?![\w%])|#\s?\d+", re.IGNORECASE)


def _find_prefix(text: str, pos: int) -> tuple[list[dict], str | None, str | None, int]:
    """Qualifiers directly before ``pos``. Returns (qualifiers, bound_type, opener, new_start)."""
    quals: list[dict] = []
    bound: str | None = None
    opener: str | None = None
    start = pos
    for _ in range(3):
        left = text[max(0, start - 28):start]
        base = start - len(left)
        m = _APPROX_RE.search(left)
        if m and not any(q["kind"] == "approximation" for q in quals):
            quals.append({"kind": "approximation", "text": left[m.start():m.end()].rstrip(),
                          "start": base + m.start(), "end": base + m.start() + len(left[m.start():m.end()].rstrip())})
            start = base + m.start()
            continue
        hit = None
        if bound is None:
            for bt, rx in _BOUND_RES.items():
                mb = rx.search(left)
                if mb and (hit is None or mb.start() < hit[1].start()):
                    hit = (bt, mb)
        if hit:
            bt, mb = hit
            lit = left[mb.start():mb.end()].rstrip()
            quals.append({"kind": "bound", "text": lit, "start": base + mb.start(),
                          "end": base + mb.start() + len(lit), "bound_type": bt})
            bound = bt
            start = base + mb.start()
            continue
        mo = _OPENER_RE.search(left)
        if mo and opener is None:
            opener = left[mo.start():mo.end()].strip().lower()
            start = base + mo.start()
            continue
        break
    return quals, bound, opener, start


def _unit_after(text: str, pos: int) -> tuple[str, int, int, str, bool] | None:
    """Unit starting at ``pos`` (after optional single space or hyphen)."""
    for skip in (0, 1):
        p = pos + skip
        if skip and (pos >= len(text) or text[pos] not in "  -"):
            break
        hit = match_unit_at(text, p)
        if hit:
            unit, s, e = hit
            if skip and text[pos] == "-" and text[s:e].startswith("-"):
                continue
            # single-letter symbols need a clean right edge (avoid "5 m" inside "5 more")
            return text[s:e], s, e, unit.dimension, unit.ambiguous
    return None


def parse_text(text: str, qualifier_phrases: Iterable[str] = DEFAULT_QUALIFIER_PHRASES,
               label_words: Iterable[str] = DEFAULT_LABEL_WORDS) -> TextParse:
    """Parse one piece of text (a narrative segment, a paragraph, a table cell)."""
    out = TextParse()
    if not text:
        return out
    chars = list(text)

    # -- generic qualifier phrases (recorded, never interpreted)
    qual_rx = _qualifier_regex(tuple(qualifier_phrases))
    if qual_rx is not None:
        for m in qual_rx.finditer(text):
            out.qualifiers.append({"kind": "annotation", "text": text[m.start():m.end()],
                                   "start": m.start(), "end": m.end()})

    # -- things that contain digits but are not measurements
    for rx in (_DOI_RE, _URL_RE, _DATE_RE, _P_RE):
        for m in rx.finditer(text):
            _mask(chars, m.start(), m.end())
    for m in _DECIMAL_COMMA_RE.finditer("".join(chars)):
        out.unparsed.append({"start": m.start(), "end": m.end(), "text": text[m.start():m.end()],
                             "reason": "ambiguous_decimal_comma"})
        _mask(chars, m.start(), m.end())
    for m in _N_RE.finditer("".join(chars)):
        out.sample_sizes.append(SampleSize(m.start(), m.end(), text[m.start():m.end()], int(m.group("n")),
                                           m.group("rep"), m.start("n"), m.end("n")))
        _mask(chars, m.start(), m.end())
    for m in _COUNT_NOUN_RE.finditer("".join(chars)):
        out.sample_sizes.append(SampleSize(m.start(), m.end(), text[m.start():m.end()], int(m.group("n")),
                                           m.group("rep"), m.start("n"), m.end("n"), from_count_phrase=True))
        _mask(chars, m.start(), m.end())
    label_rx = _label_regex(tuple(label_words))
    for m in label_rx.finditer("".join(chars)):
        _mask(chars, m.start(), m.end())
    intervals: list[dict] = []
    for m in _CI_RE.finditer("".join(chars)):
        intervals.append({"type": "ci", "start": m.start(), "end": m.end(), "literal": text[m.start():m.end()],
                          "level": float(m.group("level")), "lo": m.group("lo"), "hi": m.group("hi")})
        _mask(chars, m.start(), m.end())
    for m in _SPREAD_RE.finditer("".join(chars)):
        kind = "iqr" if m.group("kind").lower() != "range" else "range"
        intervals.append({"type": kind, "start": m.start(), "end": m.end(), "literal": text[m.start():m.end()],
                          "level": None, "lo": m.group("lo"), "hi": m.group("hi")})
        _mask(chars, m.start(), m.end())
    for m in _TIMEWORD_BEFORE_RE.finditer("".join(chars)):
        word = m.group("word").lower().rstrip("s")
        q = Quantity(m.start(), m.end(), m.group("num"), m.start("num"), m.end("num"), "exact",
                     value_num=float(m.group("num")), unit_literal=text[m.start("word"):m.end("word")],
                     unit_start=m.start("word"), unit_end=m.end("word"), unit_dimension=TIME_DIMENSION,
                     role="timepoint")
        q.flags.append(f"time_unit_normalized_as:{_TIMEWORD_UNIT[word]}")
        out.timepoints.append(q)
        _mask(chars, m.start(), m.end())

    masked = "".join(chars)
    tokens = list(NUM_RE.finditer(masked))
    by_start = {m.start(): i for i, m in enumerate(tokens)}
    consumed: set[int] = set()
    unit_spans: list[tuple[int, int]] = []

    def _skip(start: int, end: int, reason: str) -> None:
        out.unparsed.append({"start": start, "end": end, "text": text[start:end], "reason": reason})

    for i, m in enumerate(tokens):
        if i in consumed or any(us <= m.start() < ue for us, ue in unit_spans):
            continue
        s, e = m.start(), m.end()
        a_literal = text[s:e]
        # sign directly attached to the number
        if s > 0 and text[s - 1] in "-−+" and (s == 1 or text[s - 2] in " \t\n(=:[<>≤≥~≈±,"):
            if not (s >= 2 and text[s - 2].isdigit()):
                s -= 1
                a_literal = text[s:e]
        quals, bound, opener, expr_start = _find_prefix(text, s)
        pos = e
        unit_a = _unit_after(masked, pos)
        if unit_a:
            pos = unit_a[2]
        q: Quantity | None = None

        # ---- A ± B
        pm = _PM_RE.match(masked, pos)
        if pm and pm.end() in by_start and by_start[pm.end()] not in consumed:
            j = by_start[pm.end()]
            b = tokens[j]
            consumed.add(j)
            pos = b.end()
            unit_b = _unit_after(masked, pos)
            if unit_b:
                pos = unit_b[2]
            unit = unit_b or unit_a
            q = Quantity(expr_start, pos, a_literal, s, e,
                         "approximate" if any(x["kind"] == "approximation" for x in quals) else "exact",
                         value_num=number_from_literal(a_literal),
                         uncertainty_literal=text[b.start():b.end()], uncertainty_start=b.start(),
                         uncertainty_end=b.end(), uncertainty_value=number_from_literal(text[b.start():b.end()]),
                         uncertainty_type="unspecified_plus_minus")
        else:
            # ---- A – B / A to B / between A and B
            rs = _RANGE_SEP_RE.match(masked, pos)
            j = by_start.get(rs.end()) if rs else None
            sep = rs.group("sep") if rs else None
            is_range = (rs is not None and j is not None and j not in consumed
                        and (sep != "and" or opener == "between")
                        and not (sep in "-−" and rs.end() - rs.start() == 1 and False))
            if is_range:
                b = tokens[j]
                bpos = b.end()
                unit_b = _unit_after(masked, bpos)
                if unit_a and unit_b and unit_a[3] != unit_b[3]:
                    is_range = False      # two quantities of different dimensions, not a range
                elif bound is not None:
                    is_range = False      # "> 5 - 10" is not a well-formed range; leave both numbers visible
            if is_range:
                consumed.add(j)
                pos = bpos
                if unit_b:
                    pos = unit_b[2]
                unit = unit_b or unit_a
                lo, hi = number_from_literal(a_literal), number_from_literal(text[b.start():b.end()])
                q = Quantity(expr_start, pos, text[s:b.end()], s, b.end(), "range", value_low=lo, value_high=hi)
                if lo > hi:
                    q.flags.append("range_bounds_not_ascending")
                if opener == "from" or (opener and opener.endswith("from")):
                    q.flags.append("from_to_may_be_change_not_range")
            else:
                unit = unit_a
                if unit is None and e < len(text) and (text[e].isalpha() or text[e] == "_"):
                    _skip(s, e, "number_attached_to_letters")      # identifier-like ("3D", "2nd", "5x")
                    continue
                val = number_from_literal(a_literal)
                if (unit is None and not quals and re.fullmatch(r"\d{4}", a_literal)
                        and 1900 <= int(a_literal) <= 2100):
                    _skip(s, e, "year_like_number")
                    continue
                if (unit is None and not quals and re.fullmatch(r"\d{1,3}", a_literal)
                        and _CAP_WORD_BEFORE_RE.search(text[max(0, s - 24):s])):
                    _skip(s, e, "label_like_number")               # "Reservoir 2", "Line 14"
                    continue
                if bound is not None:
                    q = Quantity(expr_start, pos, a_literal, s, e, "bound", bound_type=bound)
                    if bound in ("gt", "ge"):
                        q.value_low = val
                    else:
                        q.value_high = val
                else:
                    q = Quantity(expr_start, pos, a_literal, s, e,
                                 "approximate" if any(x["kind"] == "approximation" for x in quals) else "exact",
                                 value_num=val)
        q.qualifiers = quals
        if q.statement_type == "range" and any(x["kind"] == "approximation" for x in quals):
            q.flags.append("approximate_range")
        if unit:
            q.unit_literal, q.unit_start, q.unit_end, q.unit_dimension, q.unit_ambiguous = unit
            unit_spans.append((unit[1], unit[2]))
            if q.unit_ambiguous:
                q.flags.append("unit_ambiguous")
        else:
            q.flags.append("no_unit_detected")
        if unit_a and unit and unit is not unit_a and unit_a[0] != unit[0]:
            q.flags.append("mixed_units_in_expression")

        # ---- timepoint or measurement?
        if q.is_time:
            left = text[max(0, q.expr_start - 14):q.expr_start]
            over_as_prep = any(x["kind"] == "bound" and x["text"].lower() in ("over", "under", "above", "below")
                               for x in quals) and q.statement_type == "bound" and \
                any(x["text"].lower() == "over" for x in quals)
            if _TIME_PREP_RE.search(left) or _TIME_POST_RE.match(text, q.expr_end) or over_as_prep:
                q.role = "timepoint"
                if over_as_prep:
                    # "over 48 h" read as a duration, not as a lower bound
                    q.statement_type, q.value_num, q.value_low, q.bound_type = "exact", q.value_low, None, None
                    q.flags.append("over_read_as_duration")
        _relative_semantics(text, q)
        (out.timepoints if q.role == "timepoint" else out.quantities).append(q)

    # -- uncertainty type for ± and attachment of intervals
    sd, se = bool(_SD_RE.search(text)), bool(_SE_RE.search(text))
    for q in out.quantities:
        if q.uncertainty_type == "unspecified_plus_minus":
            if sd and not se:
                q.uncertainty_type = "sd"
            elif se and not sd:
                q.uncertainty_type = "se"
            elif sd and se:
                q.flags.append("uncertainty_type_ambiguous")
            else:
                q.flags.append("uncertainty_type_unspecified")
    for iv in intervals:
        before = [q for q in out.quantities if q.expr_end <= iv["start"]]
        if not before or before[-1].uncertainty_literal is not None:
            out.unparsed.append({"start": iv["start"], "end": iv["end"], "text": iv["literal"],
                                 "reason": "interval_not_attached_to_a_value"})
            continue
        q = before[-1]
        q.uncertainty_type = iv["type"]
        q.uncertainty_literal, q.uncertainty_start, q.uncertainty_end = iv["literal"], iv["start"], iv["end"]
        q.uncertainty_low, q.uncertainty_high = number_from_literal(iv["lo"]), number_from_literal(iv["hi"])
        q.confidence_level = iv["level"]

    stats = {m.group(1).lower(): m for m in _STAT_RE.finditer(text)}
    if len(stats) == 1:
        (name, m), = stats.items()
        out.summary_statistic = {"text": text[m.start():m.end()], "start": m.start(), "end": m.end(), "name": name}
    mn = _NORMALIZED_RE.search(text)
    if mn:
        out.normalization = {"text": text[mn.start():mn.end()].rstrip(), "start": mn.start(),
                             "end": mn.start() + len(text[mn.start():mn.end()].rstrip())}
    out.quantities.sort(key=lambda x: x.expr_start)
    out.timepoints.sort(key=lambda x: x.expr_start)
    return out


def _relative_semantics(text: str, q: Quantity) -> None:
    """Record explicit relative-quantity semantics; flag what the text leaves unstated."""
    dim = q.unit_dimension
    if dim is None or q.role == "timepoint":
        return
    if dim not in RELATIVE_DIMENSIONS:
        q.relative_semantics = "absolute"
        return
    end = q.expr_end
    if dim == "relative_percent":
        m = _PCT_OF_RE.match(text, end)
        if m:
            q.reference_definition = m.group("ref").strip()
            q.reference_start = m.start("ref")
            q.reference_end = m.start("ref") + len(m.group("ref").rstrip())
            if _PER_MASS_VOL_RE.match(text, m.end("ref")):
                q.relative_semantics = "percent_of_reference_per_mass_or_volume"
            elif _COUNTED_RE.search(q.reference_definition):
                q.relative_semantics = "percent_of_counted_population"
            else:
                q.relative_semantics = "percent_of_reference"
                q.flags.append("percent_reference_needs_review")
        else:
            q.relative_semantics = "unspecified_relative"
            q.flags.append("relative_reference_unspecified")
    elif dim == "relative_percent_per_mass_or_volume":
        q.relative_semantics = "percent_of_reference_per_mass_or_volume"
        m = _PCT_OF_RE.match(text, end)
        if m:
            q.reference_definition = m.group("ref").strip()
            q.reference_start = m.start("ref")
            q.reference_end = m.start("ref") + len(m.group("ref").rstrip())
        else:
            q.flags.append("relative_reference_unspecified")
    elif dim == "relative_fold":
        q.relative_semantics = "fold_change"
        m = _BASELINE_RE.match(text, end)
        if m:
            q.reference_definition = m.group("ref").strip()
            q.reference_start = m.start("ref")
            q.reference_end = m.start("ref") + len(m.group("ref").rstrip())
        else:
            q.flags.append("fold_baseline_unspecified")
    elif dim == "arbitrary":
        q.relative_semantics = "arbitrary_units"
    else:
        q.relative_semantics = "unspecified_relative"
        q.flags.append("relative_reference_unspecified")


def check_transcription(source_text: str, start: int, end: int, literal: str) -> bool:
    """True when ``literal`` is exactly the stored source text at ``[start, end)``."""
    if start is None or end is None or start < 0 or end > len(source_text) or start >= end:
        return False
    return source_text[start:end] == literal
