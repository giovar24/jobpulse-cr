# -*- coding: utf-8 -*-
"""Modelo de perfil harvard-ats-v1: secciones del CV y builders de facts.

Las secciones siguen el orden del CV Harvard (estandar ATS):
datos personales, resumen, educacion, experiencia (con logros), proyectos,
habilidades, certificaciones, idiomas.

Cada seccion se persiste como facts de `candidate_profile_facts` con las
convenciones definidas aqui (value_text legible + notes estructuradas), de
modo que el matching (compare_job_to_candidate_profile) sigue funcionando
sobre skill/language/education/experience y las secciones nuevas (contact,
summary, project) quedan inertes para el matching.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# Orden de secciones del CV (estandar Harvard/ATS).
SECCIONES_CV = (
    "contact",
    "summary",
    "education",
    "experience",
    "project",
    "skill",
    "certification",
    "language",
)

CONTACTO_CODIGOS = ("nombre", "email", "telefono", "ubicacion", "linkedin")
RESUMEN_CODE = "perfil-profesional"
EXPERIENCIA_TOTAL_CODE = "total"
UNIT_ANIOS = "años"

PREFIJO_UBICACION = "Ubicación: "
PREFIJO_LOGROS = "Logros:"
VIÑETA = "• "
PREFIJO_EMISOR = "Emisor: "
PREFIJO_ANIO = "Año: "
PREFIJO_ROL = "Rol: "
PREFIJO_ENLACE = "Enlace: "


def slugify(texto: str) -> str:
    """Codigo estable: minusculas, sin acentos, guiones (convencion migrar_perfil)."""
    decomposed = unicodedata.normalize("NFKD", str(texto or ""))
    sin_acentos = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    limpio = re.sub(r"[^a-z0-9]+", "-", sin_acentos.lower()).strip("-")
    return limpio[:80] or "sin-codigo"


def anios_periodo(inicio: str, fin: str) -> float:
    """Años aproximados entre inicio y fin (formatos MM/YYYY o YYYY)."""

    def _a_datetime(v: str) -> datetime | None:
        v = (v or "").strip()
        if not v:
            return None
        for fmt in ("%m/%Y", "%Y"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
        return None

    d1, d2 = _a_datetime(inicio), _a_datetime(fin)
    if not d1 or not d2 or d2 < d1:
        return 0.0
    return round((d2 - d1).days / 365.25, 1)


# ─── Builders: seccion CV -> fact ────────────────────────────────────────────

def fact_contacto(campo: str, valor: str) -> dict[str, Any]:
    return {
        "category": "contact",
        "value_code": slugify(campo),
        "value_text": str(valor or "").strip(),
    }


def fact_resumen(texto: str) -> dict[str, Any]:
    return {
        "category": "summary",
        "value_code": RESUMEN_CODE,
        "value_text": str(texto or "").strip(),
    }


def fact_educacion(
    *,
    institucion: str,
    titulo: str,
    campo: str = "",
    inicio: str = "",
    fin: str = "",
    estado: str = "completed",
) -> dict[str, Any]:
    partes = [titulo.strip(), institucion.strip()]
    periodo = "-".join(x for x in (inicio.strip(), fin.strip()) if x.strip())
    texto = " — ".join(p for p in partes if p)
    if periodo:
        texto += f" ({periodo})"
    fact: dict[str, Any] = {
        "category": "education",
        "value_code": slugify(titulo or institucion or "educacion"),
        "value_text": texto,
        "education_status": estado or "completed",
    }
    if campo.strip():
        fact["notes"] = f"Campo: {campo.strip()}"
    return fact


def fact_experiencia(
    *,
    empresa: str,
    puesto: str,
    ubicacion: str = "",
    inicio: str = "",
    fin: str = "",
    logros: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    texto = f"{puesto.strip()} — {empresa.strip()} ({inicio.strip()} a {fin.strip()})"
    lineas_notes: list[str] = []
    if ubicacion.strip():
        lineas_notes.append(f"{PREFIJO_UBICACION}{ubicacion.strip()}")
    logros_limpios = [str(l).strip() for l in logros if str(l).strip()]
    if logros_limpios:
        lineas_notes.append(PREFIJO_LOGROS)
        lineas_notes.extend(VIÑETA + l for l in logros_limpios)
    return {
        "category": "experience",
        "value_code": slugify(f"{empresa}-{puesto}")[:80],
        "value_text": texto,
        "numeric_value": anios_periodo(inicio, fin),
        "unit": UNIT_ANIOS,
        "notes": "\n".join(lineas_notes) if lineas_notes else None,
    }


def fact_proyecto(
    *, nombre: str, rol: str = "", descripcion: str = "", enlace: str = ""
) -> dict[str, Any]:
    lineas: list[str] = []
    if rol.strip():
        lineas.append(f"{PREFIJO_ROL}{rol.strip()}")
    if enlace.strip():
        lineas.append(f"{PREFIJO_ENLACE}{enlace.strip()}")
    if descripcion.strip():
        lineas.append(descripcion.strip())
    return {
        "category": "project",
        "value_code": slugify(nombre),
        "value_text": nombre.strip(),
        "notes": "\n".join(lineas) if lineas else None,
    }


def fact_skill(nombre: str) -> dict[str, Any]:
    return {
        "category": "skill",
        "value_code": slugify(nombre),
        "value_text": nombre.strip(),
    }


def fact_certificacion(*, nombre: str, emisor: str = "", anio: str = "") -> dict[str, Any]:
    partes = [f"{PREFIJO_EMISOR}{emisor.strip()}" if emisor.strip() else ""]
    partes.append(f"{PREFIJO_ANIO}{anio.strip()}" if anio.strip() else "")
    notes = " | ".join(p for p in partes if p)
    return {
        "category": "certification",
        "value_code": slugify(nombre)[:80],
        "value_text": nombre.strip(),
        "notes": notes or None,
    }


def fact_idioma(idioma: str, nivel: str = "") -> dict[str, Any]:
    return {
        "category": "language",
        "value_code": slugify(idioma),
        "value_text": idioma.strip(),
        "qualifier": nivel.strip() or None,
    }


# ─── Parsers: fact -> formulario (ida y vuelta de edicion) ──────────────────

def parse_experiencia_notes(notes: str | None) -> tuple[str, list[str]]:
    """(ubicacion, logros) desde notes estructuradas de un fact de experiencia."""
    ubicacion, logros, en_logros = "", [], False
    for linea in (notes or "").splitlines():
        if linea.startswith(PREFIJO_UBICACION):
            ubicacion = linea[len(PREFIJO_UBICACION):].strip()
        elif linea.startswith(PREFIJO_LOGROS):
            en_logros = True
        elif en_logros and linea.strip():
            logros.append(linea.strip().removeprefix(VIÑETA).strip())
    return ubicacion, logros


def parse_certificacion_notes(notes: str | None) -> tuple[str, str]:
    """(emisor, anio) desde notes de un fact de certificacion."""
    emisor = anio = ""
    for parte in (notes or "").split(" | "):
        if parte.startswith(PREFIJO_EMISOR):
            emisor = parte[len(PREFIJO_EMISOR):].strip()
        elif parte.startswith(PREFIJO_ANIO):
            anio = parte[len(PREFIJO_ANIO):].strip()
    return emisor, anio


def parse_proyecto_notes(notes: str | None) -> tuple[str, str, str]:
    """(rol, enlace, descripcion) desde notes de un fact de proyecto."""
    rol = enlace = ""
    descripcion: list[str] = []
    for linea in (notes or "").splitlines():
        if linea.startswith(PREFIJO_ROL):
            rol = linea[len(PREFIJO_ROL):].strip()
        elif linea.startswith(PREFIJO_ENLACE):
            enlace = linea[len(PREFIJO_ENLACE):].strip()
        elif linea.strip():
            descripcion.append(linea.strip())
    return rol, enlace, "\n".join(descripcion)


# ─── Importacion: profile.json (harvard-ats-v1) -> facts ────────────────────

def _cert_from_entrada(entrada: Any) -> dict[str, Any] | None:
    if isinstance(entrada, Mapping):
        nombre = str(entrada.get("nombre") or entrada.get("titulo") or "").strip()
        if not nombre:
            return None
        return fact_certificacion(
            nombre=nombre,
            emisor=str(entrada.get("emisor") or entrada.get("institucion") or ""),
            anio=str(entrada.get("anio") or entrada.get("año") or entrada.get("ano") or ""),
        )
    nombre = str(entrada or "").strip()
    return fact_certificacion(nombre=nombre) if nombre else None


def facts_from_harvard_json(ruta: str | Path) -> tuple[str, list[dict[str, Any]]]:
    """Lee un profile.json harvard-ats-v1 y devuelve (source_name, facts).

    Cubre TODAS las secciones del estandar: contacto, resumen, educacion,
    experiencia (con logros), proyectos, habilidades, certificaciones, idiomas
    y el total de experiencia declarado en busqueda.overrides.
    """
    ruta = Path(ruta)
    perfil = json.loads(ruta.read_text(encoding="utf-8"))
    cv = perfil.get("cv", {})
    facts: list[dict[str, Any]] = []
    vistos: set[tuple[str, str]] = set()

    def _agregar(fact: dict[str, Any] | None) -> None:
        if not fact or not fact.get("value_text"):
            return
        clave = (fact["category"], str(fact["value_code"]).casefold())
        if clave in vistos:
            return
        vistos.add(clave)
        facts.append(fact)

    contacto = cv.get("contacto", {}) or {}
    if cv.get("nombre"):
        _agregar(fact_contacto("nombre", cv["nombre"]))
    for campo in CONTACTO_CODIGOS[1:]:
        if contacto.get(campo):
            _agregar(fact_contacto(campo, contacto[campo]))

    if cv.get("resumen"):
        _agregar(fact_resumen(cv["resumen"]))

    for edu in cv.get("educacion", []) or []:
        _agregar(fact_educacion(
            institucion=str(edu.get("institucion") or ""),
            titulo=str(edu.get("titulo") or ""),
            campo=str(edu.get("campo") or ""),
            inicio=str(edu.get("inicio") or ""),
            fin=str(edu.get("fin") or ""),
            estado="completed",
        ))

    for exp in cv.get("experiencia", []) or []:
        logros = exp.get("logros") or []
        if isinstance(logros, str):
            logros = [logros]
        _agregar(fact_experiencia(
            empresa=str(exp.get("empresa") or "Sin empresa"),
            puesto=str(exp.get("puesto") or ""),
            ubicacion=str(exp.get("ubicacion") or ""),
            inicio=str(exp.get("inicio") or ""),
            fin=str(exp.get("fin") or ""),
            logros=[str(l) for l in logros],
        ))

    for proy in cv.get("proyectos", []) or []:
        if not isinstance(proy, Mapping):
            continue
        _agregar(fact_proyecto(
            nombre=str(proy.get("nombre") or proy.get("titulo") or ""),
            rol=str(proy.get("rol") or proy.get("tecnologias") or ""),
            descripcion=str(proy.get("descripcion") or ""),
            enlace=str(proy.get("enlace") or proy.get("url") or ""),
        ))

    for hab in cv.get("habilidades", []) or []:
        nombre = hab.get("nombre") if isinstance(hab, Mapping) else hab
        _agregar(fact_skill(str(nombre or "")))

    for cert in cv.get("certificaciones", []) or []:
        _agregar(_cert_from_entrada(cert))

    for idi in cv.get("idiomas", []) or []:
        if isinstance(idi, Mapping):
            _agregar(fact_idioma(str(idi.get("idioma") or ""), str(idi.get("nivel") or "")))
        else:
            _agregar(fact_idioma(str(idi or "")))

    overrides = (perfil.get("busqueda", {}) or {}).get("overrides", {}) or {}
    total = overrides.get("experiencia_anios")
    if total is not None:
        _agregar({
            "category": "experience",
            "value_code": EXPERIENCIA_TOTAL_CODE,
            "value_text": f"Experiencia total declarada: {total} años",
            "numeric_value": float(total),
            "unit": UNIT_ANIOS,
            "notes": "Declarado en busqueda.overrides del perfil original.",
        })

    return ruta.name, facts


# ─── Sugerencias del lector CV -> fact ──────────────────────────────────────

def suggestion_to_fact(suggestion: Any) -> dict[str, Any]:
    """Convierte una CandidateProfileSuggestion en fact listo para importar."""
    fact: dict[str, Any] = {
        "category": suggestion.category,
        "value_code": suggestion.value_code,
        "value_text": suggestion.value_text,
    }
    if suggestion.category == "education" and suggestion.education_status:
        fact["education_status"] = suggestion.education_status
    if suggestion.category == "experience" and suggestion.numeric_value is not None:
        fact["numeric_value"] = float(suggestion.numeric_value)
        # Convencion de la BD (migrar_perfil): la unidad de experiencia es
        # "años"; el extractor la reporta como "years".
        fact["unit"] = UNIT_ANIOS
    if suggestion.qualifier:
        fact["qualifier"] = suggestion.qualifier
    if suggestion.evidence:
        fact["notes"] = f"Evidencia: {suggestion.evidence}"
    return fact
