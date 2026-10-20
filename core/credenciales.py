# -*- coding: utf-8 -*-
"""core.credenciales — almacen LOCAL de credenciales + la LISTA de sitios que piden login.

Dos piezas que faltaban (pedido del usuario 2026-10-19):

1. **LA LISTA** (`lista_sitios_login()`): no existia un inventario de los
   sitios que exigen password. Se construye como union de:
     - `data/empresas/login_requerido.json`  -> registro maestro: seeds fijos
       (ANE obligatorio, Indeed obligatorio, LinkedIn recomendado) + lo que
       los providers detectan EN VIVO cada corrida (login-wall en Indeed,
       login-wall/Cloudflare en login_sites, etc.).
     - `data/empresas/pending_login.json`    -> pendientes ya detectados.
     - `data/empresas/login_sites.json`      -> portals del employer en browser.
     - `core.sesiones.BOARDS_SESION`         -> boards con sesion recomendada.
   Cada item dice: obligatorio? auto-login? ya hay credencial guardada? hay
   un pendiente sin resolver?

2. **DONDE PONER EL PASSWORD** (`credenciales.json`): JSON local en
   `data/empresas/`, agregado al `.gitignore` (nunca viaja a git). Se edita
   desde la pestaña "Credenciales" del dashboard o con `credenciales.py`
   (CLI). Las credenciales se LEEN desde ahi en la corrida y se usan para el
   login automatico con fallback a login manual (ver
   `providers/login/auto_login.py`).

Decision de diseño que se mantiene: el repo NO contiene credenciales; solo
esta bola de datos local (como `data/profile.json`).
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

RAIZ = Path(__file__).resolve().parent.parent
EMPRESAS_DIR = RAIZ / "data" / "empresas"
CRED_JSON = EMPRESAS_DIR / "credenciales.json"
REQ_JSON = EMPRESAS_DIR / "login_requerido.json"
PENDING_JSON = EMPRESAS_DIR / "pending_login.json"
LOGIN_SITES_JSON = EMPRESAS_DIR / "login_sites.json"

_LOCK = threading.Lock()

GRUPO_LABEL = {
    "boards": "Job board (agregador)",
    "directos": "Portal de la empresa",
}

# Seeds: los sites que se SABEN que piden password/sesion antes de la 1a corrida.
# `obligatorio=True` -> sin eso el proveedor no devuelve nada.
_SEED = [
    {
        "empresa": "ANE",
        "url": "https://www.ane.cr/Cuenta/Login",
        "url_check": "https://www.ane.cr/Puesto",
        "grupo": "boards",
        "obligatorio": True,
        "auto_login": True,
        "motivo": "Cuenta gratuita obligatoria: sin sesion el listado solo "
                  "muestra enlaces a /Cuenta/RegistroUsuario (0 vacantes "
                  "utiles). Es el proveedor #1 del sistema.",
        "detectado": "seed",
    },
    {
        "empresa": "Indeed",
        "url": "https://secure.indeed.com/account/login",
        "url_check": "https://cr.indeed.com/jobs?q=soporte&l=Costa+Rica",
        "grupo": "boards",
        "obligatorio": True,
        "auto_login": True,
        "motivo": "Login-wall intermitente: en varias busquedas devuelve 0 "
                  "cards con el titulo 'Iniciar sesion | Cuentas Indeed'.",
        "detectado": "seed",
    },
    {
        "empresa": "LinkedIn",
        "url": "https://www.linkedin.com/login",
        "url_check": "https://www.linkedin.com/jobs/search/?keywords=soporte"
                     "&location=Costa%20Rica",
        "grupo": "boards",
        "obligatorio": False,
        "auto_login": False,
        "motivo": "Opcional: el guest endpoint funciona con limite; con "
                  "sesion amplias resultados y evitas bloqueos progresivos.",
        "detectado": "seed",
    },
]


# ------------------------------------------------------------------ helpers
def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(empresa: str) -> str:
    return (empresa or "").strip().casefold()


def _leer(path: Path, default):
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d
    except Exception:
        pass
    return default


def _escribir(path: Path, data) -> bool:
    """Escritura atomica (tmp + replace) para que la GUI nunca lea a medias."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ------------------------------------------------------- registro: REQUIERE
