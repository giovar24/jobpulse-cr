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
        "modelo": "openai/gpt-oss-20b",
        "key_env": ("IA_API_KEY", "NVIDIA_API_KEY"),
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "modelo": "gpt-4o-mini",
        "key_env": ("IA_API_KEY", "OPENAI_API_KEY"),
    },
    # Ollama Cloud (ollama.com): endpoint OpenAI-compatible + modelos grandes
    # en la nube. La clave sale de OLLAMA_API_KEY (o IA_API_KEY).
    "ollama": {
        "base": "https://ollama.com/v1",
        "modelo": "gemma4:31b",
        "key_env": ("IA_API_KEY", "OLLAMA_API_KEY"),
    },
}

# Nombres visibles en la UI (la clave interna sigue siendo el id del preset).
PROVEEDOR_NOMBRES = {
    "google": "Google (Gemini)",
    "nvidia": "NVIDIA NIM",
    "openai": "OpenAI",
    "ollama": "Ollama Cloud",
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
# v3: perfil y CV adaptado con esquema Harvard/ATS (contacto, educacion,
# experiencia y proyectos estructurados).
PROMPT_VERSION = "v3"


class IAError(Exception):
    """Fallo de la API (red, HTTP, JSON invalido)."""

    def __init__(self, mensaje: str, status: Optional[int] = None):
        super().__init__(mensaje)
        self.status = status


class IACuotaError(IAError):
    """Cuota agotada (429 o tope local de llamadas)."""


class IAModeloError(IAError):
    """El modelo elegido no existe o no esta habilitado para la cuenta.

    No se reintenta partiendo el lote: es un problema de configuracion, no
    del prompt. El usuario debe elegir otro modelo en el selector o con
    `--ia-modelo`.
    """


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
    exp_resumen = []
    for e in (getattr(profile, "experiencia", None) or [])[:3]:
        if isinstance(e, dict):
            emp = e.get("empresa", "")
            pue = e.get("puesto", "")
            if emp or pue:
                exp_resumen.append(f"{pue} @ {emp}".strip(" @"))
    edu_resumen = []
    for e in (getattr(profile, "educacion", None) or [])[:2]:
        if isinstance(e, dict):
            inst = e.get("institucion", "")
            tit = e.get("titulo", "")
            if inst or tit:
                edu_resumen.append(f"{tit} — {inst}".strip(" —"))
    partes = [
        f"Nombre: {profile.nombre or '(sin nombre)'}",
        f"Resumen: {getattr(profile, 'resumen_profesional', '') or 'no indicado'}",
        f"Titulos objetivo: {', '.join(profile.titulos or [])}",
        f"Skills: {', '.join(profile.skills or [])}",
        f"Temas de busqueda: {temas}",
        f"Experiencia resumida: {'; '.join(exp_resumen) or 'no indicada'}",
        f"Educacion resumida: {'; '.join(edu_resumen) or 'no indicada'}",
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
    """Parsea JSON tolerando ```json ... ```, texto alrededor o varios objetos.

    Algunos modelos (p. ej. GLM) agregan un segundo objeto o comentarios
    después del JSON: cortar del primer '{' al último '}' falla con
    "Extra data". Acá se decodifica el primer objeto COMPLETO y se ignoran
    los restos; si hay varios, se prefiere el que trae las claves esperadas.
    """
    t = (texto or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    if not t:
        raise IAError("la IA devolvio una respuesta vacia")
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    encontrados = []
    for i, ch in enumerate(t):
        if ch not in "{[":
            continue
        try:
            obj, _ = dec.raw_decode(t[i:])
        except ValueError:
            continue
        encontrados.append(obj)
    dicts = [o for o in encontrados if isinstance(o, dict)]
    # El objeto bueno es el que trae perfil + evaluacion juntos (CV) o la
    # lista de resultados (vacantes). Evita quedarnos con un sub-objeto
    # suelto si el JSON venia truncado o anidado.
    for obj in dicts:
        if "perfil" in obj and "evaluacion" in obj:
            return obj
    for obj in dicts:
        if "resultados" in obj:
            return obj
    for obj in dicts:
        if "perfil" in obj or "evaluacion" in obj:
            return obj
    if dicts:
        return dicts[0]
    raise IAError("la IA no devolvio JSON reconocible")


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
        self.api_key = cfg.get("api_key") or self._clave_proveedor(preset)
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
        if os.getenv("OLLAMA_API_KEY"):
            return "ollama"
        if os.getenv("NVIDIA_API_KEY"):
            return "nvidia"
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return "google"
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        return "google"

    @staticmethod
    def _clave_proveedor(preset: Dict) -> str:
        """Clave del proveedor, priorizando la especifica sobre IA_API_KEY.

        Asi se pueden tener varias APIs configuradas a la vez
        (GEMINI_API_KEY + NVIDIA_API_KEY + ...) y elegir una en la UI.
        """
        for n in preset.get("key_env", ()):
            if n == "IA_API_KEY":
                continue
            v = os.getenv(n)
            if v:
                return v
        return os.getenv("IA_API_KEY") or ""

    @property
    def disponible(self) -> bool:
        return bool(self.api_key)

    def motivo_no_disponible(self) -> str:
        return (
            "Falta la clave de esta API. Definí GEMINI_API_KEY / "
            "NVIDIA_API_KEY / OLLAMA_API_KEY / OPENAI_API_KEY (o IA_API_KEY) "
            "en .env y elegí la API en la UI. Ver .env.example."
        )

    def resumen_uso(self) -> str:
        return (f"{self.llamadas} llamada(s), "
                f"tokens entrada={self.tokens_in} salida={self.tokens_out}")

    def listar_modelos(self) -> List[str]:
        """Lista viva de modelos del proveedor (GET /models).

        No se hardcodea nada: NVIDIA/Google agregan, retiran y deshabilitan
        modelos por cuenta. La UI y `--ia-listar-modelos` usan esto.
        """
        if not self.disponible:
            raise IAError(self.motivo_no_disponible())
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            r = httpx.get(f"{self.base_url}/models", headers=headers,
                          timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise IAError(f"error de red listando modelos: {exc}") from exc
        if r.status_code >= 400:
            raise IAError(
                f"HTTP {r.status_code} al listar modelos: "
                f"{_texto_plano(r.text, 240)}", status=r.status_code)
        try:
            data = r.json()
        except ValueError as exc:
            raise IAError("la lista de modelos no vino en JSON") from exc
        filas = data.get("data") if isinstance(data, dict) else data
        if not isinstance(filas, list):
            raise IAError("respuesta de /models sin lista 'data'")
        return sorted({str(f.get("id")) for f in filas
                       if isinstance(f, dict) and f.get("id")})

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
            if r.status_code in (404, 410):
                detalle = _texto_plano(r.text, 240)
                raise IAModeloError(
                    f"el modelo '{payload.get('model')}' no esta disponible "
                    f"para tu cuenta (HTTP {r.status_code}). Elegi otro en el "
                    f"selector de la UI o con --ia-modelo "
                    f"(--ia-listar-modelos muestra la lista). Detalle: {detalle}",
                    status=r.status_code)
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
            eleccion = data["choices"][0]
            mensaje = eleccion.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise IAError("respuesta de la IA sin 'choices[0].message'") from exc
        contenido = mensaje.get("content")
        razon = mensaje.get("reasoning_content")
        fin = eleccion.get("finish_reason")
        if not contenido and razon:
            # Modelos de razonamiento (Nemotron Lightning, GLM, etc.) a veces
            # mandan todo en reasoning_content y dejan content vacio.
            logger.info("IA: 'content' vacio; uso reasoning_content.")
            contenido = razon
        if not contenido:
            detalle = ""
            if fin == "length":
                detalle = " (max_tokens agotado"
                rt = ((uso.get("completion_tokens_details") or {})
                      .get("reasoning_tokens"))
                if rt:
                    detalle += f", {rt} en razonamiento"
                detalle += ")"
            raise IAError(
                "la IA devolvio una respuesta vacia" + detalle + ". Elegi otro "
                "modelo en el selector (p. ej. openai/gpt-oss-20b).")
        if fin == "length":
            rt = ((uso.get("completion_tokens_details") or {})
                  .get("reasoning_tokens"))
            extra = f", {rt} en razonamiento" if rt else ""
            raise IAError(
                "la IA corto la respuesta por max_tokens" + extra
                + ". Elegi un modelo que no gaste tanto en razonar "
                  "(p. ej. openai/gpt-oss-20b) o reintenta la corrida.")
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
            except IAModeloError as exc:
                logger.error("IA: %s", exc)
                errores.update({v["job_id"]: str(exc) for v in bloque})
                break
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
        except IAModeloError as exc:
            return {}, {v["job_id"]: str(exc) for v in bloque}
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
            "Eres un evaluador de CVs, reclutador tecnico y experto en ATS "
            "para Costa Rica. Recibis el texto de un CV y el perfil que el "
            "sistema ya tiene del candidato. Tu tarea tiene TRES partes:\n"
            "1) PERFIL: extraer un perfil estructurado CON FORMATO DE CV "
            "HARVARD/ATS. Usa SOLO lo que el CV demuestra: no inventes "
            "empresas, fechas, titulos, logros ni skills. Si un dato no esta, "
            "deja el campo vacio o la lista vacia. El bloque 'cv' es la "
            "fuente visual (lo que un ATS parsea); el bloque 'busqueda' es "
            "intencion de busqueda propuesta (no la inventes si el CV no la "
            "sugiere).\n"
            "Reglas del bloque 'cv':\n"
            "- 'contacto': solo email, telefono, ubicacion, linkedin, github "
            "y portafolio si aparecen. No inventes.\n"
            "- 'resumen': 2-4 lineas con el perfil real del CV, sin "
            "adjetivos vacios.\n"
            "- 'educacion': una entrada por estudio, en orden cronologico "
            "inverso. Fechas en MM/AAAA o AAAA; si no aparecen, vacias. "
            "Incluye gpa, honores y cursos solo si el CV los menciona.\n"
            "- 'experiencia': una entrada por puesto, en orden cronologico "
            "inverso. 'logros' son vinetas que empiezan con verbo de accion; "
            "incluye metricas SOLO si el CV las trae. No agregues logros "
            "inventados.\n"
            "- 'proyectos': solo si el CV los menciona.\n"
            "- 'habilidades': lista plana de palabras clave ATS "
            "(herramientas, tecnologias, metodos). Sin frases largas.\n"
            "- 'certificaciones': texto 'Nombre — Emisor (Ano)' si el CV lo "
            "trae.\n"
            "- 'idiomas': idioma y nivel reales.\n"
            "Reglas del bloque 'busqueda':\n"
            "- 'puestos_objetivo': cargos que el CV respalda y que tienen "
            "sentido como proximo paso. Nunca empresas.\n"
            "- 'ubicaciones': solo si el CV o el objetivo las mencionan. Si "
            "no, 'Costa Rica'.\n"
            "- 'temas': 3-6 busquedas; 'puesto' en espanol y 'puesto_en' el "
            "MISMO cargo traducido al ingles. Nunca la empresa.\n"
            "2) EVALUACION: puntuar el CV (0-100) para el objetivo indicado "
            "y dar mejoras concretas. En 'palabras_clave_faltantes' pon "
            "terminos que el mercado pide y el CV no evidencia; NO las "
            "agregues al CV adaptado sin evidencia.\n"
            "3) CV ADAPTADO: 'cv_adaptado_markdown' en formato Harvard/ATS: "
            "una sola columna, sin tablas, sin fotos ni graficos, "
            "encabezados estandar. Encabezado con nombre, email, telefono, "
            "LinkedIn y ciudad. Secciones: Perfil (opcional), Educacion, "
            "Experiencia, Proyectos, Habilidades, Certificaciones, Idiomas. "
            "Si el candidato tiene 3+ anos, pone Experiencia antes de "
            "Educacion; si no, Educacion primero. Fechas MM/AAAA, vinetas "
            "con verbos de accion y metricas reales. Reescribi SOLO el "
            "contenido real del CV (reordenar, aclarar, cuantificar lo que "
            "ya dice); no inventes experiencia ni logros. Mantenelo en "
            "espanol.\n"
            "Responde SOLO JSON valido, sin markdown alrededor, con este "
            "esquema:\n"
            '{"perfil":{'
            '"cv":{"nombre":"",'
            '"contacto":{"email":"","telefono":"","ubicacion":"",'
            '"linkedin":"","github":"","portafolio":""},'
            '"resumen":"",'
            '"educacion":[{"institucion":"","titulo":"","campo":"",'
            '"ubicacion":"","inicio":"","fin":"","gpa":"","honores":"",'
            '"cursos":[]}],'
            '"experiencia":[{"empresa":"","puesto":"","ubicacion":"",'
            '"inicio":"","fin":"","logros":[]}],'
            '"proyectos":[{"nombre":"","descripcion":"","tecnologias":[],'
            '"inicio":"","fin":"","link":""}],'
            '"habilidades":[],"certificaciones":[],'
            '"idiomas":[{"idioma":"","nivel":""}]},'
            '"busqueda":{"puestos_objetivo":[],"ubicaciones":["Costa Rica"],'
            '"temas":[{"puesto":"","puesto_en":"","ubicacion":"Costa Rica"}]}},'
            '"evaluacion":{"puntaje":0,"resumen":"","fortalezas":[],'
            '"debilidades":[],"palabras_clave_faltantes":[],'
            '"sugerencias":[{"donde":"","cambio":"","por_que":""}]},'
            '"cv_adaptado_markdown":""}'
        )

    def _user_cv(self, cv_texto: str, perfil_txt: str,
                 vacantes: Optional[List[dict]], puesto: str = "") -> str:
        # Muestra corta: el objetivo es detectar palabras clave, no analizar el
        # mercado completo. Con 25 vacantes largas el prompt crecia de mas y el
        # modelo devolvia JSON incompletos.
        muestra = []
        for v in (vacantes or [])[:6]:
            muestra.append({
                "titulo": v.get("title", ""),
                "empresa": v.get("company", ""),
                "descripcion": (v.get("description") or "")[:300],
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
            if isinstance(anidado, dict) and ("perfil" in anidado
                                              or "cv" in anidado):
                data = anidado
                break
        perfil_raw = data.get("perfil")
        if not isinstance(perfil_raw, dict) and isinstance(data.get("cv"), dict):
            # El modelo devolvio el perfil a nivel raiz (sin "perfil").
            perfil_raw = data
        if not isinstance(perfil_raw, dict):
            raise IAError(
                "la respuesta no trae 'perfil' (claves: "
                + ", ".join(sorted(str(k) for k in data.keys())) + ")")
        ev = (data.get("evaluacion") or data.get("evaluación")
              or data.get("evaluation"))
        if not isinstance(ev, dict):
            ev = perfil_raw.get("evaluacion") or perfil_raw.get("evaluación")
        if not isinstance(ev, dict):
            raise IAError(
                "la respuesta no trae 'evaluacion' (claves: "
                + ", ".join(sorted(str(k) for k in data.keys())) + ")")

        # Normaliza al esquema canonico Harvard/ATS. Acepta tanto el formato
        # nuevo (cv + busqueda) como el plano anterior.
        from core.profile import normalizar_perfil
        perfil = normalizar_perfil(perfil_raw)
        cv = perfil["cv"]
        b = perfil["busqueda"]

        # Un modelo de razonamiento puede devolver el esqueleto del JSON con
        # todo vacio tras gastar max_tokens "pensando": no lo damos por bueno.
        if not any((
            cv["nombre"], cv["contacto"], cv["resumen"], cv["educacion"],
            cv["experiencia"], cv["proyectos"], cv["habilidades"],
            b["puestos_objetivo"], b["temas"], ev.get("resumen"),
            data.get("cv_adaptado_markdown"),
            perfil_raw.get("cv_adaptado_markdown"),
        )):
            raise IAError(
                "la IA devolvio un perfil/evaluacion vacios (probablemente un "
                "modelo de razonamiento que gasto max_tokens pensando). Elegi "
                "otro modelo en el selector (p. ej. openai/gpt-oss-20b).")

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
        return {
            "perfil": perfil,
            "evaluacion": {
                "puntaje": round(max(0.0, min(100.0, puntaje)), 1),
                "resumen": _texto_plano(ev.get("resumen"), 600),
                "fortalezas": self._lista_texto(ev.get("fortalezas"), 10),
                "debilidades": self._lista_texto(ev.get("debilidades"), 10),
                "palabras_clave_faltantes": self._lista_texto(
                    ev.get("palabras_clave_faltantes"), 25),
                "sugerencias": sugerencias[:10],
            },
            "cv_adaptado_markdown": str(
                data.get("cv_adaptado_markdown")
                or perfil_raw.get("cv_adaptado_markdown") or "").strip(),
        }

    def evaluar_cv(self, cv_texto: str, perfil, vacantes: Optional[List[dict]] = None,
                   puesto: str = "") -> dict:
        # El esquema Harvard/ATS (educacion, experiencia, proyectos) mas la
        # evaluacion y el CV adaptado no entran comodos en 4000: gpt-oss-20b
        # se cortaba con finish_reason=length. 6000 deja margen sin disparar
        # la espera a 5+ minutos.
        user = self._user_cv(cv_texto, perfil_texto(perfil), vacantes, puesto)
        data, _ = self._chat_json(self._system_cv(), user, max_tokens=6000)
        try:
            return self._normalizar_cv(data)
        except IAError as exc:
            # A veces el modelo devuelve un JSON valido pero con otra forma
            # (evaluacion anidada, claves extra, perfil suelto...). Un unico
            # reintento con instruccion explicita lo endereza sin partir el lote.
            logger.warning("CV: respuesta invalida (%s); reintento unico.", exc)
            estricto = user + (
                "\n\nIMPORTANTE: responde SOLO un objeto JSON completo, sin "
                "texto ni markdown. Las claves 'perfil' y 'evaluacion' van AL "
                "MISMO NIVEL, dentro del objeto raiz.")
            data2, _ = self._chat_json(self._system_cv(), estricto,
                                       max_tokens=6000)
            return self._normalizar_cv(data2)


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
    from core.profile import normalizar_perfil
    from core.cv_render import cv_harvard_markdown

    p = normalizar_perfil(perfil)
    cv = p["cv"]
    b = p["busqueda"]
    contacto = cv.get("contacto") or {}
    c_txt = " | ".join(
        str(contacto[k]) for k in ("email", "telefono", "ubicacion", "linkedin")
        if contacto.get(k)
    ) or "(sin datos)"
    lineas += ["", "## Perfil Harvard/ATS extraído"]
    lineas.append(f"- **Contacto:** {c_txt}")
    lineas.append(f"- **Resumen:** {cv.get('resumen') or '(sin resumen)'}")
    lineas.append(f"- **Puestos objetivo:** "
                  f"{', '.join(b.get('puestos_objetivo') or []) or '(sin datos)'}")
    lineas.append(f"- **Habilidades:** "
                  f"{', '.join(cv.get('habilidades') or []) or '(sin datos)'}")
    temas_txt = "; ".join(
        f"{t.get('puesto', '')} ({t.get('ubicacion', '')})"
        for t in (b.get("temas") or [])) or "(sin datos)"
    lineas.append(f"- **Temas:** {temas_txt}")
    lineas.append(f"- **Educación:** {len(cv.get('educacion') or [])} registro(s)"
                  f" · **Experiencia:** {len(cv.get('experiencia') or [])} puesto(s)"
                  f" · **Proyectos:** {len(cv.get('proyectos') or [])}")
    lineas += ["", "### CV Harvard/ATS reconstruido desde el perfil",
               "", cv_harvard_markdown(p), ""]
    lineas += ["", "## CV adaptado por la IA (revisar antes de usar)",
               "", resultado.get("cv_adaptado_markdown") or "(sin texto)", ""]
    return "\n".join(lineas)
