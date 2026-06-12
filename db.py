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
  last_checked  TEXT,
  tone            TEXT NOT NULL DEFAULT '',   -- тон бренда для AI-ответов
  reply_signature TEXT NOT NULL DEFAULT '');  -- подпись/контакт в конце ответа

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
  added_at TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS subscriptions(
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  chat_id    TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
  PRIMARY KEY(company_id, chat_id));
"""


def _migrate(conn):
    """Старые БД: chats без is_admin -> добавить; прежние чаты получали всё,
    значит они админы. companies: поля тона/подписи для AI-ответов."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)")}
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE chats ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE chats SET is_admin=1")
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    if "tone" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN tone TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE companies ADD COLUMN reply_signature TEXT NOT NULL DEFAULT ''")
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
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


def get_review(conn, review_row_id: int):
    return conn.execute("SELECT * FROM reviews WHERE id=?",
                        (review_row_id,)).fetchone()


def new_reviews_count(conn, company_id: int, days: int = 7) -> int:
    """Сколько «новых» (появившихся после добавления) отзывов за days дней."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE company_id=? AND is_new=1 AND seen_at>=?",
        (company_id, cutoff_iso)).fetchone()
    return row["n"]


# ---------- telegram chats ----------

def add_chat(conn, chat_id: str, title: str, is_admin=None) -> bool:
    """Зарегистрировать чат. Возвращает True, если чат новый.

    is_admin=None: самый первый чат становится админом (это владелец),
    остальные — обычные получатели (их подвязывают к компаниям).
    """
    chat_id = str(chat_id)
    if conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone():
        conn.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
        conn.commit()
        return False
    if is_admin is None:
        is_admin = conn.execute("SELECT 1 FROM chats LIMIT 1").fetchone() is None
    conn.execute("INSERT INTO chats(chat_id,title,added_at,is_admin) VALUES(?,?,?,?)",
                 (chat_id, title, now_iso(), int(is_admin)))
    conn.commit()
    return True


def list_chats(conn):
    return conn.execute("SELECT * FROM chats ORDER BY added_at").fetchall()


def get_chat(conn, chat_id: str):
    return conn.execute("SELECT * FROM chats WHERE chat_id=?",
                        (str(chat_id),)).fetchone()


def set_chat_admin(conn, chat_id: str, is_admin: bool):
    conn.execute("UPDATE chats SET is_admin=? WHERE chat_id=?",
                 (int(is_admin), str(chat_id)))
    conn.commit()


def delete_chat(conn, chat_id: str):
    conn.execute("DELETE FROM chats WHERE chat_id=?", (str(chat_id),))
    conn.commit()


# ---------- подписки «компания -> чат» ----------

def set_subscriptions(conn, company_id: int, chat_ids: list):
    conn.execute("DELETE FROM subscriptions WHERE company_id=?", (company_id,))
    for cid in chat_ids:
        conn.execute("INSERT OR IGNORE INTO subscriptions(company_id,chat_id) "
                     "VALUES(?,?)", (company_id, str(cid)))
    conn.commit()


def subscribed_chat_ids(conn, company_id: int) -> set:
    rows = conn.execute("SELECT chat_id FROM subscriptions WHERE company_id=?",
                        (company_id,)).fetchall()
    return {r["chat_id"] for r in rows}


def notify_targets(conn, company_id=None):
    """Чаты-получатели уведомления: админы (получают всё) + подписанные на
    компанию. company_id=None — только админы (служебные сообщения)."""
    targets = {c["chat_id"]: c for c in list_chats(conn) if c["is_admin"]}
    if company_id is not None:
        subs = subscribed_chat_ids(conn, company_id)
        for c in list_chats(conn):
            if c["chat_id"] in subs:
                targets[c["chat_id"]] = c
    return list(targets.values())


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

        checks.append(("первый чат — админ", add_chat(conn, 42, "Эмиль") is True
                       and get_chat(conn, 42)["is_admin"] == 1))
        checks.append(("второй чат — не админ", add_chat(conn, 77, "Клиент") is True
                       and get_chat(conn, 77)["is_admin"] == 0))
        checks.append(("повторный /start — не новый", add_chat(conn, 42, "Эмиль 2") is False
                       and get_chat(conn, 42)["title"] == "Эмиль 2"
                       and get_chat(conn, 42)["is_admin"] == 1))

        # маршрутизация: админ получает всё, подписанный — свою компанию
        checks.append(("targets: только админ",
                       [t["chat_id"] for t in notify_targets(conn, cid)] == ["42"]))
        set_subscriptions(conn, cid, ["77"])
        checks.append(("targets: админ + подписка",
                       sorted(t["chat_id"] for t in notify_targets(conn, cid)) == ["42", "77"]))
        checks.append(("targets: служебное — только админ",
                       [t["chat_id"] for t in notify_targets(conn)] == ["42"]))
        set_chat_admin(conn, 42, False)
        checks.append(("снятие админа", notify_targets(conn) == []))
        set_chat_admin(conn, 42, True)

        delete_chat(conn, 77)
        checks.append(("cascade подписок при удалении чата",
                       subscribed_chat_ids(conn, cid) == set()))
        delete_chat(conn, 42)

        delete_company(conn, cid)
        checks.append(("cascade reviews", recent_reviews(conn, cid) == []))
        conn.close()

        # миграция старой схемы chats (без is_admin)
        old = sqlite3.connect(Path(td) / "old.db")
        old.execute("CREATE TABLE chats(chat_id TEXT PRIMARY KEY, "
                    "title TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL)")
        old.execute("INSERT INTO chats VALUES('1','старый','2026-01-01')")
        old.execute("CREATE TABLE companies(id INTEGER PRIMARY KEY, "
                    "firm_id TEXT, city_slug TEXT, added_at TEXT)")
        old.execute("INSERT INTO companies VALUES(1,'9','almaty','2026-01-01')")
        old.commit()
        old.close()
        conn = connect(Path(td) / "old.db")
        checks.append(("миграция: старый чат стал админом",
                       get_chat(conn, "1")["is_admin"] == 1))
        row = get_company(conn, 1)
        checks.append(("миграция: тон/подпись добавлены",
                       row["tone"] == "" and row["reply_signature"] == ""))
        conn.close()  # иначе Windows не даст удалить временную папку

        ok = 0
        for label, passed in checks:
            print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
            ok += passed
        print(f"{ok}/{len(checks)} passed")
