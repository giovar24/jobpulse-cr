"""ANE (ane.cr) por BARRIDO POR FECHA.

El buscador propio de ANE esta roto: devuelve 0 incluso busquando a mano
terminos de software, asi que NO se usa `?Empleos=`. En su lugar se recorre
el listado general `/Puesto?Pagina=N`, que ya viene ordenado mas-reciente-
primero, y se corta cuando las fechas caen fuera de la ventana (o se llega al
tope de paginas). El detalle se lee por HTTP sobre la sesion de Brave
(`ctx.request`), que es rapido y no navega a cada puesto.

CORTE EFECTIVO (2026-10-21). Medido en vivo: ANE publica ~130 vacantes por
dia, asi que el tope de 15 paginas (~120 tarjetas) cubre ~1,5 dias y el corte
por ventana de 30 dias NUNCA se alcanza. Lo que hacia "descargar de mas" era
otra cosa: cada corrida volvia a bajar el detalle de TODAS las tarjetas,
aunque ya estuvieran en `jobs_raw` (el descarte por duplicado ocurre despues,
en `save_jobs`). Por eso ahora:

  1. Las tarjetas cuyo job_id YA esta guardado con descripcion (fuente='ane')
     no se descargan: se saltan. Sin descripcion se reintentan, porque la
     corrida anterior pudo fallar el parseo y `save_jobs` rellena vacios.
  2. Si se encadenan `ANE_KNOWN_STREAK` (15) tarjetas ya guardadas, el
     listado ya entro en territorio de corridas anteriores -> se corta.
  3. Si una pagina no trae NINGUNA fecha legible se corta con un aviso: sin
     orden de fechas no se puede verificar la ventana y bajar detalles a
     ciegas es justo lo que se quiere evitar.

El tope `ANE_MAX_PAGES` sigue como limite duro de primera corrida (DB vacia)
o de corridas muy espaciadas.

Solo se extraen los campos que JobPulse persiste: titulo, empresa, ubicacion,
fecha, fecha de vencimiento, link, descripcion, requisitos y salario. El resto
del scraper standalone (checkpoints, CSV, historial, campos extra como
plazas/nivel/correo) NO se porto a proposito: aqui lo que interesa es lo crudo
para que el gate de relevancia (core/gate.py) decida que queda.
"""
import asyncio
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import date, timedelta
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from providers.base import BaseProvider, JobOffer
from core.dates import parse_date

logger = logging.getLogger("jobpulse.ane")

BASE = "https://www.ane.cr"

# Tarjetas ya guardadas encadenadas que hacen cortar el barrido. Una pagina
# autenticada trae 8, asi que 15 obliga a cruzar mas de una pagina de historial
# antes de confiar en que ya estamos en zona vieja (una vacante nueva
# intercalada reinicia la racha).
CONOCIDOS_SEGUIDOS = int(os.getenv("ANE_KNOWN_STREAK", "15"))


def _limpiar(t: str) -> str:
    return " ".join((t or "").split())


