# -*- coding: utf-8 -*-
"""Pruebas del embudo: exclusion permanente, barrera de ingesta y veredictos.

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v

Sin red ni Ollama: todo con la IA local ya aplicada (gate_score almacenado).
"""
import os
import tempfile
import unittest

import aiosqlite

from core.database import JobDatabase
from core.exclusiones import (SCORE_PISO_DEFAULT, criterio_exclusion,
                              clave_exclusion, excluir_y_purgar,
                              filtrar_offers)
from providers.base import JobOffer


def _offer(job_id, **kw):
    base = dict(source="indeed", search_term="soporte", job_id=job_id,
                title="Técnico de Soporte", company="Acme",
                location="Heredia, Costa Rica", link=f"http://x/{job_id}",
                description="Atender tickets", requirements="", salary="",
                gate_score=0.30, gate_razon="")
    base.update(kw)
    return JobOffer(**base)


class TestCriterioExclusion(unittest.TestCase):
    OBJ = ("tecnologia", "atencion_cliente")

    def test_dominio_nunca_se_excluye(self):
        self.assertIsNone(criterio_exclusion(
            "Technical Support Specialist", 0.05, "tecnologia", self.OBJ))

    def test_sin_score_beneficio_de_la_duda(self):
        self.assertIsNone(criterio_exclusion("Cocinero B", None, None, self.OBJ))

    def test_junk_profundo_se_excluye(self):
        motivo = criterio_exclusion("Cocinero B", 0.10, "alimentos", self.OBJ)
        self.assertIsNotNone(motivo)
        self.assertIn("score 0.10", motivo)

    def test_score_sobre_el_piso_no_se_excluye(self):
        self.assertIsNone(criterio_exclusion(
            "Project Manager", SCORE_PISO_DEFAULT + 0.01, None, self.OBJ))

    def test_regla_de_los_mixtos_el_sector_salva(self):
        # Perfil LEGAL: "abogado de derecho digital" es candidato aunque el
        # titulo diga "digital" y el score del embedding sea bajo.
        self.assertIsNone(criterio_exclusion(
            "Abogado de Derecho Digital", 0.10, "legal", ("legal",)))
        # El mismo puesto con perfil TECNOLOGICO: no es candidato.
        self.assertIsNotNone(criterio_exclusion(
            "Abogado de Derecho Digital", 0.10, "legal", self.OBJ))

    def test_sector_nunca_mata_solo(self):
        # Sector ajeno PERO score alto: sobrevive (lo decide el gate/IA).
        self.assertIsNone(criterio_exclusion(
            "Consultor SAP", 0.30, "finanzas", self.OBJ))


class TestClaveYBarrera(unittest.TestCase):
    def test_clave_normaliza(self):
        # Accentos, mayusculas y ORDEN insensitive; la puntuacion de la
        # empresa tokeniza igual ("Concentrix S.A." == "concentrix s.a.").
        c1 = clave_exclusion("Concentrix S.A.", "Técnico Medio", "Heredia, CR")
        c2 = clave_exclusion("concentrix s.a.", "tecnico medio", "heredia, cr")
        c3 = clave_exclusion("Concentrix S.A.", "Medio Técnico", "CR, Heredia")
        self.assertEqual(c1, c2)
        self.assertEqual(c1, c3)

    def test_clave_sin_ubicacion_none(self):
        self.assertIsNone(clave_exclusion("Acme", "Cocinero", ""))

    def test_barrera_por_job_id_y_clave(self):
        clave = clave_exclusion("Acme", "Cocinero B", "San José")
        offers = [
            _offer("a1"),                                   # limpia
            _offer("b1"),                                   # job_id excluido
            _offer("c1", title="Cocinero B", location="San José"),  # clave excluida
        ]
        vivos, fuera = filtrar_offers(offers, {clave}, {"b1"})
        self.assertEqual(fuera, 2)
        self.assertEqual([o.job_id for o in vivos], ["a1"])


class TestExcluirYPurgar(unittest.IsolatedAsyncioTestCase):
    async def _leer_raw(self, path):
        async with aiosqlite.connect(path) as con:
            con.row_factory = aiosqlite.Row
            return {r["job_id"]: dict(r) for r in await (await con.execute(
                "SELECT * FROM jobs_raw")).fetchall()}

    async def test_embudo_completo_idempotente(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = JobDatabase(path)
            await db.init_db()
            # staging (jobs_raw) = lo que llega del scraping; good1 ademas
            # paso el gate -> tambien esta en la curada.
            lote = [
                _offer("junk1", title="Cocinero B", company="Restaurante X",
                       gate_score=0.10),
                _offer("good1", gate_score=0.30),
                _offer("domlow1", title="Analista de Datos",
                       gate_score=0.10),   # del dominio: nunca excluida
                _offer("near1", title="Project Manager",
                       gate_score=0.25),   # sobre el piso: no excluida
            ]
            await db.save_jobs(lote, tabla="jobs_raw")
            await db.save_jobs([lote[1]])

            res = await excluir_y_purgar(db)          # profile=None -> default
            self.assertEqual(res["excluidas"], 1)    # solo junk1
            self.assertEqual(res["borradas"], 1)     # su fila del crudo

            filas = await self._leer_raw(path)
            self.assertNotIn("junk1", filas)
            self.assertIn("good1", filas)
            self.assertIn("domlow1", filas)
            self.assertIn("near1", filas)

            # Idempotente: segunda pasada no encuentra nada nuevo.
            res2 = await excluir_y_purgar(db)
            self.assertEqual(res2["excluidas"], 0)

            # No resucita: la barrera detecta un REPOST con job_id NUEVO
            # pero la misma clave (empresa+titulo+ubicacion).
            clave = clave_exclusion("Restaurante X", "Cocinero B",
                                    "Heredia, Costa Rica")
            claves, ids = await db.exclusiones_conocidas()
            self.assertIn(clave, claves)
            repost = _offer("junk1-repost", title="Cocinero B",
                            company="Restaurante X")
            vivos, fuera = filtrar_offers([repost], claves, ids)
            self.assertEqual(fuera, 1)

    async def test_veredictos_usuario_es_final(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = JobDatabase(path)
            await db.init_db()
            await db.save_jobs([_offer("v1")], tabla="jobs_raw")

            n = await db.guardar_veredictos({"v1": {
                "veredicto": "apta", "razon": "maneja redes", "por": "usuario"}})
            self.assertGreaterEqual(n, 1)

            # La IA NO pisa la decision del usuario...
            n2 = await db.guardar_veredictos({"v1": {
                "veredicto": "descartada", "razon": "ia opina otra cosa",
                "por": "ia"}})
            self.assertEqual(n2, 0)

            # ...y la fila conserva el veredicto del usuario.
            filas = await self._leer_raw(path)
            self.assertEqual(filas["v1"]["veredicto"], "apta")
            self.assertEqual(filas["v1"]["veredicto_por"], "usuario")


if __name__ == "__main__":
    unittest.main()
