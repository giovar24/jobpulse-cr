# -*- coding: utf-8 -*-
"""Pruebas de core.text.limpiar_texto (frontera unica de normalizacion).

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v
"""
import unittest

from core.text import html_to_text, limpiar_texto, split_requirements, strip_html


class TestLimpiarTexto(unittest.TestCase):
    def test_none_y_vacio(self):
        self.assertEqual(limpiar_texto(None), "")
        self.assertEqual(limpiar_texto(""), "")
        self.assertEqual(limpiar_texto("   "), "")

    def test_quita_html(self):
        self.assertEqual(limpiar_texto("<p>Hola <b>mundo</b></p>"), "Hola mundo")
        self.assertEqual(
            limpiar_texto('<ul style="x:1"><li>Uno</li><li>Dos</li></ul>'),
            "Uno Dos")

    def test_decodifica_entidades(self):
        self.assertEqual(limpiar_texto("Soporte & Redes &#33;"),
                         "Soporte & Redes !")
        self.assertEqual(limpiar_texto("a < b > c"), "a < b > c")

    def test_quita_script_y_style(self):
        self.assertEqual(
            limpiar_texto("<script>var x=1;</script>Hola<style>p{}</style>"),
            "Hola")

    def test_colapsa_espacios(self):
        self.assertEqual(limpiar_texto("Uno  dos\t\ttres\n\n cuatro"),
                         "Uno dos tres cuatro")

    def test_caracteres_invisibles(self):
        self.assertEqual(limpiar_texto("x\u200by"), "xy")          # zero-width
        self.assertEqual(limpiar_texto("a\u00a0b"), "a b")         # nbsp
        self.assertEqual(limpiar_texto("\ufeffHola"), "Hola")      # BOM
        self.assertEqual(limpiar_texto("a\x00b"), "a b")           # control

    def test_conservar_saltos(self):
        self.assertEqual(limpiar_texto("Uno<br/>Dos", conservar_saltos=True),
                         "Uno\nDos")
        self.assertEqual(limpiar_texto("Uno\n\nDos", conservar_saltos=True),
                         "Uno\nDos")
        # Sin saltos: todo a una sola linea.
        self.assertEqual(limpiar_texto("Uno\nDos"), "Uno Dos")

    def test_idempotente(self):
        sucio = "<p>Hola &  adiós</p>\n<script>x</script>\u200b"
        primera = limpiar_texto(sucio, conservar_saltos=True)
        self.assertEqual(limpiar_texto(primera, conservar_saltos=True), primera)

    def test_strip_html_compatibilidad(self):
        # strip_html era la funcion historica: ahora delega en limpiar_texto.
        self.assertEqual(strip_html("<b>Hola</b>"), "Hola")
        self.assertEqual(strip_html(None), "")


class TestHtmlToText(unittest.TestCase):
    def test_salto_de_linea_estructural(self):
        self.assertEqual(html_to_text("<p>Uno</p><p>Dos</p>"), "Uno\nDos")
        self.assertEqual(html_to_text("a<br>b"), "a\nb")

    def test_split_requirements(self):
        desc, req = split_requirements(
            "Somos una empresa grande e innovadora en tecnología.\n"
            "Requisitos: inglés avanzado")
        self.assertTrue(desc.startswith("Somos una empresa"))
        self.assertIn("inglés avanzado", req)


if __name__ == "__main__":
    unittest.main()
