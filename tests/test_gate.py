# -*- coding: utf-8 -*-
"""Pruebas de la calibracion por modelo y del veto lexico del gate.

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v
"""
import unittest

from core.gate import (DEFAULT_MIN_SCORE, ESCAPE_SCORE, _CALIB_POR_MODELO,
                       calibracion, titulo_en_dominio)


class TestCalibracionPorModelo(unittest.TestCase):
    def test_embeddinggemma(self):
        self.assertEqual(
            calibracion("google/embeddinggemma-300m"), (0.26, 0.32))

    def test_mpnet_fallback(self):
        self.assertEqual(
            calibracion("sentence-transformers/paraphrase-multilingual-mpnet-base-v2"),
            (0.30, 0.45))

    def test_modelo_desconocido_cae_en_default(self):
        self.assertEqual(calibracion("un/modelo-nuevo"),
                         (DEFAULT_MIN_SCORE, ESCAPE_SCORE))
        self.assertEqual(calibracion(""), (DEFAULT_MIN_SCORE, ESCAPE_SCORE))
        self.assertEqual(calibracion(None), (DEFAULT_MIN_SCORE, ESCAPE_SCORE))

    def test_constantes_son_las_de_gemma(self):
        """Los defaults del modulo son los del modelo activo (EmbeddingGemma)."""
        self.assertEqual((DEFAULT_MIN_SCORE, ESCAPE_SCORE),
                         _CALIB_POR_MODELO["google/embeddinggemma-300m"])


class TestVetoLexico(unittest.TestCase):
    def test_tokens_nuevos_por_calibracion(self):
        # Hallazgos reales de la muestra estratificada (2026-10-22):
        # titulos de dominio que el veto antiguo dejaba caer.
        self.assertTrue(titulo_en_dominio("Client Services Representative - Full Time"))
        self.assertTrue(titulo_en_dominio("Administración funcional de plataformas ITSM"))
        self.assertTrue(titulo_en_dominio("Moderador de Contenido"))
        self.assertTrue(titulo_en_dominio("Content Moderator"))

    def test_dominio_clasico(self):
        self.assertTrue(titulo_en_dominio("Técnico de Soporte Técnico Bilingüe"))
        self.assertTrue(titulo_en_dominio("Help Desk Analyst"))
        self.assertTrue(titulo_en_dominio("IT Support Specialist"))
        self.assertTrue(titulo_en_dominio("Agente de Servicio al Cliente"))

    def test_no_dominio(self):
        self.assertFalse(titulo_en_dominio("Mecánico Automotriz"))
        self.assertFalse(titulo_en_dominio("Ingeniero Mecánico"))
        self.assertFalse(titulo_en_dominio("Operario de Manufactura II"))
        self.assertFalse(titulo_en_dominio("SAP SD Consultant"))
        self.assertFalse(titulo_en_dominio("Executive Assistant / Asistente de Gerencia"))
        self.assertFalse(titulo_en_dominio("Sales Representative"))


if __name__ == "__main__":
    unittest.main()
