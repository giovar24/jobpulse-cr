# -*- coding: utf-8 -*-
"""
jobpulse_v15.location_utils — Normalización de ubicaciones para Costa Rica.

Maneja ambos separadores comunes:
- Coma: "San José, Costa Rica" / "Escazú, San José"
- Slash: "San José / Escazú / Escazú" / "Guanacaste / Liberia / Nacascolo"
"""

from __future__ import annotations

from typing import Dict, Optional


# Provincias de Costa Rica (clave normalizada -> nombre oficial)
PROVINCIAS_CR = {
    "alajuela": "Alajuela",
    "cartago": "Cartago",
    "guanacaste": "Guanacaste",
    "heredia": "Heredia",
    "limón": "Limón",
    "limon": "Limón",
    "puntarenas": "Puntarenas",
    "san josé": "San José",
    "san jose": "San José",
}

# Cantones principales (para extraer ciudad cuando la provincia es el último segmento)
CANTONES_CR = {
    # San José
    "san josé": "San José",
    "san jose": "San José",
    "escazú": "Escazú",
    "escazu": "Escazú",
    "desamparados": "Desamparados",
    "puriscal": "Puriscal",
    "tarrazú": "Tarrazú",
    "tarrazu": "Tarrazú",
    "aserrí": "Aserrí",
    "aserri": "Aserrí",
    "mora": "Mora",
    "goicoechea": "Goicoechea",
    "santa ana": "Santa Ana",
    "alajuelita": "Alajuelita",
    "vázquez de coronado": "Vázquez de Coronado",
    "vazquez de coronado": "Vázquez de Coronado",
    "acosta": "Acosta",
    "tibás": "Tibás",
    "tibas": "Tibás",
    "moravia": "Moravia",
    "montes de oca": "Montes de Oca",
    "turrubares": "Turrubares",
    "dota": "Dota",
    "curridabat": "Curridabat",
    "perez zeledón": "Pérez Zeledón",
    "perez zeledon": "Pérez Zeledón",
    "león cortés": "León Cortés",
    "leon cortes": "León Cortés",
    # Alajuela
    "alajuela": "Alajuela",
    "san ramón": "San Ramón",
    "san ramon": "San Ramón",
    "grecia": "Grecia",
    "san mateo": "San Mateo",
    "santo domingo": "Santo Domingo",
    "palmares": "Palmares",
    "poás": "Poás",
    "poas": "Poás",
    "oresi": "Orosi",
    "san carlos": "San Carlos",
    "zarcero": "Zarcero",
    "sarchí": "Sarchí",
    "sarchi": "Sarchí",
    "upala": "Upala",
    "los chiles": "Los Chiles",
    "guatuso": "Guatuso",
    "río cuarto": "Río Cuarto",
    "rio cuarto": "Río Cuarto",
    # Cartago
    "cartago": "Cartago",
    "paraiso": "Paraíso",
    "la unión": "La Unión",
    "la union": "La Unión",
    "jiménez": "Jiménez",
    "jimenez": "Jiménez",
    "turrialba": "Turrialba",
    "alvarado": "Alvarado",
    "oresi": "Orosi",
    "el guarco": "El Guarco",
    # Heredia
    "heredia": "Heredia",
    "barva": "Barva",
    "santo domingo": "Santo Domingo",
    "santa bárbara": "Santa Bárbara",
    "santa barbara": "Santa Bárbara",
    "san rafael": "San Rafael",
    "san isidro": "San Isidro",
    "belén": "Belén",
    "belen": "Belén",
    "flores": "Flores",
    "san pablo": "San Pablo",
    # Guanacaste
    "liberia": "Liberia",
    "nicoya": "Nicoya",
    "santa cruz": "Santa Cruz",
    "bagaces": "Bagaces",
    "carrillo": "Carrillo",
    "cañas": "Cañas",
    "canas": "Cañas",
    "abangares": "Abangares",
    "tilarán": "Tilarán",
    "tilaran": "Tilarán",
    "nandayure": "Nandayure",
    "la cruz": "La Cruz",
    "hoganchon": "Hojancha",
    # Puntarenas
    "puntarenas": "Puntarenas",
    "esparza": "Esparza",
    "buenos aires": "Buenos Aires",
    "montes de oro": "Montes de Oro",
    "oresa": "Osa",
    "quepos": "Quepos",
    "golfito": "Golfito",
    "coto brus": "Coto Brus",
    "parrita": "Parrita",
    "corredores": "Corredores",
    "garabito": "Garabito",
    # Limón
    "limón": "Limón",
    "limon": "Limón",
    "pococí": "Pococí",
    "pococi": "Pococí",
    "siquirres": "Siquirres",
    "talabeca": "Talamanca",
    "matina": "Matina",
    "guácimo": "Guácimo",
    "guacimo": "Guácimo",
}

