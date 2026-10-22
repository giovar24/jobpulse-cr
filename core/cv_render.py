# -*- coding: utf-8 -*-
"""Renderizado de CV en formato Harvard/ATS (markdown) a partir del perfil estructurado."""


def cv_harvard_markdown(datos: dict) -> str:
    """Genera texto en formato Harvard/ATS unicolumna y limpio."""
    if not isinstance(datos, dict):
        return "(sin datos de perfil)"

    # Si viene envuelto en canonical
    cv = datos.get("cv") if isinstance(datos.get("cv"), dict) else datos
    contacto = cv.get("contacto") or {}
    nombre = cv.get("nombre") or contacto.get("nombre") or "Candidato"
    resumen = cv.get("resumen") or ""
    educacion = cv.get("educacion") or []
    experiencia = cv.get("experiencia") or []
    proyectos = cv.get("proyectos") or []
    habilidades = cv.get("habilidades") or []
    certificaciones = cv.get("certificaciones") or []
    idiomas = cv.get("idiomas") or []

    lineas = [f"# {nombre}", ""]

    # Contacto en una línea
    c_partes = []
    if contacto.get("email"):
        c_partes.append(contacto["email"])
    if contacto.get("telefono"):
        c_partes.append(contacto["telefono"])
    if contacto.get("ubicacion"):
        c_partes.append(contacto["ubicacion"])
    if contacto.get("linkedin"):
        c_partes.append(contacto["linkedin"])
    if contacto.get("github"):
        c_partes.append(contacto["github"])
    if contacto.get("portafolio"):
        c_partes.append(contacto["portafolio"])
    if c_partes:
        lineas.append(" | ".join(c_partes))
        lineas.append("")

    if resumen:
        lineas += ["## Perfil Profesional", resumen, ""]

    if experiencia:
        lineas.append("## Experiencia Profesional")
        for exp in experiencia:
            emp = exp.get("empresa") or "Empresa"
            pue = exp.get("puesto") or "Puesto"
            ubi = exp.get("ubicacion") or ""
            ini = exp.get("inicio") or ""
            fin = exp.get("fin") or "Presente"
            f_str = f"{ini} – {fin}".strip(" –")
            sub = f"{ubi} | {f_str}".strip(" |")
            lineas.append(f"### {emp} — {pue}")
            if sub:
                lineas.append(sub)
            for logro in exp.get("logros") or []:
                lineas.append(f"- {logro}")
            lineas.append("")

    if educacion:
        lineas.append("## Educación")
        for ed in educacion:
            inst = ed.get("institucion") or "Institución"
            tit = ed.get("titulo") or "Título"
            camp = ed.get("campo") or ""
            t_completo = f"{tit} en {camp}" if camp else tit
            ubi = ed.get("ubicacion") or ""
            ini = ed.get("inicio") or ""
            fin = ed.get("fin") or ""
            f_str = f"{ini} – {fin}".strip(" –")
            sub = f"{ubi} | {f_str}".strip(" |")
            lineas.append(f"### {inst} — {t_completo}")
            if sub:
                lineas.append(sub)
            if ed.get("gpa"):
                lineas.append(f"- GPA / Nota: {ed['gpa']}")
            if ed.get("honores"):
                lineas.append(f"- Honores: {ed['honores']}")
            for cur in ed.get("cursos") or []:
                lineas.append(f"- Curso relevante: {cur}")
            lineas.append("")

    if proyectos:
        lineas.append("## Proyectos Destacados")
        for pr in proyectos:
            nombre_p = pr.get("nombre") or "Proyecto"
            tec = ", ".join(pr.get("tecnologias") or [])
            link = pr.get("link") or ""
            sub_p = f"Tecnologías: {tec}" if tec else ""
            if link:
                sub_p += f" | {link}" if sub_p else link
            lineas.append(f"### {nombre_p}")
            if sub_p:
                lineas.append(sub_p)
            if pr.get("descripcion"):
                lineas.append(pr["descripcion"])
            lineas.append("")

    if habilidades:
        lineas.append("## Habilidades Técnicas y Herramientas")
        lineas.append(", ".join(habilidades))
        lineas.append("")

    if certificaciones:
        lineas.append("## Certificaciones")
        for cert in certificaciones:
            lineas.append(f"- {cert}")
        lineas.append("")

    if idiomas:
        lineas.append("## Idiomas")
        for idm in idiomas:
            lineas.append(f"- {idm.get('idioma', '')}: {idm.get('nivel', '')}")
        lineas.append("")

    return "\n".join(lineas).strip()
