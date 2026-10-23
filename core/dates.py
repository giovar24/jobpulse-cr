"""Normalización de fechas de publicación.

Los portales devuelven fechas heterogéneas:
  - relativas en inglés o español ("2 weeks ago", "hace 3 días", "30+ days ago")
  - absolutas ISO ("2026-05-14")
  - absolutas en texto ("14 de mayo de 2026", "May 14, 2026")

parse_date() intenta devolver siempre una fecha ISO (YYYY-MM-DD); si no puede,
devuelve el texto original limpio para no perder el dato.
"""
import re
from datetime import date, timedelta

_UNITS = {
    "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "minuto": "minutes", "minutos": "minutes",
    "hour": "hours", "hours": "hours", "hora": "hours", "horas": "hours",
    "day": "days", "days": "days", "dia": "days", "dias": "days",
    "día": "days", "días": "days",
    "semana": "weeks", "semanas": "weeks", "week": "weeks", "weeks": "weeks",
    "mes": "months", "meses": "months", "month": "months", "months": "months",
}

_REL_RE = re.compile(
    r"(?:hace\s+)?(\d+)\s*\+?\s*"
    r"(min|mins|minute|minutes|minuto|minutos|hour|hours|hora|horas|"
    r"day|days|d[ií]a|d[ií]as|week|weeks|semana|semanas|"
    r"month|months|mes|meses)\b",
    re.IGNORECASE,
)

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _apply(today: date, n: int, kind: str) -> str:
    if kind == "minutes" or kind == "hours":
        return today.isoformat()
    if kind == "days":
        return (today - timedelta(days=n)).isoformat()
    if kind == "weeks":
        return (today - timedelta(weeks=n)).isoformat()
    if kind == "months":
        return (today - timedelta(days=30 * n)).isoformat()
    return today.isoformat()


def _absolute(text: str) -> str:
    """Devuelve ISO si el texto es una fecha absoluta reconocible, si no ''."""
    # ISO / ISO con hora
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    # dd/mm/yyyy o dd-mm-yyyy
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    # "14 de mayo de 2026"
    m = re.search(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)(?:\s+de\s+(\d{4}))?", text, re.IGNORECASE)
    if m and m.group(2).lower() in _MONTHS_ES:
        y = int(m.group(3)) if m.group(3) else date.today().year
        try:
            return date(y, _MONTHS_ES[m.group(2).lower()], int(m.group(1))).isoformat()
        except ValueError:
            pass
    # "May 14, 2026" / "14 May 2026"
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text, re.IGNORECASE)
    if m and m.group(1).lower() in _MONTHS_EN:
        try:
            return date(int(m.group(3)), _MONTHS_EN[m.group(1).lower()], int(m.group(2))).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\s+([a-z]{3,9})\.?\s+(\d{4})", text, re.IGNORECASE)
    if m and m.group(2).lower() in _MONTHS_EN:
        try:
            return date(int(m.group(3)), _MONTHS_EN[m.group(2).lower()], int(m.group(1))).isoformat()
        except ValueError:
            pass
    return ""


def parse_date(text) -> str:
    """Devuelve una fecha ISO (YYYY-MM-DD) o el texto original limpio."""
    if not text:
        return ""
    t = " ".join(str(text).split())
    low = t.lower()
    today = date.today()

    if re.search(r"\bhoy\b|\btoday\b|\bjust posted\b", low):
        return today.isoformat()
    if re.search(r"\bayer\b|\byesterday\b", low):
        return (today - timedelta(days=1)).isoformat()

    m = _REL_RE.search(low)
    if m:
        return _apply(today, int(m.group(1)), _UNITS[m.group(2).lower()])

    iso = _absolute(t)
    return iso or t


def fecha_efectiva(fila) -> str:
    """Fecha ISO para PROCESAR por fecha — presente en TODAS las filas.

    - Si `date_published` es legible (la fecha que trae la pagina), esa manda.
    - Si no (CINDE no publica fecha), se INFIERE la del scrape
      (`created_at`): cota superior razonable de la publicacion.
    - "" solo si tampoco hay created_at (no ocurre en la base).

    OJO: es la fecha de VENTANA/TTL/orden. Para MOSTRAR se sigue usando
    `date_published`: no se inventa la fecha de publicacion del registro.
    """
    fila = fila or {}
    iso = parse_date(fila.get("date_published") or "")
    try:
        return date.fromisoformat(iso[:10]).isoformat()
    except (ValueError, TypeError):
        pass
    creado = str(fila.get("created_at") or "")
    try:
        return date.fromisoformat(creado[:10]).isoformat()
    except ValueError:
        return ""
