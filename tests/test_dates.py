# -*- coding: utf-8 -*-
"""Pruebas de core.dates.fecha_efectiva (fecha de pagina o, si no la hay,
la del scrape como inferida: TODOS los registros procesan por fecha).

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v
"""
import unittest
from datetime import date, timedelta

from core.dates import fecha_efectiva


class TestFechaEfectiva(unittest.TestCase):
    def test_la_fecha_de_la_pagina_manda(self):
        fila = {"date_published": "2026-10-20",
                "created_at": "2026-10-22 10:00:00"}
        self.assertEqual(fecha_efectiva(fila), "2026-10-20")

    def test_sin_fecha_de_pagina_infiere_la_del_scrape(self):
        fila = {"date_published": None,
                "created_at": "2026-10-19 16:23:00"}
        self.assertEqual(fecha_efectiva(fila), "2026-10-19")

    def test_fecha_ilegible_cae_en_el_scrape(self):
        fila = {"date_published": "Publicado hace un rato",
                "created_at": "2026-10-19 16:23:00"}
        self.assertEqual(fecha_efectiva(fila), "2026-10-19")

    def test_relativas_que_parse_date_resuelve_mandan(self):
        hoy = date.today().isoformat()
        ayer = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(fecha_efectiva(
            {"date_published": "posted today", "created_at": "2026-01-01"}),
            hoy)
        self.assertEqual(fecha_efectiva(
            {"date_published": "yesterday", "created_at": "2026-01-01"}),
            ayer)

    def test_sin_nada_devuelve_vacio(self):
        self.assertEqual(fecha_efectiva({}), "")
        self.assertEqual(fecha_efectiva(None), "")

    def test_fila_real_cinde(self):
        """El caso real: CINDE no publica fecha; procesa con el scrape."""
        fila = {"job_id": "cinde-123", "date_published": None,
                "created_at": "2026-10-19 09:13:01"}
        self.assertEqual(fecha_efectiva(fila), "2026-10-19")


if __name__ == "__main__":
    unittest.main()
