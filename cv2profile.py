"""Analizador de CV -> perfil del candidato (contract con todo JobPulse).

Uso:
    python cv2profile.py MiCV.pdf              # analiza, muestra y sugiere cambios
    python cv2profile.py MiCV.pdf --save       # fusiona y guarda data/profile.json (con backup)
    python cv2profile.py MiCV.pdf --replace    # reemplaza titulos/skills/temas en vez de fusionar
    python cv2profile.py MiCV.pdf --out otropath.json

Tras guardar, ejecutan de nuevo:  python search.py --perfil  /  streamlit run app.py
Todo el analisis es 100% local (embeddings + lexicon); nada sale a internet.
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from core.cv_analysis import CVAnalyzer, extract_text, merge_profile
from core.profile import DEFAULT_PROFILE_PATH

if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") not in ("utf8",):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def show(proposed, actual=None):
    print("\n" + "=" * 60)
    print("ANALISIS DEL CV\n")
    print("Titulos sugeridos:")
    for t in proposed["titulos"][:8]:
        print(f"  - {t}")
    print(f"\nSkills detectadas ({len(proposed['skills'])}):")
    for chunk in [proposed["skills"][i:i+6] for i in range(0, min(30, len(proposed['skills'])), 6)]:
        print("  " + " | ".join(chunk))
    if len(proposed["skills"]) > 30:
        print(f"  ... y {len(proposed['skills'])-30} mas")
    print("\nTemas de scraping sugeridos (ES / EN):")
    for t in proposed["temas"]:
        print(f"  - {t['puesto']}  /  {t.get('puesto_en')}")
    print("\n--- Ficha ATS (lo que un ATS extraeria de este CV) ---")
    exp = proposed.get("experiencia_anios")
    print(f"  Experiencia     : {f'{exp:g} años' if exp else 'no detectada'}")
    print(f"  Educacion       : {proposed.get('educacion_nivel') or 'no detectada'}")
    certs = proposed.get("certificaciones") or []
    print(f"  Certificaciones : {', '.join(certs) if certs else '(ninguna)'}")
    idiomas = proposed.get("idiomas") or []
    if idiomas:
        print("  Idiomas         : " +
              ", ".join(f"{i['idioma']} ({i['nivel']})" for i in idiomas))
    else:
        print("  Idiomas         : (ninguno)")
    contacto = proposed.get("contacto") or {}
    if contacto:
        print("  Contacto        : " + " | ".join(
            f"{k}={v}" for k, v in contacto.items()))
    print("  Nota: pesos del score ATS (skills .40 / titulo .30 / exp .15 /"
          " educacion .075 / idiomas .075) van en profile.json ('pesos_ats').")
    if actual:
        print("\nCambios respecto al perfil actual:")
        for key in ("titulos", "skills"):
            nuevos = [t for t in proposed.get(key, []) if t and str(t).lower() not in
                      {str(x).lower() for x in actual.get(key, [])}]
            if nuevos:
                print(f"  + {key}: {', '.join(nuevos)}"
                      f"{'' if len(nuevos) <= 8 else f' (+{len(nuevos)-8} mas)'}")
        if proposed.get("temas") and proposed["temas"] != actual.get("temas"):
            print("  ~ temas: se reemplazaran por los derivados del CV")
    print("=" * 60)


def backup(path: Path):
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = path.with_name(path.stem + f"_backup_{stamp}.json")
    if path.exists():
        bk.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return bk
    return None


def main():
    p = argparse.ArgumentParser(description="CV -> perfil del candidato")
    p.add_argument("cv", help="Ruta del CV (pdf, docx, txt, md)")
    p.add_argument("--save", action="store_true", help="Guardar los cambios en el perfil")
    p.add_argument("--replace", action="store_true", help="Reemplazar titulos/skills/temas (default: fusionar)")
    p.add_argument("--out", default=None, help="Ruta del perfil destino (default: data/profile.json)")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("sentence_transformers.SentenceTransformer").setLevel(logging.ERROR)

    out_path = Path(args.out) if args.out else DEFAULT_PROFILE_PATH
    texto = extract_text(args.cv)
    if not texto.strip():
        print("[error] El CV no tiene texto legible (¿PDF escaneado sin OCR?).")
        sys.exit(1)

    analyzer = CVAnalyzer()
    proposed = analyzer.analyze(texto)

    actual_data = None
    if out_path.exists():
        try:
            actual_data = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] Perfil actual invalido ({out_path}): {exc}")
    show(proposed, actual_data)

    if actual_data:
        final = merge_profile(actual_data, proposed, replace=args.replace)
    else:
        final = {
            "nombre": re.sub(r"\s+", " ", (texto.splitlines() or ["Candidato"])[0]).strip()[:40] or "Candidato",
            "titulos": proposed["titulos"],
            "skills": proposed["skills"],
            "temas": proposed["temas"],
            "experiencia_anios": proposed.get("experiencia_anios", 0),
            "educacion_nivel": proposed.get("educacion_nivel", ""),
            "certificaciones": proposed.get("certificaciones", []),
            "idiomas": proposed.get("idiomas", []),
            "contacto": proposed.get("contacto", {}),
            "pesos": {"titulo": 1.0, "skills": 0.7, "tema": 0.5},
            "pesos_ats": {"skills": 0.40, "titulo": 0.30, "experiencia": 0.15,
                          "educacion": 0.075, "idiomas": 0.075},
            "filtros": {"dias": None, "fuentes": []},
        }

    if not args.save:
        print("\n(dry-run) Con --save se aplica al perfil" +
              (f" {out_path}" if actual_data else f" {out_path} (nuevo)"))
        return

    bk = backup(out_path)
    final['_comentario'] = "Perfil del candidato. Generado por cv2profile.py"
    out_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nPerfil guardado: {out_path}" + (f" (backup: {bk})" if bk else " (nuevo)"))
    print("Verifica el efecto con:")
    print("  python search.py --perfil")
    print("  streamlit run app.py")


if __name__ == "__main__":
    main()
