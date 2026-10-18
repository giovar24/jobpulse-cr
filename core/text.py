"""Utilidades de texto para las descripciones de vacantes."""
import re
from bs4 import BeautifulSoup


def strip_html(text) -> str:
    """Convierte HTML en texto plano (colapsa espacios)."""
    if not text:
        return ""
    return " ".join(BeautifulSoup(str(text), "html.parser").get_text(" ", strip=True).split())


def html_to_text(text) -> str:
    """Convierte HTML en texto plano conservando saltos de línea.

    Mantener los saltos permite separar secciones (encabezados, listas) después.
    """
    if not text:
        return ""
    raw = str(text)
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|li|ul|ol|div|h[1-6]|tr)>", "\n", raw)
    plain = BeautifulSoup(raw, "html.parser").get_text(" ")
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n\s*\n+", "\n", plain)
    return plain.strip()


# Encabezados típicos de la sección de requisitos.
_REQ_HEAD = re.compile(
    r"(requisitos|requirements|qualifications|calificaciones|"
    r"perfil(?: del puesto| requerido| profesional| buscado| ideal)|"
    r"what you.?ll need|what we.?re looking for|"
    r"who you are|must have|minimum qualifications|experiencia requerida|"
    r"conocimientos(?: requeridos)?|se requiere)",
    re.IGNORECASE,
)


def split_requirements(text: str):
    """Separa (descripción, requisitos) a partir de un encabezado conocido.

    Si no hay un encabezado claro, devuelve (texto, "") para no inventar secciones.
    """
    if not text:
        return "", ""
    m = _REQ_HEAD.search(text)
    if not m:
        return text, ""

    newline = text.rfind("\n", 0, m.start())
    # Si el encabezado no está al inicio de una línea (texto de una sola línea),
    # se parte exactamente en el encabezado.
    start = newline + 1 if newline != -1 else m.start()
    desc = text[:start].strip()
    req = text[start:].strip()

    # Si el encabezado abre el texto o casi, no hay descripción que separar.
    if len(desc) < 30:
        return text, ""
    return desc, req
