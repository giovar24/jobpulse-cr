# -*- coding: utf-8 -*-
"""Pruebas de las claves conservadoras para duplicados exactos (core.dedupe).

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v
"""
import unittest

from core.dedupe import (clave_duplicado_exacto, norm_compania_exacta,
                         norm_lugar_exacto, norm_titulo_exacto)


class TestClavesExactas(unittest.TestCase):
    def test_normaliza_mayusculas_tildes_y_puntuacion(self):
        self.assertEqual(norm_titulo_exacto("Técnico Medio en Informática"),
                         norm_titulo_exacto("tecnico medio en informatica"))
        self.assertEqual(norm_compania_exacta("Concentrix S.A."),
                         norm_compania_exacta("concentrix s a"))
        self.assertEqual(norm_lugar_exacto("San José, Costa Rica"),
                         norm_lugar_exacto("san jose, costa rica"))

    def test_orden_insensible(self):
        # Los agregadores reordenan: "Engineer, Senior" vs "Senior Engineer".
        self.assertEqual(norm_titulo_exacto("Ingeniero de Soporte"),
                         norm_titulo_exacto("Soporte de Ingeniero"))

    def test_no_confunde_niveles(self):
        # La clave exacta NO quita numeros romanos: I y II son puestos distintos.
        self.assertNotEqual(norm_titulo_exacto("Support Engineer I"),
                            norm_titulo_exacto("Support Engineer II"))

    def test_clave_requiere_empresa_titulo_y_ubicacion(self):
        self.assertIsNone(clave_duplicado_exacto("Acme", "Soporte", ""))
        self.assertIsNone(clave_duplicado_exacto("", "Soporte", "Heredia"))
        self.assertIsNone(clave_duplicado_exacto("Acme", "", "Heredia"))
        clave = clave_duplicado_exacto("Acme", "Soporte", "Heredia")
        self.assertIsNotNone(clave)

    def test_misma_clave_distinta_redaccion(self):
        a = clave_duplicado_exacto("Concentrix + Webhelp", "Bilingual CSR",
                                   "Heredia, Costa Rica")
        b = clave_duplicado_exacto("webhelp + concentrix", "bilingual csr",
                                   "Heredia, CR")
        # Distinta redaccion de empresa NO es igual (esa fusion es la fase
        # difusa, no la exacta): la clave exacta debe ser conservadora.
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
