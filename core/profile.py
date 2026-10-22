# -*- coding: utf-8 -*-
"""Perfil del candidato: fuente unica de verdad del sistema.

Esquema Harvard/ATS v2 (bloques 'cv' y 'busqueda').
Retrocompatible con el formato plano anterior para no romper corridas ni scripts.
"""
import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PATH = BASE_DIR / "data" / "profile.json"
DEFAULT_PROFILE_PATH = DEFAULT_PATH

SCHEMA = "harvard-ats-v1"
DEFAULT_COMENTARIO = "Perfil del candidato. Fuente única para CV Harvard y búsqueda."

_CONTACTO_LIMITES = {
    "email": 120,
    "telefono": 40,
    "ubicacion": 120,
    "linkedin": 200,
    "github": 200,
    "portafolio": 200,
}

_EDU_MAP = {
    "maestria": "maestria", "maestría": "maestria", "master": "maestria",
    "masters": "maestria", "mba": "maestria", "magister": "maestria",
    "universitario": "universitario", "universidad": "universitario",
    "licenciatura": "universitario", "licenciado": "universitario",
    "bachelor": "universitario", "bachelors": "universitario",
    "grado": "universitario",
    "tecnologo": "tecnologo", "tecnólogo": "tecnologo",
    "tecnico": "tecnico", "técnico": "tecnico", "technical": "tecnico",
    "associate": "tecnico",
    "bachiller": "bachiller", "high school": "bachiller",
    "secundaria": "bachiller", "colegio": "bachiller",
}

_SENIORITY_MAP = {
    "junior": "junior", "jr": "junior", "entry": "junior",
    "trainee": "junior", "practicante": "junior",
    "mid": "mid", "medio": "mid", "intermedio": "mid",
    "semi senior": "mid", "semisenior": "mid", "ssr": "mid",
    "senior": "senior", "sr": "senior", "lead": "senior",
    "principal": "senior",
}


def _texto(valor, limite: int = 500) -> str:
    return re.sub(r"\s+", " ", str(valor or "")).strip()[:limite]


def _lista_texto(valor, limite: int = 40, max_len: int = 200) -> List[str]:
    if not isinstance(valor, list):
        return []
    out = []
    for x in valor:
        if isinstance(x, (dict, list)):
            continue
        t = _texto(x, max_len)
        if t and t not in out:
            out.append(t)
        if len(out) >= limite:
            break
    return out


def _contacto(valor) -> Dict:
    if not isinstance(valor, dict):
        return {}
    out = {}
    for k, lim in _CONTACTO_LIMITES.items():
        t = _texto(valor.get(k), lim)
        if t:
            out[k] = t
    if not out.get("ubicacion"):
        t = _texto(valor.get("ciudad"), 120)
        if t:
            out["ubicacion"] = t
    return out


def _educacion(items) -> List[Dict]:
    if not isinstance(items, list):
        return []
    out = []
    for e in items[:10]:
        if isinstance(e, str):
            e = {"institucion": e}
        if not isinstance(e, dict):
            continue
        fila = {
            "institucion": _texto(e.get("institucion"), 160),
            "titulo": _texto(e.get("titulo"), 160),
            "campo": _texto(e.get("campo"), 120),
            "ubicacion": _texto(e.get("ubicacion"), 120),
            "inicio": _texto(e.get("inicio"), 40),
            "fin": _texto(e.get("fin"), 40),
            "gpa": _texto(e.get("gpa"), 60),
            "honores": _texto(e.get("honores"), 200),
            "cursos": _lista_texto(e.get("cursos"), 12, 120),
        }
        if any(fila.values()):
            out.append(fila)
    return out


