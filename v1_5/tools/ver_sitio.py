"""Inspecciona la config de un sitio en la BD."""
import sys

from jobpulse_v15.db import Database

db = Database()
db.initialize()
with db.connection() as conn:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(source_sites)").fetchall()]
    print("cols:", cols)
    patron = sys.argv[1] if len(sys.argv) > 1 else "%procomer%"
    for row in conn.execute(
        "SELECT * FROM source_sites WHERE site_id LIKE ?", (patron,)
    ).fetchall():
        d = dict(zip(cols, row))
        for k, v in d.items():
            print(f"  {k}: {v}")
        print()
