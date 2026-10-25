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
| Manual | `python start_ia_server.py` (verifica/arranca/espera el modelo). El comando replica el build Vulkan del usuario (`D:\llama-b11662-bin-win-vulkan-x64`, modelo `D:\Models\Qwen352B`). Log: `data/ia_server.log`. |
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
IA_MODEL=qwen3.5-2b
IA_API_KEY=...            # llama.cpp acepta cualquier valor
```

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
