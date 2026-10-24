# -*- coding: utf-8 -*-
"""
jobpulse_v15.configuration_window - Ventana de configuraci\u00f3n global de JobPulse CR.

Pesta\u00f1as:
1. IA Provider - Configuraci\u00f3n de proveedor LLM (OpenRouter, NVIDIA, Google, Ollama, LM Studio, Custom/llama.cpp)
2. Credenciales/Login - Gesti\u00f3n de sitios que requieren login (ANE, Indeed, LinkedIn, etc.)
3. UI Preferences - Tema, colores, fuente
4. Scraping - Puerto Brave CDP, timeouts, p\u00e1ginas por defecto
"""

from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from jobpulse_v15.credenciales import (
    credencial,
    lista_sitios_login,
    upsert_credencial,
    borrar_credencial,
    marcar_auto_login,
    agregar_sitio_login,
    quitar_sitio_login,
)
from jobpulse_v15.db import Database
from jobpulse_v15.repository import Repository

# IA Providers disponibles
IA_PROVIDERS = {
    "openrouter": {"nombre": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "modelo_def": "~openai/gpt-latest"},
    "nvidia": {"nombre": "NVIDIA NIM", "base_url": "https://integrate.api.nvidia.com/v1", "modelo_def": "openai/gpt-oss-20b"},
    "google": {"nombre": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "modelo_def": "gemini-2.5-flash"},
    "ollama": {"nombre": "Ollama (local)", "base_url": "http://localhost:11434/v1", "modelo_def": "auto"},
    "lmstudio": {"nombre": "LM Studio (local)", "base_url": "http://localhost:1234/v1", "modelo_def": "auto"},
    "custom": {"nombre": "Custom (llama.cpp, vLLM, etc.)", "base_url": "", "modelo_def": ""},
}

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".jobpulse_v15", "ui_config.json")


