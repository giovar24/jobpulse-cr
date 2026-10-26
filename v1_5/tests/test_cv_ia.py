# -*- coding: utf-8 -*-
"""Tests de cv_ia (capa IA de perfil): saneo y prompt. Sin red ni modelo."""
import asyncio
import json
from pathlib import Path

from jobpulse_v15.cv_ia import (
    _mensaje,
    extraer_cv_ia,
    sanea_harvard,
)
from jobpulse_v15.profile_model import (
    facts_desde_perfil_dict,
    facts_from_harvard_json,
)


class ClienteFalso:
    """Sustituto de IAClient: devuelve una respuesta fija (async como el real)."""

    def __init__(self, respuesta: dict) -> None:
        self.respuesta = respuesta
        self.recibido: list | None = None

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001
        self.recibido = messages
        return self.respuesta


def test_sanea_harvard_respuesta_completa() -> None:
    respuesta = {
        "cv": {
            "nombre": "Ana Perez",
            "contacto": {"email": "a@b.co", "telefono": "8888", "linkedin": "x"},
            "resumen": "Perfil",
            "educacion": [{"institucion": "U", "titulo": "Dip", "fin": "2002"}],
            "experiencia": [{"empresa": "E", "puesto": "P", "logros": ["a", "b"]}],
            "habilidades": ["Git", " SQL "],
            "certificaciones": [{"nombre": "CCNA", "emisor": "Cisco", "anio": "2011"}],
            "idiomas": [{"idioma": "Español", "nivel": "Nativo"}],
        },
        "busqueda": {"overrides": {"experiencia_anios": "14"}},
    }
    saneado = sanea_harvard(respuesta)
    assert saneado["cv"]["nombre"] == "Ana Perez"
    assert saneado["cv"]["habilidades"] == ["Git", "SQL"]
    assert saneado["cv"]["certificaciones"][0]["anio"] == 2011  # texto -> int
    assert saneado["busqueda"]["overrides"]["experiencia_anios"] == 14.0


def test_sanea_harvard_tolerante_a_basura() -> None:
    # cv ausente, secciones con tipos malos, campos basura
    respuesta = {
        "cv": {
            "habilidades": [None, 42, "Git", "  ", {"raro": 1}],
            "educacion": "Instituto Interamericano",  # string suelto
            "idiomas": ["Inglés B2"],  # string en vez de mapping
            "experiencia": [{"empresa": 123, "logros": "uno solo"}],
        },
        "busqueda": {"overrides": {"experiencia_anios": "no es numero"}},
    }
    saneado = sanea_harvard(respuesta)
    cv = saneado["cv"]
    assert cv["habilidades"] == ["Git"]
    assert cv["educacion"] == [{"institucion": "Instituto Interamericano",
                                "titulo": None, "campo": None,
                                "inicio": None, "fin": None}]
    assert cv["idiomas"] == [{"idioma": "Inglés B2", "nivel": None}]
    assert cv["experiencia"] == [
        {"empresa": "123", "puesto": None, "ubicacion": None,
         "inicio": None, "fin": None, "logros": "uno solo"}
    ]
    assert saneado["busqueda"]["overrides"]["experiencia_anios"] is None


def test_sanea_harvard_no_dict() -> None:
    for basura in (None, "texto", [1, 2], 42):
        saneado = sanea_harvard(basura)
        assert saneado["cv"] == {}
        assert saneado["busqueda"]["overrides"]["experiencia_anios"] is None


def test_mensaje_contiene_cv_y_esquema() -> None:
    mensajes = _mensaje("CURRICULUM DE PRUEBA")
    assert mensajes[0]["role"] == "system"
    assert "NO inventes" in mensajes[0]["content"]
    assert "harvard-ats-v1" in mensajes[1]["content"]
    assert "CURRICULUM DE PRUEBA" in mensajes[1]["content"]


def test_extraer_cv_ia_con_cliente_falso() -> None:
    cliente = ClienteFalso({"cv": {"nombre": "X", "habilidades": ["Git"]}})
    perfil = asyncio.run(extraer_cv_ia("texto del cv", cliente))
    assert perfil["cv"]["nombre"] == "X"
    assert cliente.recibido is not None


def test_extraer_cv_ia_texto_vacio() -> None:
    try:
        asyncio.run(extraer_cv_ia("   ", ClienteFalso({})))
    except ValueError as exc:
        assert "vacio" in str(exc)
    else:
        raise AssertionError("debia rechazar texto vacio")


def test_facts_desde_perfil_dict_equivalente_a_json(tmp_path: Path) -> None:
    perfil = {
        "cv": {
            "nombre": "Ana Perez",
            "contacto": {"email": "a@b.co"},
            "resumen": "Perfil profesional",
            "educacion": [{"institucion": "U", "titulo": "Diplomado", "fin": "2002"}],
            "experiencia": [{"empresa": "E", "puesto": "P", "logros": ["a"]}],
            "habilidades": ["Git"],
            "certificaciones": [{"nombre": "CCNA", "emisor": "Cisco", "anio": 2011}],
            "idiomas": [{"idioma": "Español", "nivel": "Nativo"}],
        },
        "busqueda": {"overrides": {"experiencia_anios": 14}},
    }
    desde_dict = facts_desde_perfil_dict(perfil)
    ruta = tmp_path / "perfil.json"
    ruta.write_text(json.dumps(perfil, ensure_ascii=False), encoding="utf-8")
    nombre, desde_json = facts_from_harvard_json(ruta)
    assert nombre == "perfil.json"
    assert [dict(f) for f in desde_dict] == [dict(f) for f in desde_json]
    categorias = {f["category"] for f in desde_dict}
    assert {"contact", "summary", "education", "experience", "skill",
            "certification", "language"} <= categorias
    total = [f for f in desde_dict if f["value_code"] == "total"]
    assert total and total[0]["numeric_value"] == 14.0
