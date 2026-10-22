# -*- coding: utf-8 -*-
"""Pruebas de JobDatabase: limpieza en la frontera de save_jobs y dup_of.

Correr desde la raiz del proyecto:
    python -m unittest discover -s tests -v

No requiere Ollama ni red: solo aiosqlite sobre una base temporal.
"""
import os
import tempfile
import unittest

import aiosqlite

from core.database import JobDatabase
from providers.base import JobOffer


def _offer(job_id, **kw):
    base = dict(source="indeed", search_term="soporte", job_id=job_id,
                title="Técnico de Soporte", company="Acme & Co",
                location="Heredia, Costa Rica", link=f"http://x/{job_id}",
                description="<ul><li>Atender  tickets</li></ul>\n\nSalario a",
                requirements="", salary="")
    base.update(kw)
    return JobOffer(**base)


class TestSaveYMarcar(unittest.IsolatedAsyncioTestCase):
    async def _leer(self, path, tabla="jobs"):
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            filas = [dict(r) for r in await (await db.execute(
                f"SELECT * FROM {tabla}")).fetchall()]
        return {f["job_id"]: f for f in filas}

    async def test_save_limpia_en_la_frontera(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = JobDatabase(path)
            await db.init_db()
            await db.save_jobs([_offer("a1")])

            filas = await self._leer(path)
            fila = filas["a1"]
            # title sin HTML ni entidades, espacios colapsados
            self.assertEqual(fila["title"], "Técnico de Soporte")
            self.assertEqual(fila["company"], "Acme & Co")
            # description conserva saltos pero sin <ul>/<li>
            self.assertEqual(fila["description"], "Atender tickets\nSalario a")

    async def test_save_no_guarda_filas_sin_titulo_o_link(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = JobDatabase(path)
            await db.init_db()
            await db.save_jobs([_offer("x1", title=""),
                                _offer("x2", link="")])
            filas = await self._leer(path)
            self.assertEqual(len(filas), 0)

    async def test_marca_duplicados_exactos_con_dup_of(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = JobDatabase(path)
            await db.init_db()
            # Duplicado exacto (misma empresa+titulo+ubicacion normalizados).
            await db.save_jobs([
                _offer("a1"), _offer("b1", link="http://x/b"),
                _offer("c1", title="Otro puesto totalmente distinto"),
            ])

            marcados = await db.marcar_duplicados_exactos(("jobs",))
            self.assertEqual(marcados, 1)

            filas = await self._leer(path)
            self.assertIsNone(filas["c1"]["dup_of"])
            canonico, copia = filas["a1"], filas["b1"]
            self.assertTrue((canonico["dup_of"] is None
                            and copia["dup_of"] == "a1")
                           or (copia["dup_of"] is None
                               and canonico["dup_of"] == "b1"))

            # Idempotente: re-marcar no duplica ni cambia el resultado.
            marcados2 = await db.marcar_duplicados_exactos(("jobs",))
            filas2 = await self._leer(path)
            self.assertEqual(marcados2, 1)
            self.assertEqual(
                {k: v["dup_of"] for k, v in filas.items()},
                {k: v["dup_of"] for k, v in filas2.items()})

    async def test_sin_ubicacion_no_se_marca(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            db = JobDatabase(path)
            await db.init_db()
            await db.save_jobs([
                _offer("a1", location=""),
                _offer("b1", location="", link="http://x/b"),
            ])
            marcados = await db.marcar_duplicados_exactos(("jobs",))
            self.assertEqual(marcados, 0)


if __name__ == "__main__":
    unittest.main()