# Palabras que indican que es nombre de empresa, no ubicación
EMPRESA_KEYWORDS = {
    "hcltech", "procomer", "cinde", "ane", "bret", "linkedin", "indeed",
    "afz", "evolution", "free zone", "zona franca", "zf", "parque",
    "empresa", "company", "corp", "inc", "ltd", "sa", "s.a.",
}

# Palabras que indican ubicación genérica (no filtrar)
UBICACION_GENERICA = {
    "costa rica", "costa", "rica", "provincia", "provincia de", "provincia", "pais", "país",
}


def _normalize_location_string(location: str) -> str:
    """Normaliza string de ubicación para matching."""
    if not location:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFD", location.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip()


def _extract_parts(location: str) -> list[str]:
    """
    Extrae partes de la ubicación manejando ambos separadores.
    Prioridad: slash > coma (porque slash suele indicar jerarquía: provincia/cantón/distrito)
    Filtra partes que parecen nombres de empresa SOLO a nivel de parte individual.
    """
    if not location:
        return []
    
    def _looks_like_empresa(part: str) -> bool:
        p_norm = _normalize_location_string(part)
        # Si es ubicación genérica conocida, NO filtrar
        for kw in UBICACION_GENERICA:
            if kw in p_norm:
                return False
        # Usar word boundaries para evitar falsos positivos (ej: "sa" en "san jose")
        import re
        for kw in EMPRESA_KEYWORDS:
            # Normalizar keyword también
            kw_norm = _normalize_location_string(kw)
            # Buscar como palabra completa
            pattern = r"(^|[^a-z0-9])" + re.escape(kw_norm) + r"([^a-z0-9]|$)"
            if re.search(pattern, p_norm):
                return True
        return False
    
    # Primero intentar slash (formato: Provincia / Cantón / Distrito)
    if "/" in location:
        raw_parts = [p.strip() for p in location.split("/") if p.strip()]
        # Filtrar cada parte individualmente
        parts = [p for p in raw_parts if not _looks_like_empresa(p)]
        if len(parts) >= 2:
            return parts
    
    # Fallback: coma - AQUÍ es donde está el problema con "HCLTech, Belen Heredia"
    # Dividir por coma PRIMERO, luego filtrar cada parte
    if "," in location:
        raw_parts = [p.strip() for p in location.split(",") if p.strip()]
        parts = [p for p in raw_parts if not _looks_like_empresa(p)]
        if len(parts) >= 2:
            return parts
        # Si después de filtrar queda solo 1 parte pero la original tenía 2+,
        # mantener las partes no-empresa (puede ser "Belen Heredia" válido)
        if len(parts) == 1 and len(raw_parts) >= 2:
            return parts
    
    # Sin separador conocido, devolver como lista simple (si no parece empresa)
    clean = location.strip()
    if clean and not _looks_like_empresa(clean):
        return [clean]
    return []


