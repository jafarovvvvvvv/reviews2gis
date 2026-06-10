"""
SQLite-хранилище мониторинга отзывов (используется веб-приложением app.py).

Таблицы:
  settings  — ключ/значение (bot_token, check_interval_min, notify_all, tg_offset)
  companies — отслеживаемые карточки 2GIS + статус фоновой задачи
  reviews   — все увиденные отзывы; «новые» определяются дифом против этой таблицы
  chats     — Telegram-чаты, привязанные к уведомлениям

Соединение открывается на операцию (БД маленькая, накладные расходы — копейки),
схема создаётся при первом подключении. Самопроверка без сети: python db.py
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "monitor.db"

# статусы компании
ST_ADDING = "adding"      # первичная загрузка карточки (фон)
ST_CHECKING = "checking"  # идёт проверка
ST_OK = "ok"
ST_ERROR = "error"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS companies(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  firm_id       TEXT NOT NULL UNIQUE,
  domain        TEXT NOT NULL DEFAULT 'https://2gis.kz',
  city_slug     TEXT NOT NULL,
  url           TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '…',
  category      TEXT NOT NULL DEFAULT '',
  rating        REAL,
  reviews_count INTEGER,
  status        TEXT NOT NULL DEFAULT 'adding',
  error         TEXT NOT NULL DEFAULT '',
  added_at      TEXT NOT NULL,
  last_checked  TEXT);

CREATE TABLE IF NOT EXISTS reviews(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  review_id    TEXT NOT NULL,
  rating       INTEGER,
  date_created TEXT,
  author       TEXT NOT NULL DEFAULT '',
  text         TEXT NOT NULL DEFAULT '',
  answered     INTEGER NOT NULL DEFAULT 0,
  is_new       INTEGER NOT NULL DEFAULT 0,
  seen_at      TEXT NOT NULL,
  UNIQUE(company_id, review_id));

CREATE TABLE IF NOT EXISTS chats(
  chat_id  TEXT PRIMARY KEY,
  title    TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ---------- settings ----------

def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


# ---------- companies ----------

def add_company(conn, firm_id: str, domain: str, city_slug: str, url: str) -> int:
    cur = conn.execute(
        "INSERT INTO companies(firm_id,domain,city_slug,url,added_at) VALUES(?,?,?,?,?)",
        (firm_id, domain, city_slug, url, now_iso()))
    conn.commit()
    return cur.lastrowid


def get_company(conn, company_id: int):
    return conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()


def find_company_by_firm(conn, firm_id: str):
    return conn.execute("SELECT * FROM companies WHERE firm_id=?", (firm_id,)).fetchone()


def list_companies(conn):
    return conn.execute("SELECT * FROM companies ORDER BY id").fetchall()


def set_company_status(conn, company_id: int, status: str, error: str = ""):
    conn.execute("UPDATE companies SET status=?, error=? WHERE id=?",
                 (status, error, company_id))
    conn.commit()


def update_company_info(conn, company_id: int, **fields):
    """Обновить только переданные (и не-None) поля + last_checked."""
    fields = {k: v for k, v in fields.items() if v is not None}
    fields["last_checked"] = now_iso()
    keys = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE companies SET {keys} WHERE id=?",
                 (*fields.values(), company_id))
    conn.commit()


def delete_company(conn, company_id: int):
    conn.execute("DELETE FROM companies WHERE id=?", (company_id,))
    conn.commit()


# ---------- reviews ----------

def save_reviews(conn, company_id: int, reviews: list, mark_new: bool) -> list:
    """Сохранить отзывы; вернуть те, которых раньше не видели.

    mark_new=False — первичная загрузка (baseline): существующие отзывы
    запоминаем молча, уведомлять о них не надо.
    """
    inserted = []
    for rv in reviews:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reviews(company_id,review_id,rating,date_created,"
            "author,text,answered,is_new,seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (company_id, rv["review_id"], rv.get("rating"), rv.get("date_created"),
             rv.get("author", ""), rv.get("text", ""), int(rv.get("answered", False)),
             int(mark_new), now_iso()))
        if cur.rowcount:
            inserted.append(rv)
    conn.commit()
    return inserted if mark_new else []


def recent_reviews(conn, company_id: int, limit: int = 30):
    return conn.execute(
        "SELECT * FROM reviews WHERE company_id=? "
        "ORDER BY date_created DESC LIMIT ?", (company_id, limit)).fetchall()


def latest_review(conn, company_id: int):
    rows = recent_reviews(conn, company_id, limit=1)
    return rows[0] if rows else None


def new_reviews_count(conn, company_id: int, days: int = 7) -> int:
    """Сколько «новых» (появившихся после добавления) отзывов за days дней."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE company_id=? AND is_new=1 AND seen_at>=?",
        (company_id, cutoff_iso)).fetchone()
    return row["n"]


