"""Benchmark de modelos de embeddings locales (100% CPU) para JobPulse CR.

Compara el modelo actual (fallback mpnet) contra alternativas abiertas en
tareas REALES del proyecto:
  - titulos-es    : ranking de titulos de CV en español
  - busqueda-es   : consultas de vacantes en español
  - en            : consultas en ingles
  - cross         : emparejamiento cruzado ES<->EN (temas del scraper)
  - evidencia     : skill -> frase del CV (revision IA)

Cada modelo se evalua con el envoltorio que su fabricante recomienda:
  raw   -> texto plano (mpnet, MiniLM, gte)
  e5    -> "query: ..." / "passage: ..." (intfloat e5)
  gemma -> prompts del proyecto (EmbeddingGemma; gated, falla sin HF_TOKEN)

Uso:  python bench_modelos.py
"""
import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if sys.stdout and sys.stdout.encoding and \
        sys.stdout.encoding.lower().replace("-", "") not in ("utf8",):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import numpy as np
from sentence_transformers import SentenceTransformer

# (nombre, envoltorio)
MODELOS = [
    ("paraphrase-multilingual-mpnet-base-v2", "raw"),
    ("intfloat/multilingual-e5-small", "e5"),
    ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "raw"),
    ("Alibaba-NLP/gte-multilingual-base", "raw"),
    ("google/embeddinggemma-300m", "gemma"),
]

# (categoria, consulta, positivo, negativos)
TAREAS = [
    ("titulo-es", "ingeniero de software senior con 8 anios de experiencia",
     "Desarrollador Full Stack Senior en banca digital",
     ["Auxiliar de Bodega en Heredia", "Enfermera turno nocturno"]),
    ("titulo-es", "desarrollador full stack react node ecommerce",
     "Desarrollo de modulos e-commerce con Node.js, Spring Boot y React",
     ["Facturacion y contabilidad", "Moderador de Contenido"]),
    ("titulo-es", "mesa de ayuda bilingue soporte remoto",
     "Soporte Tecnico N1 - Mesa de Ayuda con usuarios externos",
     ["Vendedor Comercial", "Motorizado"]),
    ("titulo-es", "analista qa pruebas automatizadas selenium",
     "QA Automation Tester con Selenium y Cypress",
     ["Auxiliar Administrativo", "Operador de Produccion"]),
    ("busqueda-es", "analista de soporte tecnico entrada de datos",
     "Analista de Soporte TI con digitacion de tickets",
     ["Chofer repartidor", "Enfermera"]),
    ("busqueda-es", "contabilidad facturacion electronica nicaragua",
     "Especialista en Facturacion Electronica y contabilidad",
     ["QA Automation Tester", "Motorizado"]),
    ("en", "senior software engineer microservices aws",
     "Senior Software Engineer - microservices on AWS",
     ["Warehouse Associate", "Payroll Specialist"]),
    ("en", "bilingual help desk technician english fluent",
     "Bilingual IT Support Technician with fluent English",
     ["Bookkeeper", "Content Moderator"]),
    ("en", "ci cd pipelines gitlab docker",
     "Implemented CI/CD pipelines with GitLab and Docker",
     ["Accountant", "Graphic Designer"]),
    ("cross", "ingeniero de software",
     "Software Engineer",
     ["Warehouse Associate", "Enfermera"]),
    ("cross", "soporte tecnico bilingue",
     "Bilingual IT Support Technician",
     ["Sales Representative", "Auxiliar de Bodega"]),
    ("cross", "desarrollador web frontend",
     "Frontend Web Developer",
     ["Bookkeeper", "Motorizado"]),
    ("cross", "especialista en nominas",
     "Payroll Specialist",
     ["Systems Administrator", "Moderador de Contenido"]),
    ("evidencia", "Spring Boot",
     "js, Spring Boot y React",
     ["Profesional Avanzado C1", "Instituto Tecnologico de Costa Rica"]),
    ("evidencia", "Redis",
     "optimizacion de consultas SQL y Redis",
     ["Espanol: Nativo C2", "modulos logisticos e-commerce"]),
    ("evidencia", "GitLab",
     "pipelines de integracion continua (CI/CD) con GitLab y Docker",
     ["autogestion de clientes", "Licenciatura en Ingenieria"]),
]


def wrap(kind: str, text: str, side: str) -> str:
    """Envoltorio recomendado por el fabricante (q=consulta, d=documento)."""
    if kind == "e5":
        return ("query: " if side == "q" else "passage: ") + text
    if kind == "gemma":
        return ("task: search result | query: " if side == "q"
                else "title: none | text: ") + text
    return text