def _experiencia(items) -> List[Dict]:
    if not isinstance(items, list):
        return []
    out = []
    for e in items[:15]:
        if isinstance(e, str):
            e = {"empresa": e}
        if not isinstance(e, dict):
            continue
        logros = e.get("logros")
        if isinstance(logros, str):
            logros = [l.strip(" -•\t") for l in re.split(r"\r?\n|;\s*|(?:\s*[•·]\s*)", logros) if l.strip(" -•\t")]
        fila = {
            "empresa": _texto(e.get("empresa"), 160),
            "puesto": _texto(e.get("puesto"), 160),
            "ubicacion": _texto(e.get("ubicacion"), 120),
            "inicio": _texto(e.get("inicio"), 40),
            "fin": _texto(e.get("fin"), 40),
            "logros": _lista_texto(logros, 12, 300),
        }
        if any(fila.values()):
            out.append(fila)
    return out


def _proyectos(items) -> List[Dict]:
    if not isinstance(items, list):
        return []
    out = []
    for p in items[:10]:
        if not isinstance(p, dict):
            continue
        tec = p.get("tecnologias")
        if isinstance(tec, str):
            tec = [t.strip() for t in re.split(r"[,;]", tec) if t.strip()]
        desc = p.get("descripcion")
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        fila = {
            "nombre": _texto(p.get("nombre"), 160),
            "descripcion": _texto(desc, 600),
            "tecnologias": _lista_texto(tec, 15, 60),
            "inicio": _texto(p.get("inicio"), 40),
            "fin": _texto(p.get("fin"), 40),
            "link": _texto(p.get("link"), 200),
        }
        if any(fila.values()):
            out.append(fila)
    return out


def _idiomas(items) -> List[Dict]:
    if not isinstance(items, list):
        return []
    out = []
    for i in items[:8]:
        if isinstance(i, str):
            out.append({"idioma": _texto(i, 40), "nivel": "no especificado"})
        elif isinstance(i, dict) and i.get("idioma"):
            out.append({
                "idioma": _texto(i.get("idioma"), 40),
                "nivel": _texto(i.get("nivel"), 40) or "no especificado",
            })
    return out


def _certificaciones(items) -> List[str]:
    out = []
    for c in items or []:
        if isinstance(c, dict):
            nombre = _texto(c.get("nombre") or c.get("certificacion"), 120)
            emisor = _texto(c.get("emisor") or c.get("institucion"), 80)
            fecha = _texto(c.get("fecha") or c.get("anio"), 20)
            txt = " — ".join(x for x in (nombre, emisor) if x)
            if fecha:
                txt += f" ({fecha})"
        else:
            txt = _texto(c, 200)
        if txt and txt not in out:
            out.append(txt)
    return out[:20]


def _temas(items) -> List[Dict]:
    out = []
    for t in items or []:
        if isinstance(t, dict) and t.get("puesto"):
            out.append({
                "puesto": _texto(t.get("puesto"), 90),
                "puesto_en": _texto(t.get("puesto_en"), 90) or _texto(t.get("puesto"), 90),
                "ubicacion": _texto(t.get("ubicacion"), 90) or "Costa Rica",
            })
    return out


def _anio_de(fecha) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", str(fecha or ""))
    return int(m.group(0)) if m else None


