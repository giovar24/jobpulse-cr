# -*- coding: utf-8 -*-
"""Pruebas del taxonomo de sectores y de la auto-evaluacion del perfil.

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v
"""
import unittest
from types import SimpleNamespace

from core.sectores import (SECTORES_OBJETIVO_DEFAULT, inferir_sector,
                           inferir_sectores_perfil, sectores_objetivo_de)


class TestInferirSector(unittest.TestCase):
    def test_sectores_claros(self):
        casos = {
            "Cocinero B": "alimentos",
            "Mecánico Automotriz": "manufactura",
            "Técnico de Soporte Técnico": "tecnologia",
            "Abogado Corporativo": "legal",
            "Enfermera General": "salud",
            "Contador Público": "finanzas",
            "Profesor de Matemáticas": "educacion",
            "Reclutadora de Personal": "rrhh",
            "Diseñador Gráfico": "marketing_diseno",
        }
        for titulo, sector in casos.items():
            self.assertEqual(inferir_sector(titulo), sector, titulo)

    def test_bilingue_customer(self):
        self.assertEqual(inferir_sector("Bilingual Customer Service Representative"),
                         "atencion_cliente")
        self.assertEqual(inferir_sector("Soporte Técnico Bilingüe"),
                         "tecnologia")

    def test_mixed_job_la_funcion_manda(self):
        # Rol mixto: la FUNCION del titulo define el sector, no el adjetivo.
        self.assertEqual(inferir_sector("Abogado de Derecho Digital"), "legal")
        self.assertEqual(inferir_sector("Ingeniero Mecánico Diseñador CAD"),
                         "manufactura")

    def test_desconocido_none(self):
        self.assertIsNone(inferir_sector("Especialista en Estrategia"))
        self.assertIsNone(inferir_sector(""))


class TestAutoEvaluacionPerfil(unittest.TestCase):
    def _perfil_tech(self):
        return SimpleNamespace(busqueda={
            "puestos_objetivo": [
                "Analista de Soporte", "Atención al Cliente Bilingüe",
                "Soporte Técnico Bilingüe", "Bilingual IT Support Technician",
            ]},
            titulos=["Técnico en Soporte"],
            experiencia=[{"puesto": "Soporte Técnico Junior"}],
            skills=["Windows", "Help Desk"])

    def _perfil_abogado(self):
        return SimpleNamespace(busqueda={
            "puestos_objetivo": ["Abogado Corporativo", "Asesor Legal"]},
            titulos=["Licenciatura en Derecho"],
            experiencia=[{"puesto": "Abogado Corporativo"}],
            skills=["Derecho Laboral"])

    def test_tecnico_da_tecnologia(self):
        sectores = inferir_sectores_perfil(self._perfil_tech())
        self.assertIn("tecnologia", sectores)
        self.assertIn("atencion_cliente", sectores)
        self.assertEqual(sectores[0], "tecnologia")

    def test_abogado_da_legal(self):
        sectores = inferir_sectores_perfil(self._perfil_abogado())
        self.assertEqual(sectores[0], "legal")
        self.assertNotIn("tecnologia", sectores)

    def test_manual_pisa_a_la_inferencia(self):
        p = self._perfil_tech()
        p.busqueda["sectores"] = ["legal"]
        self.assertEqual(sectores_objetivo_de(p), ("legal",))

    def test_vacio_cae_en_default(self):
        self.assertEqual(sectores_objetivo_de(SimpleNamespace(
            busqueda={}, titulos=[], experiencia=[], skills=[])),
            SECTORES_OBJETIVO_DEFAULT)


if __name__ == "__main__":
    unittest.main()
