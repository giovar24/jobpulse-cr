"""Motor de busqueda semantica 100% local con EmbeddingGemma (300M).

Genera embeddings de las vacantes guardadas en la DB, los cachea en
data/ (solo se vectorizan los empleos nuevos) y permite:
  - Buscar por lenguaje natural con similitud de coseno.
  - Expandir tematicas de busqueda en terminos equvialentes por portal.

Si EmbeddingGemma no esta disponible (modelo gated: requiere HF_TOKEN en .env
para aceptar la licencia Gemma), cae automaticamente a un modelo multilingue
abierto de sentence-transformers para no bloquear el flujo.
"""
import json
import logging
import os
from itertools import zip_longest
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("jobpulse.search")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Prompts recomendados por Google para EmbeddingGemma (retrieval asimetrico).
QUERY_PROMPT = "task: search result | query: {text}"
DOC_PROMPT = "title: none | text: {text}"

MODEL_FALLBACK = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

# Recorte de la descripcion para no saturar el modelo ni ralentizar el encodeo.
MAX_DOC_CHARS = 2400


class SemanticSearchEngine:
    def __init__(self, model_name: Optional[str] = None, cache_name: str = "embeddings_cache"):
        self.configured_name = model_name or os.getenv(
            "EMBEDDING_MODEL", "google/embeddinggemma-300m"
        )
        self.model_name = self.configured_name
        self.cache_path = DATA_DIR / f"{cache_name}.npy"
        self.meta_path = DATA_DIR / f"{cache_name}.json"
        self._model = None
        self.jobs: List[Dict] = []
        self.doc_emb: Optional[np.ndarray] = None

    # ------------------------------------------------------------------ modelo
    @property
    def model(self):
        if self._model is None:
            logger.info("Cargando modelo: %s ...", self.model_name)
            try:
                self._model = self._load_model(self.model_name)
            except Exception as exc:  # gated / sin token / sin internet
                if self.model_name == MODEL_FALLBACK:
                    raise
                logger.warning(
                    "No se pudo cargar %s (%s). Usando fallback: %s",
                    self.model_name, exc, MODEL_FALLBACK,
                )
                self.model_name = MODEL_FALLBACK
                self._model = self._load_model(MODEL_FALLBACK)
            # size del vector, util solo para validar cache
            dim_getter = getattr(self.model, "get_embedding_dimension", None) \
                or getattr(self.model, "get_sentence_embedding_dimension")
            logger.info("Modelo listo (dim=%s).", dim_getter())
        return self._model

    def _load_model(self, name: str):
        token = os.getenv("HF_TOKEN", "")
        if token:
            os.environ.setdefault("HF_TOKEN", token)
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(name)

    # -------------------------------------------------------------- textos doc
    @staticmethod
    def build_doc_text(job: Dict) -> str:
        """Combina los campos de la oferta en un solo documento descriptivo."""
        desc = (job.get("description") or "")[:MAX_DOC_CHARS]
        parts = [
            f"Puesto: {job.get('title') or ''}",
            f"Empresa: {job.get('company') or ''}",
            f"Ubicacion: {job.get('location') or ''}",
            f"Requisitos: {job.get('requirements') or ''}",
            f"Descripcion: {desc}",
        ]
        return DOC_PROMPT.format(text=" | ".join(p for p in parts if " " in p))

    # -------------------------------------------------------------------- cache
    def _read_cache(self):
        if self.cache_path.exists() and self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
                valid = meta.get("model") == self.model_name or (
                    # Config sin HF_TOKEN: el fallback resuelto en la escritura sigue siendo valido.
                    meta.get("configured") == self.model_name
                    and meta.get("model") == MODEL_FALLBACK
                    and not os.getenv("HF_TOKEN")
                )
                if valid:
                    emb = np.load(self.cache_path)
                    ids = list(meta.get("ids", []))
                    if len(ids) == emb.shape[0]:
                        return emb, ids
            except Exception as exc:
                logger.warning("Cache de embeddings corrupto, se regenera: %s", exc)
        return None, []

    def _write_cache(self, emb: np.ndarray, ids: List[str]):
        DATA_DIR.mkdir(exist_ok=True)
        np.save(self.cache_path, emb)
        self.meta_path.write_text(
            json.dumps({'model': self.model_name, 'configured': self.configured_name,
                        'ids': ids, 'dim': int(emb.shape[1])},
                       ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ indexar
    def load(self, jobs: List[Dict]):
        """Indexa las vacantes reutilizando el cache; solo vectoriza las nuevas."""
        if not jobs:
            logger.info("No hay vacantes para indexar.")
            self.jobs, self.doc_emb = [], None
            return

        emb_cache, ids = self._read_cache()
        id_to_row = {jid: i for i, jid in enumerate(ids)} if emb_cache is not None else {}
        id_to_job = {j["job_id"]: j for j in jobs if j.get("job_id")}

        # Reordena cache / jobs para alinearlos
        common = [jid for jid in id_to_job if jid in id_to_row]
        new_ids = [jid for jid in id_to_job if jid not in id_to_row]

        self.jobs = [id_to_job[jid] for jid in common]
        if common:
            self.doc_emb = emb_cache[[id_to_row[j] for j in common]]
        else:
            self.doc_emb = None

        if new_ids:
            logger.info("Generando embeddings para %d nuevas vacantes...", len(new_ids))
            texts = [self.build_doc_text(id_to_job[j]) for j in new_ids]
            new_emb = self.model.encode(
                texts, batch_size=16, show_progress_bar=len(new_ids) > 20,
                normalize_embeddings=True,
            )
            self.jobs = self.jobs + [id_to_job[j] for j in new_ids]
            self.doc_emb = np.vstack([self.doc_emb, new_emb]) if self.doc_emb is not None else new_emb
            # El cache se reescribe con la UNION completa (cache previo + nuevos),
            # nunca con el subconjunto actual: filtrar por fuente no debe encogerlo.
            full_ids = list(ids) + new_ids
            base = emb_cache if emb_cache is not None and emb_cache.shape[1] == new_emb.shape[1] else None
            full_emb = np.vstack([base, new_emb]) if base is not None else new_emb
            if base is None:
                # Dimension cambio o cache corrupto: el union se apoya solo en lo nuevo
                # del conjunto actual; los ids viejos quedan fuera y se provisan en la
                # proxima corrida sin filtros.
                full_ids = new_ids
            self._write_cache(full_emb, full_ids)
        elif self.doc_emb is None:
            logger.info("Cache vacio: sin vacantes para indexar.")
            return
        else:
            logger.info("Cache al dia: %d vacantes en el indice para esta consulta.", len(self.jobs))

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        """Busca por lenguaje natural y devuelve las top_k ofertas ordenadas."""
        if not self.jobs or self.doc_emb is None or not query.strip():
            return []
        q = self.model.encode(
            [QUERY_PROMPT.format(text=query)], normalize_embeddings=True
        )[0]
        sims = self.doc_emb @ q  # coseno (vectores ya normalizados)
        order = np.argsort(sims)[::-1][: int(top_k)]
        out = []
        for i in order:
            job = dict(self.jobs[int(i)])
            job["score"] = float(sims[int(i)])
            out.append(job)
        return out

    def search_profile(self, profile, top_k: int = 10) -> List[Dict]:
        """Ranking de mejores opciones para un perfil de candidato.

        Cada seccion del perfil (titulo, skills, tema) genera varias consultas;
        el score de la seccion es el promedio de sus consultas, y el score del
        empleo es la suma ponderada de secciones (weights del propio perfil).
        """
        sections = profile.sections()
        if not sections or not self.jobs or self.doc_emb is None:
            return []

        queries: List[str] = []
        weights: List[float] = []
        spans: List[tuple] = []
        for name, peso, items in sections:
            start = len(queries)
            queries.extend(items)
            weights.append(peso)
            spans.append((name, start, len(queries)))

        Q = self.model.encode(
            [QUERY_PROMPT.format(text=q) for q in queries], normalize_embeddings=True
        )
        sims = self.doc_emb @ Q.T  # (n_ofertas, n_consultas)

        total_w = sum(weights) or 1.0
        out = []
        for i, row in enumerate(sims):
            breakdown, score = {}, 0.0
            for (name, a, b), peso in zip(spans, weights):
                s = float(row[a:b].mean())
                breakdown[name] = s
                score += peso * s
            score /= total_w
            job = dict(self.jobs[i])
            job["score"] = score
            job["secciones"] = breakdown
            out.append(job)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[: int(top_k)]

    # ------------------------------------------------- expansion de terminos
    VOCABULARY_ES = [
        "Soporte Tecnico", "Mesa de Ayuda", "Soporte Tecnico Bilingue",
        "Analista de Soporte", "Auxiliar Administrativo", "Agente de Call Center",
        "Atencion al Cliente Bilingue", "Digitador Data Entry",
        "Analista de Datos", "Desarrollador Web", "Desarrollador Backend",
        "Desarrollador Frontend", "QA Tester", "Programador Junior",
        "Analista de Sistemas", "Soporte de Redes", "Administrador de Sistemas",
        "Analista de Ciberseguridad", "Facturacion y Contabilidad",
        "Asistente Virtual", "Vendedor Comercial", "Moderador de Contenido",
        "Disenador Grafico", "Auxiliar de Bodega", "Motorizado",
        "Especialista en Nomina", "Analista de Nomina",
        "Tecnico de Mantenimiento", "Enfermera", "Operador de Produccion",
        "Ingeniero de Software", "Desarrollador Full Stack",
        "Ingeniero DevOps", "Arquitecto de Software",
    ]
    VOCABULARY_EN = [
        "IT Support", "Help Desk", "Bilingual IT Support Technician",
        "Technical Support Analyst", "Data Entry Clerk", "Data Analyst",
        "Web Developer", "Backend Developer", "Frontend Developer",
        "QA Automation Tester", "Junior Software Engineer",
        "Software Engineer", "Full Stack Developer",
        "Systems Administrator", "Network Engineer",
        "Cybersecurity Analyst", "Bilingual Customer Service Representative",
        "Call Center Agent", "Virtual Assistant", "Sales Representative",
        "Content Moderator", "Business Intelligence Analyst",
        "Digital Marketing Specialist", "Accountant", "Bookkeeper",
        "Payroll Specialist", "Graphic Designer", "Warehouse Associate",
        "Warehouse Operator", "Recruiting Coordinator", "Customer Success Manager",
        "IT Operations Specialist", "DevOps Engineer", "Cloud Support Engineer",
        "Support Desk Technician", "Customer Experience Associate",
        "Senior Software Engineer", "Site Reliability Engineer",
    ]

    def rank_terms(self, query: str, candidates: List[str], top_k: int = 3) -> List[str]:
        """Ordena los candidatos por cercania semantica a la tematica dada."""
        if not candidates or not query.strip():
            return []
        cand = self.model.encode(list(dict.fromkeys(candidates)), normalize_embeddings=True)
        q = self.model.encode([QUERY_PROMPT.format(text=query)], normalize_embeddings=True)[0]
        uniq = list(dict.fromkeys(candidates))
        sims = cand @ q
        order = np.argsort(sims)[::-1][: int(top_k)]
        return [uniq[int(i)] for i in order]

    def expand_searches(self, searches: List[Dict], top_n: int = 2,
                        vocab_es: Optional[List[str]] = None,
                        vocab_en: Optional[List[str]] = None) -> List[Dict]:
        """Convierte cada tematica base en variantes semanticas por idioma.

        La tematica original siempre se conserva como primer termino
        (no se pierde cobertura del scraping actual). `vocab_es`/`vocab_en`
        permiten sumar terminos del perfil del candidato al vocabulario base.
        """
        es_vocab = list(dict.fromkeys(self.VOCABULARY_ES + [v for v in (vocab_es or []) if v]))
        en_vocab = list(dict.fromkeys(self.VOCABULARY_EN + [v for v in (vocab_en or []) if v]))

        def with_original(original: str, ranked: List[str]) -> List[str]:
            if not original:
                return ranked[:top_n]
            return ([original] + [t for t in ranked if t != original])[:top_n]

        new = []
        for s in searches:
            es = s["puesto"]
            en = s.get("puesto_en", es)
            es_terms = with_original(es, self.rank_terms(es, es_vocab, top_n))
            en_terms = with_original(en, self.rank_terms(en, en_vocab, top_n))
            seen = set()
            for p_es, p_en in zip_longest(es_terms, en_terms, fillvalue=None):
                p_es = p_es or p_en
                p_en = p_en or p_es
                key = (p_es.lower(), s["ubicacion"])
                if key in seen:
                    continue
                seen.add(key)
                new.append({"puesto": p_es, "puesto_en": p_en, "ubicacion": s["ubicacion"]})
        return new
