# -*- coding: utf-8 -*-
"""Capa IA para extraer el perfil (harvard-ats-v1) desde el texto de un CV.

Complementa al extractor de reglas (candidate_profile.extract_candidate_profile):
las reglas leen ofertas y de los CVs solo sacan idiomas/años; el modelo local
(Gemma, llama.cpp) entiende la estructura del CV y rellena todas las secciones.

Flujo: read_cv_text (PDF/DOCX/TXT) -> extraer_cv_ia -> dict harvard-ats-v1
       -> profile_model.facts_desde_perfil_dict -> revision UI -> import.

Principios:
  - El modelo NO inventa: solo datos presentes en el texto (null si falta).
  - La salida se sanea SIEMPRE antes de usarse (sanea_harvard).
  - Cache IA por (modelo+mensajes) en disco; misma que el enriquecimiento.
"""

from __future__ import annotations

from typing import Any, Mapping

from jobpulse_v15.ia_api import IAClient

# ─── Prompt: CV texto -> harvard-ats-v1 ──────────────────────────────────────

PROMPT_SISTEMA = (
    "Eres un extractor de datos de CVs. Recibes el texto de un CV y devuelves "
    "UNICAMENTE un JSON valido con la estructura harvard-ats-v1. Reglas: "
    "(1) NO inventes nada: si un dato no aparece en el texto, usa null o lista "
    "vacia. (2) Fechas tal cual aparecen (ej. '04/2022', '2019', 'actual'). "
    "(3) Nivel de idioma solo si el CV lo declara (CEFR: A1-C2, nativo). "
    "(4) habilidades = lista de strings cortas tal cual el CV las nombra. "
    "(5) logros = puntos de la experiencia (logros/responsabilidades). "
    "(6) certificaciones: nombre, emisor y anio si aparecen. "
    "(7) busqueda.overrides.experiencia_anios: numero total de anios de "
    "experiencia SOLO si el CV lo declara textualmente."
)

PROMPT_USUARIO = (
    "Convierte este CV a JSON harvard-ats-v1. Estructura exacta:\n"
    "{\n"
    '  "cv": {\n'
    '    "nombre": str|null,\n'
    '    "contacto": {"email": str|null, "telefono": str|null, '
    '"ubicacion": str|null, "linkedin": str|null, "portfolio": str|null},\n'
    '    "resumen": str|null,\n'
    '    "educacion": [{"institucion": str, "titulo": str, "campo": str|null, '
    '"inicio": str|null, "fin": str|null}],\n'
    '    "experiencia": [{"empresa": str, "puesto": str, "ubicacion": str|null, '
    '"inicio": str|null, "fin": str|null, "logros": [str]}],\n'
    '    "proyectos": [{"nombre": str, "rol": str|null, "descripcion": str|null, '
    '"enlace": str|null}],\n'
    '    "habilidades": [str],\n'
    '    "certificaciones": [{"nombre": str, "emisor": str|null, "anio": int|null}],\n'
    '    "idiomas": [{"idioma": str, "nivel": str|null}]\n'
    "  },\n"
    '  "busqueda": {"overrides": {"experiencia_anios": number|null}}\n'
    "}\n\nCV:\n<<<\n{texto}\n>>>"
)


def _mensaje(texto_cv: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PROMPT_SISTEMA},
        {"role": "user", "content": PROMPT_USUARIO.replace("{texto}", texto_cv)},
    ]


# ─── Saneo de la respuesta IA ────────────────────────────────────────────────


def _str_o_none(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _lista(valor: Any) -> list[Any]:
    """Lista de mappings/strings; un elemento suelto se envuelve."""
    if valor is None:
        return []
    if isinstance(valor, (list, tuple)):
        return list(valor)
    return [valor]


def sanea_harvard(respuesta: Any) -> dict[str, Any]:
    """Normaliza la respuesta del modelo a un dict harvard-ats-v1 seguro.

    Tolerante a: cv ausente, secciones como string, entradas con tipos
    mezclados, numeros como texto. Nunca lanza; lo que no se entiende se
    descarta (la revision UI es la puerta final).
    """
    if not isinstance(respuesta, Mapping):
        return {"cv": {}, "busqueda": {"overrides": {"experiencia_anios": None}}}
    cv = respuesta.get("cv")
    cv = cv if isinstance(cv, Mapping) else {}

    def _mapeos(valor: Any, *claves: str) -> list[dict[str, Any]]:
        def _campo(v: Any) -> Any:
            # Escalares -> texto (el modelo manda numeros/datos sueltos);
            # listas/dict pasan tal cual (logros puede ser lista o string).
            if v is None or isinstance(v, (list, dict)):
                return v
            texto = str(v).strip()
            return texto or None

        limpios: list[dict[str, Any]] = []
        for entrada in _lista(valor):
            if isinstance(entrada, Mapping):
                limpios.append({k: _campo(entrada.get(k)) for k in claves})
            elif isinstance(entrada, str) and entrada.strip() and claves:
                # String suelto: va al primer campo identificador de la seccion
                # (educacion->institucion, experiencia->empresa, idiomas->idioma...).
                limpios.append({
                    k: (entrada.strip() if k == claves[0] else None) for k in claves
                })
        return limpios

    contacto = cv.get("contacto")
    contacto = contacto if isinstance(contacto, Mapping) else {}

    cv_limpio: dict[str, Any] = {
        "nombre": _str_o_none(cv.get("nombre")),
        "contacto": {k: _str_o_none(contacto.get(k)) for k in
                     ("email", "telefono", "ubicacion", "linkedin", "portfolio")},
        "resumen": _str_o_none(cv.get("resumen")),
        "educacion": _mapeos(cv.get("educacion"),
                             "institucion", "titulo", "campo", "inicio", "fin"),
        "experiencia": _mapeos(cv.get("experiencia"),
                               "empresa", "puesto", "ubicacion", "inicio", "fin", "logros"),
        "proyectos": _mapeos(cv.get("proyectos"), "nombre", "rol", "descripcion", "enlace"),
        "habilidades": [
            s.strip() for s in _lista(cv.get("habilidades"))
            if isinstance(s, str) and s.strip()
        ],
        "certificaciones": [
            {**e, "anio": int(e["anio"])} if str(e.get("anio") or "").strip().isdigit()
            else {k: v for k, v in e.items() if k != "anio"}
            for e in _mapeos(cv.get("certificaciones"), "nombre", "emisor", "anio")
        ],
        "idiomas": _mapeos(cv.get("idiomas"), "idioma", "nivel"),
    }

    overrides = respuesta.get("busqueda")
    overrides = overrides.get("overrides", {}) if isinstance(overrides, Mapping) else {}
    total = overrides.get("experiencia_anios") if isinstance(overrides, Mapping) else None
    try:
        total = float(total) if total is not None and str(total).strip() else None
    except (TypeError, ValueError):
        total = None

    return {"cv": cv_limpio, "busqueda": {"overrides": {"experiencia_anios": total}}}


# ─── Extraccion ──────────────────────────────────────────────────────────────


async def extraer_cv_ia(texto_cv: str, cliente: IAClient | None = None) -> dict[str, Any]:
    """Texto del CV -> dict harvard-ats-v1 saneado (modelo del .env).

    Async como el resto del pipeline IA (ia_enrichment); en scripts:
    asyncio.run(extraer_cv_ia(texto)).
    """
    if not texto_cv.strip():
        raise ValueError("El texto del CV esta vacio.")
    cliente = cliente or IAClient()
    respuesta = await cliente.chat_json(_mensaje(texto_cv), temperature=0.0, max_tokens=3000)
    return sanea_harvard(respuesta)
