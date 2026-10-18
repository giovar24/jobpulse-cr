"""Perfil del candidato: fuente unica de verdad del sistema.

Un archivo data/profile.json describe al candidato (titulos, skills, temas de
busqueda, pesos). Todo JobPulse se deriva de ahi:
  - main.py       -> temas de scraping (expandidos semanticamente).
  - search.py     -> ranking de "mejores opciones para el candidato".

Contrato con el futuro analizador de CV: su unico trabajo es leer un CV y
escribir/actualizar data/profile.json con este mismo formato.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PATH = BASE_DIR / "data" / "profile.json"
DEFAULT_PROFILE_PATH = DEFAULT_PATH  # alias legible para los entrypoints


class CandidateProfile:
    def __init__(self, data: Dict, path: Optional[Path] = None):
        self.path = path
        self.nombre = (data.get("nombre") or "").strip()
        self.titulos = [t for t in data.get("titulos", []) if t and str(t).strip()]
        self.skills = [t for t in data.get("skills", []) if t and str(t).strip()]
        self.temas = [
            t for t in data.get("temas", [])
            if isinstance(t, dict) and t.get("puesto")
        ]
        self.pesos = data.get("pesos") or {}
        self.filtros = data.get("filtros") or {}
        # --- campos ATS (opcionales; los rellena cv2profile.py) ---
        self.experiencia_anios = data.get("experiencia_anios") or 0
        self.educacion_nivel = (data.get("educacion_nivel") or "").strip()
        self.idiomas = data.get("idiomas") or []       # [{"idioma": ..., "nivel": ...}]
        self.certificaciones = [
            c for c in data.get("certificaciones", []) if c and str(c).strip()
        ]
        self.contacto = data.get("contacto") or {}      # email / telefono / linkedin / ciudad
        self.pesos_ats = data.get("pesos_ats") or {}    # pesos del score ATS (suman 1)

    @classmethod
    def load(cls, path=None) -> Optional["CandidateProfile"]:
        """Carga el perfil; None si el archivo no existe o es invalido."""
        p = Path(path) if path else DEFAULT_PATH
        if not p.exists():
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return cls(json.load(f), p)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] Perfil invalido ({p}): {exc}")
            return None

    def to_dict(self) -> Dict:
        """Perfil como dict plano (lo que consume core.ats)."""
        return {
            "nombre": self.nombre,
            "titulos": self.titulos,
            "skills": self.skills,
            "temas": self.temas,
            "experiencia_anios": self.experiencia_anios,
            "educacion_nivel": self.educacion_nivel,
            "idiomas": self.idiomas,
            "certificaciones": self.certificaciones,
            "pesos": self.pesos,
            "pesos_ats": self.pesos_ats,
        }

    def sections(self):
        """Devuelve [(nombre_seccion, peso, items)] con solo las que tengan datos."""
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
        """Convierte los temas en el formato de busquedas del scraper."""
        return [
            {
                "puesto": t["puesto"],
                "puesto_en": t.get("puesto_en") or t["puesto"],
                "ubicacion": t.get("ubicacion") or "Costa Rica",
            }
            for t in self.temas
        ]
