"""Explicit unit table.

An explicit, reviewable table is used instead of a units library (DECISIONS.md):
every conversion the application can perform is listed here, with its factor.
Nothing outside this table is ever converted.

* ``factor`` converts a value in the unit to the dimension's base unit
  (``value_base = value * factor``). ``None`` means *recognised but not
  convertible* (relative quantities, arbitrary units, calendar months/years,
  temperatures with an offset).
* ``ambiguous`` marks symbols with more than one common reading. They are
  recognised, flagged, and never converted until a reviewer fixes the unit.
* Relative quantities (percent, fold, arbitrary units) are never converted
  into one another or into absolute units.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class UnitDef:
    symbol: str                 # canonical spelling
    dimension: str
    factor: float | None        # to base unit of the dimension; None = not convertible
    aliases: tuple[str, ...] = ()
    ambiguous: bool = False
    note: str = ""


#: Base unit per dimension (conversion target).
BASE_UNITS: dict[str, str] = {
    "mass": "g", "volume": "L", "length": "m", "time": "s", "amount": "mol",
    "mass_concentration": "g/L", "molar_concentration": "mol/L", "volumetric_flow": "L/s",
    "rate": "1/s", "velocity": "m/s", "pressure": "Pa", "mass_per_mass": "g/g",
}

_U = UnitDef
UNIT_TABLE: tuple[UnitDef, ...] = (
    # mass
    _U("kg", "mass", 1e3), _U("g", "mass", 1.0), _U("mg", "mass", 1e-3),
    _U("µg", "mass", 1e-6, ("μg", "ug", "mcg")), _U("ng", "mass", 1e-9), _U("pg", "mass", 1e-12),
    # volume
    _U("L", "volume", 1.0, ("l", "liter", "liters", "litre", "litres")),
    _U("mL", "volume", 1e-3, ("ml", "cc")), _U("µL", "volume", 1e-6, ("μL", "uL", "ul", "µl", "μl")),
    _U("m³", "volume", 1e3, ("m3", "m^3")), _U("cm³", "volume", 1e-3, ("cm3", "cm^3")),
    # length
    _U("km", "length", 1e3), _U("m", "length", 1.0, (), True, "'m' may also be written for minutes or months"),
    _U("cm", "length", 1e-2), _U("mm", "length", 1e-3), _U("µm", "length", 1e-6, ("μm", "um")),
    _U("nm", "length", 1e-9),
    # time
    _U("s", "time", 1.0, ("sec", "secs", "second", "seconds")),
    _U("min", "time", 60.0, ("mins", "minute", "minutes")),
    _U("h", "time", 3600.0, ("hr", "hrs", "hour", "hours")),
    _U("d", "time", 86400.0, ("day", "days")),
    _U("wk", "time", 604800.0, ("wks", "week", "weeks")),
    _U("mo", "time", None, ("month", "months"), False, "calendar months have no fixed length; not converted"),
    _U("yr", "time", None, ("yrs", "year", "years"), False, "calendar years have no fixed length; not converted"),
    # amount of substance
    _U("mol", "amount", 1.0), _U("mmol", "amount", 1e-3), _U("µmol", "amount", 1e-6, ("μmol", "umol")),
    _U("nmol", "amount", 1e-9), _U("pmol", "amount", 1e-12),
    # mass concentration
    _U("g/L", "mass_concentration", 1.0, ("g/l",)), _U("mg/L", "mass_concentration", 1e-3, ("mg/l",)),
    _U("µg/L", "mass_concentration", 1e-6, ("μg/L", "ug/L", "ug/l")),
    _U("ng/L", "mass_concentration", 1e-9, ("ng/l",)),
    _U("g/mL", "mass_concentration", 1e3, ("g/ml",)), _U("mg/mL", "mass_concentration", 1.0, ("mg/ml",)),
    _U("µg/mL", "mass_concentration", 1e-3, ("μg/mL", "ug/mL", "ug/ml")),
    _U("ng/mL", "mass_concentration", 1e-6, ("ng/ml",)), _U("pg/mL", "mass_concentration", 1e-9, ("pg/ml",)),
    _U("kg/m³", "mass_concentration", 1.0, ("kg/m3", "kg/m^3")),
    # molar concentration
    _U("mol/L", "molar_concentration", 1.0, ("mol/l",)), _U("mmol/L", "molar_concentration", 1e-3, ("mmol/l",)),
    _U("µmol/L", "molar_concentration", 1e-6, ("μmol/L", "umol/L", "umol/l")),
    _U("nmol/L", "molar_concentration", 1e-9, ("nmol/l",)),
    _U("M", "molar_concentration", 1.0, (), True, "'M' may mean molar, mega or million"),
    _U("mM", "molar_concentration", 1e-3), _U("µM", "molar_concentration", 1e-6, ("μM", "uM")),
    _U("nM", "molar_concentration", 1e-9), _U("pM", "molar_concentration", 1e-12),
    # volumetric flow
    _U("L/s", "volumetric_flow", 1.0, ("l/s",)), _U("L/min", "volumetric_flow", 1 / 60.0, ("l/min",)),
    _U("L/h", "volumetric_flow", 1 / 3600.0, ("l/h", "L/hr")),
    _U("mL/s", "volumetric_flow", 1e-3, ("ml/s",)), _U("mL/min", "volumetric_flow", 1e-3 / 60.0, ("ml/min",)),
    _U("mL/h", "volumetric_flow", 1e-3 / 3600.0, ("ml/h", "mL/hr")),
    _U("m³/h", "volumetric_flow", 1e3 / 3600.0, ("m3/h", "m^3/h")),
    # first-order rate
    _U("1/s", "rate", 1.0, ("/s", "s⁻¹", "s-1", "s^-1")), _U("1/min", "rate", 1 / 60.0, ("/min", "min⁻¹", "min-1", "min^-1")),
    _U("1/h", "rate", 1 / 3600.0, ("/h", "h⁻¹", "h-1", "h^-1", "/hr")),
    _U("1/d", "rate", 1 / 86400.0, ("/d", "d⁻¹", "d-1", "d^-1", "/day")),
    # velocity
    _U("m/s", "velocity", 1.0), _U("cm/s", "velocity", 1e-2), _U("mm/s", "velocity", 1e-3),
    _U("m/h", "velocity", 1 / 3600.0),
    # pressure
    _U("Pa", "pressure", 1.0), _U("kPa", "pressure", 1e3), _U("MPa", "pressure", 1e6),
    _U("bar", "pressure", 1e5), _U("mbar", "pressure", 1e2),
    # mass per mass
    _U("g/g", "mass_per_mass", 1.0), _U("mg/g", "mass_per_mass", 1e-3), _U("µg/g", "mass_per_mass", 1e-6, ("μg/g", "ug/g")),
    _U("mg/kg", "mass_per_mass", 1e-6), _U("µg/kg", "mass_per_mass", 1e-9, ("μg/kg", "ug/kg")),
    _U("g/kg", "mass_per_mass", 1e-3),
    # temperature (offset scales: recognised, not converted by a factor table)
    _U("°C", "temperature", None, ("degC", "℃"), False, "offset scale; not converted"),
    _U("K", "temperature", None, (), True, "'K' may mean kelvin or thousand"),
    # relative and arbitrary quantities: recognised, never converted
    _U("%", "relative_percent", None, ("percent", "per cent"), False, "requires explicit reference semantics"),
    _U("%/g", "relative_percent_per_mass_or_volume", None, ("% per g", "%/mg", "%/kg", "%/mL", "%/ml", "%/L",
                                                           "% per mL", "% per ml", "% per kg", "% per L"),
       False, "percent of a reference amount per unit mass or volume"),
    _U("fold", "relative_fold", None, ("-fold", "x-fold"), False, "requires a stated baseline"),
    _U("a.u.", "arbitrary", None, ("AU", "au", "arb. units", "arb. u.", "arbitrary units", "arbitrary unit", "RFU",
                                   "RLU"), False, "instrument units; not comparable across instruments"),
    _U("ppm", "relative_parts_per", None, (), True, "basis (mass, volume or molar) not stated by the symbol"),
    _U("ppb", "relative_parts_per", None, (), True, "basis (mass, volume or molar) not stated by the symbol"),
)

_BY_SPELLING: dict[str, UnitDef] = {}
for _u in UNIT_TABLE:
    for _sp in (_u.symbol, *_u.aliases):
        _BY_SPELLING.setdefault(_sp, _u)
# word-like aliases are matched case-insensitively
_WORDLIKE: dict[str, UnitDef] = {sp: u for sp, u in _BY_SPELLING.items()
                                 if len(sp) >= 3 and sp.isalpha() and sp.islower()}

RELATIVE_DIMENSIONS = {"relative_percent", "relative_percent_per_mass_or_volume", "relative_fold", "arbitrary",
                       "relative_parts_per"}
TIME_DIMENSION = "time"


def _spellings_regex() -> re.Pattern[str]:
    spellings = sorted(_BY_SPELLING, key=len, reverse=True)
    alt = "|".join(re.escape(s) for s in spellings)
    # a unit must not run into further letters ("mg" in "mgmt"), and "%" may stand alone
    return re.compile(rf"(?:{alt})(?![A-Za-zµμ³°])")


UNIT_RE = _spellings_regex()
_WORDLIKE_RE = re.compile(
    r"(?:" + "|".join(re.escape(s) for s in sorted(_WORDLIKE, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def match_unit_at(text: str, pos: int) -> tuple[UnitDef, int, int] | None:
    """Match a table unit starting exactly at ``pos``. Returns ``(unit, start, end)``."""
    m = UNIT_RE.match(text, pos)
    if m:
        return _BY_SPELLING[m.group(0)], m.start(), m.end()
    m = _WORDLIKE_RE.match(text, pos)
    if m:
        return _WORDLIKE[m.group(0).lower()], m.start(), m.end()
    return None


def lookup(unit_text: str | None, extra_units: dict[str, str] | None = None) -> tuple[str, UnitDef | None]:
    """Classify a unit string.

    Returns ``(status, unit)`` where status is ``recognized``, ``ambiguous``,
    ``project_defined`` (listed in project configuration; no conversion),
    ``unrecognized`` or ``missing``.
    """
    if unit_text is None or not unit_text.strip():
        return "missing", None
    text = unit_text.strip()
    unit = _BY_SPELLING.get(text) or _WORDLIKE.get(text.lower())
    if unit is None:
        if extra_units and text in extra_units:
            return "project_defined", UnitDef(text, extra_units[text], None, note="project-defined; no conversion")
        return "unrecognized", None
    return ("ambiguous" if unit.ambiguous else "recognized"), unit


class ConversionError(Exception):
    """A requested conversion is not supported by the explicit table."""


def conversion(unit_from: str, unit_to: str) -> tuple[float, str]:
    """Factor and rule id for converting ``unit_from`` to ``unit_to``.

    Raises :class:`ConversionError` for unknown, ambiguous, relative, arbitrary or
    cross-dimension units. Relative quantities are never converted automatically.
    """
    s_from, u_from = lookup(unit_from)
    s_to, u_to = lookup(unit_to)
    for label, status, text in (("source", s_from, unit_from), ("target", s_to, unit_to)):
        if status == "ambiguous":
            raise ConversionError(f"{label} unit {text!r} is ambiguous; a reviewer must state the intended unit.")
        if status != "recognized":
            raise ConversionError(f"{label} unit {text!r} is not in the conversion table ({status}).")
    assert u_from is not None and u_to is not None
    if u_from.dimension in RELATIVE_DIMENSIONS or u_to.dimension in RELATIVE_DIMENSIONS:
        raise ConversionError(
            f"{unit_from!r} -> {unit_to!r}: relative and arbitrary quantities are never converted automatically."
        )
    if u_from.dimension != u_to.dimension:
        raise ConversionError(
            f"{unit_from!r} ({u_from.dimension}) and {unit_to!r} ({u_to.dimension}) have different dimensions."
        )
    if u_from.factor is None or u_to.factor is None:
        raise ConversionError(f"{unit_from!r} -> {unit_to!r}: no fixed conversion factor ({u_from.note or u_to.note}).")
    factor = u_from.factor / u_to.factor
    return factor, f"unit_conversion:{u_from.symbol}->{u_to.symbol}"


def base_unit(unit_text: str) -> str | None:
    """Base unit of the unit's dimension, or ``None`` when no conversion is defined."""
    status, unit = lookup(unit_text)
    if status != "recognized" or unit is None or unit.factor is None:
        return None
    return BASE_UNITS.get(unit.dimension)


def unit_table_rows() -> list[dict]:
    """The table as rows, for documentation and export."""
    return [{"symbol": u.symbol, "dimension": u.dimension, "factor_to_base": u.factor,
             "base_unit": BASE_UNITS.get(u.dimension), "aliases": ", ".join(u.aliases),
             "ambiguous": u.ambiguous, "note": u.note} for u in UNIT_TABLE]
