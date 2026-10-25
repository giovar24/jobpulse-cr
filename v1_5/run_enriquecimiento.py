"""Pase completo de enriquecimiento IA sobre la BD v1.5.

1) Pase CRITICO (city/region/work_mode/date_published) en ciclos hasta que
   deje de avanzar (los 'unknown' irresolubles no se reintentan eternamente).
2) Pase COMPLETO (incluye secundarios: employment_type, salary, skills,
   languages) para los jobs que le falta alguno.

Uso: python run_enriquecimiento.py [--solo-criticos]
"""
import asyncio
import json
import sys
import time

from dotenv import load_dotenv

from jobpulse_v15 import ia_enrichment as iae


async def main() -> None:
    load_dotenv()
    cliente = iae.get_ia_client()
    if not cliente:
        print("ERROR: IA no configurada (.env)")
        sys.exit(1)
    print(f"Servidor IA: {cliente.base_url} | modelo: {cliente.modelo}")

    error = await iae.verificar_servidor_ia(cliente)
    if error:
        print(f"ABORTADO: {error}")
        sys.exit(2)
    print("Preflight OK: el servidor responde.\n")

    t0 = time.time()

    # --- pase critico en ciclos ---
    ciclo = 0
    while True:
        ciclo += 1
        r = await iae.run_ia_enrichment(limit=500, only_critical=True)
        if "error" in r:
            print(f"CRITICO ciclo {ciclo}: ERROR {r['error']}")
            sys.exit(3)
        s = r["stats"]
        print(f"CRITICO ciclo {ciclo}: total={s['total']} enriched={s['enriched']} "
              f"failed={s['failed']} skipped={s['skipped']} "
              f"fills={json.dumps(s['fields_filled'], ensure_ascii=False)}")
        if s["total"] == 0 or s["enriched"] == 0:
            break
        if ciclo >= 5:  # guardia de seguridad
            break

    solo_criticos = "--solo-criticos" in sys.argv
    if not solo_criticos:
        # --- pase completo (incluye secundarios) ---
        r = await iae.run_ia_enrichment(limit=2000, only_critical=False)
        if "error" in r:
            print(f"COMPLETO: ERROR {r['error']}")
            sys.exit(4)
        s = r["stats"]
        print(f"COMPLETO: total={s['total']} enriched={s['enriched']} "
              f"failed={s['failed']} skipped={s['skipped']} "
              f"fills={json.dumps(s['fields_filled'], ensure_ascii=False)}")

    print(f"\nDURACION: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(main())