def _anios_experiencia(experiencia: List[Dict]) -> float:
    anio_actual = datetime.now().year
    intervalos = []
    for e in experiencia:
        ini = _anio_de(e.get("inicio"))
        fin_raw = str(e.get("fin") or "")
        if re.search(r"actual|present|now|hoy", fin_raw, re.IGNORECASE):
            fin = anio_actual
        else:
            fin = _anio_de(fin_raw) or ini
        if ini and fin and fin >= ini:
            intervalos.append((ini, min(fin, anio_actual)))
    if not intervalos:
        return 0.0
    intervalos.sort()
    total = 0.0
    cur_s, cur_e = intervalos[0]
    for s, e in intervalos[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += max(1.0, float(cur_e - cur_s))
            cur_s, cur_e = s, e
    total += max(1.0, float(cur_e - cur_s))
    return float(min(total, 45.0))


def _nivel_educacion(educacion: List[Dict]) -> str:
    texto_completo = " ".join(
        f"{e.get('titulo', '')} {e.get('campo', '')} {e.get('institucion', '')}"
        for e in educacion
    ).lower()
    norm = unicodedata.normalize("NFKD", texto_completo).encode("ascii", "ignore").decode("utf-8")
    for nivel, patron in (
        ("maestria", r"\b(maestria|master|mba|magister|postgrado|posgrado|doctorado|phd)\b"),
        ("universitario", r"(licenciatura|licenciado|bachelor|universitario|universidad|grado|ingenieria|ingeniería|bachillerato\s+en|bachiller\s+en)"),
        ("tecnologo", r"\b(tecnologo|tecnologa)\b"),
        ("tecnico", r"\b(tecnico|technical|associate|diplomado)\b"),
        ("bachiller", r"\b(bachiller|secundaria|colegio|high school|nivel\s+medio|diploma)\b"),
    ):
        if re.search(patron, norm):
            return nivel
    return ""


def _seniority(experiencia: List[Dict], anios: float) -> str:
    if experiencia:
        head = unicodedata.normalize("NFKD", (experiencia[0].get("puesto") or "")).encode("ascii", "ignore").decode("utf-8").lower()
        if re.search(r"\b(senior|sr\.?|principal|lead|lider|arquitecto|staff)\b", head):
            return "senior"
        if re.search(r"\b(junior|jr\.?|trainee|becario|asistente)\b", head):
            return "junior"
        if re.search(r"\b(mid|intermedio|pleno|especialista)\b", head):
            return "mid"
    if anios >= 8.0:
        return "senior"
    if 0.0 < anios < 2.0:
        return "junior"
    if anios >= 2.0:
        return "mid"
    return ""


def _hash_cv(cv: dict) -> str:
    return hashlib.sha256(json.dumps(cv, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def normalizar_perfil(data: Dict) -> Dict:
    if not isinstance(data, dict):
        data = {}
    raw_cv = data.get("cv") if isinstance(data.get("cv"), dict) else {}
    raw_b = data.get("busqueda") if isinstance(data.get("busqueda"), dict) else {}

    nombre = _texto(raw_cv.get("nombre") or data.get("nombre") or "", 100)
    contacto = _contacto(raw_cv.get("contacto") or data.get("contacto"))
    resumen = _texto(raw_cv.get("resumen") or data.get("resumen_profesional") or data.get("resumen") or "", 1200)
    educacion = _educacion(raw_cv.get("educacion") if "educacion" in raw_cv else data.get("educacion"))
    experiencia = _experiencia(raw_cv.get("experiencia") if "experiencia" in raw_cv else data.get("experiencia"))
    proyectos = _proyectos(raw_cv.get("proyectos") if "proyectos" in raw_cv else data.get("proyectos"))
    habilidades = _lista_texto(raw_cv.get("habilidades") if "habilidades" in raw_cv else data.get("skills"), 60, 60)
    certificaciones = _certificaciones(raw_cv.get("certificaciones") if "certificaciones" in raw_cv else data.get("certificaciones"))
    idiomas = _idiomas(raw_cv.get("idiomas") if "idiomas" in raw_cv else data.get("idiomas"))

    cv = {
        "nombre": nombre,
        "contacto": contacto,
        "resumen": resumen,
        "educacion": educacion,
        "experiencia": experiencia,
        "proyectos": proyectos,
        "habilidades": habilidades,
        "certificaciones": certificaciones,
        "idiomas": idiomas,
    }

    puestos = _lista_texto(raw_b.get("puestos_objetivo") if "puestos_objetivo" in raw_b else data.get("titulos"), 30, 90)
    ubicaciones = _lista_texto(raw_b.get("ubicaciones"), 20, 90)
    temas = _temas(raw_b.get("temas") if "temas" in raw_b else data.get("temas"))
    if not ubicaciones and contacto.get("ubicacion"):
        ubicaciones = [contacto["ubicacion"]]

    overrides = dict(raw_b.get("overrides") or {})
    if not experiencia and data.get("experiencia_anios") not in (None, "", 0, 0.0):
        try:
            overrides.setdefault("experiencia_anios", float(data["experiencia_anios"]))
        except (TypeError, ValueError):
            pass
    if not educacion and data.get("educacion_nivel"):
        overrides.setdefault("educacion_nivel", str(data["educacion_nivel"]).strip())
    if not experiencia and data.get("seniority"):
        overrides.setdefault("seniority", str(data["seniority"]).strip())

    pesos_def = {"titulo": 1.0, "skills": 0.7, "tema": 0.5}
    pesos_raw = raw_b.get("pesos") or data.get("pesos") or {}
    pesos = {}
    for k, d_val in pesos_def.items():
        try:
            pesos[k] = float(pesos_raw.get(k, d_val))
        except (TypeError, ValueError):
            pesos[k] = d_val

    pesos_ats_def = {"skills": 0.40, "titulo": 0.30, "experiencia": 0.15, "educacion": 0.075, "idiomas": 0.075}
    ats_raw = raw_b.get("pesos_ats") or data.get("pesos_ats") or pesos_ats_def
    pesos_ats = {}
    for k, d_val in pesos_ats_def.items():
        try:
            pesos_ats[k] = float(ats_raw.get(k, d_val))
        except (TypeError, ValueError):
            pesos_ats[k] = d_val

    filtros_def = {"dias": None, "fuentes": []}
    filtros_raw = raw_b.get("filtros") or data.get("filtros") or filtros_def
    filtros = {
        "dias": int(filtros_raw["dias"]) if filtros_raw.get("dias") is not None and str(filtros_raw["dias"]).isdigit() else None,
        "fuentes": [str(x) for x in filtros_raw.get("fuentes", []) if x],
    }

    busqueda = {
        "puestos_objetivo": puestos,
        "ubicaciones": ubicaciones,
        "temas": temas,
        "skills_extra": _lista_texto(raw_b.get("skills_extra"), 30, 60),
        "skills_excluir": _lista_texto(raw_b.get("skills_excluir"), 30, 60),
        "overrides": overrides,
        "pesos": pesos,
        "pesos_ats": pesos_ats,
        "filtros": filtros,
    }

    meta = dict(data.get("meta") or {})
    meta.setdefault("schema", SCHEMA)
    if "cv_hash" not in meta:
        meta["cv_hash"] = _hash_cv(cv)

    return {
        "schema": data.get("schema") or SCHEMA,
        "_comentario": data.get("_comentario") or DEFAULT_COMENTARIO,
        "cv": cv,
        "busqueda": busqueda,
        "meta": meta,
    }


def guardar_perfil(data: Dict, path=None) -> Optional[Path]:
    """Escribe el perfil canónico en disco con backup automático."""
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    perfil = normalizar_perfil(data)
    perfil["meta"]["actualizado"] = datetime.now().isoformat(timespec="seconds")
    perfil["meta"]["cv_hash"] = _hash_cv(perfil["cv"])

    bk = None
    if p.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bk = p.with_name(p.stem + f"_backup_{stamp}.json")
        try:
            bk.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            bk = None
    p.write_text(json.dumps(perfil, ensure_ascii=False, indent=2), encoding="utf-8")
    return bk


def backups_perfil(path=None) -> List[Path]:
    p = Path(path) if path else DEFAULT_PATH
    return sorted(p.parent.glob(p.stem + "_backup_*.json"),
                  key=lambda f: f.stat().st_mtime, reverse=True)


class CandidateProfile:
    def __init__(self, data: Dict, path: Optional[Path] = None):
        self.path = path
        self.datos = normalizar_perfil(data)
        self.cv = self.datos["cv"]
        self.busqueda = self.datos["busqueda"]

        self.nombre = self.cv["nombre"]
        self.contacto = self.cv["contacto"]
        self.resumen_profesional = self.cv["resumen"]
        self.educacion = self.cv["educacion"]
        self.experiencia = self.cv["experiencia"]
        self.proyectos = self.cv["proyectos"]
        self.habilidades = self.cv["habilidades"]

        # --- Campos derivados / intenciones para búsqueda y ATS ---
        overrides = self.busqueda.get("overrides") or {}

        # Experiencia años
        if "experiencia_anios" in overrides:
            try:
                self.experiencia_anios = float(overrides["experiencia_anios"])
            except (TypeError, ValueError):
                self.experiencia_anios = _anios_experiencia(self.cv["experiencia"])
        else:
            self.experiencia_anios = _anios_experiencia(self.cv["experiencia"])

        # Educación nivel
        if "educacion_nivel" in overrides:
            self.educacion_nivel = str(overrides["educacion_nivel"]).strip()
        else:
            self.educacion_nivel = _nivel_educacion(self.cv["educacion"])

        # Seniority
        if "seniority" in overrides:
            self.seniority = str(overrides["seniority"]).strip()
        else:
            self.seniority = _seniority(self.cv["experiencia"], self.experiencia_anios)

        # Skills (unión de habilidades del CV + extra - excluir)
        base_skills = self.cv["habilidades"]
        extra = self.busqueda["skills_extra"]
        excluir = {s.lower() for s in self.busqueda["skills_excluir"]}
        unidas = []
        for s in base_skills + extra:
            if s.lower() not in excluir and s not in unidas:
                unidas.append(s)
        self.skills = unidas

        # Títulos de búsqueda
        puestos_obj = self.busqueda["puestos_objetivo"]
        if puestos_obj:
            self.titulos = puestos_obj
        else:
            # Derivar títulos de la experiencia más reciente; si no hay CV
            # estructurado todavía, usar los temas de búsqueda guardados.
            der = []
            for exp in self.cv["experiencia"]:
                p = exp.get("puesto")
                if p and p not in der:
                    der.append(p)
            if not der:
                for t in self.busqueda.get("temas") or []:
                    p = t.get("puesto") if isinstance(t, dict) else None
                    if p and p not in der:
                        der.append(p)
            self.titulos = der[:8]

        # Temas de búsqueda
        temas_raw = self.busqueda["temas"]
        if temas_raw:
            self.temas = temas_raw
        else:
            loc = (self.busqueda["ubicaciones"] or [self.contacto.get("ubicacion") or "Costa Rica"])[0]
            self.temas = [
                {"puesto": t, "puesto_en": t, "ubicacion": loc}
                for t in self.titulos[:5]
            ]

        self.certificaciones = self.cv["certificaciones"]
        self.idiomas = self.cv["idiomas"]
        self.pesos = self.busqueda["pesos"]
        self.pesos_ats = self.busqueda["pesos_ats"]
        self.filtros = self.busqueda["filtros"]

    @classmethod
    def load(cls, path=None) -> Optional["CandidateProfile"]:
        p = Path(path) if path else DEFAULT_PATH
        if not p.exists():
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return cls(json.load(f), p)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] Perfil inválido ({p}): {exc}")
            return None

    def to_dict(self) -> Dict:
        """Perfil plano compatible con core.ats y search_engine."""
        return {
            "nombre": self.nombre,
            "titulos": self.titulos,
            "skills": self.skills,
            "temas": self.temas,
            "experiencia_anios": self.experiencia_anios,
            "educacion_nivel": self.educacion_nivel,
            "idiomas": self.idiomas,
            "certificaciones": self.certificaciones,
            "seniority": self.seniority,
            "contacto": self.contacto,
            "pesos": self.pesos,
            "pesos_ats": self.pesos_ats,
            "filtros": self.filtros,
            "cv": self.cv,
            "busqueda": self.busqueda,
        }

    def sections(self):
        out = []
        if self.titulos:
            out.append(("titulo", float(self.pesos.get("titulo", 1.0)), self.titulos))
        if self.skills:
            out.append(("skills", float(self.pesos.get("skills", 0.7)), self.skills))
        if self.temas:
            items = [
                f"{t['puesto']} ({t.get('ubicacion') or 'Costa Rica'})"
                for t in self.temas
            ]
            out.append(("tema", float(self.pesos.get("tema", 0.5)), items))
        return out

    def to_searches(self) -> List[Dict]:
        return [
            {
                "puesto": t["puesto"],
                "puesto_en": t.get("puesto_en") or t["puesto"],
                "ubicacion": t.get("ubicacion") or "Costa Rica",
            }
            for t in self.temas
        ]
