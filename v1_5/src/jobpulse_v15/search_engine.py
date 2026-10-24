# -*- coding: utf-8 -*-
"""jobpulse_v15.search_engine — Motor de busqueda semantica con embeddings locales.

Caracteristicas:
- Embeddings locales en CPU (sentence-transformers)
- Cache incremental en disco (data/usuario/embeddings_cache.npy + _meta.json)
- Expansion semantica ES<->EN de terminos de busqueda
- Prompts asimetricos (QUERY_PROMPT / DOC_PROMPT) para retrieval
- Fallback automatico si modelo gated (EmbeddingGemma) -> paraphrase-multilingual-mpnet
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("jobpulse.search")

# Rutas de cache
RAIZ = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = RAIZ / "data" / "usuario"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
EMB_CACHE_FILE = CACHE_DIR / "embeddings_cache.npy"
EMB_META_FILE = CACHE_DIR / "embeddings_meta.json"

# Modelos
MODELO_PRIMARY = "google/embeddinggemma-300m"      # 768 dim, MRL, prompts asimetricos
MODELO_FALLBACK = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"  # 768 dim

# Vocabularios para expansion semantica
VOCAB_ES = [
    "soporte tecnico", "ayuda tecnica", "mesa de ayuda", "help desk", "service desk",
    "atencion al cliente", "servicio al cliente", "customer service", "customer support",
    "soporte bilingue", "bilingual support", "tecnico de soporte", "analista de soporte",
    "tecnico helpdesk", "especialista soporte", "coordinador soporte", "lider soporte",
    "soporte nivel 1", "soporte nivel 2", "soporte n1", "soporte n2", "soporte l1", "soporte l2",
    "administrador sistemas", "sysadmin", "devops", "cloud", "aws", "azure", "gcp",
    "redes", "network", "seguridad", "security", "ciberseguridad", "cybersecurity",
    "bases de datos", "dba", "sql", "oracle", "postgresql", "mysql", "mongodb",
    "desarrollo", "developer", "programador", "software engineer", "fullstack", "backend", "frontend",
    "python", "java", "javascript", "typescript", "c#", ".net", "go", "rust",
    "analista", "analyst", "business analyst", "data analyst", "data scientist",
    "proyecto", "project manager", "scrum master", "product owner", "agile",
    "calidad", "qa", "testing", "automatizacion", "automation", "devsecops",
]

VOCAB_EN = [
    "technical support", "it support", "help desk", "service desk", "helpdesk",
    "customer service", "customer support", "client support",
    "bilingual support", "multilingual support", "support analyst", "support engineer",
    "tier 1 support", "tier 2 support", "level 1 support", "level 2 support", "l1 support", "l2 support",
    "system administrator", "sysadmin", "devops engineer", "cloud engineer", "aws", "azure", "gcp",
    "network engineer", "network administrator", "security analyst", "cybersecurity",
    "database administrator", "dba", "sql", "oracle", "postgresql", "mysql", "mongodb",
    "software developer", "software engineer", "fullstack developer", "backend developer", "frontend developer",
    "python developer", "java developer", "javascript developer", "typescript developer", "c# developer", ".net developer", "go developer", "rust developer",
    "business analyst", "data analyst", "data scientist", "machine learning engineer",
    "project manager", "scrum master", "product owner", "agile coach",
    "quality assurance", "qa engineer", "test automation", "automation engineer", "devsecops",
]


class SemanticSearchEngine:
    """Motor de busqueda semantica con cache incremental y expansion de terminos."""

    def __init__(
        self,
        model_name: str = MODELO_PRIMARY,
        cache_dir: Path = CACHE_DIR,
        device: str = "cpu",
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.device = device
        self._model = None
        self._embeddings = None  # np.ndarray (n, dim)
        self._meta = {}          # job_id -> {"hash": ..., "idx": ...}
        self._ids = []           # lista de job_id alineada con _embeddings
        self._dim = None

    # ---------------------------------------------------------------- modelo
    def _load_model(self):
        """Carga sentence-transformers con fallback automatico."""
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer
        try:
            logger.info("Cargando modelo embeddings: %s", self.model_name)
            self._model = SentenceTransformer(self.model_name, device=self.device)
            logger.info("Modelo cargado OK (dim=%d)", self._model.get_sentence_embedding_dimension())
        except Exception as e:
            if "401" in str(e) or "gated" in str(e).lower():
                logger.warning("Modelo %s gated/401 -> fallback a %s: %s", self.model_name, MODELO_FALLBACK, e)
                self.model_name = MODELO_FALLBACK
                self._model = SentenceTransformer(self.model_name, device=self.device)
                logger.info("Fallback cargado OK (dim=%d)", self._model.get_sentence_embedding_dimension())
            else:
                raise
        self._dim = self._model.get_sentence_embedding_dimension()

    # ---------------------------------------------------------------- cache
    def _load_cache(self) -> bool:
        """Carga cache desde disco. Return True si cache valido y coincide modelo."""
        if not EMB_CACHE_FILE.exists() or not EMB_META_FILE.exists():
            return False
        try:
            meta = json.loads(EMB_META_FILE.read_text(encoding="utf-8"))
            if meta.get("model") != self.model_name:
                logger.info("Cache invalido: modelo distinto (cache=%s, actual=%s)", meta.get("model"), self.model_name)
                return False
            if meta.get("dim") != self._dim:
                logger.info("Cache invalido: dimension distinta")
                return False
            self._embeddings = np.load(EMB_CACHE_FILE)
            self._meta = meta.get("items", {})
            self._ids = [item["job_id"] for item in sorted(self._meta.values(), key=lambda x: x["idx"])]
            logger.info("Cache cargado: %d vectores", len(self._ids))
            return True
        except Exception as e:
            logger.warning("Error cargando cache: %s", e)
            return False

    def _save_cache(self):
        """Guarda cache en disco atomico."""
        try:
            meta = {
                "model": self.model_name,
                "dim": self._dim,
                "items": self._meta,
                "ts": time.time(),
            }
            tmp_npy = EMB_CACHE_FILE.with_suffix(".npy.tmp")
            tmp_meta = EMB_META_FILE.with_suffix(".json.tmp")
            np.save(tmp_npy, self._embeddings)
            tmp_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            tmp_npy.replace(EMB_CACHE_FILE)
            tmp_meta.replace(EMB_META_FILE)
            logger.debug("Cache guardado: %d vectores", len(self._ids))
        except Exception as e:
            logger.warning("Error guardando cache: %s", e)

    # ---------------------------------------------------------------- indexado
    def index_jobs(self, jobs: List[Dict[str, Any]], force_reindex: bool = False) -> int:
        """
        Indexa (o re-indexa) lista de jobs.
        Cada job debe tener: job_id, title, company, location_raw, description_raw, requirements_raw.
        Solo (re)indexa los que falten o hayan cambiado (hash de contenido).
        Retorna numero de jobs indexados nuevo/actualizados.
        """
        self._load_model()
        if not force_reindex:
            self._load_cache()

        new_or_changed = []
        for job in jobs:
            jid = job.get("job_id")
            if not jid:
                continue
            # Hash del contenido vectorizable
            content = self._build_doc_text(job)
            h = hashlib.sha256(content.encode()).hexdigest()[:16]
            meta = self._meta.get(jid)
            if meta is None or meta.get("hash") != h:
                new_or_changed.append((jid, content, h))

        if not new_or_changed:
            logger.info("Index: sin cambios (%d jobs ya indexados)", len(self._ids))
            return 0

        logger.info("Index: calculando embeddings para %d jobs nuevos/actualizados", len(new_or_changed))
        texts = [c for _, c, _ in new_or_changed]
        # Prompt DOC para documento (retrieval asimetrico)
        doc_prompt = self._get_doc_prompt()
        embeddings = self._model.encode(
            [doc_prompt + t for t in texts],
            batch_size=32,
            show_progress_bar=True,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

        # Actualizar arrays
        if self._embeddings is None:
            self._embeddings = embeddings
        else:
            self._embeddings = np.vstack([self._embeddings, embeddings])

        # Actualizar meta
        start_idx = len(self._ids)
        for i, (jid, _, h) in enumerate(new_or_changed):
            idx = start_idx + i
            self._meta[jid] = {"hash": h, "idx": idx}
            self._ids.append(jid)

        self._save_cache()
        logger.info("Index completado: %d totales, %d nuevos/actualizados", len(self._ids), len(new_or_changed))
        return len(new_or_changed)

    def _build_doc_text(self, job: Dict[str, Any]) -> str:
        """Construye texto combinado para embedding del documento (job)."""
        parts = []
        if job.get("title"):
            parts.append(f"Puesto: {job['title']}")
        if job.get("company"):
            parts.append(f"Empresa: {job['company']}")
        if job.get("location_raw"):
            parts.append(f"Ubicacion: {job['location_raw']}")
        if job.get("description_raw"):
            parts.append(f"Descripcion: {job['description_raw'][:3000]}")
        if job.get("requirements_raw"):
            parts.append(f"Requisitos: {job['requirements_raw'][:2000]}")
        return " | ".join(parts)

    def _get_doc_prompt(self) -> str:
        """Prompt para documento (retrieval asimetrico)."""
        if "embeddinggemma" in self.model_name.lower():
            return "task: search result | document: "
        return ""

    def _get_query_prompt(self) -> str:
        """Prompt para query (retrieval asimetrico)."""
        if "embeddinggemma" in self.model_name.lower():
            return "task: search result | query: "
        return ""

    # ---------------------------------------------------------------- busqueda
    def search(
        self,
        query: str,
        top_k: int = 20,
        filter_fn: Optional[callable] = None,
    ) -> List[Tuple[str, float, Dict]]:
        """
        Busca semanticamente. Retorna lista de (job_id, score, job_dict).
        filter_fn(job_dict) -> bool para filtrar antes de rankear.
        """
        self._load_model()
        if self._embeddings is None or len(self._ids) == 0:
            logger.warning("Index vacio")
            return []

        # Embedding de query con prompt QUERY
        q_prompt = self._get_query_prompt()
        q_emb = self._model.encode(
            [q_prompt + query],
            convert_to_numpy=True,
            normalize_embeddings=True,
        )[0]

        # Similitud coseno (dot product ya que estan normalizados)
        scores = self._embeddings @ q_emb  # (n,)
        # Top-k indices
        top_idx = np.argpartition(-scores, min(top_k, len(scores) - 1))[:top_k]
        top_idx = top_idx[np.argsort(-scores[top_idx])]

        results = []
        for idx in top_idx:
            jid = self._ids[idx]
            score = float(scores[idx])
            # job_dict basico para filtro (se completa desde DB en caller)
            job = {"job_id": jid, "_score": score}
            if filter_fn and not filter_fn(job):
                continue
            results.append((jid, score, job))
        return results

    # ---------------------------------------------------------------- expansion
    def expand_terms(
        self,
        base_terms: List[str],
        lang: str = "es",
        top_n: int = 5,
        threshold: float = 0.35,
    ) -> List[str]:
        """
        Expande terminos base con vocabulario semantico.
        Retorna lista: base_terms + variantes cercanas (sin duplicados).
        """
        self._load_model()
        vocab = VOCAB_ES if lang == "es" else VOCAB_EN
        if not vocab:
            return base_terms

        # Embeddings de vocabulario (cache simple)
        vocab_emb = self._model.encode(
            vocab, batch_size=64, convert_to_numpy=True, normalize_embeddings=True
        )

        expanded = set(base_terms)
        for term in base_terms:
            t_emb = self._model.encode([term], convert_to_numpy=True, normalize_embeddings=True)[0]
            sims = vocab_emb @ t_emb
            # Top-N sobre threshold
            top_idx = np.argpartition(-sims, min(top_n, len(sims) - 1))[:top_n]
            top_idx = top_idx[np.argsort(-sims[top_idx])]
            for idx in top_idx:
                if sims[idx] >= threshold:
                    expanded.add(vocab[idx])

        return list(expanded)

    def rank_profile_terms(
        self,
        profile_sections: Dict[str, List[str]],
        lang: str = "es",
    ) -> Dict[str, List[Tuple[str, float]]]:
        """
        Rankea vocabulario contra secciones del perfil (titulos, skills, temas).
        Retorna {seccion: [(termino, score), ...]} ordenado por score desc.
        """
        self._load_model()
        vocab = VOCAB_ES if lang == "es" else VOCAB_EN
        if not vocab:
            return {k: [] for k in profile_sections}

        vocab_emb = self._model.encode(vocab, batch_size=64, convert_to_numpy=True, normalize_embeddings=True)

        results = {}
        for section, terms in profile_sections.items():
            if not terms:
                results[section] = []
                continue
            # Promedio de embeddings de los terminos de la seccion
            term_embs = self._model.encode(terms, batch_size=32, convert_to_numpy=True, normalize_embeddings=True)
            section_emb = np.mean(term_embs, axis=0)
            section_emb = section_emb / (np.linalg.norm(section_emb) + 1e-8)

            sims = vocab_emb @ section_emb
            ranked = sorted(zip(vocab, sims), key=lambda x: -x[1])
            results[section] = [(v, float(s)) for v, s in ranked[:20]]

        return results


def search_profile(
    engine: SemanticSearchEngine,
    profile: Dict[str, Any],
    jobs: List[Dict[str, Any]],
    top_k: int = 50,
    weights: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """
    Ranking completo contra perfil del candidato.

    profile: {"titulos": [...], "skills": [...], "temas": [...], "pesos": {...}}
    jobs: lista de dicts con job_id y campos basicos.

    Retorna jobs ordenados con score combinado y desglose por seccion.
    """
    if not jobs:
        return []

    # Pesos default (se normalizan)
    weights = weights or profile.get("pesos", {"titulos": 1.0, "skills": 1.0, "temas": 1.0})
    total_w = sum(weights.values()) or 1.0
    weights = {k: v / total_w for k, v in weights.items()}

    # 1. Embedding de cada seccion del perfil
    section_embs = {}
    for section in ["titulos", "skills", "temas"]:
        terms = profile.get(section, [])
        if terms:
            embs = engine._model.encode(terms, batch_size=32, convert_to_numpy=True, normalize_embeddings=True)
            section_embs[section] = np.mean(embs, axis=0)
            section_embs[section] = section_embs[section] / (np.linalg.norm(section_embs[section]) + 1e-8)

    # 2. Embedding de cada job
    job_texts = [engine._build_doc_text(j) for j in jobs]
    doc_prompt = engine._get_doc_prompt()
    job_embs = engine._model.encode(
        [doc_prompt + t for t in job_texts],
        batch_size=32,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )

    # 3. Scores por seccion + combinado
    for i, job in enumerate(jobs):
        scores = {}
        for section, w in weights.items():
            if section in section_embs:
                scores[section] = float(job_embs[i] @ section_embs[section]) * w
            else:
                scores[section] = 0.0
        job["semantic_scores"] = scores
        job["semantic_combined"] = sum(scores.values())

    # 4. Ordenar por combinado desc
    jobs.sort(key=lambda j: -j.get("semantic_combined", 0))
    return jobs[:top_k]