"""Migra el perfil Harvard/ATS de la version anterior (profile.json v1.4)
a las tablas candidate_profile_facts de v1_5, via import_candidate_profile_facts
(importacion revertible desde la UI 'Mi perfil' -> Revertir).

Uso: python migrar_perfil.py [ruta_profile.json]
"""
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from jobpulse_v15.repository import Repository
from jobpulse_v15.db import Database


def slug(texto: str) -> str:
    """Codigo estable: minusculas, sin acentos, guiones."""
    decomposed = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    limpio = re.sub(r"[^a-z0-9]+", "-", sin_acentos.lower()).strip("-")
    return limpio[:80] or "sin-codigo"


def anios_periodo(inicio: str, fin: str) -> float:
    """Anios aproximados entre inicio y fin (formatos MM/YYYY o YYYY)."""

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


def main() -> None:
    ruta = Path(sys.argv[1] if len(sys.argv) > 1 else
                r"D:\Proyectos\jobpulse_cr\data\usuario\profile.json")
    perfil = json.loads(ruta.read_text(encoding="utf-8"))
    cv = perfil.get("cv", {})
    busqueda = perfil.get("busqueda", {})
    overrides = busqueda.get("overrides", {})

    facts: list[dict] = []

    # --- Habilidades -> skill ---
    for hab in cv.get("habilidades", []):
        facts.append({
            "category": "skill",
            "value_code": slug(hab),
            "value_text": hab,
        })

    # --- Idiomas -> language (nivel en qualifier) ---
    for idi in cv.get("idiomas", []):
        facts.append({
            "category": "language",
            "value_code": slug(idi["idioma"]),
            "value_text": idi["idioma"],
            "qualifier": idi.get("nivel") or None,
        })

    # --- Certificaciones -> certification ---
    for cert in cv.get("certificaciones", []):
        facts.append({
            "category": "certification",
            "value_code": slug(cert)[:80],
            "value_text": cert,
        })

    # --- Educacion -> education (status completed) ---
    for edu in cv.get("educacion", []):
        partes = [edu.get("titulo", ""), edu.get("institucion", "")]
        periodo = "-".join(x for x in (edu.get("inicio"), edu.get("fin")) if x)
        texto = " — ".join(p for p in partes if p) + (
            f" ({periodo})" if periodo else "")
        facts.append({
            "category": "education",
            "value_code": slug(edu.get("titulo") or edu.get("institucion", "edu")),
            "value_text": texto.strip(),
            "education_status": "completed",
        })

    # --- Experiencia -> experience (un fact por empleo + total) ---
    for exp in cv.get("experiencia", []):
        empresa = exp.get("empresa", "Sin empresa")
        puesto = exp.get("puesto", "")
        anios = anios_periodo(exp.get("inicio", ""), exp.get("fin", ""))
        facts.append({
            "category": "experience",
            "value_code": slug(f"{empresa}-{puesto}")[:80],
            "value_text": f"{puesto} — {empresa} ({exp.get('inicio', '')} a {exp.get('fin', '')})",
            "numeric_value": anios,
            "unit": "años",
        })

    anios_total = overrides.get("experiencia_anios")
    if anios_total is not None:
        facts.append({
            "category": "experience",
            "value_code": "total",
            "value_text": (
                f"{cv.get('resumen', '')[:180]} "
                f"[Total declarado: {anios_total} años en Soporte IT]"
            ),
            "numeric_value": float(anios_total),
            "unit": "años",
        })

    db = Database()
    db.initialize()
    repo = Repository(db)
    import_id = repo.import_candidate_profile_facts(
        "Migracion profile.json v1.4 (harvard-ats-v1)", facts
    )
    print(f"Import {import_id} creada con {len(facts)} facts:")
    por_categoria: dict[str, int] = {}
    for f in facts:
        por_categoria[f["category"]] = por_categoria.get(f["category"], 0) + 1
    for cat, n in sorted(por_categoria.items()):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