# ---------- telegram chats ----------

def add_chat(conn, chat_id: str, title: str):
    conn.execute("INSERT INTO chats(chat_id,title,added_at) VALUES(?,?,?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                 (str(chat_id), title, now_iso()))
    conn.commit()


def list_chats(conn):
    return conn.execute("SELECT * FROM chats ORDER BY added_at").fetchall()


def delete_chat(conn, chat_id: str):
    conn.execute("DELETE FROM chats WHERE chat_id=?", (str(chat_id),))
    conn.commit()


if __name__ == "__main__":
    # самопроверка на временной БД
    import sys
    import tempfile
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as td:
        conn = connect(Path(td) / "t.db")
        checks = []

        set_setting(conn, "bot_token", "abc")
        set_setting(conn, "bot_token", "xyz")
        checks.append(("settings upsert", get_setting(conn, "bot_token") == "xyz"))
        checks.append(("settings default", get_setting(conn, "nope", "d") == "d"))

        cid = add_company(conn, "123", "https://2gis.kz", "almaty", "u")
        checks.append(("company added", get_company(conn, cid)["status"] == ST_ADDING))
        checks.append(("find by firm", find_company_by_firm(conn, "123")["id"] == cid))

        base = [{"review_id": "r1", "rating": 5, "date_created": "2026-01-01",
                 "author": "A", "text": "ok", "answered": True}]
        checks.append(("baseline молчит", save_reviews(conn, cid, base, mark_new=False) == []))
        fresh = base + [{"review_id": "r2", "rating": 1, "date_created": "2026-06-01",
                         "author": "B", "text": "плохо", "answered": False}]
        new = save_reviews(conn, cid, fresh, mark_new=True)
        checks.append(("диф: только новый", [r["review_id"] for r in new] == ["r2"]))
        checks.append(("повтор не дублирует", save_reviews(conn, cid, fresh, True) == []))
        checks.append(("recent сортировка",
                       recent_reviews(conn, cid)[0]["review_id"] == "r2"))
        checks.append(("счётчик новых за 7д", new_reviews_count(conn, cid) == 1))

        update_company_info(conn, cid, name="Тест", rating=4.5, reviews_count=None)
        row = get_company(conn, cid)
        checks.append(("update info", row["name"] == "Тест" and row["rating"] == 4.5
                       and row["last_checked"] is not None))

        add_chat(conn, 42, "Эмиль")
        add_chat(conn, 42, "Эмиль (личный)")
        checks.append(("chat upsert", [c["title"] for c in list_chats(conn)] == ["Эмиль (личный)"]))
        delete_chat(conn, 42)
        checks.append(("chat delete", list_chats(conn) == []))

        delete_company(conn, cid)
        checks.append(("cascade reviews", recent_reviews(conn, cid) == []))
        conn.close()  # иначе Windows не даст удалить временную папку

        ok = 0
        for label, passed in checks:
            print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
            ok += passed
        print(f"{ok}/{len(checks)} passed")
