"""
Бэкап monitor.db через штатный SQLite backup API (безопасно при работающей
панели — консистентный снимок, не «копия на лету»).

  python backup.py                 # снимок в backups/, хранить 14 последних
  python backup.py --keep 30       # хранить 30
  python backup.py --dest /path    # своя папка назначения

Поставьте в cron / Планировщик задач Windows раз в сутки (см. DEPLOY.md).
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import db


def backup(src: Path = None, dest_dir: str = "backups", keep: int = 14) -> Path:
    src = Path(src or db.DB_PATH)
    if not src.exists():
        raise SystemExit(f"БД не найдена: {src}")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = dest / f"{src.stem}_{ts}.db"

    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(out)
        try:
            source.backup(target)   # атомарный снимок
        finally:
            target.close()
    finally:
        source.close()

    # ротация: оставить keep самых свежих
    snaps = sorted(dest.glob(f"{src.stem}_*.db"))
    for old in snaps[:-keep] if keep > 0 else []:
        old.unlink()
    return out


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Бэкап monitor.db")
    p.add_argument("--keep", type=int, default=14, help="сколько снимков хранить")
    p.add_argument("--dest", default="backups", help="папка назначения")
    p.add_argument("--src", default=None, help="путь к БД (по умолчанию monitor.db)")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            conn = db.connect(tdp / "src.db")
            db.add_company(conn, "1", "https://2gis.kz", "almaty", "u")
            conn.close()
            out = backup(tdp / "src.db", dest_dir=str(tdp / "bk"), keep=2)
            ok = out.exists()
            # проверим, что снимок читается и содержит данные
            c2 = sqlite3.connect(out)
            n = c2.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            c2.close()
            # ротация: делаем 3 снимка при keep=2 -> остаётся 2
            import time as _t
            for _ in range(3):
                _t.sleep(1.05)
                backup(tdp / "src.db", dest_dir=str(tdp / "bk"), keep=2)
            left = len(list((tdp / "bk").glob("src_*.db")))
            print(f"  [{'OK ' if ok and n == 1 else 'FAIL'}] снимок создан и читается (компаний={n})")
            print(f"  [{'OK ' if left == 2 else 'FAIL'}] ротация keep=2 -> осталось {left}")
            print("selftest done")
    else:
        out = backup(src=args.src, dest_dir=args.dest, keep=args.keep)
        print(f"Бэкап: {out}")
