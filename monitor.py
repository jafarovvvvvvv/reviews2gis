"""
Мониторинг отзывов: разбор ссылки 2GIS, проверка компании через Scraper,
диф новых отзывов против БД и рассылка уведомлений в Telegram.

Проверка одной компании = parse_firm (обновляет название/рейтинг/кол-во) +
сырые отзывы из той же сессии (scraper.last_reviews_data). Playwright в один
момент времени один — SCRAPE_LOCK.

Чистые функции (parse_2gis_url, extract_review) — самотесты: python monitor.py
"""
import asyncio
import random
import re
import sys
from dataclasses import dataclass

import ai_reply
import db
import tg
from lead_scoring import NA
from scraper import Scraper

# ссылка на карточку: https://2gis.kz/almaty/firm/700000010… (+ /tab/…, ?…)
URL_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?(2gis\.(?:kz|ru|com|kg|uz|az|by))/([a-z0-9_-]+)/", re.I)
URL_FIRM_RE = re.compile(r"/firm/(\d{6,})")

SCRAPE_LOCK = asyncio.Lock()  # один Playwright одновременно (add/check/scheduler)

# автоустановка браузера: не больше одной попытки на запуск приложения
_REPAIR_LOCK = asyncio.Lock()
_repair_attempted = False

DEAD_THRESHOLD = 3  # столько ошибок подряд у компании -> алерт «парсер умер»


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


async def _scrape(row):
    """Открыть карточку и забрать сырые отзывы (один Playwright за раз)."""
    async with SCRAPE_LOCK:
        cfg = MonitorCfg(city_slug=row["city_slug"], domain=row["domain"])
        async with Scraper(cfg) as s:
            c = await s.parse_firm(row["firm_id"])
            return c, s.last_reviews_data


