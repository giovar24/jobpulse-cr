"""Reporte: toda la informacion que JobPulse extrae de un CV.

Uso:  python reporte_cv.py <ruta_cv>
100% local; imprime el texto crudo + cada extractor con su valor.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") not in ("utf8",):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from core.cv_analysis import (
    CVAnalyzer, deacc, detect_certifications, detect_contact,
    detect_education_level, detect_experience_years, detect_languages,
    detect_seniority, extract_text,
)


def main():
    ruta = sys.argv[1] if len(sys.argv) > 1 else "data/demo_cv_en.txt"
    texto = extract_text(ruta)
    print("=" * 78)
    print(f"CV: {Path(ruta).name}  ({len(texto)} caracteres extraidos)")
    print("=" * 78)

    print("\n[1] TEXTO CRUDO (lo que el sistema lee)")
    print("-" * 78)
    print(texto)

    print("\n[2] EXTRACCIONES (regex puro, sin IA)")
    print("-" * 78)
    print(f"  Nombre (1a linea)      : {texto.splitlines()[0].strip()}")
    print(f"  Titular (2a linea)     : {texto.splitlines()[1].strip()}")
    print(f"  Seniority              : {detect_seniority(texto) or '(no detectado)'}")
    print(f"  Experiencia            : {detect_experience_years(texto):g} años")
    print(f"  Educacion              : {detect_education_level(texto) or '(no detectada)'}")
    print(f"  Certificaciones        : {', '.join(detect_certifications(texto)) or '(ninguna)'}")
    print(f"  Idiomas (regex)        : {', '.join(detect_languages(texto)) or '(ninguno)'}")
    contacto = detect_contact(texto)
    for k, v in contacto.items():
        print(f"  Contacto.{k:<14}: {v}")

    print("\n[3] IA LOCAL (embeddings sobre el encabezado del CV)")
    print("-" * 78)
    analyzer = CVAnalyzer()
    propuesto = analyzer.analyze(texto)
    print("  Titulos sugeridos (ES):")
    for t in propuesto["titulos"]:
        print(f"    - {t}")
    print(f"\n  Skills detectadas ({len(propuesto['skills'])}):")
    print("    " + " | ".join(propuesto["skills"]))
    print("\n  Temas de scraping (ES / EN):")
    for t in propuesto["temas"]:
        print(f"    - {t['puesto']}  /  {t.get('puesto_en')}")

    print("\n[4] REVISION IA (evidencia por frase: mejor coseno dentro del CV)")
    print("-" * 78)
    rev = analyzer.review(propuesto, texto)

    def _tabla(nombre, regs, minimo):
        print(f"  {nombre}")
        print(f"    {'CAMPO':<26} {'coseno':>6}  {'estado':<9} evidencia (mejor frase)")
        for k, r in sorted(regs.items(), key=lambda x: -x[1]["score"]):
            estado = "ok" if r["score"] >= minimo else "⚠ revisar"
            ev = r["evidencia"][:56] + ("..." if len(r["evidencia"]) > 56 else "")
            print(f"    {k:<26} {r['score']:>6.3f}  {estado:<9} {ev}")

    _tabla("TITULOS:", rev["titulos"], rev["min_titulo"])
    print()
    _tabla("SKILLS:", rev["skills"], rev["min_skill"])
    if rev["avisos"]:
        print(f"\n  Avisos ({len(rev['avisos'])}):")
        for a in rev["avisos"]:
            print(f"    ! {a}")
    else:
        print("\n  Sin avisos: todos los campos tienen evidencia en el CV.")

    print("\n[5] PERFIL COMPLETO QUE SE GUARDARIA EN profile.json")
    print("-" * 78)
    import json
    perfil = {
        "nombre": texto.splitlines()[0].strip().title(),
        "titulos": propuesto["titulos"],
        "skills": propuesto["skills"],
        "temas": propuesto["temas"],
        "experiencia_anios": propuesto["experiencia_anios"],
        "educacion_nivel": propuesto["educacion_nivel"],
        "seniority": propuesto["seniority"],
        "idiomas": propuesto["idiomas"],
        "certificaciones": propuesto["certificaciones"],
        "contacto": propuesto["contacto"],
    }
    print(json.dumps(perfil, ensure_ascii=False, indent=2))
    print("=" * 78)
    print("Para aplicarlo:  python cv2profile.py \"...\" --save")
    print("=" * 78)


if __name__ == "__main__":
    main()