def _load_ui_config() -> Dict[str, Any]:
    """Carga configuraci\u00f3n de UI desde archivo."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_ui_config(config: Dict[str, Any]) -> None:
    """Guarda configuraci\u00f3n de UI en archivo."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class ConfigurationWindow:
    """Ventana modal de configuraci\u00f3n global."""

    def __init__(self, parent: tk.Tk, repository: Repository) -> None:
        self.parent = parent
        self.repository = repository
        self.db = Database()
        self.db.initialize()

        self.window = tk.Toplevel(parent)
        self.window.title("Configuraci\u00f3n - JobPulse CR 1.5")
        self.window.geometry("850x650")
        self.window.minsize(750, 550)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self._build_notebook()
        self._load_all_settings()

    def _build_notebook(self) -> None:
        """Construye el notebook con pesta\u00f1as."""
        main_frame = ttk.Frame(self.window, padding=10)
        main_frame.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        # Pesta\u00f1a 1: IA Provider
        self.tab_ia = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_ia, text="\U0001F916 IA Provider")
        self._build_ia_tab()

        # Pesta\u00f1a 2: Credenciales/Login
        self.tab_cred = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_cred, text="\U0001F511 Credenciales / Login")
        self._build_cred_tab()

        # Pesta\u00f1a 3: UI Preferences
        self.tab_ui = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_ui, text="\U0001F3A8 UI / Apariencia")
        self._build_ui_tab()

        # Pesta\u00f1a 4: Scraping
        self.tab_scrape = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_scrape, text="\U0001F50D Scraping")
        self._build_scrape_tab()

        # Botones inferiores
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(btn_frame, text="Guardar todo", command=self._save_all).pack(side="right")
        ttk.Button(btn_frame, text="Cerrar", command=self.close).pack(side="right", padx=(0, 8))

    # ============================================================
    # Pesta\u00f1a 1: IA Provider
    # ============================================================
    def _build_ia_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_ia, text="Configuraci\u00f3n de IA Externa (LLM)", padding=10)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        # Proveedor
        ttk.Label(frame, text="Proveedor:").grid(row=0, column=0, sticky="w", pady=4)
        self.ia_provider_var = tk.StringVar()
        provider_combo = ttk.Combobox(
            frame,
            textvariable=self.ia_provider_var,
            values=list(IA_PROVIDERS.keys()),
            state="readonly",
            width=30,
        )
        provider_combo.grid(row=0, column=1, sticky="ew", pady=4, padx=(6, 0))
        provider_combo.bind("<<ComboboxSelected>>", self._on_provider_change)

        # Base URL
        ttk.Label(frame, text="Base URL:").grid(row=1, column=0, sticky="w", pady=4)
        self.ia_base_url_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.ia_base_url_var, width=50).grid(
            row=1, column=1, sticky="ew", pady=4, padx=(6, 0)
        )

        # API Key
        ttk.Label(frame, text="API Key:").grid(row=2, column=0, sticky="w", pady=4)
        self.ia_api_key_var = tk.StringVar()
        self.ia_key_entry = ttk.Entry(frame, textvariable=self.ia_api_key_var, width=50, show="*")
        self.ia_key_entry.grid(row=2, column=1, sticky="ew", pady=4, padx=(6, 0))
        ttk.Button(frame, text="Mostrar/Ocultar", command=self._toggle_key_visibility).grid(
            row=2, column=2, padx=(6, 0)
        )

        # Modelo
        ttk.Label(frame, text="Modelo:").grid(row=3, column=0, sticky="w", pady=4)
        self.ia_model_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.ia_model_var, width=50).grid(
            row=3, column=1, sticky="ew", pady=4, padx=(6, 0)
        )
        ttk.Button(frame, text="Listar modelos", command=self._list_models).grid(
            row=3, column=2, padx=(6, 0)
        )

        # Test connection
        ttk.Button(frame, text="Probar conexi\u00f3n", command=self._test_ia_connection).grid(
            row=4, column=1, sticky="w", pady=(10, 0), padx=(6, 0)
        )

        # Info
        info_text = (
            "Variables de entorno equivalentes: IA_PROVEEDOR, IA_BASE_URL, IA_MODEL, IA_API_KEY.\n"
            "Para llama.cpp local: IA_PROVEEDOR=custom, IA_BASE_URL=http://localhost:8080/v1"
        )
        ttk.Label(frame, text=info_text, foreground="gray", wraplength=600, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )

    def _on_provider_change(self, event: tk.Event = None) -> None:
        """Actualiza campos base_url y modelo por defecto al cambiar proveedor."""
        provider = self.ia_provider_var.get()
        if provider in IA_PROVIDERS:
            preset = IA_PROVIDERS[provider]
            self.ia_base_url_var.set(preset["base_url"])
            if preset["modelo_def"]:
                self.ia_model_var.set(preset["modelo_def"])
            # Para custom, limpiar modelo
            if provider == "custom":
                self.ia_model_var.set("")

    def _toggle_key_visibility(self) -> None:
        current = self.ia_key_entry.cget("show")
        self.ia_key_entry.config(show="" if current == "*" else "*")

    def _list_models(self) -> None:
        """Lista modelos disponibles en el endpoint."""
        import asyncio
        from jobpulse_v15.ia_api import IAClient

        async def _do_list():
            try:
                client = IAClient()
                models = await client.listar_modelos()
                if models:
                    msg = "\n".join([f"- {m}" for m in models[:30]])
                    if len(models) > 30:
                        msg += f"\n... y {len(models) - 30} m\u00e1s"
                else:
                    msg = "No se encontraron modelos."
                self.window.after(0, lambda: messagebox.showinfo("Modelos disponibles", msg, parent=self.window))
            except Exception as e:
                self.window.after(0, lambda: messagebox.showerror("Error", str(e), parent=self.window))

        threading.Thread(target=lambda: asyncio.run(_do_list()), daemon=True).start()

    def _test_ia_connection(self) -> None:
        """Prueba la conexi\u00f3n IA con un prompt simple."""
        import asyncio
        from jobpulse_v15.ia_api import IAClient

        async def _do_test():
            try:
                config = {}
                if self.ia_provider_var.get():
                    config["proveedor"] = self.ia_provider_var.get()
                if self.ia_base_url_var.get():
                    config["base_url"] = self.ia_base_url_var.get()
                if self.ia_model_var.get():
                    config["modelo"] = self.ia_model_var.get()
                if self.ia_api_key_var.get():
                    config["api_key"] = self.ia_api_key_var.get()

                client = IAClient(config)
                resp = await client.chat_json(
                    messages=[
                        {"role": "system", "content": "Responde SOLO JSON."},
                        {"role": "user", "content": "Di hola en JSON: {\"ok\": true}"}
                    ],
                    schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                    temperature=0.1,
                    max_tokens=50
                )
                self.window.after(0, lambda: messagebox.showinfo(
                    "Conexi\u00f3n OK", f"Respuesta: {resp['content']}", parent=self.window))
            except Exception as e:
                self.window.after(0, lambda: messagebox.showerror("Error de conexi\u00f3n", str(e), parent=self.window))

        threading.Thread(target=lambda: asyncio.run(_do_test()), daemon=True).start()

    # ============================================================
    # Pesta\u00f1a 2: Credenciales / Login
    # ============================================================
    def _build_cred_tab(self) -> None:
        # Toolbar
        toolbar = ttk.Frame(self.tab_cred)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Agregar sitio", command=self._cred_add_site).pack(side="left")
        ttk.Button(toolbar, text="Eliminar sitio", command=self._cred_remove_site).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Actualizar lista", command=self._refresh_cred_list).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Login manual (abre Brave)", command=self._cred_login_manual).pack(side="right")

        # Tabla de sitios
        table_frame = ttk.Frame(self.tab_cred)
        table_frame.pack(fill="both", expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = ("empresa", "usuario", "sso", "auto", "obligatorio", "url")
        self.cred_table = ttk.Treeview(
            table_frame, columns=columns, show="tree headings", selectmode="browse"
        )
        self.cred_table.heading("#0", text="Empresa")
        self.cred_table.column("#0", width=150, minwidth=100)
        headers = {
            "empresa": "Empresa",
            "usuario": "Usuario",
            "sso": "SSO",
            "auto": "Auto-login",
            "obligatorio": "Obligatorio",
            "url": "URL Login",
        }
        widths = {"empresa": 180, "usuario": 150, "sso": 80, "auto": 80, "obligatorio": 80, "url": 250}
        for col in columns:
            self.cred_table.heading(col, text=headers[col])
            self.cred_table.column(col, width=widths[col], minwidth=60)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.cred_table.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.cred_table.xview)
        self.cred_table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.cred_table.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # Formulario edici\u00f3n
        form_frame = ttk.LabelFrame(self.tab_cred, text="Editar credencial", padding=10)
        form_frame.pack(fill="x", pady=(10, 0))
        form_frame.columnconfigure(1, weight=1)
        form_frame.columnconfigure(3, weight=1)

        ttk.Label(form_frame, text="Empresa:").grid(row=0, column=0, sticky="w", pady=4)
        self.cred_empresa_var = tk.StringVar()
        ttk.Entry(form_frame, textvariable=self.cred_empresa_var, width=30).grid(
            row=0, column=1, sticky="ew", padx=(6, 0), pady=4
        )

        ttk.Label(form_frame, text="Usuario/Email:").grid(row=0, column=2, sticky="w", pady=4, padx=(12, 0))
        self.cred_usuario_var = tk.StringVar()
        ttk.Entry(form_frame, textvariable=self.cred_usuario_var, width=30).grid(
            row=0, column=3, sticky="ew", padx=(6, 0), pady=4
        )

        ttk.Label(form_frame, text="Password:").grid(row=1, column=0, sticky="w", pady=4)
        self.cred_password_var = tk.StringVar()
        self.cred_pwd_entry = ttk.Entry(form_frame, textvariable=self.cred_password_var, width=30, show="*")
        self.cred_pwd_entry.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=4)
        ttk.Button(form_frame, text="Mostrar", command=lambda: self.cred_pwd_entry.config(
            show="" if self.cred_pwd_entry.cget("show") == "*" else "*")).grid(
            row=1, column=2, padx=(6, 0)
        )

        ttk.Label(form_frame, text="SSO:").grid(row=1, column=2, sticky="w", pady=4, padx=(12, 0))
        self.cred_sso_var = tk.StringVar()
        ttk.Combobox(form_frame, textvariable=self.cred_sso_var, values=("", "google"), state="readonly", width=15).grid(
            row=1, column=3, sticky="ew", padx=(6, 0), pady=4
        )

        ttk.Label(form_frame, text="Auto-login:").grid(row=2, column=0, sticky="w", pady=4)
        self.cred_auto_var = tk.BooleanVar()
        ttk.Checkbutton(form_frame, variable=self.cred_auto_var, text="Activado").grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=4
        )

        ttk.Label(form_frame, text="Obligatorio:").grid(row=2, column=2, sticky="w", padx=(12, 0))
        self.cred_obligatorio_var = tk.BooleanVar()
        ttk.Checkbutton(form_frame, variable=self.cred_obligatorio_var).grid(
            row=2, column=3, sticky="w", padx=(6, 0), pady=4
        )

        ttk.Label(form_frame, text="URL Login:").grid(row=3, column=0, sticky="w", pady=4)
        self.cred_url_var = tk.StringVar()
        ttk.Entry(form_frame, textvariable=self.cred_url_var, width=60).grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=4
        )

        btns = ttk.Frame(form_frame)
        btns.grid(row=4, column=0, columnspan=4, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Guardar", command=self._cred_save).pack(side="right")
        ttk.Button(btns, text="Eliminar", command=self._cred_delete).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Limpiar form", command=self._cred_clear_form).pack(side="right", padx=(0, 8))

        self.cred_table.bind("<<TreeviewSelect>>", self._on_cred_select)
        self._refresh_cred_list()

    def _refresh_cred_list(self) -> None:
        for item in self.cred_table.get_children():
            self.cred_table.delete(item)
        sitios = lista_sitios_login()
        for s in sitios:
            self.cred_table.insert(
                "", "end",
                iid=s["empresa"],
                text=s["empresa"],
                values=(
                    s["empresa"],
                    s.get("usuario", ""),
                    s.get("sso", ""),
                    "S\u00ed" if s.get("auto_login") else "No",
                    "S\u00ed" if s.get("obligatorio") else "No",
                    s.get("url", ""),
                )
            )

    def _on_cred_select(self, event: tk.Event) -> None:
        sel = self.cred_table.selection()
        if not sel:
            return
        empresa = sel[0]
        sitios = lista_sitios_login()
        s = next((x for x in sitios if x["empresa"] == empresa), None)
        if s:
            self.cred_empresa_var.set(s["empresa"])
            self.cred_usuario_var.set(s.get("usuario", ""))
            self.cred_password_var.set(s.get("password", ""))
            self.cred_sso_var.set(s.get("sso", ""))
            self.cred_auto_var.set(s.get("auto_login", False))
            self.cred_obligatorio_var.set(s.get("obligatorio", False))
            self.cred_url_var.set(s.get("url", ""))

    def _cred_clear_form(self) -> None:
        self.cred_empresa_var.set("")
        self.cred_usuario_var.set("")
        self.cred_password_var.set("")
        self.cred_sso_var.set("")
        self.cred_auto_var.set(False)
        self.cred_obligatorio_var.set(False)
        self.cred_url_var.set("")
        self.cred_table.selection_remove(self.cred_table.selection())

    def _cred_save(self) -> None:
        empresa = self.cred_empresa_var.get().strip()
        if not empresa:
            messagebox.showwarning("Empresa requerida", "Ingrese el nombre de la empresa.", parent=self.window)
            return
        try:
            upsert_credencial(
                empresa=empresa,
                usuario=self.cred_usuario_var.get().strip() or None,
                password=self.cred_password_var.get() or None,
                sso=self.cred_sso_var.get().strip() or None,
                auto_login=self.cred_auto_var.get(),
                url=self.cred_url_var.get().strip() or None,
                obligatorio=self.cred_obligatorio_var.get(),
            )
            if self.cred_auto_var.get():
                marcar_auto_login(empresa, True)
            else:
                marcar_auto_login(empresa, False)
            self._refresh_cred_list()
            self._cred_clear_form()
            messagebox.showinfo("Guardado", f"Credencial de {empresa} guardada.", parent=self.window)
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self.window)

    def _cred_delete(self) -> None:
        empresa = self.cred_empresa_var.get().strip()
        if not empresa:
            return
        if messagebox.askyesno("Confirmar", f"Eliminar credencial de {empresa}?", parent=self.window):
            borrar_credencial(empresa)
            self._refresh_cred_list()
            self._cred_clear_form()

    def _cred_add_site(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("Agregar sitio a lista de login")
        dialog.geometry("450x250")
        dialog.transient(self.window)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Empresa:").grid(row=0, column=0, sticky="w", padx=10, pady=10)
        emp_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=emp_var, width=40).grid(row=0, column=1, padx=10, pady=10)
        
        ttk.Label(dialog, text="URL Login (opcional):").grid(row=1, column=0, sticky="w", padx=10, pady=10)
        url_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=url_var, width=40).grid(row=1, column=1, padx=10, pady=10)
        
        ttk.Label(dialog, text="Grupo (opcional):").grid(row=2, column=0, sticky="w", padx=10, pady=10)
        grp_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=grp_var, width=40).grid(row=2, column=1, padx=10, pady=10)
        
        obl_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(dialog, text="Obligatorio (sin login no se scrapea)", variable=obl_var).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=10, pady=10
        )
        
        def _save():
            e = emp_var.get().strip()
            if not e:
                messagebox.showwarning("Requerido", "Nombre de empresa requerido.", parent=dialog)
                return
            try:
                agregar_sitio_login(e, url_var.get().strip() or None, grp_var.get().strip() or None, obl_var.get())
                self._refresh_cred_list()
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=dialog)
        
        ttk.Button(dialog, text="Guardar", command=_save).grid(row=4, column=0, columnspan=2, pady=20)

    def _cred_remove_site(self) -> None:
        sel = self.cred_table.selection()
        if not sel:
            return
        empresa = sel[0]
        if messagebox.askyesno("Confirmar", f"Quitar sitio {empresa} de la lista de login?", parent=self.window):
            quitar_sitio_login(empresa)
            self._refresh_cred_list()

    def _cred_login_manual(self) -> None:
        """Abre Brave con CDP para login manual."""
        import subprocess
        import webbrowser
        from jobpulse_v15.browser_manager import BraveManager
        
        mgr = BraveManager()
        try:
            mgr.start_brave()
            sitios = lista_sitios_login()
            for s in sitios:
                if s.get("url"):
                    webbrowser.open(s["url"])
            messagebox.showinfo(
                "Login manual",
                "Se abri\u00f3 Brave con CDP en puerto 9222.\n"
                "Inicia sesi\u00f3n en cada pesta\u00f1a y confirma aqu\u00ed cuando termines.\n"
                "La sesi\u00f3n quedar\u00e1 guardada para los scrapers.",
                parent=self.window
            )
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo iniciar Brave: {e}", parent=self.window)

    # ============================================================
    # Pesta\u00f1a 3: UI Preferences
    # ============================================================
    def _build_ui_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_ui, text="Apariencia y comportamiento", padding=10)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        # Tema
        ttk.Label(frame, text="Tema:").grid(row=0, column=0, sticky="w", pady=4)
        self.ui_theme_var = tk.StringVar()
        theme_combo = ttk.Combobox(frame, textvariable=self.ui_theme_var,
                                   values=("claro", "oscuro", "sistema"), state="readonly", width=15)
        theme_combo.grid(row=0, column=1, sticky="w", padx=(6, 0), pady=4)
        theme_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_theme())

        # Tama\u00f1o fuente
        ttk.Label(frame, text="Tama\u00f1o fuente base:").grid(row=1, column=0, sticky="w", pady=4)
        self.ui_font_size_var = tk.IntVar()
        ttk.Spinbox(frame, from_=8, to=16, textvariable=self.ui_font_size_var, width=5).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=4
        )

        # Guardar geometr\u00eda de ventanas
        self.ui_save_geom_var = tk.BooleanVar()
        ttk.Checkbutton(frame, text="Recordar tama\u00f1o y posici\u00f3n de ventanas",
                        variable=self.ui_save_geom_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)

        # Confirmar al cerrar
        self.ui_confirm_exit_var = tk.BooleanVar()
        ttk.Checkbutton(frame, text="Confirmar al cerrar la aplicaci\u00f3n",
                        variable=self.ui_confirm_exit_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

        # Bot\u00f3n aplicar tema
        ttk.Button(frame, text="Aplicar tema ahora", command=self._apply_theme).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

    def _apply_theme(self) -> None:
        """Aplica el tema seleccionado (b\u00e1sico: solo colores ttkthemes si est\u00e1 disponible)."""
        try:
            import ttkthemes
            theme = self.ui_theme_var.get()
            if theme == "oscuro":
                self.window.set_theme("equilux")
            elif theme == "claro":
                self.window.set_theme("arc")
        except ImportError:
            pass  # ttkthemes no instalado
        except Exception:
            pass

    # ============================================================
    # Pesta\u00f1a 4: Scraping
    # ============================================================
    def _build_scrape_tab(self) -> None:
        frame = ttk.LabelFrame(self.tab_scrape, text="Configuraci\u00f3n de scraping", padding=10)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        # Puerto Brave CDP
        ttk.Label(frame, text="Puerto CDP de Brave:").grid(row=0, column=0, sticky="w", pady=4)
        self.scrape_port_var = tk.IntVar(value=9222)
        ttk.Spinbox(frame, from_=1024, to=65535, textvariable=self.scrape_port_var, width=10).grid(
            row=0, column=1, sticky="w", padx=(6, 0), pady=4
        )

        # Timeout por defecto
        ttk.Label(frame, text="Timeout HTTP (seg):").grid(row=1, column=0, sticky="w", pady=4)
        self.scrape_timeout_var = tk.IntVar(value=30)
        ttk.Spinbox(frame, from_=5, to=120, textvariable=self.scrape_timeout_var, width=10).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=4
        )

        # P\u00e1ginas por defecto
        ttk.Label(frame, text="P\u00e1ginas por defecto (ANE):").grid(row=2, column=0, sticky="w", pady=4)
        self.scrape_ane_pages_var = tk.IntVar(value=15)
        ttk.Spinbox(frame, from_=1, to=50, textvariable=self.scrape_ane_pages_var, width=10).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=4
        )

        ttk.Label(frame, text="P\u00e1ginas por defecto (CINDE):").grid(row=3, column=0, sticky="w", pady=4)
        self.scrape_cinde_pages_var = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=20, textvariable=self.scrape_cinde_pages_var, width=10).grid(
            row=3, column=1, sticky="w", padx=(6, 0), pady=4
        )

        ttk.Label(frame, text="P\u00e1ginas por defecto (Indeed/LinkedIn):").grid(row=4, column=0, sticky="w", pady=4)
        self.scrape_board_pages_var = tk.IntVar(value=2)
        ttk.Spinbox(frame, from_=1, to=10, textvariable=self.scrape_board_pages_var, width=10).grid(
            row=4, column=1, sticky="w", padx=(6, 0), pady=4
        )

        # Delay entre peticiones
        ttk.Label(frame, text="Delay entre peticiones (seg):").grid(row=5, column=0, sticky="w", pady=4)
        self.scrape_delay_var = tk.DoubleVar(value=0.5)
        ttk.Spinbox(frame, from_=0, to=10, increment=0.5, textvariable=self.scrape_delay_var, width=10).grid(
            row=5, column=1, sticky="w", padx=(6, 0), pady=4
        )

        # Ventana de d\u00edas por defecto
        ttk.Label(frame, text="Ventana de d\u00edas (ANE):").grid(row=6, column=0, sticky="w", pady=4)
        self.scrape_days_var = tk.IntVar(value=30)
        ttk.Spinbox(frame, from_=1, to=365, textvariable=self.scrape_days_var, width=10).grid(
            row=6, column=1, sticky="w", padx=(6, 0), pady=4
        )

    # ============================================================
    # Carga / Guardado de configuraciones
    # ============================================================
    def _load_all_settings(self) -> None:
        # IA Provider (desde .env)
        from dotenv import load_dotenv
        load_dotenv()
        self.ia_provider_var.set(os.getenv("IA_PROVEEDOR", "custom"))
        self.ia_base_url_var.set(os.getenv("IA_BASE_URL", "http://127.0.0.1:8081/v1"))
        self.ia_model_var.set(os.getenv("IA_MODEL", "qwen3.5-2b"))
        self.ia_api_key_var.set(os.getenv("IA_API_KEY", "sk-no-key-needed"))

        # UI prefs (desde archivo)
        ui_cfg = _load_ui_config()
        self.ui_theme_var.set(ui_cfg.get("theme", "sistema"))
        self.ui_font_size_var.set(ui_cfg.get("font_size", 10))
        self.ui_save_geom_var.set(ui_cfg.get("save_geometry", True))
        self.ui_confirm_exit_var.set(ui_cfg.get("confirm_exit", True))

        # Scraping (valores por defecto razonables)
        self.scrape_port_var.set(9222)
        self.scrape_timeout_var.set(30)
        self.scrape_ane_pages_var.set(15)
        self.scrape_cinde_pages_var.set(10)
        self.scrape_board_pages_var.set(2)
        self.scrape_delay_var.set(0.5)
        self.scrape_days_var.set(30)

        # Credenciales
        self._refresh_cred_list()

    def _save_all(self) -> None:
        """Guarda todas las configuraciones."""
        # IA Provider -> .env (solo lectura, no escribimos .env aqu\u00ed)
        # El usuario debe editar .env manualmente o usar CLI
        
        # UI prefs -> archivo JSON
        ui_cfg = {
            "theme": self.ui_theme_var.get(),
            "font_size": self.ui_font_size_var.get(),
            "save_geometry": self.ui_save_geom_var.get(),
            "confirm_exit": self.ui_confirm_exit_var.get(),
        }
        _save_ui_config(ui_cfg)
        
        # Scraping -> podr\u00eda guardarse en .env o DB, por ahora solo feedback
        messagebox.showinfo(
            "Guardado",
            "Preferencias de UI guardadas.\n\n"
            "Nota: La configuraci\u00f3n de IA (proveedor, URL, modelo, key) "
            "debe editarse en el archivo .env manualmente.\n"
            "Las preferencias de scraping se usan en memoria durante la sesi\u00f3n.",
            parent=self.window
        )

    def close(self) -> None:
        self.window.destroy()