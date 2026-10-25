# -*- coding: utf-8 -*-
"""Tests del enriquecimiento IA con cliente simulado (sin servidor real).

Cubren: reintentos por campo, preflight del servidor, pase critico,
pase completo (incluye jobs que solo necesitan campos secundarios) y
persistencia en BD.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobpulse_v15 import ia_enrichment as iae
from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository


RESPUESTAS = {
    iae.LOCATION_PROMPT: {"city": "Heredia", "region": "Heredia"},
    iae.WORK_MODE_PROMPT: {"work_mode": "onsite"},
    iae.DATE_PROMPT: {"date_published": "2026-10-20"},
    iae.EMPLOYMENT_PROMPT: {"employment_type": "Full-time"},
    iae.SALARY_PROMPT: {
        "salary_min": 1000000, "salary_max": 1500000,
        "salary_currency": "CRC", "salary_period": "monthly",
    },
    iae.SKILLS_PROMPT: {"skills": ["SQL", "Active Directory"]},
    iae.LANGUAGES_PROMPT: {"languages": {"ingles": "B2"}},
}


class FakeIAClient:
    """Cliente IA simulado: responde segun el prompt del sistema."""

    base_url = "http://fake.test"
    modelo = "fake-model"

    def __init__(self, fallar=False):
        self.llamadas = 0
        self.fallar = fallar

    async def chat_json(self, messages, schema=None, temperature=0.0,
                        max_tokens=0):
        self.llamadas += 1
        if self.fallar:
            raise RuntimeError("servidor caido")
        system = messages[0]["content"]
        return dict(RESPUESTAS.get(system, {"ok": True}))


class TestExtractField(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_extract_reintenta_3_veces_y_devuelve_none(self):
        cliente = FakeIAClient(fallar=True)
        resultado = self._run(iae._extract_field(
            cliente, iae.WORK_MODE_PROMPT, "texto", iae.WORK_MODE_SCHEMA,
            "work_mode",
        ))
        self.assertIsNone(resultado)
        self.assertEqual(cliente.llamadas, 3)

    def test_extract_devuelve_respuesta_del_esquema(self):
        cliente = FakeIAClient()
        resultado = self._run(iae._extract_field(
            cliente, iae.WORK_MODE_PROMPT, "texto", iae.WORK_MODE_SCHEMA,
            "work_mode",
        ))
        self.assertEqual(resultado, {"work_mode": "onsite"})
        self.assertEqual(cliente.llamadas, 1)


class TestNeedsEnrichment(unittest.TestCase):
    def test_critical(self):
        self.assertTrue(iae.needs_critical_enrichment(
            {"work_mode": "unknown", "date_published": "2026-01-01"}))
        self.assertTrue(iae.needs_critical_enrichment(
            {"work_mode": "onsite", "date_published": "",
             "location_raw": "San Jose", "city": None}))
        self.assertFalse(iae.needs_critical_enrichment(
            {"work_mode": "onsite", "date_published": "2026-01-01",
             "city": "Heredia"}))

    def test_secondary(self):
        self.assertTrue(iae.needs_secondary_enrichment({"skills": None}))
        self.assertTrue(iae.needs_secondary_enrichment(
            {"skills": ["SQL"], "languages": {}, "employment_type": None,
             "salary_raw": "1.000.000/mes", "salary_min": None}))
        self.assertFalse(iae.needs_secondary_enrichment({
            "skills": ["SQL"], "languages": {"ingles": "B2"},
            "employment_type": "Full-time", "salary_min": 1000000,
            "salary_raw": "x",
        }))


class TestPreflight(unittest.TestCase):
    def test_preflight_ok(self):
        error = asyncio.run(iae.verificar_servidor_ia(FakeIAClient()))
        self.assertIsNone(error)

    def test_preflight_falla_con_mensaje(self):
        error = asyncio.run(iae.verificar_servidor_ia(FakeIAClient(fallar=True)))
        self.assertIsNotNone(error)
        self.assertIn("no responde", error)


class TestRunIAEnrichment(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        self.database = Database(self.db_path)
        self.database.initialize()
        self.repo = Repository(self.database)
        self.repo.register_platform("test", "Test", "directos")
        self.repo.register_site(
            "test-site", "test", "Sitio test",
            "https://example.test/jobs", run_groups=("all",),
        )
        self._insert_job()
        self.cliente = FakeIAClient()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert_job(self, **overrides):
        job = {
            "job_id": "T-1",
            "title": "IT Support Specialist",
            "company": "Test Corp",
            "location_raw": "San Jose, Heredia",
            "work_mode": "unknown",
            "date_published": "2026-10-20",
            "description_raw": "Soporte IT con Active Directory y SQL.",
        }
        job.update(overrides)
        self.repo.save_job(
            job, site_id="test-site", source_job_id=job["job_id"],
            source_url="https://example.test/jobs/T-1",
        )

    def _run(self, modo="criticos", limit=50):
        only_critical = modo == "criticos"
        with patch.object(
            iae, "get_ia_client", return_value=self.cliente,
        ):
            return asyncio.run(iae.run_ia_enrichment(
                db_path=self.db_path, limit=limit,
                only_critical=only_critical,
            ))

    def test_pase_critico_enriquece_y_persiste(self):
        resultado = self._run()
        stats = resultado["stats"]
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["enriched"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertGreater(stats["fields_filled"]["work_mode"], 0)
        self.assertEqual(stats["fields_filled"]["city"], 1)

        job = self.repo.get_job("T-1")
        self.assertEqual(job["work_mode"], "onsite")
        self.assertEqual(job["city"], "Heredia")
        self.assertEqual(job["region"], "Heredia")

    def test_pase_critico_salta_job_completo(self):
        # Job ya completo con criticos: no entra al pase critico.
        self.repo.save_job(
            {
                "job_id": "T-2", "title": "Full Job",
                "city": "Heredia", "work_mode": "onsite",
                "date_published": "2026-10-21",
            },
            site_id="test-site", source_job_id="T-2",
            source_url="https://example.test/jobs/T-2",
        )
        resultado = self._run(limit=50)
        stats = resultado["stats"]
        self.assertEqual(stats["total"], 1)  # solo T-1
        self.assertEqual(stats["enriched"], 1)

    def test_pase_completo_incluye_secundarios(self):
        # Job con TODOS los criticos completos, sin skills/emp type.
        self.repo.save_job(
            {
                "job_id": "T-3", "title": "Full Job 2",
                "city": "Heredia", "work_mode": "onsite",
                "date_published": "2026-10-21",
                "description_raw": "Se requiere Python y AWS.",
            },
            site_id="test-site", source_job_id="T-3",
            source_url="https://example.test/jobs/T-3",
        )
        # Pase completo: T-1 (criticos) y T-3 (solo secundarios).
        resultado_full = self._run(modo="completo", limit=50)
        self.assertEqual(resultado_full["stats"]["total"], 2)
        self.assertEqual(resultado_full["stats"]["enriched"], 2)

        job3 = self.repo.get_job("T-3")
        skills3 = job3["skills"]
        if isinstance(skills3, str):
            import json as _json
            skills3 = _json.loads(skills3) if skills3 not in ("", "null") else None
        self.assertEqual(skills3, ["SQL", "Active Directory"])
        self.assertEqual(job3["employment_type"], "Full-time")

        # Despues del pase, el critico no tiene nada pendiente.
        resultado_crit = self._run(limit=50)
        self.assertEqual(resultado_crit["stats"]["total"], 0)

    def test_salary_completo_se_persiste(self):
        # Salary solo se pide si hay salary_raw: min+max+currency+period
        # deben aplicar TODOS (antes se perdian max/currency/period).
        self.repo.save_job(
            {
                "job_id": "T-4", "title": "Job con salario",
                "city": "Heredia", "work_mode": "onsite",
                "date_published": "2026-10-21",
                "salary_raw": "1 000 000 - 1 500 000 CRC/mes",
            },
            site_id="test-site", source_job_id="T-4",
            source_url="https://example.test/jobs/T-4",
        )
        # T-4 tiene los criticos completos: usa el pase completo.
        resultado = self._run(modo="completo", limit=50)
        self.assertEqual(resultado["stats"]["enriched"], 2)  # T-1 y T-4
        self.assertEqual(resultado["stats"]["fields_filled"]["salary"], 1)

        job = self.repo.get_job("T-4")
        self.assertEqual(job["salary_min"], 1000000)
        self.assertEqual(job["salary_max"], 1500000)
        self.assertEqual(job["salary_currency"], "CRC")
        self.assertEqual(job["salary_period"], "monthly")
        # skills/languages NO tocados: deben seguir vacios de verdad
        # (NULL/''), no la cadena basura 'null'.
        self.assertIn(job["skills"], (None, "", "[]"))

    def test_preflight_servidor_apagado_aborta_antes(self):
        self.cliente = FakeIAClient(fallar=True)
        resultado = self._run(limit=50)
        self.assertIn("error", resultado)
        self.assertIn("no responde", resultado["error"])
        # No toco la BD: el job sigue sin city.
        job = self.repo.get_job("T-1")
        self.assertIsNone(job["city"])


if __name__ == "__main__":
    unittest.main()
