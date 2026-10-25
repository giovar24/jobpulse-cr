# -*- coding: utf-8 -*-
"""Tests del modelo de perfil harvard-ats-v1 y su puente a candidate_profile_facts."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jobpulse_v15.db import Database
from jobpulse_v15.profile_model import (
    anios_periodo,
    fact_certificacion,
    fact_experiencia,
    facts_from_harvard_json,
    parse_certificacion_notes,
    parse_experiencia_notes,
    slugify,
    suggestion_to_fact,
)
from jobpulse_v15.repository import Repository


class TestSlugifyYPeriodos(unittest.TestCase):
    def test_slugify_sin_acentos_y_estable(self):
        self.assertEqual(slugify("Soporte Técnico N2"), "soporte-tecnico-n2")
        self.assertEqual(slugify("  AD / IAM  "), "ad-iam")
        self.assertEqual(slugify(""), "sin-codigo")

    def test_anios_periodo_mm_yyyy(self):
        self.assertEqual(anios_periodo("01/2020", "01/2022"), 2.0)
        self.assertEqual(anios_periodo("2020", "2021"), 1.0)
        self.assertEqual(anios_periodo("", "2021"), 0.0)
        self.assertEqual(anios_periodo("01/2022", "01/2020"), 0.0)


class TestFactExperienciaRoundTrip(unittest.TestCase):
    def test_notas_estructuradas_y_vuelta(self):
        fact = fact_experiencia(
            empresa="Wyde Corporation (Mphasis)",
            puesto="IT Compliance Analyst",
            ubicacion="Heredia",
            inicio="03/2021",
            fin="12/2024",
            logros=["Reduje MTTR 30%", "Migré IAM a EntraID"],
        )
        self.assertEqual(fact["category"], "experience")
        self.assertEqual(fact["numeric_value"], 3.8)
        self.assertEqual(fact["unit"], "años")
        self.assertIn("IT Compliance Analyst — Wyde Corporation (Mphasis)", fact["value_text"])
        ubicacion, logros = parse_experiencia_notes(fact["notes"])
        self.assertEqual(ubicacion, "Heredia")
        self.assertEqual(logros, ["Reduje MTTR 30%", "Migré IAM a EntraID"])

    def test_sin_logros_notes_none(self):
        fact = fact_experiencia(empresa="X", puesto="Y")
        self.assertIsNone(fact["notes"])
        self.assertEqual(parse_experiencia_notes(None), ("", []))


class TestFactCertificacionRoundTrip(unittest.TestCase):
    def test_notas_con_emisor_y_anio(self):
        fact = fact_certificacion(nombre="AWS Cloud Practitioner", emisor="AWS", anio="2023")
        emisor, anio = parse_certificacion_notes(fact["notes"])
        self.assertEqual((emisor, anio), ("AWS", "2023"))

    def test_sin_emisor_notes_none(self):
        fact = fact_certificacion(nombre="CompTIA A+")
        self.assertIsNone(fact["notes"])
        self.assertEqual(parse_certificacion_notes(None), ("", ""))


PROFILE_JSON = {
    "schema": "harvard-ats-v1",
    "cv": {
        "nombre": "Giovanni Vargas Cruz",
        "contacto": {
            "email": "giovar24@gmail.com",
            "telefono": "+506 8888-8888",
            "ubicacion": "Heredia, Costa Rica",
            "linkedin": "https://www.linkedin.com/in/x",
        },
        "resumen": "Soporte IT con 14 años de experiencia.",
        "educacion": [
            {"institucion": "INTEC", "titulo": "Diplomado en Electrónica",
             "campo": "Electrónica", "inicio": "2000", "fin": "2002"}
        ],
        "experiencia": [
            {"empresa": "Acme", "puesto": "IT Support", "ubicacion": "San José",
             "inicio": "01/2020", "fin": "01/2024", "logros": ["MTTR -30%"]},
            {"empresa": "Beta", "puesto": "Help Desk", "inicio": "2018", "fin": "2019"},
        ],
        "proyectos": [],
        "habilidades": ["Active Directory", "Azure", "Active Directory"],
        "certificaciones": ["ITIL v4", {"nombre": "AWS CCP", "emisor": "AWS", "anio": "2023"}],
        "idiomas": [{"idioma": "Español", "nivel": "nativo"}, {"idioma": "Inglés", "nivel": "B2"}],
    },
    "busqueda": {"overrides": {"experiencia_anios": 14}},
}


class TestFactsFromHarvardJson(unittest.TestCase):
    def test_import_completo_y_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "profile.json"
            ruta.write_text(
                __import__("json").dumps(PROFILE_JSON, ensure_ascii=False), encoding="utf-8"
            )
            source_name, facts = facts_from_harvard_json(ruta)
        self.assertEqual(source_name, "profile.json")
        por_categoria: dict[str, int] = {}
        for fact in facts:
            por_categoria[fact["category"]] = por_categoria.get(fact["category"], 0) + 1
        # 5 contacto (nombre + 4) + 1 resumen + 1 educacion + 3 experiencia
        # (2 empleos + total) + 2 skills (dedupe de "Active Directory")
        # + 2 certificaciones + 2 idiomas
        self.assertEqual(por_categoria.get("contact"), 5)
        self.assertEqual(por_categoria.get("summary"), 1)
        self.assertEqual(por_categoria.get("education"), 1)
        self.assertEqual(por_categoria.get("experience"), 3)
        self.assertEqual(por_categoria.get("skill"), 2)
        self.assertEqual(por_categoria.get("certification"), 2)
        self.assertEqual(por_categoria.get("language"), 2)
        # el total de experiencia llega como fact experience/total
        total = next(f for f in facts if f["value_code"] == "total")
        self.assertEqual(total["numeric_value"], 14.0)

    def test_import_en_bd_acepta_secciones_nuevas(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "bd.db"))
            db.initialize()
            repo = Repository(db)
            facts = [
                {"category": "contact", "value_code": "nombre", "value_text": "Test User"},
                {"category": "summary", "value_code": "perfil-profesional", "value_text": "Resumen"},
                {"category": "project", "value_code": "migrador", "value_text": "Migrador BD"},
            ]
            import_id = repo.import_candidate_profile_facts("test.json", facts)
            guardados = repo.list_candidate_profile_facts()
            self.assertEqual(len(guardados), 3)
            categorias = {f["category"] for f in guardados}
            self.assertEqual(categorias, {"contact", "summary", "project"})
            # revertir deja la BD sin facts nuevos
            resultado = repo.revert_last_candidate_profile_import()
            self.assertIsNotNone(resultado)
            self.assertEqual(repo.list_candidate_profile_facts(), [])
            self.assertGreater(import_id, 0)

    def test_rechaza_education_status_en_categoria_no_educacion(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "bd.db"))
            db.initialize()
            repo = Repository(db)
            with self.assertRaises(ValueError):
                repo.save_candidate_profile_facts([
                    {"category": "contact", "value_code": "nombre",
                     "value_text": "X", "education_status": "completed"}
                ])


class TestSuggestionToFact(unittest.TestCase):
    def test_mapeo_completo(self):
        sug = SimpleNamespace(
            category="experience", value_code="5-years",
            value_text="5 years de experiencia", qualifier=None,
            education_status=None, numeric_value=5, unit="years",
            evidence="soporte tecnico por 5 years",
        )
        fact = suggestion_to_fact(sug)
        self.assertEqual(fact["category"], "experience")
        self.assertEqual(fact["numeric_value"], 5.0)
        self.assertEqual(fact["unit"], "años")
        self.assertIn("Evidencia:", fact["notes"])

    def test_mapeo_minimo(self):
        sug = SimpleNamespace(
            category="skill", value_code="azure", value_text="Azure",
            qualifier=None, education_status=None, numeric_value=None,
            unit=None, evidence="",
        )
        fact = suggestion_to_fact(sug)
        self.assertEqual(fact, {"category": "skill", "value_code": "azure", "value_text": "Azure"})


if __name__ == "__main__":
    unittest.main()
