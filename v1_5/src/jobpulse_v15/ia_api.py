"""Cliente IA externa compatible OpenAI para v1.5.

Soporta múltiples proveedores vía presets:
- openrouter  -> openrouter.ai/api/v1
- nvidia      -> integrate.api.nvidia.com/v1
- google      -> generativelanguage.googleapis.com/v1beta/openai/
- ollama      -> ollama.com/v1 (cloud) o localhost:11434/v1 (local)
- lmstudio    -> localhost:1234/v1 (LM Studio local)
- custom      -> cualquier endpoint OpenAI-compatible (llama.cpp, vLLM, etc.)

Uso:
    client = IAClient({"proveedor": "custom", "base_url": "http://localhost:8080/v1", "modelo": "mi-modelo"})
    modelos = client.listar_modelos()
    resp = client.chat_json([{"role": "user", "content": "Hola"}])
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("jobpulse.ia")

# ──────────────────────────────────────────────────────────────
# Configuración de presets
# ──────────────────────────────────────────────────────────────

@dataclass
class ProviderPreset:
    nombre: str
    base_url: str
    env_key: str                    # variable de entorno para la API key
    modelo_default: str
    requiere_key: bool = True
    headers_extra: Dict[str, str] = field(default_factory=dict)


PRESETS: Dict[str, ProviderPreset] = {
    "openrouter": ProviderPreset(
        nombre="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        modelo_default="~openai/gpt-latest",
        headers_extra={"HTTP-Referer": "https://github.com/giovar24/jobpulse-cr", "X-Title": "JobPulse CR"},
    ),
    "nvidia": ProviderPreset(
        nombre="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        env_key="NVIDIA_API_KEY",
        modelo_default="openai/gpt-oss-20b",
    ),
    "google": ProviderPreset(
        nombre="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        env_key="GEMINI_API_KEY",
        modelo_default="gemini-2.5-flash",
    ),
    "ollama": ProviderPreset(
        nombre="Ollama Cloud",
        base_url="https://ollama.com/v1",
        env_key="OLLAMA_API_KEY",
        modelo_default="auto",
        requiere_key=False,
    ),
    "lmstudio": ProviderPreset(
        nombre="LM Studio (local)",
        base_url="http://localhost:1234/v1",
        env_key="LMSTUDIO_API_KEY",
        modelo_default="auto",  # placeholder; se resuelve via /models
        requiere_key=False,
    ),
    "custom": ProviderPreset(
        nombre="Custom (llama.cpp, vLLM, etc.)",
        base_url="",        # obligatorio vía config
        env_key="IA_API_KEY",
        modelo_default="",  # obligatorio vía config
        requiere_key=False,
    ),
}

PROVEEDOR_NOMBRES = {k: v.nombre for k, v in PRESETS.items()}

PROMPT_VERSION = "v3"  # invalida cache al cambiar

# ──────────────────────────────────────────────────────────────
# Errores
# ──────────────────────────────────────────────────────────────

class IAError(Exception):
    """Error base del cliente IA."""
    pass


class IAModeloError(IAError):
    """Modelo no encontrado (404/410) - fatal, no reintentar."""
    pass


class IACuotaError(IAError):
    """429 / 402 - cuota agotada."""
    pass


# ──────────────────────────────────────────────────────────────
# Cache de respuestas (persistente en disco)
# ──────────────────────────────────────────────────────────────

CACHE_DIR = Path(os.getenv("JOBPULSE_IA_CACHE", "data/usuario/ia_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(proveedor: str, modelo: str, messages: List[Dict], **kwargs) -> str:
    """Hash determinista de la petición para cache."""
    payload = json.dumps({
        "prov": proveedor,
        "model": modelo,
        "msg": messages,
        "kw": sorted(kwargs.items()),
        "pv": PROMPT_VERSION,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _cache_get(key: str) -> Optional[Dict]:
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _cache_set(key: str, data: Dict) -> None:
    path = CACHE_DIR / f"{key}.json"
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# Cliente principal
# ──────────────────────────────────────────────────────────────

class IAClient:
    """Cliente HTTP para endpoints OpenAI-compatible."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 120.0,
        lote: int = 8,
        max_vacantes: int = 60,
        max_llamadas: int = 12,
        rpm: int = 8,
    ) -> None:
        """
        Args:
            config: dict con claves opcionales:
                - proveedor: "openrouter" | "nvidia" | "google" | "ollama" | "lmstudio" | "custom"
                - base_url: override (requerido si proveedor="custom")
                - modelo: override del default del preset
                - api_key: override (si no, usa env var del preset)
            timeout: timeout por request (segundos)
            lote: tamaño de lote para procesamiento batch
            max_vacantes: tope de items a evaluar en una corrida
            max_llamadas: tope de llamadas HTTP por corrida
            rpm: requests por minuto (rate limit)
        """
        config = config or {}

        # Resolver proveedor
        self.proveedor = config.get("proveedor") or os.getenv("IA_PROVEEDOR") or "custom"
        if self.proveedor not in PRESETS:
            raise IAError(f"Proveedor desconocido: {self.proveedor}. Válidos: {list(PRESETS)}")

        preset = PRESETS[self.proveedor]

        # base_url: config > env > preset
        self.base_url = (
            config.get("base_url")
            or os.getenv("IA_BASE_URL")
            or preset.base_url
        ).rstrip("/")
        if not self.base_url:
            raise IAError(f"base_url requerido para proveedor '{self.proveedor}'")

        # api_key: config > env específico > env genérico > preset.env_key
        self.api_key = (
            config.get("api_key")
            or os.getenv(preset.env_key)
            or os.getenv("IA_API_KEY")
            or ""
        )
        if preset.requiere_key and not self.api_key:
            raise IAError(f"Falta API key para {preset.nombre}. Define {preset.env_key} o IA_API_KEY en .env")

        # modelo: config > env > preset.default
        self.modelo = (
            config.get("modelo")
            or os.getenv("IA_MODEL")
            or preset.modelo_default
        )
        # Permitir modelo vacío para proveedores con auto-detección (lmstudio, ollama)
        # Se resolverá llamando a listar_models() antes de chat()
        auto_detect_proveedores = {"lmstudio", "ollama"}
        if self.modelo == "auto" or (self.proveedor in auto_detect_proveedores and not self.modelo):
            self.modelo = ""
        elif not self.modelo:
            raise IAError(f"modelo requerido para proveedor '{self.proveedor}'")

        # Parámetros de control
        self.timeout = timeout
        self.lote = lote
        self.max_vacantes = max_vacantes
        self.max_llamadas = max_llamadas
        self.rpm = rpm

        # Estado runtime
        self.llamadas = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._min_interval = 60.0 / max(1, rpm)
        self._last_call = 0.0

        # Cliente HTTP
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
                "Content-Type": "application/json",
                **preset.headers_extra,
            },
        )

    @property
    def disponible(self) -> bool:
        return bool(self.base_url and (self.api_key or not PRESETS[self.proveedor].requiere_key))

    def motivo_no_disponible(self) -> str:
        if not self.base_url:
            return "base_url no configurado"
        if PRESETS[self.proveedor].requiere_key and not self.api_key:
            return f"falta API key ({PRESETS[self.proveedor].env_key})"
        return "desconocido"

    # ──────────────────────────────────────────────────────────
    # Utilidades de rate limiting y request
    # ──────────────────────────────────────────────────────────

    async def _rate_limit(self) -> None:
        now = time.monotonic()
        wait = self._last_call + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _post(self, endpoint: str, payload: Dict) -> Dict:
        """POST con reintento único en 429/5xx."""
        await self._rate_limit()
        url = f"{self.base_url}{endpoint}"

        for intento in range(2):
            try:
                resp = await self._client.post(url, json=payload)
            except httpx.TimeoutException as e:
                raise IAError(f"Timeout ({self.timeout}s) en {endpoint}") from e
            except httpx.RequestError as e:
                raise IAError(f"Error de red: {e}") from e

            if resp.status_code == 404:
                # Modelo no encontrado (404/410) -> fatal
                raise IAModeloError(
                    f"Modelo '{self.modelo}' no encontrado (404) en {self.proveedor}. "
                    f"Verifica con --ia-listar-modelos y elige otro con --ia-modelo."
                )
            if resp.status_code == 401:
                raise IAError("API key inválida o expirada (401)")
            if resp.status_code in (402, 429):
                if intento == 0:
                    await asyncio.sleep(5)
                    continue
                raise IACuotaError(f"Cuota agotada o rate limit ({resp.status_code})")
            if resp.status_code >= 500:
                if intento == 0:
                    await asyncio.sleep(3)
                    continue
                raise IAError(f"Error servidor {resp.status_code}: {resp.text[:200]}")

            try:
                return resp.json()
            except json.JSONDecodeError as e:
                raise IAError(f"Respuesta no es JSON válido: {resp.text[:200]}") from e

        raise IAError("Máximo de reintentos alcanzado")

    # ──────────────────────────────────────────────────────────
    # API pública
    # ──────────────────────────────────────────────────────────

    async def listar_modelos(self) -> List[str]:
        """GET /models en vivo. Devuelve lista de IDs de modelos."""
        await self._rate_limit()
        url = f"{self.base_url}/models"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception as e:
            logger.warning("IA listar_modelos falló (%s): %s", self.proveedor, e)
            return []

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 4000,
        response_format: Optional[Dict] = None,
        use_cache: bool = True,
    ) -> Dict:
        """
        Chat completion síncrono (usa asyncio.run internamente).
        Para uso en CLI/scripts que no son async.
        """
        import asyncio
        return asyncio.run(self.achat(messages, temperature=temperature, max_tokens=max_tokens,
                                       response_format=response_format, use_cache=use_cache))

    async def achat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 4000,
        response_format: Optional[Dict] = None,
        use_cache: bool = True,
    ) -> Dict:
        """Chat completion asíncrono con cache opcional."""
        if self.llamadas >= self.max_llamadas:
            raise IAError(f"Límite de llamadas alcanzado ({self.max_llamadas})")

        # Cache key
        cache_key = _cache_key(self.proveedor, self.modelo, messages,
                               temperature=temperature, max_tokens=max_tokens,
                               response_format=response_format)
        if use_cache:
            cached = _cache_get(cache_key)
            if cached:
                logger.debug("IA cache hit: %s", cache_key[:8])
                return cached

        payload = {
            "model": self.modelo,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        # OpenRouter requiere max_tokens explícito para validar presupuesto
        if self.proveedor == "openrouter" and "max_tokens" not in payload:
            payload["max_tokens"] = 3000

        result = await self._post("/chat/completions", payload)
        self.llamadas += 1

        # Contar tokens si vienen en response
        usage = result.get("usage") or {}
        self.tokens_in += usage.get("prompt_tokens", 0)
        self.tokens_out += usage.get("completion_tokens", 0)

        if use_cache:
            _cache_set(cache_key, result)

        return result

    async def chat_json(
        self,
        messages: List[Dict[str, str]],
        *,
        schema: Optional[Dict] = None,
        temperature: float = 0.1,
        max_tokens: int = 4000,
        use_cache: bool = True,
    ) -> Dict:
        """
        Chat completion que exige respuesta JSON válido.
        Si schema se provee, usa response_format={type: "json_schema", ...}
        """
        rf = None
        if schema:
            rf = {"type": "json_schema", "json_schema": {"name": "response", "schema": schema, "strict": True}}
        else:
            rf = {"type": "json_object"}

        resp = await self.achat(messages, temperature=temperature, max_tokens=max_tokens,
                                response_format=rf, use_cache=use_cache)

        content = resp["choices"][0]["message"]["content"]
        return self._json_de_texto(content)

    @staticmethod
    def _json_de_texto(texto: str) -> Dict:
        """Extrae JSON válido de texto que puede tener basura alrededor."""
        import re
        # 1. Buscar ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", texto, re.DOTALL)
        if m:
            texto = m.group(1)
        else:
            # 2. Buscar primer { ... } balanceado
            m = re.search(r"(\{.*\})", texto, re.DOTALL)
            if m:
                texto = m.group(1)

        # 3. Intentar json.loads, si falla probar raw_decode para objetos múltiples
        try:
            return json.loads(texto)
        except json.JSONDecodeError:
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(texto)
                return obj
            except Exception as e:
                raise IAError(f"No se pudo parsear JSON de la respuesta: {e}\nTexto: {texto[:500]}")

    # ──────────────────────────────────────────────────────────
    # Hash de vacante/CV para cache
    # ──────────────────────────────────────────────────────────

    def hash_vacante(self, vacante: Dict, perfil_hash: str) -> str:
        """Hash único de vacante + perfil + versión de prompt."""
        contenido = json.dumps({
            "titulo": vacante.get("title", ""),
            "empresa": vacante.get("company", ""),
            "desc": (vacante.get("description_raw", "") or "")[:3000],
            "reqs": (vacante.get("requirements_raw", "") or "")[:1500],
            "perfil": perfil_hash,
            "pv": PROMPT_VERSION,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(contenido.encode()).hexdigest()[:16]

    def hash_cv(self, cv_texto: str) -> str:
        return hashlib.sha256(cv_texto.encode()).hexdigest()[:16]

    def resumen_uso(self) -> str:
        return f"{self.llamadas} llamadas, {self.tokens_in} tok in, {self.tokens_out} tok out"

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> IAClient:
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()


# ──────────────────────────────────────────────────────────────
# Funciones de conveniencia para CLI
# ──────────────────────────────────────────────────────────────

import asyncio


async def _listar_modelos_cli(proveedor: str = "", modelo: str = "") -> None:
    """Lista modelos del proveedor y sale."""
    cfg = {}
    if proveedor:
        cfg["proveedor"] = proveedor
    if modelo:
        cfg["modelo"] = modelo
    client = IAClient(cfg)
    try:
        modelos = await client.listar_modelos()
        actual = modelo or client.modelo
        print(f"{len(modelos)} modelo(s) en {client.proveedor} ({client.base_url}):")
        for m in modelos:
            print(("  * " if m == actual else "    ") + m)
        if actual not in modelos:
            print(f"\n⚠ El modelo actual '{actual}' no aparece en la lista; puede haber sido retirado.")
        print(f"\nUsa: python -m jobpulse_v15 --ia --ia-modelo \"<modelo>\"")
    finally:
        await client.close()


def listar_modelos_cli(proveedor: str = "", modelo: str = "") -> None:
    asyncio.run(_listar_modelos_cli(proveedor, modelo))