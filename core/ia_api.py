"""Cliente de IA externa (API compatible con OpenAI) para JobPulse CR.

La IA local (embeddings, `core/search_engine.py` + `core/gate.py`) es barata
pero debil para separar matices. Este modulo agrega una segunda opinion con un
LLM fuerte, SIEMPRE a demanda (`main.py --ia` / `--ia-cv`): nunca corre dentro
de una corrida de scraping.

Proveedores gratis con preset (misma interfaz /chat/completions):
  - google : Google AI Studio (Gemini), free tier.
  - nvidia : NVIDIA NIM, free tier con creditos.
  - openai : OpenAI (por si algun dia se paga).
  - custom : cualquier endpoint compatible (OpenRouter, Groq, Ollama...).

Control de cuota (free tier):
  - Cache por hash: una vacante o un CV no se vuelve a mandar si nada cambio.
  - Lotes de `IA_LOTE` vacantes por request (default 8).
  - Tope de vacantes por ejecucion: `IA_MAX_VACANTES` (default 60).
  - Tope de requests por ejecucion: `IA_MAX_LLAMADAS` (default 12).
  - Ritmo: `IA_RPM` requests por minuto (default 8).
  - 429/5xx: espera y 1 reintento; si sigue, corta con mensaje claro y
    conserva lo ya evaluado.

Las evaluaciones NO se tiran: `ia_evals` (append-only en `core/database.py`)
guarda cada respuesta con modelo, hash y tokens, para calibrar la IA local
(`core/ia_calibracion.py`) y, mas adelante, destilar un reranker local.
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("jobpulse.ia")

# base_url + modelo por defecto de cada proveedor (endpoint OpenAI-compatible).
PRESETS = {
    "google": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "modelo": "gemini-2.5-flash",
        "key_env": ("IA_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    },
    "nvidia": {
        "base": "https://integrate.api.nvidia.com/v1",
        "modelo": "meta/llama-3.3-70b-instruct",
        "key_env": ("IA_API_KEY", "NVIDIA_API_KEY"),
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "modelo": "gpt-4o-mini",
        "key_env": ("IA_API_KEY", "OPENAI_API_KEY"),
    },
}

DEFAULT_IA_MIN = 70.0     # score 0-100 a partir del cual "aplica"
MAX_RAZON = 320
MAX_DESC = 1800
# Valores que entiende core/ats.py (_EDU_ORDER); el LLM a veces contesta en
# ingles o con "licenciatura": se normaliza para no perder el puntaje ATS.
_EDU_MAP = {
    "maestria": "maestria", "maestría": "maestria", "master": "maestria",
    "masters": "maestria", "mba": "maestria",
    "universitario": "universitario", "universidad": "universitario",
    "licenciatura": "universitario", "licenciado": "universitario",
    "bachelor": "universitario", "bachelors": "universitario",
    "grado": "universitario",
    "tecnologo": "tecnologo", "tecnólogo": "tecnologo",
    "tecnico": "tecnico", "técnico": "tecnico", "technical": "tecnico",
    "associate": "tecnico",
    "bachiller": "bachiller", "high school": "bachiller",
    "secundaria": "bachiller", "colegio": "bachiller",
}
_SENIORITY_MAP = {
    "junior": "junior", "jr": "junior", "entry": "junior",
    "trainee": "junior", "practicante": "junior",
    "mid": "mid", "medio": "mid", "intermedio": "mid",
    "semi senior": "mid", "semisenior": "mid", "ssr": "mid",
    "senior": "senior", "sr": "senior", "lead": "senior",
    "principal": "senior",
}
# Subir esto invalida la cache de evaluaciones (cambio de prompt = nueva
# pregunta; las respuestas viejas quedan en ia_evals como historial).
PROMPT_VERSION = "v1"


class IAError(Exception):
    """Fallo de la API (red, HTTP, JSON invalido)."""

    def __init__(self, mensaje: str, status: Optional[int] = None):
        super().__init__(mensaje)
        self.status = status


class IACuotaError(IAError):
    """Cuota agotada (429 o tope local de llamadas)."""


def _entero(nombre: str, default: int) -> int:
    try:
        return int(os.getenv(nombre, str(default)))
    except (TypeError, ValueError):
        return default


def _flotante(nombre: str, default: float) -> float:
    try:
        return float(os.getenv(nombre, str(default)))
    except (TypeError, ValueError):
        return default


def _texto_plano(valor, limite: int = 500) -> str:
    return re.sub(r"\s+", " ", str(valor or "")).strip()[:limite]


def perfil_texto(profile) -> str:
    """Resumen compacto del perfil para el prompt (sin datos de contacto)."""
    if profile is None:
        return "(sin perfil cargado)"
    temas = "; ".join(
        f"{t.get('puesto', '')} ({t.get('ubicacion') or 'Costa Rica'})"
        for t in (profile.temas or [])
    )
    idiomas = ", ".join(
        f"{d.get('idioma', '')} {d.get('nivel', '')}".strip()
        for d in (profile.idiomas or []) if isinstance(d, dict)
    )
    partes = [
        f"Nombre: {profile.nombre or '(sin nombre)'}",
        f"Titulos objetivo: {', '.join(profile.titulos or [])}",
        f"Skills: {', '.join(profile.skills or [])}",
        f"Temas de busqueda: {temas}",
        f"Experiencia: {profile.experiencia_anios or 0} anios",
        f"Educacion: {profile.educacion_nivel or 'no indicada'}",
        f"Seniority: {profile.seniority or 'no indicado'}",
        f"Idiomas: {idiomas or 'no indicados'}",
        f"Certificaciones: {', '.join(profile.certificaciones or []) or 'no indicadas'}",
    ]
    return "\n".join(p for p in partes if p.split(": ", 1)[-1])


def perfil_hash(profile) -> str:
    return hashlib.sha256(perfil_texto(profile).encode("utf-8")).hexdigest()[:16]


def _json_de_texto(texto: str) -> dict:
    """Parsea JSON tolerando ```json ... ``` o texto alrededor."""
    t = (texto or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i:j + 1])
        raise


def _retry_after(resp) -> Optional[float]:
    try:
        return float(resp.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None


class IAClient:
    """Cliente sincronico minimo para /chat/completions con JSON."""

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.proveedor = (cfg.get("proveedor") or os.getenv("IA_PROVEEDOR")
                          or self._proveedor_por_clave()).strip().lower()
        preset = PRESETS.get(self.proveedor, PRESETS["google"])
        self.base_url = (cfg.get("base_url") or os.getenv("IA_BASE_URL")
                         or preset["base"]).rstrip("/")
        self.modelo = (cfg.get("modelo") or os.getenv("IA_MODEL")
                       or preset["modelo"])
        self.api_key = (cfg.get("api_key") or os.getenv("IA_API_KEY")
                        or self._clave_entorno(preset["key_env"]))
        self.lote = max(1, _entero("IA_LOTE", 8))
        self.max_vacantes = max(0, _entero("IA_MAX_VACANTES", 60))
        self.max_llamadas = max(1, _entero("IA_MAX_LLAMADAS", 12))
        self.rpm = max(0.0, _flotante("IA_RPM", 8.0))
        self.timeout = max(10.0, _flotante("IA_TIMEOUT", 120.0))
        self.max_desc = max(300, _entero("IA_MAX_DESC", MAX_DESC))
        self.ia_min = _flotante("IA_MIN", DEFAULT_IA_MIN)
        self.llamadas = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._ultima_llamada = 0.0

    # ------------------------------------------------------------- config
    @staticmethod
    def _proveedor_por_clave() -> str:
        """Sin IA_PROVEEDOR: deducirlo de la clave presente (evita mezclar
        una clave de NVIDIA con el endpoint de Google)."""
        if os.getenv("NVIDIA_API_KEY"):
            return "nvidia"
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return "google"
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        return "google"

    @staticmethod
    def _clave_entorno(nombres) -> str:
        for n in nombres:
            v = os.getenv(n)
            if v:
                return v
        return ""

    @property
    def disponible(self) -> bool:
        return bool(self.api_key)

    def motivo_no_disponible(self) -> str:
        return (
            "Falta la clave de la IA. Definí IA_API_KEY en .env "
            "(o GEMINI_API_KEY / NVIDIA_API_KEY) y IA_PROVEEDOR=google|nvidia. "
            "Ver .env.example."
        )

    def resumen_uso(self) -> str:
        return (f"{self.llamadas} llamada(s), "
                f"tokens entrada={self.tokens_in} salida={self.tokens_out}")

    # -------------------------------------------------------------- hashes
    def hash_vacante(self, vacante: dict, p_hash: str) -> str:
        """Huella de lo que la IA vio: si no cambia, no se vuelve a pagar."""
        partes = [
            PROMPT_VERSION, self.proveedor, self.modelo, p_hash,
            str(vacante.get("job_id") or ""), str(vacante.get("title") or ""),
            str(vacante.get("company") or ""),
            str(vacante.get("location") or ""),
            str(vacante.get("salary") or ""),
            (vacante.get("description") or "")[:self.max_desc],
            (vacante.get("requirements") or "")[:800],
        ]
        return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:32]

    def hash_cv(self, cv_texto: str, p_hash: str, puesto: str = "") -> str:
        # OJO: el perfil NO entra en el hash. Tras guardar el perfil propuesto
        # (--ia-guardar) el hash cambiaria y se volveria a pagar la misma
        # consulta; el perfil actual solo es contexto del prompt. Para forzar
        # una reevaluacion esta --ia-forzar.
        partes = [PROMPT_VERSION, self.proveedor, self.modelo,
                  puesto or "", cv_texto or ""]
        return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:32]

    # --------------------------------------------------------------- HTTP
    def _reservar(self):
        if self.llamadas >= self.max_llamadas:
            raise IACuotaError(
                f"tope local de {self.max_llamadas} llamadas por ejecucion "
                f"(IA_MAX_LLAMADAS). Corré de nuevo mañana o subí el tope.")
        self.llamadas += 1

    def _esperar_turno(self):
        if not self.rpm:
            return
        pausa = 60.0 / self.rpm
        delta = time.monotonic() - self._ultima_llamada
        if delta < pausa:
            time.sleep(pausa - delta)
        self._ultima_llamada = time.monotonic()

    def _post(self, payload: dict) -> dict:
        self._reservar()
        self._esperar_turno()
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        intento = 0
        while True:
            intento += 1
            try:
                r = httpx.post(url, json=payload, headers=headers,
                               timeout=self.timeout)
            except httpx.HTTPError as exc:
                if intento <= 2:
                    time.sleep(2 * intento)
                    continue
                raise IAError(f"error de red hablando con la IA: {exc}") from exc

            if r.status_code == 429:
                if intento <= 2:
                    espera = _retry_after(r) or float(3 * intento)
                    espera = min(espera, 60.0)
                    logger.warning("IA: 429 (cuota); espero %.0f s y reintento "
                                   "(%d/2)...", espera, intento)
                    time.sleep(espera)
                    continue
                raise IACuotaError(
                    "cuota de la IA agotada (HTTP 429). Probá más tarde o "
                    "cambiá IA_MODEL/IA_PROVEEDOR (free tier limitado).",
                    status=429)
            if r.status_code >= 500 and intento <= 2:
                time.sleep(2 * intento)
                continue
            if r.status_code >= 400:
                cuerpo = _texto_plano(r.text, 300)
                raise IAError(f"HTTP {r.status_code}: {cuerpo}",
                              status=r.status_code)
            try:
                return r.json()
            except ValueError as exc:
                raise IAError("la IA devolvio algo que no es JSON") from exc

    def _chat_json(self, system: str, user: str,
                   max_tokens: int = 3000) -> Tuple[dict, dict]:
        """Pide una respuesta JSON; si el proveedor rechaza el modo JSON o
        `temperature`, reintenta sin ese campo (mismo prompt)."""
        payload = {
            "model": self.modelo,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            data = self._post(payload)
        except IAError as exc:
            if exc.status != 400:
                raise
            logger.info("IA: el proveedor rechazo response_format; "
                        "reintento sin modo JSON.")
            payload.pop("response_format", None)
            try:
                data = self._post(payload)
            except IAError as exc2:
                if exc2.status != 400:
                    raise
                logger.info("IA: el proveedor rechazo temperature; "
                            "reintento sin ese campo.")
                payload.pop("temperature", None)
                data = self._post(payload)
        uso = data.get("usage") or {}
        self.tokens_in += int(uso.get("prompt_tokens") or 0)
        self.tokens_out += int(uso.get("completion_tokens") or 0)
        try:
            contenido = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise IAError("respuesta de la IA sin 'choices[0].message.content'") from exc
        try:
            return _json_de_texto(contenido), uso
        except (ValueError, TypeError) as exc:
            raise IAError(f"la IA devolvio JSON invalido: {exc}") from exc

    # ------------------------------------------------------------ vacantes
    @staticmethod
    def _system_vacantes() -> str:
        return (
            "Eres un evaluador experto de vacantes para un candidato que vive "
            "en Costa Rica. Puntua de 0 a 100 que tan adecuada es cada vacante "
            "para SU perfil (0 = nada que ver, 100 = encaje perfecto).\n"
            "Criterios: coincidencia con titulos y skills, seniority, idioma "
            "requerido, ubicacion o remoto, salario vs expectativa, y que el "
            "puesto sea real (no comisiones sin base, ventas puerta a puerta, "
            "requisitos imposibles).\n"
            "No inventes datos. Se breve: la razon va en 1-2 frases.\n"
            "Responde SOLO JSON valido, sin markdown, con este esquema:\n"
            '{"resultados":[{"job_id":"...","score":0,"razon":"..."}]}\n'
            "Incluye TODOS los job_id de la lista, sin omitir ninguno."
        )

    def _user_vacantes(self, perfil_txt: str, bloque: List[dict]) -> str:
        vacantes = []
        for v in bloque:
            vacantes.append({
                "job_id": v.get("job_id", ""),
                "titulo": v.get("title", ""),
                "empresa": v.get("company", ""),
                "ubicacion": v.get("location", ""),
                "salario": v.get("salary", ""),
                "publicada": (v.get("date_published") or "")[:10],
                "descripcion": (v.get("description") or "")[:self.max_desc],
                "requisitos": (v.get("requirements") or "")[:700],
            })
        return (
            "PERFIL DEL CANDIDATO\n" + perfil_txt + "\n\n"
            "VACANTES A EVALUAR (JSON)\n"
            + json.dumps(vacantes, ensure_ascii=False)
        )

    def _normalizar_vacantes(self, data, bloque: List[dict]) -> Tuple[Dict, List[dict]]:
        filas = data
        if isinstance(data, dict):
            for clave in ("resultados", "results", "vacantes", "items"):
                if isinstance(data.get(clave), list):
                    filas = data[clave]
                    break
        if not isinstance(filas, list):
            raise IAError("la IA no devolvio una lista de resultados")
        ids = {v.get("job_id") for v in bloque}
        out, faltan = {}, {v.get("job_id"): v for v in bloque}
        for fila in filas:
            if not isinstance(fila, dict):
                continue
            jid = str(fila.get("job_id") or "")
            if jid not in ids:
                continue
            try:
                score = float(fila.get("score"))
            except (TypeError, ValueError):
                continue
            score = max(0.0, min(100.0, score))
            out[jid] = {
                "score": round(score, 1),
                "razon": _texto_plano(fila.get("razon") or fila.get("reason"), MAX_RAZON),
            }
            faltan.pop(jid, None)
        return out, list(faltan.values())

    def _puntuar_bloque(self, bloque: List[dict], perfil_txt: str) -> Tuple[Dict, List[dict]]:
        data, _ = self._chat_json(self._system_vacantes(),
                                  self._user_vacantes(perfil_txt, bloque))
        return self._normalizar_vacantes(data, bloque)

    def puntuar_vacantes(self, vacantes: List[dict], perfil,
                         progreso=None
                         ) -> Tuple[Dict[str, dict], Dict[str, str]]:
        """Evalua vacantes por lotes. Devuelve ({job_id: {score, razon}}, errores)."""
        resultados: Dict[str, dict] = {}
        errores: Dict[str, str] = {}
        if not vacantes:
            return resultados, errores
        pendientes = list(vacantes)
        if self.max_vacantes and len(pendientes) > self.max_vacantes:
            logger.warning("IA: tope de %d vacantes por ejecucion "
                           "(IA_MAX_VACANTES); quedan %d sin evaluar.",
                           self.max_vacantes, len(pendientes) - self.max_vacantes)
            pendientes = pendientes[:self.max_vacantes]
        perfil_txt = perfil_texto(perfil)
        bloques = [pendientes[i:i + self.lote]
                   for i in range(0, len(pendientes), self.lote)]
        for n, bloque in enumerate(bloques, 1):
            try:
                res, faltan = self._puntuar_bloque(bloque, perfil_txt)
            except IACuotaError as exc:
                logger.error("IA: %s", exc)
                errores.update({v["job_id"]: str(exc) for v in bloque})
                break
            except IAError as exc:
                logger.warning("IA: lote %d fallo (%s); reintento por mitades.", n, exc)
                res, err = self._partir(bloque, perfil_txt)
                faltan = []
                errores.update(err)
            resultados.update(res)
            if faltan:
                res2, err2 = self._partir(faltan, perfil_txt)
                resultados.update(res2)
                errores.update(err2)
            if progreso:
                progreso(f"IA: lote {n}/{len(bloques)} · "
                         f"{len(resultados)} evaluadas")
            logger.info("IA: lote %d/%d -> %d/%d evaluadas", n, len(bloques),
                        len(resultados), len(pendientes))
        for v in pendientes:
            jid = v.get("job_id")
            if jid not in resultados and jid not in errores:
                errores[jid] = "sin respuesta de la IA"
        return resultados, errores

    def _partir(self, bloque: List[dict], perfil_txt: str,
                profundidad: int = 0) -> Tuple[Dict, Dict[str, str]]:
        """Reintenta un lote que fallo, partiendolo; 1 vacante = error final."""
        if not bloque:
            return {}, {}
        try:
            res, faltan = self._puntuar_bloque(bloque, perfil_txt)
            err = {}
            if faltan:
                r2, e2 = self._partir(faltan, perfil_txt, profundidad + 1)
                res.update(r2)
                err.update(e2)
            return res, err
        except IACuotaError as exc:
            return {}, {v["job_id"]: str(exc) for v in bloque}
        except IAError as exc:
            if len(bloque) == 1:
                return {}, {bloque[0]["job_id"]: str(exc)}
            if profundidad > 4:
                return {}, {v["job_id"]: str(exc) for v in bloque}
            medio = len(bloque) // 2
            r1, e1 = self._partir(bloque[:medio], perfil_txt, profundidad + 1)
            r2, e2 = self._partir(bloque[medio:], perfil_txt, profundidad + 1)
            r1.update(r2)
            e1.update(e2)
            return r1, e1

    # ----------------------------------------------------------------- CV
    @staticmethod
    def _system_cv() -> str:
        return (
            "Eres un evaluador de CVs y reclutador tecnico para Costa Rica. "
            "Recibis el texto de un CV y el perfil que el sistema ya tiene del "
            "candidato. Tu tarea tiene DOS partes:\n"
            "1) PERFIL: extraer un perfil estructurado para buscar vacantes. "
            "Usá SOLO lo que el CV demuestra: no inventes empresas, fechas, "
            "titulos ni skills. Si un dato no esta, deja el campo vacio.\n"
            "2) EVALUACION: puntuar el CV (0-100) para el objetivo indicado y "
            "dar mejoras concretas. En 'palabras_clave_faltantes' pon terminos "
            "que el mercado pide y el CV no evidencia; NO las agregues al CV "
            "adaptado sin evidencia.\n"
            "El 'cv_adaptado_markdown' debe reescribir SOLO el contenido real "
            "del CV (reordenar, aclarar, cuantificar lo que ya dice), en "
            "espanol, con secciones: Perfil, Experiencia, Habilidades, "
            "Educacion. No agregues experiencia ni logros inventados.\n"
            "Responde SOLO JSON valido, sin markdown alrededor, con este esquema:\n"
            '{"perfil":{"nombre":"","titulos":[],"skills":[],'
            '"temas":[{"puesto":"","puesto_en":"","ubicacion":"Costa Rica"}],'
            '"experiencia_anios":0,'
            '"educacion_nivel":"maestria|universitario|tecnologo|tecnico|bachiller|",'
            '"certificaciones":[],'
            '"idiomas":[{"idioma":"","nivel":""}],'
            '"seniority":"junior|mid|senior|"},'
            '"evaluacion":{"puntaje":0,"resumen":"","fortalezas":[],'
            '"debilidades":[],"palabras_clave_faltantes":[],'
            '"sugerencias":[{"donde":"","cambio":"","por_que":""}]},'
            '"cv_adaptado_markdown":""}'
        )

    def _user_cv(self, cv_texto: str, perfil_txt: str,
                 vacantes: Optional[List[dict]], puesto: str = "") -> str:
        muestra = []
        for v in (vacantes or [])[:12]:
            muestra.append({
                "titulo": v.get("title", ""),
                "empresa": v.get("company", ""),
                "descripcion": (v.get("description") or "")[:600],
            })
        objetivo = puesto or "el que mejor encaje con el CV y el perfil"
        return (
            f"OBJETIVO: adaptar el CV para {objetivo} en Costa Rica.\n\n"
            "PERFIL ACTUAL DEL SISTEMA (referencia, puede estar incompleto)\n"
            + perfil_txt + "\n\n"
            "VACANTES RECIENTES EN EL MERCADO (para detectar palabras clave)\n"
            + json.dumps(muestra, ensure_ascii=False) + "\n\n"
            "CV (texto extraido; puede tener ruido de PDF)\n"
            + (cv_texto or "")[:14000]
        )

    @staticmethod
    def _lista_texto(valor, limite: int = 40) -> List[str]:
        if not isinstance(valor, list):
            return []
        out = []
        for x in valor:
            if isinstance(x, (dict, list)):
                continue
            t = _texto_plano(x, 120)
            if t and t not in out:
                out.append(t)
            if len(out) >= limite:
                break
        return out

    def _normalizar_cv(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise IAError("la IA no devolvio un objeto de evaluacion")
        # Algunos modelos envuelven todo en {"resultado": {...}}.
        for clave in ("resultado", "result", "data"):
            anidado = data.get(clave)
            if isinstance(anidado, dict) and "perfil" in anidado:
                data = anidado
                break
        perfil = data.get("perfil")
        if not isinstance(perfil, dict):
            raise IAError("la respuesta no trae 'perfil'")
        ev = data.get("evaluacion")
        if not isinstance(ev, dict):
            raise IAError("la respuesta no trae 'evaluacion'")

        temas = []
        for t in (perfil.get("temas") or []):
            if isinstance(t, dict) and t.get("puesto"):
                temas.append({
                    "puesto": _texto_plano(t.get("puesto"), 90),
                    "puesto_en": _texto_plano(t.get("puesto_en"), 90)
                                 or _texto_plano(t.get("puesto"), 90),
                    "ubicacion": _texto_plano(t.get("ubicacion"), 90)
                                 or "Costa Rica",
                })
        idiomas = []
        for d in (perfil.get("idiomas") or []):
            if isinstance(d, dict) and d.get("idioma"):
                idiomas.append({
                    "idioma": _texto_plano(d.get("idioma"), 40),
                    "nivel": _texto_plano(d.get("nivel"), 40) or "no especificado",
                })
        try:
            anios = float(perfil.get("experiencia_anios") or 0)
        except (TypeError, ValueError):
            anios = 0.0
        try:
            puntaje = float(ev.get("puntaje"))
        except (TypeError, ValueError):
            puntaje = 0.0
        sugerencias = []
        for s in (ev.get("sugerencias") or []):
            if isinstance(s, dict):
                sugerencias.append({
                    "donde": _texto_plano(s.get("donde"), 80),
                    "cambio": _texto_plano(s.get("cambio"), 300),
                    "por_que": _texto_plano(s.get("por_que"), 240),
                })
        edu = _texto_plano(perfil.get("educacion_nivel"), 30).lower()
        sen = _texto_plano(perfil.get("seniority"), 20).lower()
        return {
            "perfil": {
                "nombre": _texto_plano(perfil.get("nombre"), 80),
                "titulos": self._lista_texto(perfil.get("titulos"), 8),
                "skills": self._lista_texto(perfil.get("skills"), 35),
                "temas": temas[:6],
                "experiencia_anios": min(max(anios, 0.0), 50.0),
                "educacion_nivel": _EDU_MAP.get(edu, ""),
                "certificaciones": self._lista_texto(perfil.get("certificaciones"), 15),
                "idiomas": idiomas[:6],
                "seniority": _SENIORITY_MAP.get(sen, ""),
            },
            "evaluacion": {
                "puntaje": round(max(0.0, min(100.0, puntaje)), 1),
                "resumen": _texto_plano(ev.get("resumen"), 600),
                "fortalezas": self._lista_texto(ev.get("fortalezas"), 10),
                "debilidades": self._lista_texto(ev.get("debilidades"), 10),
                "palabras_clave_faltantes": self._lista_texto(
                    ev.get("palabras_clave_faltantes"), 25),
                "sugerencias": sugerencias[:10],
            },
            "cv_adaptado_markdown": str(data.get("cv_adaptado_markdown") or "").strip(),
        }

    def evaluar_cv(self, cv_texto: str, perfil, vacantes: Optional[List[dict]] = None,
                   puesto: str = "") -> dict:
        data, _ = self._chat_json(
            self._system_cv(),
            self._user_cv(cv_texto, perfil_texto(perfil), vacantes, puesto),
            max_tokens=8000,
        )
        return self._normalizar_cv(data)


def cv_a_markdown(resultado: dict, meta: dict) -> str:
    """Informe legible (y copiable) de la evaluacion de CV."""
    ev = resultado.get("evaluacion") or {}
    perfil = resultado.get("perfil") or {}
    lineas = [
        "# Evaluación del CV con IA",
        "",
        f"- **Fecha:** {meta.get('ts', '')}",
        f"- **Modelo:** {meta.get('proveedor', '')}/{meta.get('modelo', '')}",
        f"- **CV:** {meta.get('cv_path', '')}",
        f"- **Puntaje:** **{ev.get('puntaje', 0):.0f}/100**",
        "",
        "## Resumen",
        ev.get("resumen") or "(sin resumen)",
        "",
        "## Fortalezas",
    ]
    lineas += [f"- {x}" for x in (ev.get("fortalezas") or [])] or ["- (ninguna)"]
    lineas += ["", "## Debilidades"]
    lineas += [f"- {x}" for x in (ev.get("debilidades") or [])] or ["- (ninguna)"]
    lineas += ["", "## Palabras clave que pide el mercado y el CV no evidencia"]
    lineas += [f"- {x}" for x in (ev.get("palabras_clave_faltantes") or [])] or ["- (ninguna)"]
    lineas += ["", "## Sugerencias concretas"]
    for i, s in enumerate(ev.get("sugerencias") or [], 1):
        lineas.append(f"{i}. **{s.get('donde') or 'CV'}** → {s.get('cambio')}")
        if s.get("por_que"):
            lineas.append(f"   - Por qué: {s['por_que']}")
    lineas += ["", "## Perfil propuesto para buscar vacantes"]
    lineas.append(f"- **Títulos:** {', '.join(perfil.get('titulos') or [])}")
    lineas.append(f"- **Skills:** {', '.join(perfil.get('skills') or [])}")
    lineas.append("- **Temas:** " + "; ".join(
        f"{t.get('puesto', '')} ({t.get('ubicacion', '')})"
        for t in (perfil.get("temas") or [])))
    lineas.append(f"- **Experiencia:** {perfil.get('experiencia_anios', 0):.0f} años"
                  f" · **Seniority:** {perfil.get('seniority') or 'no indicado'}")
    lineas += ["", "## CV adaptado (revisar antes de usar)",
               "", resultado.get("cv_adaptado_markdown") or "(sin texto)", ""]
    return "\n".join(lineas)