async def _install_browsers() -> str:
    """Автопочинка «Executable doesn't exist»: ставим Chromium тем же
    интерпретатором и в том же окружении, где упал запуск. Возвращает ''
    при успехе, иначе текст причины."""
    global _repair_attempted
    async with _REPAIR_LOCK:
        if _repair_attempted:
            return "автоустановка уже выполнялась в этом запуске"
        _repair_attempted = True
        print(f"[monitor] браузер не найден — запускаю: "
              f"{sys.executable} -m playwright install chromium", flush=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
            if proc.returncode == 0:
                print("[monitor] установка браузера завершена", flush=True)
                return ""
            lines = (out or b"").decode(errors="replace").strip().splitlines()
            return "установка не удалась: " + (lines[-1][:150] if lines
                                               else f"код {proc.returncode}")
        except Exception as e:
            return f"установка не удалась: {e}"


async def notify_chats(conn, text: str, company_id=None) -> list:
    """Разослать text получателям: админ-чаты (получают всё) + чаты,
    подписанные на company_id. Возвращает список ошибок."""
    token = db.get_setting(conn, "bot_token")
    chats = db.notify_targets(conn, company_id)
    errors = []
    if not token:
        return ["Telegram не настроен (нет токена) — уведомление не отправлено"]
    if not chats:
        return ["нет получателей: напишите боту /start (админ) или подпишите "
                "чат на компанию на её странице"]
    for chat in chats:
        try:
            await tg.send_message(token, chat["chat_id"], text)
        except tg.TgError as e:
            errors.append(f"чат {chat['chat_id']}: {e}")
    return errors


async def notify_document(conn, filename: str, content: bytes, caption: str,
                          company_id=None) -> list:
    """Разослать файл (отчёт) тем же получателям, что и notify_chats."""
    token = db.get_setting(conn, "bot_token")
    chats = db.notify_targets(conn, company_id)
    errors = []
    if not token:
        return ["Telegram не настроен (нет токена) — файл не отправлен"]
    if not chats:
        return ["нет получателей: напишите боту /start (админ)"]
    for chat in chats:
        try:
            await tg.send_document(token, chat["chat_id"], filename, content, caption)
        except tg.TgError as e:
            errors.append(f"чат {chat['chat_id']}: {e}")
    return errors


async def _on_check_failed(conn, company_id: int, name: str, err: str):
    """Учесть ошибку проверки; при DEAD_THRESHOLD подряд — алерт админам (один раз)."""
    count = db.bump_errors(conn, company_id)
    if count == DEAD_THRESHOLD:
        await notify_chats(
            conn, f"⚠️ <b>Парсер не справляется</b>: «{name}» — {count} ошибки "
                  f"подряд. Возможно, 2ГИС сменил структуру или блокирует. "
                  f"Последняя: {err[:200]}")


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
        if row["firm_id"] == "demo":  # демо-компания не скрапится
            db.set_company_status(conn, company_id, db.ST_OK, "")
            return {"new": [], "errors": []}
        # Не было ни одной успешной проверки -> это baseline, даже если проверку
        # запустили вручную: первичная загрузка могла упасть (нет браузера и т.п.),
        # и первый успешный прогон не должен счесть все старые отзывы «новыми»
        # и захлестнуть Telegram уведомлениями.
        baseline = baseline or row["last_checked"] is None
        db.set_company_status(conn, company_id,
                              db.ST_ADDING if baseline else db.ST_CHECKING)
        c = data = None
        err = ""
        try:
            c, data = await _scrape(row)
        except Exception as e:
            err = str(e)
        if err and "Executable doesn't exist" in err:
            # самовосстановление: ставим браузер и пробуем ещё раз
            print(f"[monitor] {err}", flush=True)  # полный текст с путём — в консоль
            fail = await _install_browsers()
            if not fail:
                try:
                    c, data = await _scrape(row)
                    err = ""
                except Exception as e2:
                    err = str(e2)
                    fail = "браузер не виден даже после установки"
            if err:
                err = (f"Браузер Playwright недоступен ({fail}). "
                       f"{err.splitlines()[0][:160]} — запустите панель из обычного "
                       f"PowerShell: python app.py")
        if err:
            db.set_company_status(conn, company_id, db.ST_ERROR, err[:300])
            await _on_check_failed(conn, company_id, row["name"], err)
            result["errors"].append(err)
            return result

        if c.name == NA and data is None:
            # ни карточка, ни отзывы не прочитались — не затираем имя, фиксируем ошибку
            db.set_company_status(conn, company_id, db.ST_ERROR,
                                  "карточка не прочиталась (блок/структура изменилась?)")
            await _on_check_failed(conn, company_id, row["name"],
                                   "карточка не прочиталась")
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

        db.clear_errors(conn, company_id)  # успешная проверка — сбросить счётчик

        # снимок метрик для графиков динамики (по загруженному окну отзывов)
        if c.rating is not None:
            answered_pct = (round(100 * sum(1 for r in reviews if r.get("answered"))
                                  / len(reviews)) if reviews else None)
            db.add_rating_snapshot(conn, company_id, c.rating, c.reviews_count,
                                   answered_pct)

        # уведомления: старые первыми, чтобы лента в Telegram шла хронологично
        new.sort(key=lambda r: r.get("date_created") or "")
        notify_all = db.get_setting(conn, "notify_all", "1") == "1"
        fresh_row = db.get_company(conn, company_id)  # с обновлённым именем/тоном
        for rv in new:
            if notify_all or tg.is_negative(rv.get("rating")):
                # AI-варианты ответа; любая ошибка генерации не должна
                # останавливать доставку алерта
                ai = []
                try:
                    ai = await ai_reply.generate_for_company(conn, fresh_row, rv)
                except ai_reply.AiReplyError as e:
                    print(f"[ai] {e}", flush=True)
                    result["errors"].append(str(e))
                result["errors"] += await notify_chats(
                    conn, tg.fmt_review_alert(c.name, row["url"], rv, ai_replies=ai),
                    company_id=company_id)
        result["new"] = new
        return result
    finally:
        conn.close()


_check_times: list = []  # отметки времени последних проверок (лимит/час)


def _hourly_room(cap: int) -> int:
    """Сколько проверок ещё можно сделать в текущем часовом окне."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    _check_times[:] = [t for t in _check_times if t > cutoff]
    return max(0, cap - len(_check_times))


async def send_weekly_digest(company_id: int) -> list:
    """Собрать и разослать еженедельный дайджест по компании. Список ошибок."""
    conn = db.connect()
    try:
        c = db.get_company(conn, company_id)
        if c is None:
            return ["компания удалена"]
        delta = None
        if c["rating"] is not None:
            week_ago = db.rating_days_ago(conn, company_id, 7)
            if week_ago is not None:
                delta = round(c["rating"] - week_ago, 2)
        new7 = db.new_reviews_count(conn, company_id, days=7)
        neg_unanswered = sum(
            1 for r in db.recent_reviews(conn, company_id, limit=50)
            if tg.is_negative(r["rating"]) and not r["answered"])
        text = tg.fmt_weekly_digest(c["name"], c["rating"], delta, new7, neg_unanswered)
        return await notify_chats(conn, text, company_id=company_id)
    finally:
        conn.close()


async def run_auto_tasks():
    """Авто-дайджесты (раз в 7 дней) и авто-отчёты (раз в 30 дней) для компаний,
    у которых это включено в настройках. Отчёт строим максимум один за тик."""
    conn = db.connect()
    try:
        digest_on = db.get_setting(conn, "auto_digest", "0") == "1"
        report_on = db.get_setting(conn, "auto_report", "0") == "1"
        companies = [c for c in db.list_companies(conn)
                     if c["firm_id"] != "demo" and c["status"] == db.ST_OK]
    finally:
        conn.close()
    if digest_on:
        for c in companies:
            conn = db.connect()
            due = db.auto_due(conn, c["id"], "digest", 7)
            conn.close()
            if due:
                await send_weekly_digest(c["id"])
                conn = db.connect(); db.mark_auto(conn, c["id"], "digest"); conn.close()
    if report_on:
        for c in companies:  # дорого (LLM) — не больше одного отчёта за тик
            conn = db.connect()
            due = db.auto_due(conn, c["id"], "report", 30)
            conn.close()
            if due:
                import insights
                try:
                    rid = await insights.build_report(c["id"])
                    conn = db.connect()
                    rep = db.get_report(conn, rid)
                    fname = f"otchet_{c['id']}_{rep['created_at'][:10]}.html"
                    await notify_document(
                        conn, fname, rep["html"].encode("utf-8"),
                        f"📊 Ежемесячный отчёт — {c['name']} · {rep['period']}",
                        company_id=c["id"])
                    db.mark_auto(conn, c["id"], "report")
                    conn.close()
                except Exception as e:
                    print(f"[auto-report] {c['name']}: {e}", flush=True)
                    conn = db.connect(); db.mark_auto(conn, c["id"], "report"); conn.close()
                break


async def scheduler_loop(interval_poll_sec: int = 60):
    """Фоновый цикл: проверять компании, у которых подошёл интервал.

    Очередь под масштаб: порядок перемешивается (джиттер), за час делается не
    больше max_checks_per_hour проверок — чтобы не словить бан 2ГИС при многих
    компаниях. Остаток разъезжается по следующим тикам."""
    from datetime import datetime, timedelta, timezone
    await asyncio.sleep(5)  # дать приложению подняться
    while True:
        try:
            conn = db.connect()
            try:
                interval_min = int(db.get_setting(conn, "check_interval_min", "360"))
                cap = int(db.get_setting(conn, "max_checks_per_hour", "40"))
                rows = db.list_companies(conn)
            finally:
                conn.close()
            now = datetime.now(timezone.utc)
            due = []
            for r in rows:
                if r["status"] in (db.ST_ADDING, db.ST_CHECKING):
                    continue  # уже в работе
                if r["firm_id"] == "demo":
                    continue  # демо не скрапим
                last = r["last_checked"]
                if last is None:
                    due.append(r["id"])
                    continue
                if now - datetime.fromisoformat(last) >= timedelta(minutes=interval_min):
                    due.append(r["id"])
            random.shuffle(due)  # джиттер: не дёргать карточки в одном порядке
            for cid in due:
                if _hourly_room(cap) <= 0:
                    print(f"[scheduler] достигнут лимит {cap} проверок/час — "
                          f"остаток ({len(due)}) перенесён", flush=True)
                    break
                _check_times.append(datetime.now(timezone.utc))
                await check_company(cid)
            await run_auto_tasks()  # дайджесты/авто-отчёты по расписанию
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

    _check_times.clear()
    from datetime import datetime, timedelta, timezone
    checks.append(("лимит/час: пусто -> весь cap", _hourly_room(40) == 40))
    _check_times.extend(datetime.now(timezone.utc) for _ in range(40))
    checks.append(("лимит/час: исчерпан", _hourly_room(40) == 0))
    _check_times[:] = [datetime.now(timezone.utc) - timedelta(hours=2) for _ in range(40)]
    checks.append(("лимит/час: старые отметки забываются", _hourly_room(40) == 40))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