def _norm_lab(s: str) -> str:
    """Etiqueta sin acentos ni ':' final, para comparar sin romperse si ANE
    cambia 'Cómputo' por 'Computo'."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return _limpiar(s).rstrip(":").strip().lower()


# Secciones del detalle que ANE publica como <ul class="list-3"> DETRAS de un
# <strong>, SIN <p>. `_campo()` no las ve (busca <p><strong>et</strong>: valor),
# asi que antes se tiraban. Muestreado en 3 paginas reales (2026-10-20):
#   "Habilidades importantes..."  -> 5, 7 y 9 items
#   "Requisitos adicionales"      -> 0, 2 y 3 items
#   "Licencias de conducir"       -> 0, 2 y 1 items
# Ese texto era TODO el requisito que hoy sale vacio en 124/254 vacantes ANE.
_REQ_UL = (
    "habilidades importantes",
    "requisitos adicionales",
    "licencias de conducir",
    "idiomas",
    "programas de computo",
    "vehiculos",
)


class AneProvider(BaseProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.login_url = f"{BASE}/Cuenta/Login"
        self.jobs_url = f"{BASE}/Puesto"
        self.user = config.get("username")
        self.password = config.get("password")
        # Ruta de jobs.db para saber que vacantes ANE ya estan guardadas.
        # Sin ella el barrido funciona igual, solo que sin memoria (re-baja).
        self.db_path = config.get("db_path")

    async def fetch(self, search_term: str, location: str, pages: int, port: int,
                    dias: int = 30) -> List[JobOffer]:
        """Barrido por fecha: ignora `search_term` (el filtro lo hace el gate).

        Debe llamarse UNA sola vez por corrida, no por termino de busqueda.
        """
        return await asyncio.to_thread(self._sweep, location, pages, port, dias, search_term)

    # ------------------------------------------------------------ barrido
    def _sweep(self, location: str, pages: int, port: int, dias: int,
               search_term: str) -> List[JobOffer]:
        from playwright.sync_api import sync_playwright

        jobs: List[JobOffer] = []
        corte = date.today() - timedelta(days=dias)
        conocidos = self._conocidos()
        paginas_leidas = detalles = saltadas = 0
        motivo = ""

        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
                ctx = browser.contexts[0]

                if not self._asegurar_sesion(ctx):
                    logger.warning("ANE: sin sesion de detalle; barrido omitido.")
                    return []

                pagina, vacias = 1, 0
                seguidos = 0  # racha de conocidas, se mantiene entre paginas
                while pagina <= max(1, pages):
                    html = self._obtener(ctx, f"{self.jobs_url}?Pagina={pagina}")
                    cards = self._parse_listado(html) if html else []
                    if not cards:
                        vacias += 1
                        if vacias >= 2:
                            motivo = "dos paginas sin tarjetas"
                            break
                        pagina += 1
                        continue
                    vacias = 0
                    paginas_leidas += 1

                    cortar = False
                    con_fecha = 0
                    for c in cards:
                        iso = self._fecha_iso(c["fecha_raw"])
                        if iso:
                            con_fecha += 1
                            # Listado orden descendente: una fecha fuera de
                            # ventana significa que todo lo que sigue es mas
                            # viejo -> corta.
                            if date.fromisoformat(iso) < corte:
                                cortar, motivo = True, "fecha fuera de ventana"
                                break

                        # Ya guardada con descripcion: su detalle no aporta
                        # nada nuevo (save_jobs solo rellenaria vacios).
                        if c["pid"] in conocidos:
                            saltadas += 1
                            seguidos += 1
                            if seguidos >= CONOCIDOS_SEGUIDOS:
                                cortar = True
                                motivo = f"{CONOCIDOS_SEGUIDOS} seguidas ya guardadas"
                                break
                            continue
                        seguidos = 0

                        detalle = self._obtener(ctx, c["url"], requiere_sesion=True)
                        det = self._parse_detalle(detalle, c) if detalle else None
                        if det is None:
                            det = {}

                        jobs.append(JobOffer(
                            source="ane",
                            search_term=search_term,
                            job_id=c["pid"],
                            title=det.get("titulo") or c["titulo"],
                            company=det.get("empresa") or c["empresa"],
                            location=det.get("ubicacion") or c["ubicacion"] or location,
                            link=c["url"],
                            date_published=iso,
                            expires_at=det.get("vence") or "",
                            description=det.get("descripcion") or "",
                            requirements=det.get("requisitos") or "",
                            salary=det.get("salario") or "",
                        ))
                        detalles += 1
                        time.sleep(0.5)

                    if not con_fecha:
                        # Sin fechas no se puede verificar el orden ni la
                        # ventana: bajar detalles a ciegas es lo que se evita.
                        logger.warning(
                            "ANE: pagina %d sin fechas legibles (ej: %r); se "
                            "corta el barrido. Revisar el selector de fecha "
                            "(small.job-listing-description).",
                            pagina, cards[0]["fecha_raw"])
                        cortar = True
                        motivo = motivo or "pagina sin fecha"

                    if cortar:
                        break
                    pagina += 1
                    time.sleep(0.6)
                else:
                    motivo = "tope de paginas"
        except Exception as e:
            logger.error(f"ANE barrido fallido: {e}")
            motivo = motivo or f"error: {e}"

        logger.info(
            "ANE barrido: %d nuevas, %d ya guardadas saltadas, %d paginas "
            "leidas, %d detalles (tope %d paginas, ventana %d dias; corte: %s).",
            len(jobs), saltadas, paginas_leidas, detalles, pages, dias,
            motivo or "fin")
        return jobs

    def _conocidos(self) -> set:
        """job_ids ANE ya guardados CON descripcion (los que no hay que bajar).

        Los que estan pero sin descripcion se dejan fuera a proposito: se
        reintentan, porque `save_jobs` rellena campos vacios y la corrida
        anterior pudo fallar el parseo del detalle.
        """
        if not self.db_path or not os.path.exists(self.db_path):
            return set()
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                  timeout=5)
            try:
                return {str(r[0]) for r in con.execute(
                    "SELECT job_id FROM jobs_raw WHERE source = 'ane' "
                    "AND description IS NOT NULL AND description <> ''")}
            finally:
                con.close()
        except Exception as e:
            logger.warning("ANE: no se pudo leer jobs_raw (%s); barrido sin "
                           "memoria (se re-bajan los detalles).", e)
            return set()

    # ------------------------------------------------------------ sesion
    def _asegurar_sesion(self, ctx) -> bool:
        """True si hay sesion activa (o si logra iniciarla). El listado solo
        muestra enlaces reales a /Puesto/Detalles cuando hay sesion; sin ella
        apuntan a /Cuenta/RegistroUsuario."""
        html = self._obtener(ctx, self.jobs_url)
        if html and "Puesto/Detalles" in html:
            return True
        if not (self.user and self.password):
            logger.warning("ANE: sin credencial (pestaña Credenciales / CLI "
                           "credenciales.py --set \"ANE\") ni sesion activa en "
                           "Brave: barrido ANE omitido.")
            return False
        logger.info("ANE: iniciando sesion...")
        return self._login(ctx)

    def _login(self, ctx) -> bool:
        page = ctx.new_page()
        try:
            page.goto(self.login_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.fill("#NombreUsuarioLogin", self.user)
            page.fill("#ContrasennaLogin", self.password)
            page.click("button[type='submit']")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            ok = page.query_selector("#NombreUsuarioLogin") is None
            logger.info("ANE: login %s.", "correcto" if ok else "fallido")
            return ok
        except Exception as e:
            logger.error(f"ANE login fallo: {e}")
            return False
        finally:
            page.close()

    def _obtener(self, ctx, url: str, requiere_sesion: bool = False,
                 intentos: int = 3):
        """GET por HTTP a traves de la sesion de Brave (comparte cookies)."""
        for i in range(intentos):
            try:
                resp = ctx.request.get(url, timeout=30000, fail_on_status_code=False)
                if resp.status == 404:
                    return None
                if resp.status >= 500:
                    raise RuntimeError(f"HTTP {resp.status}")
                html = resp.text()
                if requiere_sesion and not self._sesion_valida(html):
                    raise RuntimeError("sesion ANE caducada (redirige a login)")
                return html
            except Exception as e:
                espera = (2, 5, 10)[min(i, 2)]
                logger.warning("ANE fallo %s (%s); reintento %d/%d en %ds",
                               url, e, i + 1, intentos, espera)
                if i == intentos - 1:
                    logger.error("ANE agota reintentos: %s", url)
                    return None
                time.sleep(espera)
        return None

    @staticmethod
    def _sesion_valida(html: str) -> bool:
        if "Cuenta/RegistroUsuario" in html:
            return False
        if 'id="loginForm"' in html:
            return False
        if "Iniciar sesión" in html and "dashboard-content-inner" not in html:
            return False
        if "dashboard-content-inner" in html:
            return True
        return True

    # ------------------------------------------------------------ parseo
    @staticmethod
    def _fecha_iso(txt: str) -> str:
        """'Publicado el viernes, 25 de septiembre del 2026' -> '2026-09-25'.

        Cambia ' del ' por ' de ' para que parse_date capture el anio explicito
        (evita que 'diciembre del 2025' herede el anio actual al borde de anio).
        Sirve igual para la fecha de vencimiento del detalle
        ('viernes, 20 de noviembre del 2026').
        """
        if not txt:
            return ""
        t = txt.replace("Publicado el ", "").replace(" del ", " de ").strip()
        iso = parse_date(t)
        try:
            date.fromisoformat(iso[:10])
            return iso[:10]
        except Exception:
            return ""

    def _parse_listado(self, html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        cards: List[Dict[str, str]] = []
        for job in soup.select("div.job-listing"):
            box = job.select_one("div.job-listing-description")
            if not box:
                continue
            titulo = job.select_one("h3.job-listing-title")
            empresa = job.select_one("h4.job-listing-company")
            small = job.select_one("small.job-listing-description")
            link = job.select_one("a[href*='/Puesto/Detalles']")
            href = link.get("href", "") if link else ""
            m = re.search(r"Puesto=(\d+)", href)
            if not (m and titulo):
                continue
            url = href if href.startswith("http") else BASE + href.split("&")[0]
            ubic = [_limpiar(li.get_text()) for li in box.select("ul > li")]
            cards.append({
                "pid": m.group(1),
                "titulo": _limpiar(titulo.get_text()),
                "empresa": _limpiar(empresa.get_text()) if empresa else "",
                "fecha_raw": _limpiar(small.get_text()) if small else "",
                "ubicacion": "; ".join(dict.fromkeys(x for x in ubic if x)),
                "url": url,
            })
        return cards

    @staticmethod
    def _campo(cont, label: str) -> str:
        """<p><strong>ETIQUETA</strong>: valor (a veces en <span>)."""
        if cont is None:
            return ""
        for p in cont.find_all("p"):
            s = p.find("strong")
            if s and _limpiar(s.get_text()).rstrip(":").strip().lower() == label.lower():
                txt = _limpiar(p.get_text(" ", strip=True))
                return txt.split(":", 1)[1].strip() if ":" in txt else ""
        return ""

    @staticmethod
    def _lista_tras(cont, label: str) -> List[str]:
        """<strong>ETIQUETA</strong>: ... <ul class="list-3"><li>...</li></ul>"""
        if cont is None:
            return []
        for s in cont.find_all("strong"):
            if label.lower() in _limpiar(s.get_text()).lower():
                ul = s.find_next("ul", class_="list-3")
                if ul:
                    return [_limpiar(li.get_text()) for li in ul.find_all("li")
                            if _limpiar(li.get_text())]
        return []

    @staticmethod
    def _secciones_ul(cont) -> List[tuple]:
        """TODAS las secciones <strong> + <ul class="list-3"> de una columna.

        Devuelve [(etiqueta_normalizada, etiqueta_visible, items)]. A
        diferencia de _lista_tras no busca un label concreto (que por
        substring puede pegarse a la seccion equivocada) ni exige <p>:
        recorre cada <strong> y se queda con el <ul> que lo sigue INMEDIATO
        (si hay otro <strong> en medio, ese <ul> pertenece al otro).
        """
        out: List[tuple] = []
        vistos = set()
        if cont is None:
            return out
        for st in cont.find_all("strong"):
            visible = _limpiar(st.get_text()).rstrip(":").strip()
            lab = _norm_lab(visible)
            if not lab or lab in vistos:
                continue
            ul = st.find_next("ul", class_="list-3")
            if ul is None or ul.find_previous("strong") is not st:
                continue
            items = [i for i in (_limpiar(li.get_text()) for li in ul.find_all("li")) if i]
            if items:
                vistos.add(lab)
                out.append((lab, visible, items))
        return out

    def _parse_detalle(self, html: str, card: Dict[str, str]):
        """Devuelve solo lo que persiste JobPulse, o None si no es un detalle."""
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("div.dashboard-content-inner")
        if root is None:
            return None  # muro de login
        cajas = root.select("div.row > div.col-xl-6")
        izq = der = None
        if len(cajas) >= 1:
            izq = cajas[0].select_one("div.dashboard-box > div.content.with-padding")
        if len(cajas) >= 2:
            der = cajas[1].select_one("div.dashboard-box > div.content.with-padding")

        h2 = root.select_one("div.content.with-padding h2.text-center")
        sm = root.select_one("div.content.with-padding p.text-center small")

        # "Funciones principales" es la descripcion. "Detalles del salario" es
        # texto libre del empleador que en varios puestos ES la descripcion
        # completa (Puesto=120575: funciones="Soldadura y construcción." y el
        # detalle trae "Se necesita operarios de construcción. Que sepa
        # soldar. Responsable. Licencia B1 al día"): se agrega si difiere.
        desc = self._campo(der, "Funciones principales del empleo") \
            or self._campo(der, "Detalle del horario")
        extra = self._campo(der, "Detalles del salario")
        if extra and extra not in desc:
            desc = f"{desc} | {extra}".strip(" |") if desc else extra

        # Requisitos: el <p> etiquetado + TODAS las secciones que ANE publica
        # como <ul class="list-3"> (habilidades, requisitos adicionales,
        # licencias, idiomas, programas, vehiculos). Antes solo se leia el
        # <p>: 124/254 vacantes ANE quedaban sin requisitos.
        reqs = [self._campo(der, "Requisitos adicionales para el empleo")]
        for lab_n, lab_txt, items in self._secciones_ul(der):
            if lab_n.startswith(_REQ_UL):
                reqs.append(f"{lab_txt}: " + ", ".join(items))
        reqs = [r for r in reqs if r]

        return {
            "titulo": _limpiar(h2.get_text()) if h2 else card["titulo"],
            "empresa": _limpiar(sm.get_text()) if sm else card["empresa"],
            "ubicacion": "; ".join(self._lista_tras(izq, "Ubicaciones del empleo"))
            or card["ubicacion"],
            "descripcion": desc,
            "requisitos": ". ".join(reqs),
            "salario": self._campo(der, "Salario"),
            # ANE publica en el detalle "Fecha de vencimiento de la vacante"
            # (ej. "viernes, 20 de noviembre del 2026"). Es la fecha real de
            # vigencia: con ella se poda la vacante aunque tenga < 30 dias.
            "vence": self._fecha_iso(
                self._campo(der, "Fecha de vencimiento de la vacante")),
        }
