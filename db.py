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

CREATE TABLE IF NOT EXISTS review_topics(
  company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  review_id    TEXT NOT NULL,
  theme        TEXT NOT NULL,
  severity     INTEGER NOT NULL DEFAULT 1,   -- 1 мимоходом, 2 жалоба, 3 ущерб
  quote        TEXT NOT NULL DEFAULT '',
  rating       INTEGER,
  date_created TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(company_id, review_id, theme));

CREATE TABLE IF NOT EXISTS insights_reports(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  period       TEXT NOT NULL DEFAULT '',
  reviews_used INTEGER NOT NULL DEFAULT 0,
  data_json    TEXT NOT NULL,
  html         TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS rating_history(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  ts            TEXT NOT NULL,
  rating        REAL,
  reviews_count INTEGER,
  answered_pct  INTEGER);

CREATE TABLE IF NOT EXISTS auto_runs(
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,        -- 'digest' (еженедельно) | 'report' (ежемесячно)
  last_ts    TEXT NOT NULL,
  PRIMARY KEY(company_id, kind));
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
    if "consecutive_errors" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN "
                     "consecutive_errors INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    # путь читается при каждом вызове (demo-режим переустанавливает DB_PATH)
    conn = sqlite3.connect(path or DB_PATH)
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


def bump_errors(conn, company_id: int) -> int:
    """+1 к счётчику ошибок подряд, вернуть новое значение."""
    conn.execute("UPDATE companies SET consecutive_errors=consecutive_errors+1 "
                 "WHERE id=?", (company_id,))
    conn.commit()
    row = conn.execute("SELECT consecutive_errors FROM companies WHERE id=?",
                       (company_id,)).fetchone()
    return row["consecutive_errors"] if row else 0


def clear_errors(conn, company_id: int):
    conn.execute("UPDATE companies SET consecutive_errors=0 WHERE id=?",
                 (company_id,))
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


# ---------- аналитика (insights) ----------

def replace_topics(conn, company_id: int, topics: list):
    """Полная замена тем компании (классификация пересчитывается целиком)."""
    conn.execute("DELETE FROM review_topics WHERE company_id=?", (company_id,))
    for t in topics:
        conn.execute(
            "INSERT OR IGNORE INTO review_topics(company_id,review_id,theme,severity,"
            "quote,rating,date_created) VALUES(?,?,?,?,?,?,?)",
            (company_id, t["review_id"], t["theme"], t.get("severity", 1),
             t.get("quote", ""), t.get("rating"), t.get("date_created", "")))
    conn.commit()


def save_report(conn, company_id: int, period: str, reviews_used: int,
                data_json: str, html: str) -> int:
    cur = conn.execute(
        "INSERT INTO insights_reports(company_id,created_at,period,reviews_used,"
        "data_json,html) VALUES(?,?,?,?,?,?)",
        (company_id, now_iso(), period, reviews_used, data_json, html))
    conn.commit()
    return cur.lastrowid


def list_reports(conn, company_id: int):
    return conn.execute(
        "SELECT id,created_at,period,reviews_used FROM insights_reports "
        "WHERE company_id=? ORDER BY id DESC", (company_id,)).fetchall()


def get_report(conn, report_id: int):
    return conn.execute("SELECT * FROM insights_reports WHERE id=?",
                        (report_id,)).fetchone()


def prev_report_data(conn, company_id: int, before_id: int):
    """data_json предыдущего отчёта (для динамики месяц-к-месяцу) или None."""
    row = conn.execute(
        "SELECT data_json FROM insights_reports WHERE company_id=? AND id<? "
        "ORDER BY id DESC LIMIT 1", (company_id, before_id)).fetchone()
    return row["data_json"] if row else None


# ---------- история рейтинга (графики динамики) ----------

def add_rating_snapshot(conn, company_id: int, rating, reviews_count,
                        answered_pct=None, ts=None) -> bool:
    """Снимок метрик. Пропускаем, если рейтинг и кол-во не изменились с прошлого
    снимка — иначе частые проверки засоряют историю одинаковыми точками."""
    last = conn.execute(
        "SELECT rating,reviews_count FROM rating_history WHERE company_id=? "
        "ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
    if last and last["rating"] == rating and last["reviews_count"] == reviews_count:
        return False
    conn.execute(
        "INSERT INTO rating_history(company_id,ts,rating,reviews_count,answered_pct) "
        "VALUES(?,?,?,?,?)", (company_id, ts or now_iso(), rating, reviews_count,
                              answered_pct))
    conn.commit()
    return True


def rating_history(conn, company_id: int):
    return conn.execute(
        "SELECT ts,rating,reviews_count,answered_pct FROM rating_history "
        "WHERE company_id=? ORDER BY id", (company_id,)).fetchall()


def rating_days_ago(conn, company_id: int, days: int):
    """Рейтинг из истории ~days дней назад (ближайший снимок до отсечки) или None."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT rating FROM rating_history WHERE company_id=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (company_id, cutoff)).fetchone()
    if row is None:  # истории столько нет — берём самый ранний снимок
        row = conn.execute("SELECT rating FROM rating_history WHERE company_id=? "
                           "ORDER BY ts LIMIT 1", (company_id,)).fetchone()
    return row["rating"] if row else None


# ---------- авто-расписания (дайджест/отчёт) ----------

def auto_due(conn, company_id: int, kind: str, period_days: int) -> bool:
    """Пора ли запускать авто-задачу: не было ни разу или прошло >= period_days."""
    from datetime import datetime, timezone, timedelta
    row = conn.execute("SELECT last_ts FROM auto_runs WHERE company_id=? AND kind=?",
                       (company_id, kind)).fetchone()
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(row["last_ts"])
    except (ValueError, TypeError):
        return True
    return datetime.now(timezone.utc) - last >= timedelta(days=period_days)


def mark_auto(conn, company_id: int, kind: str):
    conn.execute("INSERT INTO auto_runs(company_id,kind,last_ts) VALUES(?,?,?) "
                 "ON CONFLICT(company_id,kind) DO UPDATE SET last_ts=excluded.last_ts",
                 (company_id, kind, now_iso()))
    conn.commit()


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
        checks.append(("errors: bump", bump_errors(conn, cid) == 1 and bump_errors(conn, cid) == 2))
        clear_errors(conn, cid)
        checks.append(("errors: clear", get_company(conn, cid)["consecutive_errors"] == 0))

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

        # аналитика: темы и отчёты
        replace_topics(conn, cid, [{"review_id": "r1", "theme": "цены", "severity": 3,
                                    "quote": "дорого", "rating": 2,
                                    "date_created": "2026-05-01"}])
        replace_topics(conn, cid, [{"review_id": "r1", "theme": "персонал"}])
        n = conn.execute("SELECT COUNT(*) AS n FROM review_topics "
                         "WHERE company_id=?", (cid,)).fetchone()["n"]
        checks.append(("topics: полная замена", n == 1))
        rid1 = save_report(conn, cid, "янв", 10, '{"a":1}', "<html>1")
        rid2 = save_report(conn, cid, "фев", 20, '{"a":2}', "<html>2")
        checks.append(("reports: список новые первыми",
                       [r["id"] for r in list_reports(conn, cid)] == [rid2, rid1]))
        checks.append(("reports: get", get_report(conn, rid1)["html"] == "<html>1"))
        checks.append(("reports: prev для динамики",
                       prev_report_data(conn, cid, rid2) == '{"a":1}'
                       and prev_report_data(conn, cid, rid1) is None))

        # история рейтинга
        checks.append(("history: первый снимок", add_rating_snapshot(conn, cid, 4.0, 100) is True))
        checks.append(("history: дубль пропущен", add_rating_snapshot(conn, cid, 4.0, 100) is False))
        checks.append(("history: изменение записано", add_rating_snapshot(conn, cid, 4.1, 105, 50) is True))
        hist = rating_history(conn, cid)
        checks.append(("history: порядок и значения",
                       [h["rating"] for h in hist] == [4.0, 4.1]
                       and hist[1]["answered_pct"] == 50))
        checks.append(("history: рейтинг N дней назад (мало истории -> ранний)",
                       rating_days_ago(conn, cid, 30) == 4.0))

        # авто-расписания
        checks.append(("auto: впервые due", auto_due(conn, cid, "digest", 7) is True))
        mark_auto(conn, cid, "digest")
        checks.append(("auto: только что -> не due", auto_due(conn, cid, "digest", 7) is False))
        checks.append(("auto: другой kind независим", auto_due(conn, cid, "report", 30) is True))

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
