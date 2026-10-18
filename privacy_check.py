"""Auditoria de privacidad: revisa los archivos TRACKED por git antes de commitear.

Busca datos personales en el contenido que va al repo publico:
  - emails reales (excluye example.com / ejemplo.* de las plantillas de demo)
  - rutas personales del equipo (C:\\Users\\<usuario>)
  - terminos de la lista negra opcional data/privacy_blacklist.txt
    (gitignored: cada quien agrega ahi su nombre, empresa, telefono...)

Los archivos de demo (data/demo_*, data/profile.example.json) estan excluidos
por design: son sinteticos y publicos.

Uso:  python privacy_check.py   ->  exit 0 limpio / exit 1 revisar antes de commit.
"""
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
EXCLUDES = ("demo_", "profile.example.json", "privacy_check.py")

EMAIL = re.compile(r"(?<![\w.])[A-Za-z0-9._%+-]+@(?!example\.|ejemplo\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USERS = re.compile(r"[CDEF]:\\+[Uu]sers\\+[^\\'\"\s]+")


def _es_placeholder(segmento: str) -> bool:
    """True si el fragmento tras 'Users\\' es plantilla, no usuario real."""
    s = segmento.strip().strip(".,;:()<>\"'`")
    if not s or set(s) <= {".", "_"}:
        return True
    if "<" in s or ">" in s:
        return True
    # Convencion de plantillas: MAYUSCULAS_CON_GUIONES (p.ej. TU_USUARIO)
    return s == s.upper() and "_" in s and not any(c.isdigit() for c in s)


def tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=str(BASE))
    return [f for f in out.stdout.splitlines() if f.strip()]


def blacklist():
    p = BASE / "data" / "privacy_blacklist.txt"
    if p.exists():
        return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return []


def main():
    black = blacklist()
    hits = []
    for f in tracked_files():
        if any(x in f for x in EXCLUDES):
            continue
        p = BASE / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m_email = EMAIL.search(line)
            m_users = USERS.search(line)
            if m_users and _es_placeholder(m_users.group(0).split("\\")[-1]):
                m_users = None
            if m_email:
                hits.append((f, i, "email", line[:80]))
            if m_users:
                hits.append((f, i, "ruta personal", line[:80]))
            for b in black:
                if b.lower() in line.lower():
                    hits.append((f, i, f"blacklist: {b[:24]}", line[:80]))

    if hits:
        print("PRIVACIDAD: datos personales en archivos tracked (revisar ANTES de commitear):")
        for f, i, tag, line in hits:
            print(f"  {f}:{i} [{tag}] {line}")
        return 1
    print("PRIVACIDAD: archivos tracked sin datos personales detectados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