def cargar_requeridos() -> List[dict]:
    """login_requerido.json (se crea con los seeds en el primer acceso)."""
    if not REQ_JSON.exists():
        _escribir(REQ_JSON, [dict(s, ts=_ahora()) for s in _SEED])
    data = _leer(REQ_JSON, None)
    if not isinstance(data, list):
        _escribir(REQ_JSON, [dict(s, ts=_ahora()) for s in _SEED])
        return [dict(s, ts=_ahora()) for s in _SEED]
    return [d for d in data if isinstance(d, dict) and d.get("empresa")]


def guardar_requeridos(data: List[dict]) -> bool:
    return _escribir(REQ_JSON, data)


def registrar_requerimiento(empresa: str, url: str = "", motivo: str = "",
                            grupo: str = "", obligatorio: bool = True,
                            url_check: str = "") -> dict:
    """Registra/actualiza un sitio que pide login (lo llaman los providers
    cuando detectan un login-wall). Dedupe por nombre de empresa."""
    if not empresa:
        return {}
    with _LOCK:
        data = cargar_requeridos()
        key = _norm(empresa)
        nuevo = None
        for it in data:
            if _norm(it.get("empresa", "")) == key:
                nuevo = it
                break
        if nuevo is None:
            nuevo = {"empresa": empresa.strip(), "detectado": "runtime"}
            data.append(nuevo)
        if url:
            nuevo["url"] = url
        if url_check:
            nuevo["url_check"] = url_check
        if motivo:
            # el ultimo motivo gana (es el estado actual del sitio)
            nuevo["motivo"] = motivo
        if grupo:
            nuevo["grupo"] = grupo
        if "obligatorio" not in nuevo:
            nuevo["obligatorio"] = bool(obligatorio)
        nuevo["ts"] = _ahora()
        guardar_requeridos(data)
        return nuevo


def marcar_auto_login(empresa: str, valor: bool) -> bool:
    with _LOCK:
        data = cargar_requeridos()
        for it in data:
            if _norm(it.get("empresa", "")) == _norm(empresa):
                it["auto_login"] = bool(valor)
                it["ts"] = _ahora()
                return guardar_requeridos(data)
    return False


# ------------------------------------------------------- credenciales store
def cargar_credenciales() -> Dict[str, dict]:
    data = _leer(CRED_JSON, {})
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def guardar_credenciales(data: Dict[str, dict]) -> bool:
    return _escribir(CRED_JSON, data)


def credencial(empresa: str) -> Optional[dict]:
    """Credencial guardada para `empresa` (case-insensitive).

    Fallback .env: ANE sigue soportando ANE_USER/ANE_PASSWORD (compatibilidad
    con lo que ya existe); si no hay JSON, se usa esa.
    """
    data = cargar_credenciales()
    for k, v in data.items():
        if _norm(k) == _norm(empresa) or _norm(v.get("empresa", "")) == _norm(empresa):
            if v.get("usuario") and v.get("password"):
                return v
    if _norm(empresa) == "ane":
        u, p = os.getenv("ANE_USER"), os.getenv("ANE_PASSWORD")
        if u and p:
            return {"empresa": "ANE", "usuario": u, "password": p,
                    "fuente": ".env", "auto_login": True}
    return None


def upsert_credencial(empresa: str, usuario: str, password: str,
                      url: str = "", auto_login: bool = True,
                      nota: str = "") -> dict:
    data = cargar_credenciales()
    clave = _norm(empresa)
    prev = data.get(clave) or {}
    reg = {
        "empresa": empresa.strip(),
        "usuario": usuario.strip(),
        "password": password,
        "url": url or prev.get("url", ""),
        "auto_login": bool(auto_login),
        "nota": nota or prev.get("nota", ""),
        "ts": _ahora(),
    }
    data[clave] = reg
    guardar_credenciales(data)
    # tener credencial activa el auto-login del sitio en el registro
    marcar_auto_login(empresa, bool(auto_login))
    return reg


def borrar_credencial(empresa: str) -> bool:
    data = cargar_credenciales()
    clave = _norm(empresa)
    hit = None
    for k in list(data):
        if _norm(k) == clave or _norm(data[k].get("empresa", "")) == clave:
            hit = k
    if hit is None:
        return False
    del data[hit]
    guardar_credenciales(data)
    marcar_auto_login(empresa, False)
    return True