def normalize_cr_location(location: str) -> Dict[str, Optional[str]]:
    """
    Normaliza ubicación de Costa Rica a city/region.
    
    Args:
        location: String de ubicación raw (ej: "San José / Escazú / Escazú", "Guanacaste", "San José, Costa Rica")
        
    Returns:
        Dict con 'city' y 'region' (claves oficiales de provincia/cantón)
    """
    if not location:
        return {"city": None, "region": None}
    
    parts = _extract_parts(location)
    if not parts:
        return {"city": None, "region": None}
    
    loc_norm = _normalize_location_string(location)
    
    # Buscar provincia en cualquier parte
    province_found = None
    province_key = None
    for key, prov in PROVINCIAS_CR.items():
        if key in loc_norm:
            province_found = prov
            province_key = key
            break
    
    if not province_found:
        return {"city": None, "region": None}
    
    # Normalizar todas las partes
    parts_norm = [_normalize_location_string(p) for p in parts]
    
    # Buscar el cantón (ciudad) en TODAS las partes, no solo las pre-provincia
    city = None
    for i, part_norm in enumerate(parts_norm):
        # Saltar la provincia misma
        if part_norm == province_key:
            continue
        if part_norm in CANTONES_CR:
            city = CANTONES_CR[part_norm]
            break
        # Si no está en cantones conocidos pero parece un nombre de lugar válido
        # (no es "costa rica", "provincia", etc.)
        if part_norm and part_norm not in UBICACION_GENERICA:
            # Filtrar palabras genéricas del nombre de la ciudad
            city_raw = parts[i].strip()
            # Remover sufijos comunes como ", Costa Rica", " Provincia de X"
            import re
            city_raw = re.sub(r',?\s*(costa rica|provincia de .+)$', '', city_raw, flags=re.IGNORECASE).strip()
            if city_raw:
                city = city_raw.title()
                break
    
    # Si solo tenemos una parte (ej: "Guanacaste"), es solo provincia
    if city is None and len(parts) == 1:
        return {"city": None, "region": province_found}
    
    return {"city": city, "region": province_found}


def extract_date_from_source_updated(source_updated_at: Optional[str], scrape_date: Optional[str] = None) -> Optional[str]:
    """
    Extrae date_published usando source_updated_at como fallback.
    
    Para fuentes que no exponen fecha de publicación (CINDE, etc.),
    usamos la fecha de actualización de la fuente o la fecha de scrape.
    """
    if source_updated_at:
        try:
            # Acepta ISO date o ISO datetime
            if "T" in source_updated_at:
                return source_updated_at.split("T")[0]
            return source_updated_at[:10]
        except (ValueError, IndexError):
            pass
    
    if scrape_date:
        try:
            if "T" in scrape_date:
                return scrape_date.split("T")[0]
            return scrape_date[:10]
        except (ValueError, IndexError):
            pass
    
    return None


def normalize_work_mode(wfh: Optional[str]) -> str:
    """Normaliza modalidad de trabajo."""
    if not wfh:
        return "unknown"
    w = wfh.strip().lower()
    if w in ("remoto", "remote", "teletrabajo", "100% remoto", "100 % remoto"):
        return "remote"
    if w in ("híbrido", "hibrido", "hybrid", "mixto", "semipresencial"):
        return "hybrid"
    if w in ("presencial", "onsite", "oficina", "en sitio", "en sitio "):
        return "onsite"
    return "unknown"


def normalize_employment_type(raw: Optional[str]) -> Optional[str]:
    """Normaliza tipo de empleo desde texto estructurado (no desde descripción libre)."""
    if not raw:
        return None
    r = raw.strip().lower()
    # Solo normalizar valores conocidos, NO intentar inferir de texto libre
    if r in ("full-time", "tiempo completo", "full time", "tiempo-completo"):
        return "Full-time"
    if r in ("part-time", "medio tiempo", "part time", "medio-tiempo", "parttime"):
        return "Part-time"
    if r in ("contract", "contrato", "freelance", "temporal"):
        return "Contract"
    if r in ("internship", "pasantía", "pasantia", "practicante"):
        return "Internship"
    # Si no coincide con valores conocidos, devolver None en lugar de texto basura
    return None


if __name__ == "__main__":
    # Test rápido
    test_cases = [
        "San José / Escazú / Escazú",
        "Guanacaste / Liberia / Nacascolo",
        "San José, Costa Rica",
        "Alajuela, Grecia",
        "Guanacaste",
        "HCLTech, Belen Heredia.",
        "HCLTech, AFZ",
        "San Carlos, Provincia de San José",
        "Puntarenas / Golfito / Guaycará",
        "Remote",
        "Virtual",
    ]
    
    for loc in test_cases:
        result = normalize_cr_location(loc)
        print(f'"{loc}" -> city: {result["city"]}, region: {result["region"]}')