def load_model(nombre: str):
    try:
        return SentenceTransformer(nombre)
    except Exception:
        try:
            return SentenceTransformer(nombre, trust_remote_code=True,
                                       model_kwargs={"trust_remote_code": True})
        except Exception as exc:
            return exc  # fila ERROR


def benchmark(nombre: str, kind: str) -> dict:
    fila = {"modelo": nombre, "kind": kind}
    t0 = time.perf_counter()
    model = load_model(nombre)
    fila["carga_s"] = time.perf_counter() - t0
    if isinstance(model, Exception):
        fila["error"] = str(model).splitlines()[0][:70]
        return fila

    fila["dims"] = model.get_sentence_embedding_dimension()

    qs = [wrap(kind, q, "q") for _, q, _, _ in TAREAS]
    docs, offsets, off = [], [], 0
    for _, _, pos, negs in TAREAS:
        block = [wrap(kind, t, "d") for t in [pos] + negs]
        offsets.append((off, len(block)))
        docs.extend(block)
        off += len(block)

    model.encode(["warmup " * 5] * 2, normalize_embeddings=True)  # cache CUDA/BLAS
    t0 = time.perf_counter()
    Q = model.encode(qs, normalize_embeddings=True)
    D = model.encode(docs, normalize_embeddings=True)
    fila["encode_ms"] = round((time.perf_counter() - t0) * 1000)

    hits = {"total": 0, "n": 0, "cross": [0, 0], "evidencia": [0, 0]}
    margins = []
    for i, (cat, _, _, negs) in enumerate(TAREAS):
        base, count = offsets[i]
        pos_vec = D[base]
        neg_vecs = D[base + 1: base + count]
        s_pos = float(Q[i] @ pos_vec)
        s_negs = (neg_vecs @ Q[i]).tolist() if len(neg_vecs) else []
        hit = s_pos >= (max(s_negs) if s_negs else -1.0)
        hits["total"] += int(hit)
        hits["n"] += 1
        margins.append(s_pos - (max(s_negs) if s_negs else 0.0))
        if cat in ("cross", "evidencia"):
            hits[cat][0] += int(hit)
            hits[cat][1] += 1
    fila["hits"] = hits
    fila["margen"] = round(float(np.mean(margins)), 3)

    del model, Q, D
    gc.collect()
    return fila


def main():
    print("=" * 96)
    print("BENCHMARK DE MODELOS DE EMBEDDINGS (local, CPU) - tareas reales de JobPulse CR")
    print("=" * 96)
    filas = []
    for nombre, kind in MODELOS:
        print(f"\n>> {nombre} ...", flush=True)
        f = benchmark(nombre, kind)
        if "error" in f:
            print(f"   ERROR: {f['error']}")
        else:
            h = f["hits"]
            print(f"   hits {h['total']}/{h['n']} | cross {h['cross'][0]}/{h['cross'][1]}"
                  f" | evidencia {h['evidencia'][0]}/{h['evidencia'][1]}"
                  f" | margen {f['margen']:+.3f} | dims {f['dims']}"
                  f" | carga {f['carga_s']:.1f}s | encode {f['encode_ms']}ms")
        filas.append(f)

    print("\n" + "=" * 96)
    print(f"{'MODELO':<56} {'hits':>6} {'cross':>6} {'evid':>6} {'margen':>7} {'encode':>7}")
    print("-" * 96)
    ok = []
    for f in filas:
        if "error" in f:
            print(f"{f['modelo']:<56} {'ERROR':>6}  ({f['error']})")
            continue
        h = f["hits"]
        ok.append(f)
        print(f"{f['modelo']:<56} "
              f"{h['total']:>3}/{h['n']:<2} {h['cross'][0]:>3}/{h['cross'][1]:<2} "
              f"{h['evidencia'][0]:>3}/{h['evidencia'][1]:<2} "
              f"{f['margen']:>+7.3f} {f['encode_ms']:>5}ms")
    if ok:
        ganador = max(ok, key=lambda f: (f["hits"]["total"], f["margen"], -f["encode_ms"]))
        actual = next((f for f in ok if f["kind"] == "raw"
                       and "mpnet" in f["modelo"]), None)
        print("-" * 96)
        print(f"Ganador: {ganador['modelo']} ({ganador['hits']['total']}/{ganador['hits']['n']} hits)")
        if actual and ganador is not actual:
            d = ganador["hits"]["total"] - actual["hits"]["total"]
            print(f"  vs actual (mpnet): {d:+d} hits")
    print("=" * 96)


if __name__ == "__main__":
    main()