# ------------------------------------------------------------- LA LISTA
def _pendientes() -> Dict[str, dict]:
    out = {}
    for p in _leer(PENDING_JSON, []):
        if isinstance(p, dict) and p.get("empresa"):
            out[_norm(p["empresa"])] = p
    return out


def lista_sitios_login() -> List[dict]:
    """LA LISTA: todos los sitios que piden password/sesion, con estado.

    Cada item: empresa, url, url_check, grupo, motivo, obligatorio,
    auto_login, ts, tiene_credencial, usuario, pendiente, pendiente_ts.
    """
    items: Dict[str, dict] = {}

    def _base(empresa, **kw):
        k = _norm(empresa)
        if k not in items:
            items[k] = {
                "empresa": empresa, "url": "", "url_check": "",
                "grupo": "", "motivo": "", "obligatorio": False,
                "auto_login": False, "ts": "", "detectado": "",
            }
        items[k].update(kw)
        return items[k]

    for r in cargar_requeridos():
        it = _base(r.get("empresa") or "?")
        it.update({
            "url": r.get("url") or it.get("url") or "",
            "url_check": r.get("url_check") or it.get("url_check") or "",
            "grupo": r.get("grupo") or it.get("grupo") or "",
            "motivo": r.get("motivo") or it.get("motivo") or "",
            "obligatorio": bool(r.get("obligatorio")),
            "auto_login": bool(r.get("auto_login")),
            "ts": r.get("ts") or "",
            "detectado": r.get("detectado") or "runtime",
        })

    # portals del employer que ya estan en el provider de navegador
    for s in _leer(LOGIN_SITES_JSON, []):
        if not isinstance(s, dict) or not s.get("empresa"):
            continue
        it = _base(s["empresa"])
        it["url"] = it.get("url") or (s.get("urls") or [""])[0]
        it["grupo"] = it.get("grupo") or "directos"
        it["motivo"] = it.get("motivo") or (s.get("nota") or "")[:180]
        it["requiere_sesion_navegador"] = True

    # pendientes detectados en corridas anteriores
    for k, p in _pendientes().items():
        it = _base(p.get("empresa") or "?")
        it["url"] = it.get("url") or (p.get("url") or "")
        it["motivo"] = it.get("motivo") or p.get("motivo") or ""
        it["pendiente"] = p.get("motivo")
        it["pendiente_ts"] = p.get("ts")

    # estado de credencial + pendiente (cruzado otra vez por si venia solo)
    creds = cargar_credenciales()
    pends = _pendientes()
    for k, it in items.items():
        c = None
        for ck, cv in creds.items():
            if _norm(ck) == k or _norm(cv.get("empresa", "")) == k:
                c = cv
                break
        if c is None and k == "ane":
            c = credencial("ANE")   # fallback .env
        it["tiene_credencial"] = bool(c and c.get("usuario"))
        it["usuario"] = (c or {}).get("usuario") or (c or {}).get("fuente") or ""
        it["password_fuente"] = (c or {}).get("fuente", "json" if c else "")
        p = pends.get(k)
        if p:
            it["pendiente"] = p.get("motivo")
            it["pendiente_ts"] = p.get("ts")
        it.setdefault("pendiente", None)
        it["grupo_label"] = GRUPO_LABEL.get(it.get("grupo") or "",
                                            "Job board (agregador)")
    return sorted(items.values(), key=lambda x: (not x["obligatorio"],
                                                 x["empresa"].casefold()))


def faltantes(obligatorios: bool = True) -> List[dict]:
    """Sitios de la lista que todavia NO tienen credencial guardada."""
    return [s for s in lista_sitios_login()
            if (s["obligatorio"] or not obligatorios)
            and not s["tiene_credencial"]]


def resumen() -> dict:
    lst = lista_sitios_login()
    return {
        "total": len(lst),
        "obligatorios": sum(1 for s in lst if s["obligatorio"]),
        "con_credencial": sum(1 for s in lst if s["tiene_credencial"]),
        "pendientes": sum(1 for s in lst if s.get("pendiente")),
        "auto_login": sum(1 for s in lst if s.get("auto_login")),
    }
