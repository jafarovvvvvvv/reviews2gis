"""
Мониторинг отзывов: разбор ссылки 2GIS, проверка компании через Scraper,
диф новых отзывов против БД и рассылка уведомлений в Telegram.

Проверка одной компании = parse_firm (обновляет название/рейтинг/кол-во) +
сырые отзывы из той же сессии (scraper.last_reviews_data). Playwright в один
момент времени один — SCRAPE_LOCK.

Чистые функции (parse_2gis_url, extract_review) — самотесты: python monitor.py
"""
import asyncio
import re
from dataclasses import dataclass

import db
import tg
from lead_scoring import NA
from scraper import Scraper

# ссылка на карточку: https://2gis.kz/almaty/firm/700000010… (+ /tab/…, ?…)
URL_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?(2gis\.(?:kz|ru|com|kg|uz|az|by))/([a-z0-9_-]+)/", re.I)
URL_FIRM_RE = re.compile(r"/firm/(\d{6,})")

SCRAPE_LOCK = asyncio.Lock()  # один Playwright одновременно (add/check/scheduler)


@dataclass
class MonitorCfg:
    """Утиный конфиг для Scraper (вместо config.Config) — по компании из БД."""
    city_slug: str
    domain: str = "https://2gis.kz"
    category: str = ""
    headless: bool = True


def parse_2gis_url(text: str):
    """Ссылка на карточку -> (domain, city_slug, firm_id) или None."""
    text = (text or "").strip()
    md = URL_DOMAIN_RE.search(text)
    mf = URL_FIRM_RE.search(text)
    if not (md and mf):
        return None
    return f"https://{md.group(1).lower()}", md.group(2).lower(), mf.group(1)


def extract_review(raw: dict) -> dict:
    """Сырой отзыв reviews-API -> плоская запись для БД."""
    user = raw.get("user") or {}
    text = re.sub(r"\s+", " ", (raw.get("text") or "")).strip()
    return {
        "review_id": str(raw.get("id") or f"{raw.get('date_created')}|{hash(text)}"),
        "rating": raw.get("rating"),
        "date_created": raw.get("date_created") or "",
        "author": user.get("name") or "",
        "text": text,
        "answered": bool(raw.get("official_answer")),
    }


async def notify_chats(conn, text: str) -> list:
    """Разослать text во все привязанные чаты. Возвращает список ошибок."""
    token = db.get_setting(conn, "bot_token")
    chats = db.list_chats(conn)
    errors = []
    if not token or not chats:
        return ["Telegram не настроен (токен/чаты) — уведомление не отправлено"]
    for chat in chats:
        try:
            await tg.send_message(token, chat["chat_id"], text)
        except tg.TgError as e:
            errors.append(f"чат {chat['chat_id']}: {e}")
    return errors


async def check_company(company_id: int, baseline: bool = False) -> dict:
    """Проверить компанию: обновить карточку, найти новые отзывы, уведомить.

    baseline=True — первичная загрузка после добавления: текущие отзывы
    запоминаются молча, уведомлений нет.
    Возвращает {"new": [...], "errors": [...]}.
    """
    conn = db.connect()
    result = {"new": [], "errors": []}
    try:
        row = db.get_company(conn, company_id)
        if row is None:
            return {"new": [], "errors": ["компания удалена"]}
        db.set_company_status(conn, company_id,
                              db.ST_ADDING if baseline else db.ST_CHECKING)
        try:
            async with SCRAPE_LOCK:
                cfg = MonitorCfg(city_slug=row["city_slug"], domain=row["domain"])
                async with Scraper(cfg) as s:
                    c = await s.parse_firm(row["firm_id"])
                    data = s.last_reviews_data
        except Exception as e:
            db.set_company_status(conn, company_id, db.ST_ERROR, str(e)[:300])
            result["errors"].append(str(e))
            return result

        if c.name == NA and data is None:
            # ни карточка, ни отзывы не прочитались — не затираем имя, фиксируем ошибку
            db.set_company_status(conn, company_id, db.ST_ERROR,
                                  "карточка не прочиталась (блок/структура изменилась?)")
            result["errors"].append("карточка не прочиталась")
            return result

        reviews = [extract_review(r) for r in (data or {}).get("reviews") or []]
        new = db.save_reviews(conn, company_id, reviews, mark_new=not baseline)
        db.update_company_info(
            conn, company_id,
            name=None if c.name == NA else c.name,
            category=None if c.category in (NA, "") else c.category,
            rating=c.rating, reviews_count=c.reviews_count,
            status=db.ST_OK, error="")

        # уведомления: старые первыми, чтобы лента в Telegram шла хронологично
        new.sort(key=lambda r: r.get("date_created") or "")
        notify_all = db.get_setting(conn, "notify_all", "1") == "1"
        for rv in new:
            if notify_all or tg.is_negative(rv.get("rating")):
                result["errors"] += await notify_chats(
                    conn, tg.fmt_review_alert(c.name, row["url"], rv))
        result["new"] = new
        return result
    finally:
        conn.close()


async def scheduler_loop(interval_poll_sec: int = 60):
    """Фоновый цикл: проверять компании, у которых подошёл интервал."""
    await asyncio.sleep(5)  # дать приложению подняться
    while True:
        try:
            conn = db.connect()
            try:
                interval_min = int(db.get_setting(conn, "check_interval_min", "360"))
                rows = db.list_companies(conn)
            finally:
                conn.close()
            cutoff = db.now_iso()  # ISO UTC сравнивается лексикографически
            due = []
            for r in rows:
                if r["status"] in (db.ST_ADDING, db.ST_CHECKING):
                    continue  # уже в работе
                last = r["last_checked"]
                if last is None:
                    due.append(r["id"])
                    continue
                from datetime import datetime, timedelta
                last_dt = datetime.fromisoformat(last)
                if datetime.fromisoformat(cutoff) - last_dt >= timedelta(minutes=interval_min):
                    due.append(r["id"])
            for cid in due:
                await check_company(cid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[scheduler] ошибка: {e}", flush=True)
        await asyncio.sleep(interval_poll_sec)


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []

    p = parse_2gis_url("https://2gis.kz/almaty/firm/70000001062690247")
    checks.append(("url простой", p == ("https://2gis.kz", "almaty", "70000001062690247")))
    p = parse_2gis_url(" https://www.2gis.ru/moscow/firm/4504127908584798/tab/reviews?m=1 ")
    checks.append(("url ru + tab + query",
                   p == ("https://2gis.ru", "moscow", "4504127908584798")))
    p = parse_2gis_url("https://2gis.kz/ust-kamenogorsk/search/кафе/firm/123456789")
    checks.append(("url слаг с дефисом + search",
                   p == ("https://2gis.kz", "ust-kamenogorsk", "123456789")))
    checks.append(("url мусор", parse_2gis_url("просто текст") is None))
    checks.append(("url голый id", parse_2gis_url("70000001062690247") is None))

    rv = extract_review({"id": 99, "rating": 2, "date_created": "2026-06-01T10:00:00",
                         "user": {"name": "Иван"}, "text": "Очень\n\nплохо  ",
                         "official_answer": {"text": "извините"}})
    checks.append(("extract: поля", rv["review_id"] == "99" and rv["rating"] == 2
                   and rv["author"] == "Иван" and rv["answered"] is True))
    checks.append(("extract: чистка текста", rv["text"] == "Очень плохо"))
    rv2 = extract_review({"rating": 5, "date_created": "2026-01-01", "text": "ок"})
    checks.append(("extract: без id не падает", rv2["review_id"] != "" and rv2["answered"] is False))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
