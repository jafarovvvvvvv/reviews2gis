"""
SQLite-хранилище мониторинга отзывов (используется веб-приложением app.py).

Таблицы:
  settings  — ключ/значение (bot_token, check_interval_min, notify_all, tg_offset)
  companies — отслеживаемые карточки 2GIS + статус фоновой задачи
  reviews   — все увиденные отзывы; «новые» определяются дифом против этой таблицы
  chats     — Telegram-чаты, привязанные к уведомлениям
  pending_notifications — уведомления, ждущие проверки владельцем сервиса
                          (см. monitor.moderation_mode)

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
  html         TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'pain');  -- 'pain' (разбор болей, insights.py)
                                               -- | 'monthly' (отчёт клиенту, monthly.py)

CREATE TABLE IF NOT EXISTS rating_history(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  ts            TEXT NOT NULL,
  rating        REAL,
  reviews_count INTEGER,
  answered_pct  INTEGER);

CREATE TABLE IF NOT EXISTS lead_snapshots(
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  firm_id             TEXT NOT NULL,
  city_slug           TEXT NOT NULL DEFAULT '',
  category            TEXT NOT NULL DEFAULT '',
  name                TEXT NOT NULL DEFAULT '',
  url                 TEXT NOT NULL DEFAULT '',
  collected_at        TEXT NOT NULL,          -- когда данные сняты с 2ГИС
  rating              REAL,                   -- рейтинг филиала от 2ГИС
  rating_confirmed    INTEGER,
  ratings_count       INTEGER,                -- всего оценок (база рейтинга)
  reviews_count       INTEGER,                -- текстовых отзывов
  branch_scope        TEXT NOT NULL DEFAULT '',
  branches_count      INTEGER,
  org_rating          REAL,
  loaded_reviews      INTEGER,
  unanswered_count    INTEGER,
  unanswered_fresh    INTEGER,
  negative_count      INTEGER,
  negative_unanswered INTEGER,
  response_rate       REAL,                   -- доля отзывов в окне с ответом; None = окно пустое
  fresh_count         INTEGER,
  priority            TEXT NOT NULL DEFAULT '',
  priority_reason     TEXT NOT NULL DEFAULT '',
  data_json           TEXT NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS idx_snap_firm ON lead_snapshots(firm_id, collected_at);

CREATE TABLE IF NOT EXISTS auto_runs(
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,        -- 'digest' (еженедельно) | 'report' (ежемесячно)
  last_ts    TEXT NOT NULL,
  PRIMARY KEY(company_id, kind));

-- Уведомления, задержанные на проверку у владельца сервиса (см. monitor.py).
-- Клиенту такое сообщение уходит только после подтверждения кнопкой в Telegram.
CREATE TABLE IF NOT EXISTS pending_notifications(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  review_id    TEXT NOT NULL DEFAULT '',
  rating       INTEGER,
  text         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | dropped
  created_at   TEXT NOT NULL,
  decided_at   TEXT NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS idx_pending_status
  ON pending_notifications(status, company_id);
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
    snap_cols = {r["name"] for r in conn.execute("PRAGMA table_info(lead_snapshots)")}
    if snap_cols and "unanswered_fresh" not in snap_cols:
        conn.execute("ALTER TABLE lead_snapshots ADD COLUMN unanswered_fresh INTEGER")
        conn.commit()
    if snap_cols and "response_rate" not in snap_cols:
        conn.execute("ALTER TABLE lead_snapshots ADD COLUMN response_rate REAL")
        conn.commit()
    if "cabinet_nonce" not in cols:
        # nonce — для отзыва ссылки кабинета; enabled — включён ли кабинет
        conn.execute("ALTER TABLE companies ADD COLUMN cabinet_nonce TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE companies ADD COLUMN cabinet_enabled INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    rep_cols = {r["name"] for r in conn.execute("PRAGMA table_info(insights_reports)")}
    if rep_cols and "kind" not in rep_cols:
        # до появления ежемесячного отчёта в таблице лежали только разборы болей
        conn.execute("ALTER TABLE insights_reports ADD COLUMN "
                     "kind TEXT NOT NULL DEFAULT 'pain'")
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


def reset_stale_statuses(conn):
    """При старте: сбросить компании, зависшие в checking/adding из-за краша."""
    conn.execute(
        "UPDATE companies SET status=? WHERE status IN (?,?)",
        (ST_OK, ST_CHECKING, ST_ADDING))
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


def set_cabinet(conn, company_id: int, nonce=None, enabled=None):
    """Управление кабинетом клиента: nonce (обновление ссылки) и/или enabled."""
    if nonce is not None:
        conn.execute("UPDATE companies SET cabinet_nonce=? WHERE id=?",
                     (nonce, company_id))
    if enabled is not None:
        conn.execute("UPDATE companies SET cabinet_enabled=? WHERE id=?",
                     (int(enabled), company_id))
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


def unanswered_reviews(conn, company_id: int, limit: int = 50):
    """Отзывы без ответа владельца, свежие сверху. Лимит — как у company_page,
    чтобы кнопка «отправить владельцу» не разослала сотни сообщений разом."""
    return conn.execute(
        "SELECT * FROM reviews WHERE company_id=? AND answered=0 "
        "ORDER BY date_created DESC LIMIT ?", (company_id, limit)).fetchall()


def unanswered_count(conn, company_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE company_id=? AND answered=0",
        (company_id,)).fetchone()
    return row["n"]


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


# Виды отчётов в одной таблице: разбор болей (insights.py) и ежемесячный отчёт
# клиенту (monthly.py). Динамика «к прошлому разу» всегда считается внутри
# своего вида — иначе разбор сравнивался бы с отчётом и наоборот.
KIND_PAIN, KIND_MONTHLY = "pain", "monthly"


def save_report(conn, company_id: int, period: str, reviews_used: int,
                data_json: str, html: str, kind: str = KIND_PAIN) -> int:
    cur = conn.execute(
        "INSERT INTO insights_reports(company_id,created_at,period,reviews_used,"
        "data_json,html,kind) VALUES(?,?,?,?,?,?,?)",
        (company_id, now_iso(), period, reviews_used, data_json, html, kind))
    conn.commit()
    return cur.lastrowid


def list_reports(conn, company_id: int, kind: str = None):
    """Отчёты компании, новые сверху. kind=None — все виды."""
    if kind is None:
        return conn.execute(
            "SELECT id,created_at,period,reviews_used,kind FROM insights_reports "
            "WHERE company_id=? ORDER BY id DESC", (company_id,)).fetchall()
    return conn.execute(
        "SELECT id,created_at,period,reviews_used,kind FROM insights_reports "
        "WHERE company_id=? AND kind=? ORDER BY id DESC",
        (company_id, kind)).fetchall()


def get_report(conn, report_id: int):
    return conn.execute("SELECT * FROM insights_reports WHERE id=?",
                        (report_id,)).fetchone()


def prev_report_data(conn, company_id: int, before_id: int, kind: str = KIND_PAIN):
    """data_json предыдущего отчёта того же вида (динамика) или None."""
    row = conn.execute(
        "SELECT data_json FROM insights_reports WHERE company_id=? AND id<? "
        "AND kind=? ORDER BY id DESC LIMIT 1",
        (company_id, before_id, kind)).fetchone()
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

# ---------- снимки лидов (история прогонов лидогенерации) ----------
# Снимок = числа компании на момент сбора. По ним разбор показывает динамику
# («было -> стало») и решает, не устарели ли данные.

SNAPSHOT_FIELDS = ("rating", "rating_confirmed", "ratings_count", "reviews_count",
                   "branch_scope", "branches_count", "org_rating", "loaded_reviews",
                   "unanswered_count", "unanswered_fresh", "negative_count",
                   "negative_unanswered", "response_rate", "fresh_count",
                   "priority", "priority_reason")


def add_lead_snapshot(conn, c, city_slug: str = "", category: str = "") -> int:
    """Сохранить снимок компании. Возвращает id снимка."""
    import json
    vals = {f: getattr(c, f, None) for f in SNAPSHOT_FIELDS}
    vals["rating_confirmed"] = (None if vals["rating_confirmed"] is None
                                else int(vals["rating_confirmed"]))
    vals["branch_scope"] = vals["branch_scope"] or ""
    vals["priority"] = vals["priority"] or ""
    vals["priority_reason"] = vals["priority_reason"] or ""
    cols = ("firm_id,city_slug,category,name,url,collected_at," + ",".join(SNAPSHOT_FIELDS)
            + ",data_json")
    holders = ",".join("?" * (6 + len(SNAPSHOT_FIELDS) + 1))
    payload = json.dumps({"negative_quotes": getattr(c, "negative_quotes", []),
                          "last_review_date": getattr(c, "last_review_date", None),
                          "has_fresh_reviews": getattr(c, "has_fresh_reviews", None),
                          "owner_responds": getattr(c, "owner_responds", None),
                          "negative_without_response":
                              getattr(c, "negative_without_response", None)},
                         ensure_ascii=False)
    cur = conn.execute(
        f"INSERT INTO lead_snapshots({cols}) VALUES({holders})",
        (getattr(c, "firm_id", ""), city_slug, category, getattr(c, "name", ""),
         getattr(c, "url", ""), getattr(c, "collected_at", None) or now_iso(),
         *[vals[f] for f in SNAPSHOT_FIELDS], payload))
    conn.commit()
    return cur.lastrowid


def last_lead_snapshot(conn, firm_id: str):
    """Последний снимок компании (или None)."""
    return conn.execute(
        "SELECT * FROM lead_snapshots WHERE firm_id=? "
        "ORDER BY collected_at DESC, id DESC LIMIT 1", (firm_id,)).fetchone()


def prev_lead_snapshot(conn, firm_id: str, before_id: int):
    """Снимок, предшествующий указанному (для блока динамики)."""
    return conn.execute(
        "SELECT * FROM lead_snapshots WHERE firm_id=? AND id<? "
        "ORDER BY collected_at DESC, id DESC LIMIT 1", (firm_id, before_id)).fetchone()


def latest_snapshots_in(conn, city_slug: str, category: str):
    """По одному (последнему) снимку на компанию в городе+категории.

    Нужен для детерминированного выбора конкурента: сравниваем свежайшие
    известные числа, а не что попало из разных прогонов.
    """
    return conn.execute(
        "SELECT s.* FROM lead_snapshots s "
        "JOIN (SELECT firm_id, MAX(id) AS mid FROM lead_snapshots "
        "      WHERE city_slug=? AND category=? GROUP BY firm_id) t "
        "  ON s.id = t.mid "
        "ORDER BY s.firm_id", (city_slug, category)).fetchall()


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


def client_targets(conn, company_id: int):
    """Только чаты клиента: подписанные на компанию, без админских.

    Нужно для модерации: подтверждённое уведомление уходит клиенту, а владелец
    сервиса его уже видел — второй раз слать ему то же самое незачем.
    """
    subs = subscribed_chat_ids(conn, company_id)
    return [c for c in list_chats(conn)
            if c["chat_id"] in subs and not c["is_admin"]]


def admin_targets(conn):
    """Админские чаты — владелец сервиса (получатель модерации)."""
    return [c for c in list_chats(conn) if c["is_admin"]]


# ---------- очередь уведомлений на проверку (модерация) ----------

PENDING, SENT, DROPPED = "pending", "sent", "dropped"


def add_pending(conn, company_id: int, review_id: str, rating, text: str) -> int:
    """Отложить уведомление до подтверждения владельцем сервиса. Вернуть id.

    Текст кладём готовым: к моменту подтверждения AI-варианты уже сгенерированы,
    и клиент получит ровно то, что владелец видел и одобрил.
    """
    cur = conn.execute(
        "INSERT INTO pending_notifications(company_id,review_id,rating,text,"
        "status,created_at) VALUES(?,?,?,?,?,?)",
        (company_id, review_id or "", rating, text, PENDING, now_iso()))
    conn.commit()
    return cur.lastrowid


def get_pending(conn, pending_id: int):
    return conn.execute("SELECT * FROM pending_notifications WHERE id=?",
                        (pending_id,)).fetchone()


def list_pending(conn, company_id=None):
    """Неразобранная очередь, старые сверху (их отправлять первыми)."""
    if company_id is None:
        return conn.execute(
            "SELECT * FROM pending_notifications WHERE status=? ORDER BY id",
            (PENDING,)).fetchall()
    return conn.execute(
        "SELECT * FROM pending_notifications WHERE status=? AND company_id=? "
        "ORDER BY id", (PENDING, company_id)).fetchall()


def decide_pending(conn, pending_id: int, status: str) -> bool:
    """Отметить решение (SENT/DROPPED). False — записи нет или её уже разобрали:
    две одинаковые кнопки в разных админ-чатах не должны отправить клиенту
    одно и то же дважды.
    """
    cur = conn.execute(
        "UPDATE pending_notifications SET status=?, decided_at=? "
        "WHERE id=? AND status=?", (status, now_iso(), pending_id, PENDING))
    conn.commit()
    return bool(cur.rowcount)


def reopen_pending(conn, pending_id: int):
    """Вернуть запись в очередь: отправка клиенту сорвалась, кнопка должна
    сработать ещё раз, а не «уже отправлено»."""
    conn.execute("UPDATE pending_notifications SET status=?, decided_at='' "
                 "WHERE id=?", (PENDING, pending_id))
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
        checks.append(("без ответа: счётчик", unanswered_count(conn, cid) == 1))
        checks.append(("без ответа: выборка",
                       [r["review_id"] for r in unanswered_reviews(conn, cid)] == ["r2"]))

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
        # модерация делит тех же получателей на две непересекающиеся группы
        checks.append(("targets: клиентские — подписка без админов",
                       [t["chat_id"] for t in client_targets(conn, cid)] == ["77"]))
        checks.append(("targets: админские — только админ",
                       [t["chat_id"] for t in admin_targets(conn)] == ["42"]))
        set_chat_admin(conn, 42, False)
        checks.append(("снятие админа", notify_targets(conn) == []))
        set_chat_admin(conn, 42, True)

        # --- очередь уведомлений на проверку ---
        pid = add_pending(conn, cid, "r2", 1, "текст алерта")
        checks.append(("очередь: запись создана в статусе pending",
                       get_pending(conn, pid)["status"] == PENDING
                       and get_pending(conn, pid)["text"] == "текст алерта"))
        checks.append(("очередь: попадает в список ожидающих",
                       [r["id"] for r in list_pending(conn, cid)] == [pid]))
        checks.append(("очередь: решение принимается один раз",
                       decide_pending(conn, pid, SENT) is True
                       and decide_pending(conn, pid, SENT) is False))
        checks.append(("очередь: разобранное из списка уходит",
                       list_pending(conn, cid) == []))
        reopen_pending(conn, pid)
        checks.append(("очередь: возврат после сбоя отправки",
                       get_pending(conn, pid)["status"] == PENDING
                       and get_pending(conn, pid)["decided_at"] == ""))
        checks.append(("очередь: отклонение закрывает запись",
                       decide_pending(conn, pid, DROPPED) is True
                       and get_pending(conn, pid)["status"] == DROPPED))

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
        rid_m = save_report(conn, cid, "март", 5, '{"m":1}', "<html>m",
                            kind=KIND_MONTHLY)
        checks.append(("reports: вид по умолчанию — разбор болей",
                       get_report(conn, rid1)["kind"] == KIND_PAIN))
        checks.append(("reports: фильтр по виду",
                       [r["id"] for r in list_reports(conn, cid, KIND_MONTHLY)] == [rid_m]))
        checks.append(("reports: динамика считается внутри своего вида",
                       prev_report_data(conn, cid, rid_m, KIND_MONTHLY) is None
                       and prev_report_data(conn, cid, rid_m, KIND_PAIN) == '{"a":2}'))
        checks.append(("reports: список новые первыми",
                       [r["id"] for r in list_reports(conn, cid)] == [rid_m, rid2, rid1]))
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

        # --- снимки лидов ---
        from lead_scoring import Company
        snap_c = Company(firm_id="777", name="Тест-клиника", rating=5.0,
                         ratings_count=1174, reviews_count=778, branches_count=2,
                         unanswered_count=10, negative_unanswered=9, negative_count=9,
                         response_rate=0.82, fresh_count=4, has_fresh_reviews=True,
                         owner_responds=True, collected_at="2026-09-01T07:00:00+00:00")
        snap_c.compute_priority()
        sid1 = add_lead_snapshot(conn, snap_c, "kostanay", "стоматология")
        checks.append(("снимок: сохранён", sid1 > 0))
        last = last_lead_snapshot(conn, "777")
        checks.append(("снимок: читается последний",
                       last["rating"] == 5.0 and last["ratings_count"] == 1174
                       and last["reviews_count"] == 778))
        checks.append(("снимок: оценки и отзывы не перепутаны",
                       last["ratings_count"] != last["reviews_count"]))
        checks.append(("снимок: response_rate сохранён и не спутан с unanswered_count",
                       last["response_rate"] == 0.82 and last["unanswered_count"] == 10))
        checks.append(("снимок: дата сбора сохранена",
                       last["collected_at"] == "2026-09-01T07:00:00+00:00"))
        checks.append(("снимок: причина приоритета сохранена",
                       bool(last["priority_reason"])))
        checks.append(("снимок: предыдущего пока нет",
                       prev_lead_snapshot(conn, "777", sid1) is None))

        snap_c.rating = 4.9
        snap_c.unanswered_count = 12
        snap_c.collected_at = "2026-09-02T07:00:00+00:00"
        sid2 = add_lead_snapshot(conn, snap_c, "kostanay", "стоматология")
        checks.append(("снимок: последний — самый свежий",
                       last_lead_snapshot(conn, "777")["id"] == sid2))
        prev = prev_lead_snapshot(conn, "777", sid2)
        checks.append(("снимок: предыдущий найден для динамики",
                       prev["id"] == sid1 and prev["rating"] == 5.0))

        other = Company(firm_id="888", name="Конкурент", rating=4.95,
                        reviews_count=200, collected_at="2026-09-01T07:00:00+00:00")
        other.compute_priority()
        add_lead_snapshot(conn, other, "kostanay", "стоматология")
        add_lead_snapshot(conn, Company(firm_id="999", name="Другой город", rating=5.0,
                                        collected_at="2026-09-01T07:00:00+00:00"),
                          "almaty", "стоматология")
        pool = latest_snapshots_in(conn, "kostanay", "стоматология")
        checks.append(("снимки: по одному на компанию в городе+категории",
                       {r["firm_id"] for r in pool} == {"777", "888"}))
        checks.append(("снимки: берётся свежий снимок компании",
                       [r["rating"] for r in pool if r["firm_id"] == "777"] == [4.9]))
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
