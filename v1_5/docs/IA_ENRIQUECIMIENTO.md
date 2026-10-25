# Enriquecimiento IA (O1) — operación y configuración

Cómo funciona el enriquecimiento de ofertas con IA local (llama.cpp) y cómo
operarlo/depurarlo. Todo configurable desde `.env` (sin tocar código).

## Arquitectura

```
BD jobs ──> ia_enrichment.run_ia_enrichment ──> IAClient (OpenAI-compat)
                                               └─> llama-server (puerto 8081)
```

- Campos **críticos**: `city`, `region`, `work_mode`, `date_published`.
- Campos **secundarios**: `employment_type`, `salary_*`, `skills`, `languages`.
- Guardado selectivo: solo se escriben las columnas que la IA realmente
  tocó (nunca se sobre-escriben skills/languages existentes).

## Arranque del servidor

| Modo | Qué hacer |
|---|---|
| Automático (recomendado) | `jobpulse-v15.bat` levanta todo: [1] venv, [2] **servidor IA** (`start_ia_server.py`), [3] Brave CDP, [4] UI. Si el server ya responde, no hace nada. |
| Manual | `ia-server.bat` o `python start_ia_server.py` (verifica/arranca/espera el modelo). El comando replica el build Vulkan del usuario (`D:\llama-b11662-bin-win-vulkan-x64`, modelo `D:\Models\gemma-4-E2B-it-qat-UD-Q4_K_XL`, 8k ctx + MTP draft). Log: `data/ia_server.log`. |
| **Servidor externo** | Ponga `IA_BASE_URL` del `.env` a la URL remota (p. ej. `http://192.168.1.10:8081/v1`). El preflight lo detecta como no-local y **no** intenta arrancar nada local. |

Verificación rápida:
```powershell
python start_ia_server.py --check     # exit 0 = arriba / 1 = apagado
```

## Ejecutar el pase

```powershell
python run_enriquecimiento.py
python run_enriquecimiento.py --solo-criticos   # sin campos secundarios
```
El script hace **preflight** (aborta de inmediato con error claro si el
servidor está apagado — antes esto se traducía en más de 150 fallos falso),
luego pases de críticos en ciclos (para hasta cuando deja de avanzar) y
finalmente un pase completo con secundarios.

## Configuración .env

```
IA_PROVEEDOR=custom
IA_BASE_URL=http://127.0.0.1:8081/v1
IA_MODEL=gemma4-e2b-qat
IA_API_KEY=...            # llama.cpp acepta cualquier valor
```

## Modelo local: Gemma 4 E2B QAT (A/B 2026-10-25)

**Canónico desde el 2026-10-25.** Qwen 3.5 2B quedó descartado como modelo de
enriquecimiento:

| Métrica | **Gemma 4 E2B QAT** | Qwen 3.5 2B |
|---|---|---|
| Pase completo (175 jobs) | **10.3 min** (618s) | ~67 min |
| A/B 5 jobs crudos, misma caché vacía | **50.9s / 11 campos llenados** | 90.0s / 2 campos |
| Resultado del pase | skills 100%, languages 97.7% | languages quedaba en 23% |

Config del servidor (comando vivo en `start_ia_server.py`): modelo
`gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf` + mmproj + **MTP draft** (`--spec-type
draft-mtp`, decoding especulativo), **8k ctx** (suficiente: el pipeline trunca
a ~1.2k tokens de entrada + 600 de salida), KV q8_0, `--metrics`.

Lecciones operativas del A/B:

- **La caché en disco falsea benchmarks**: Qwen "respondió" en 3.1s pegando a
  entradas del pase anterior. Todo benchmark debe aislar caché con
  `JOBPULSE_IA_CACHE` — `tools/comparar_ia.py` ya lo hace.
- **Contexto**: 8k es el punto dulce para este pipeline (4k alcanzaría; 16k era
  desperdicio de VRAM de KV). Subir a 16k solo si O6 manda CVs completos.
- **Flanco conocido**: si el modelo lista >10 skills el JSON se trunca a los
  600 tokens de `max_tokens` (falla tras 3 reintentos). Si reaparece a menudo,
  subir skills a 900.

Resultado del pase con Gemma (2026-10-25): skills 15→**0**, languages
135→**4**, city 28→24, region 14→13. Quedan 23 `work_mode='unknown'` (el texto
no menciona modalidad — honesto) y 82 salary sin texto en la fuente (no
extraíble). Los 24 sin city son en su mayoría postings "Virtual/Remote" o de
LATAM genérico — ver `tools/huecos_restantes.py`.

## Depuración (guía rápida)

| Síntoma | Causa | Solución |
|---|---|---|
| `servidor no responde` en preflight | llama-server apagado | `python start_ia_server.py` o arrancarlo a mano |
| `No se pudo parsear JSON ... Unterminated string` | modelo verboso + `max_tokens` bajo | skills ahora pide 600; si vuelve a pasar, sube tokens o cambia a un modelo mayor |
| `work_mode` queda `unknown` para siempre | el texto de la oferta no menciona modalidad | correcto: se registra `unknown` de verdad (la IA dice 'unknown'), no reintenta al infinito |
| jobs con `skills='null'` en BD | bug viejo del guardado (GET) | ya corregido + script `limpiar_null.py` |

## Estado tras los fixes

- Preflight de servidor antes de cada pase (`verificar_servidor_ia`).
- Reintentos por campo: 3 intentos con backoff (1s, 2s).
- `only_critical=False` ahora SÍ incluye jobs que solo le faltan campos
  secundarios (antes el filtro descartaba y el pase completo era imposible).
- Guardado dinámico: UPDATE solo de columnas tocadas; corregidos los bugs
  que seteaban `salary_max`/`salary_currency`/`salary_period` a NULL.
- Tests: `tests/test_ia_enrichment.py` (11 tests con IA simulada).
