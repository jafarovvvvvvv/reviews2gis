"""
Мониторинг отзывов: разбор ссылки 2GIS, проверка компании, диф новых отзывов
против БД и рассылка уведомлений в Telegram.

Проверка одной компании = parse_firm (обновляет название/рейтинг/кол-во) +
сырые отзывы из той же сессии (last_reviews_data).

КАСКАД СПОСОБОВ (fetch_mode в настройках, по умолчанию "auto") задан в
fetcher.py — FETCH_MODES / fetch_ways / fetcher_class:
  1. HTTP (fetcher.HttpFetcher) — основной: ~0.9 с на карточку, браузер не нужен;
  2. браузер (scraper.Scraper) — резерв: медленно (~9 с), но переживёт JS-рендер.
Если первый способ не смог — молча пробуем второй и пишем в лог, кто сработал.
Замеры, на которых основан порядок, — в README.

Playwright в один момент времени один — SCRAPE_LOCK (HTTP лок не нужен, но
берём его же, чтобы не долбить 2GIS параллельно).

Чистые функции (parse_2gis_url, extract_review) — самотесты: python monitor.py
"""
import asyncio
import random
import re
import sys
from dataclasses import dataclass

import ai_reply
import db
import fetcher
import tg
from fetcher import FETCH_MODES, BlockedError, fetch_ways, fetcher_class
from lead_scoring import NA

# ссылка на карточку: https://2gis.kz/almaty/firm/700000010… (+ /tab/…, ?…)
URL_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?(2gis\.(?:kz|ru|com|kg|uz|az|by))/([a-z0-9_-]+)/", re.I)
URL_FIRM_RE = re.compile(r"/firm/(\d{6,})")

SCRAPE_LOCK = asyncio.Lock()  # один Playwright одновременно (add/check/scheduler)

# автоустановка браузера: не больше одной попытки на запуск приложения
_REPAIR_LOCK = asyncio.Lock()
_repair_attempted = False

DEAD_THRESHOLD = 3  # столько ошибок подряд у компании -> алерт «парсер умер»

# Каскад способов (FETCH_MODES/fetch_ways/fetcher_class) живёт в fetcher.py —
# им пользуются и мониторинг, и лидогенерация, и аналитика. Реэкспорт здесь —
# ради прежних обращений monitor.FETCH_MODES.


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


async def _scrape(row, mode: str = "auto"):
    """Прочитать карточку и сырые отзывы, перебирая способы по каскаду.

    Возвращает (Company, raw_reviews, способ). Бросает последнюю ошибку, только
    если не сработал ни один способ — тогда это действительно поломка.
    """
    ways = fetch_ways(mode)
    cfg = MonitorCfg(city_slug=row["city_slug"], domain=row["domain"])
    errors = []
    async with SCRAPE_LOCK:
        for i, way in enumerate(ways):
            try:
                async with fetcher_class(way)(cfg) as s:
                    c = await s.parse_firm(row["firm_id"])
                    data = s.last_reviews_data
                    # пустая карточка без отзывов = способ не справился
                    if c.name == NA and data is None:
                        raise fetcher.FetchError("ни карточка, ни отзывы не прочитались")
                    if i:  # сработал не первый способ — это стоит видеть в логах
                        print(f"[monitor] «{row['name']}»: способ «{way}» "
                              f"выручил после отказа «{ways[0]}»", flush=True)
                    return c, data, way
            except BlockedError:
                raise  # блок не лечится сменой способа — с тем же IP смысла нет
            except Exception as e:
                errors.append(f"{way}: {e}")
                if i + 1 < len(ways):
                    print(f"[monitor] способ «{way}» не смог ({str(e)[:120]}) — "
                          f"пробую «{ways[i + 1]}»", flush=True)
    raise fetcher.FetchError("; ".join(errors) or "не осталось способов")


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


async def notify_chats(conn, text: str, company_id=None, reply_markup=None) -> list:
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
            await tg.send_message(token, chat["chat_id"], text, reply_markup=reply_markup)
        except tg.TgError as e:
            errors.append(f"чат {chat['chat_id']}: {e}")
    return errors


# Тестовый получатель для «отправить неотвеченные владельцу» (кнопка на
# странице компании, app.py). Пока у компании нет отдельного поля «чат
# владельца» — рассылка всегда идёт сюда, Эмилю, а не реальным клиентам.
# Заменить/убрать этот хардкод, когда появится привязка чата-получателя
# к конкретному владельцу (а не общий список admin+subscriptions).
TEST_OWNER_CHAT_ID = "398016661"


async def notify_owner_test(conn, text: str, reply_markup=None) -> list:
    """Как notify_chats, но получатель — фиксированный TEST_OWNER_CHAT_ID,
    а не чаты компании (см. предупреждение выше)."""
    token = db.get_setting(conn, "bot_token")
    if not token:
        return ["Telegram не настроен (нет токена) — уведомление не отправлено"]
    try:
        await tg.send_message(token, TEST_OWNER_CHAT_ID, text, reply_markup=reply_markup)
    except tg.TgError as e:
        return [f"чат {TEST_OWNER_CHAT_ID}: {e}"]
    return []


# ---------- модерация: что уходит клиенту сразу, а что сначала мне ----------
#
# Ответы на старте пишутся руками, и клиент не должен получить сырой текст
# раньше, чем его посмотрел владелец сервиса. Режимы (настройка moderation):
#   all      — всё сначала мне (по умолчанию: ничего не улетает клиенту само);
#   negative — негатив (оценка <= 3) на проверку, позитив и нейтраль (4–5)
#              уходят клиенту сразу;
#   off      — всё уходит клиенту сразу (прежнее поведение).
MODERATION_MODES = ("all", "negative", "off")
MODERATION_DEFAULT = "all"


def moderation_mode(conn) -> str:
    """Режим модерации из настроек; мусор в значении -> безопасный «всё ко мне»."""
    mode = db.get_setting(conn, "moderation", MODERATION_DEFAULT)
    return mode if mode in MODERATION_MODES else MODERATION_DEFAULT


def needs_review(mode: str, rating) -> bool:
    """Задержать ли это уведомление на проверку у владельца сервиса."""
    if mode == "off":
        return False
    if mode == "negative":
        # оценки нет — считаем случай спорным и показываем владельцу: ошибиться
        # в эту сторону дешевле, чем отправить клиенту неудачный текст
        return rating is None or tg.is_negative(rating)
    return True  # «all» и любое непонятное значение — задерживаем


async def notify_admins(conn, text: str, reply_markup=None) -> list:
    """Сообщение только владельцу сервиса (админ-чаты), без чатов клиента."""
    token = db.get_setting(conn, "bot_token")
    if not token:
        return ["Telegram не настроен (нет токена) — уведомление не отправлено"]
    chats = db.admin_targets(conn)
    if not chats:
        return ["нет админ-чатов: напишите боту /start"]
    errors = []
    for chat in chats:
        try:
            await tg.send_message(token, chat["chat_id"], text, reply_markup=reply_markup)
        except tg.TgError as e:
            errors.append(f"чат {chat['chat_id']}: {e}")
    return errors


async def notify_clients(conn, text: str, company_id: int, reply_markup=None) -> list:
    """Сообщение только чатам клиента (подписки на компанию, без админских)."""
    token = db.get_setting(conn, "bot_token")
    if not token:
        return ["Telegram не настроен (нет токена) — уведомление не отправлено"]
    chats = db.client_targets(conn, company_id)
    if not chats:
        return ["нет чатов клиента: подпишите чат на компанию на её странице"]
    errors = []
    for chat in chats:
        try:
            await tg.send_message(token, chat["chat_id"], text, reply_markup=reply_markup)
        except tg.TgError as e:
            errors.append(f"чат {chat['chat_id']}: {e}")
    return errors


async def hold_for_review(conn, company_id: int, company_name: str, rv: dict,
                          text: str) -> list:
    """Положить уведомление в очередь и показать его владельцу с кнопками.

    Клиенту ничего не уходит, пока владелец не нажмёт «Отправить клиенту»
    (обработка — bot.handle_callback, действия send/drop).
    """
    pid = db.add_pending(conn, company_id, rv.get("review_id", ""),
                         rv.get("rating"), text)
    kb = tg.inline_kb(
        [tg.btn_cb("✅ Отправить клиенту", f"send:{pid}"),
         tg.btn_cb("🚫 Не отправлять", f"drop:{pid}")],
        [tg.btn_cb("✍ Ещё вариант ответа", f"reply:{company_id}")])
    return await notify_admins(
        conn, tg.fmt_moderation_header(company_name, rv.get("rating"),
                                       db.client_targets(conn, company_id)) + text,
        reply_markup=kb)


async def publish_pending(conn, pending_id: int) -> tuple:
    """Подтверждённое уведомление -> чатам клиента. (ok, текст результата)."""
    row = db.get_pending(conn, pending_id)
    if row is None:
        return False, "Уведомление не найдено."
    if row["status"] != db.PENDING:
        return False, ("Уже отправлено клиенту." if row["status"] == db.SENT
                       else "Это уведомление уже отклонено.")
    # занимаем запись ДО отправки: два админ-чата не должны отправить дважды
    if not db.decide_pending(conn, pending_id, db.SENT):
        return False, "Кто-то уже разобрал это уведомление."
    errors = await notify_clients(conn, row["text"], row["company_id"])
    if errors:
        # не ушло -> возвращаем в очередь, кнопка должна работать повторно
        db.reopen_pending(conn, pending_id)
        return False, "Не ушло: " + "; ".join(errors)
    return True, "Отправлено клиенту."


# Кому уходит отчёт. Решение принимается на каждой отправке (кнопка в панели,
# команда бота) и для авто-рассылки берётся из настройки report_to.
#   me  — только мне (админ-чаты): посмотреть перед тем, как показывать клиенту;
#   all — мне и чатам, привязанным к компании.
REPORT_TO = ("me", "all")
REPORT_TO_DEFAULT = "me"
REPORT_TO_LABELS = {"me": "только мне", "all": "мне и чатам компании"}


def report_to_mode(conn) -> str:
    """Кому уходит АВТО-отчёт по расписанию; мусор -> безопасное «только мне»."""
    mode = db.get_setting(conn, "report_to", REPORT_TO_DEFAULT)
    return mode if mode in REPORT_TO else REPORT_TO_DEFAULT


async def notify_document(conn, filename: str, content: bytes, caption: str,
                          company_id=None, to: str = "all") -> list:
    """Разослать файл (отчёт). to='me' — только админ-чаты, 'all' — как notify_chats."""
    token = db.get_setting(conn, "bot_token")
    chats = (db.admin_targets(conn) if to == "me"
             else db.notify_targets(conn, company_id))
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


# Компании, по которым алерт «не читается» уже отправлен. Пока компания здесь,
# новых сообщений о ней нет — придёт только «✅ снова читается».
_alerted: set = set()


async def _on_check_failed(conn, company_id: int, name: str, err: str):
    """Учесть ошибку проверки; при DEAD_THRESHOLD подряд — ОДИН алерт админам.

    Повторные ошибки той же компании молчат: пока проблема не устранена, слать
    одно и то же каждые несколько минут бессмысленно (раньше так и было —
    Telegram захлёбывался одинаковыми «Парсер не справляется»).
    """
    count = db.bump_errors(conn, company_id)
    if count < DEAD_THRESHOLD or company_id in _alerted:
        return
    _alerted.add(company_id)
    total = len(_alerted)
    tail = (f"\n\nВсего компаний с проблемой: <b>{total}</b> — похоже на общий "
            f"сбой (сеть/блокировка), а не на конкретную карточку."
            if total > 1 else "")
    await notify_chats(
        conn, f"⚠️ <b>Не читается карточка</b>: «{name}» — {count} ошибки подряд. "
              f"Проверки продолжаются, о восстановлении сообщу.\n"
              f"Причина: {err[:200]}{tail}")


async def _on_check_ok(conn, company_id: int, name: str, way: str):
    """Успешная проверка: сбросить счётчик и, если по компании был алерт,
    сообщить о восстановлении."""
    db.clear_errors(conn, company_id)
    if company_id in _alerted:
        _alerted.discard(company_id)
        await notify_chats(
            conn, f"✅ <b>Снова читается</b>: «{name}» — данные обновляются "
                  f"(способ: {way}).")


async def check_company(company_id: int, baseline: bool = False) -> dict:
    """Проверить компанию: обновить карточку, найти новые отзывы, уведомить.

    baseline=True — первичная загрузка после добавления: текущие отзывы
    запоминаются молча, уведомлений нет.
    Возвращает {"new": [...], "errors": [...]}.
    """
    conn = db.connect()
    result = {"new": [], "errors": [], "held": 0}
    try:
        row = db.get_company(conn, company_id)
        if row is None:
            return {"new": [], "errors": ["компания удалена"], "held": 0}
        if row["firm_id"] == "demo":  # демо-компания не скрапится
            db.set_company_status(conn, company_id, db.ST_OK, "")
            return {"new": [], "errors": [], "held": 0}
        # Не было ни одной успешной проверки -> это baseline, даже если проверку
        # запустили вручную: первичная загрузка могла упасть (нет браузера и т.п.),
        # и первый успешный прогон не должен счесть все старые отзывы «новыми»
        # и захлестнуть Telegram уведомлениями.
        baseline = baseline or row["last_checked"] is None
        db.set_company_status(conn, company_id,
                              db.ST_ADDING if baseline else db.ST_CHECKING)
        mode = db.get_setting(conn, "fetch_mode", "auto")
        c = data = None
        err = ""
        try:
            c, data, way = await _scrape(row, mode)
        except Exception as e:
            err = str(e)
        if err and "Executable doesn't exist" in err:
            # самовосстановление: ставим браузер и пробуем ещё раз.
            # (при mode=auto досюда обычно не доходит — HTTP-способ уже справился)
            print(f"[monitor] {err}", flush=True)  # полный текст с путём — в консоль
            fail = await _install_browsers()
            if not fail:
                try:
                    c, data, way = await _scrape(row, mode)
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

        reviews = [extract_review(r) for r in (data or {}).get("reviews") or []]
        new = db.save_reviews(conn, company_id, reviews, mark_new=not baseline)
        db.update_company_info(
            conn, company_id,
            name=None if c.name == NA else c.name,
            category=None if c.category in (NA, "") else c.category,
            rating=c.rating, reviews_count=c.reviews_count,
            status=db.ST_OK, error="")

        # успешная проверка: сбросить счётчик + сообщить, если был алерт
        await _on_check_ok(conn, company_id, c.name if c.name != NA else row["name"], way)

        # снимок метрик для графиков динамики (по загруженному окну отзывов)
        if c.rating is not None:
            # доля ответов владельца по тому же окну, что и остальные счётчики
            answered_pct = (round(100 * sum(1 for r in reviews if r.get("answered"))
                                  / len(reviews)) if reviews else None)
            db.add_rating_snapshot(conn, company_id, c.rating, c.reviews_count,
                                   answered_pct)

        # уведомления: старые первыми, чтобы лента в Telegram шла хронологично
        new.sort(key=lambda r: r.get("date_created") or "")
        notify_all = db.get_setting(conn, "notify_all", "1") == "1"
        mode = moderation_mode(conn)
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
                text = tg.fmt_review_alert(c.name, row["url"], rv, ai_replies=ai)
                if needs_review(mode, rv.get("rating")):
                    # клиенту не уходит ничего, пока владелец не подтвердит
                    result["errors"] += await hold_for_review(
                        conn, company_id, c.name, rv, text)
                    result["held"] += 1
                    continue
                kb = tg.inline_kb(
                    [tg.btn_cb("✍ Ещё вариант ответа", f"reply:{company_id}")],
                    [tg.btn_url("Открыть в 2ГИС", row["url"])])
                result["errors"] += await notify_chats(
                    conn, text, company_id=company_id, reply_markup=kb)
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


async def build_and_send_report(company_id: int, to: str = REPORT_TO_DEFAULT,
                                brand: str = "") -> tuple:
    """Собрать отчёт за неделю и разослать. Возвращает (id отчёта, ошибки).

    Единственный путь отправки отчёта: им пользуются кнопка в панели, команда
    бота и расписание — чтобы «отправить себе» везде значило одно и то же.
    Исключение сборки не глотаем: вызывающий решает, как о нём сказать.
    """
    import weekly_report
    rid = await weekly_report.build_report(company_id, brand=brand)
    conn = db.connect()
    try:
        rep = db.get_report(conn, rid)
        comp = db.get_company(conn, company_id)
        name = comp["name"] if comp else "компания"
        fname = f"otchet_{company_id}_{rep['created_at'][:10]}.html"
        errors = await notify_document(
            conn, fname, rep["html"].encode("utf-8"),
            f"📊 Отчёт за неделю — {name} · {rep['period']}",
            company_id=company_id, to=to)
    finally:
        conn.close()
    return rid, errors


async def run_auto_tasks():
    """Авто-дайджесты (раз в 7 дней) и еженедельные отчёты для компаний,
    у которых это включено в настройках. Отчёт строим максимум один за тик.

    Отчёт клиенту — weekly_report.py, это вторая часть оффера («что изменилось
    и на что жалуются чаще всего»). Уходит НЕ «раз в N дней от последнего
    запуска», а в понедельник утром (db.weekly_due): иначе время отправки
    уползало бы вперёд с каждой неделей. Кому уходит — настройка report_to,
    по умолчанию только мне. Глубокий разбор болей (insights.py) остался
    отдельным инструментом на заказ — из панели и по команде /report.
    """
    conn = db.connect()
    try:
        digest_on = db.get_setting(conn, "auto_digest", "0") == "1"
        report_on = db.get_setting(conn, "auto_report", "0") == "1"
        report_to = report_to_mode(conn)
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
            due = db.weekly_due(conn, c["id"], "report")
            conn.close()
            if not due:
                continue
            try:
                _, errors = await build_and_send_report(c["id"], to=report_to)
                for err in errors:
                    print(f"[auto-report] {c['name']}: {err}", flush=True)
            except Exception as e:
                print(f"[auto-report] {c['name']}: {e}", flush=True)
            # отметку ставим в любом случае: иначе упавшая компания будет
            # пытаться строить отчёт каждую минуту до конца понедельника
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

    # --- каскад способов (сам каскад тестируется в fetcher.py) ---
    checks.append(("каскад: у каждого способа есть класс-читатель",
                   all(fetcher_class(w) is not None
                       for ws in FETCH_MODES.values() for w in ws)))
    checks.append(("каскад: у читателей есть parse_firm",
                   all(hasattr(fetcher_class(w), "parse_firm")
                       for ws in FETCH_MODES.values() for w in ws)))

    # --- антиспам алертов ---
    class _FakeConn:
        pass

    sent = []

    async def _fake_notify(conn, text, company_id=None, reply_markup=None):
        sent.append(text)
        return []

    _real_notify, _real_bump, _real_clear = notify_chats, db.bump_errors, db.clear_errors
    _counters = {}
    try:
        globals()["notify_chats"] = _fake_notify
        db.bump_errors = lambda conn, cid: _counters.__setitem__(
            cid, _counters.get(cid, 0) + 1) or _counters[cid]
        db.clear_errors = lambda conn, cid: _counters.pop(cid, None)

        _alerted.clear()
        conn = _FakeConn()
        for _ in range(2):
            asyncio.run(_on_check_failed(conn, 1, "Клиника", "таймаут"))
        checks.append(("алерты: до порога молчим", sent == []))
        asyncio.run(_on_check_failed(conn, 1, "Клиника", "таймаут"))
        checks.append(("алерты: на 3-й ошибке одно сообщение", len(sent) == 1))
        for _ in range(5):
            asyncio.run(_on_check_failed(conn, 1, "Клиника", "таймаут"))
        checks.append(("алерты: повторы молчат", len(sent) == 1))

        asyncio.run(_on_check_ok(conn, 1, "Клиника", "http"))
        checks.append(("алерты: восстановление сообщается",
                       len(sent) == 2 and "Снова читается" in sent[1]))
        asyncio.run(_on_check_ok(conn, 1, "Клиника", "http"))
        checks.append(("алерты: без поломки — молчим о восстановлении", len(sent) == 2))

        sent.clear()
        _alerted.clear()
        _counters.clear()
        for cid, nm in ((1, "A"), (2, "B")):
            for _ in range(3):
                asyncio.run(_on_check_failed(conn, cid, nm, "таймаут"))
        checks.append(("алерты: у второй компании — пометка об общем сбое",
                       len(sent) == 2 and "общий сбой" in sent[1]))
    finally:
        globals()["notify_chats"] = _real_notify
        db.bump_errors, db.clear_errors = _real_bump, _real_clear
        _alerted.clear()

    # --- модерация: кто что получает ---
    checks.append(("модерация: по умолчанию всё сначала владельцу",
                   MODERATION_DEFAULT == "all"
                   and needs_review("all", 5) and needs_review("all", 1)))
    checks.append(("модерация: режим negative — негатив на проверку",
                   needs_review("negative", 3) and needs_review("negative", 1)))
    checks.append(("модерация: режим negative — 4 и 5 уходят клиенту сразу",
                   not needs_review("negative", 4)
                   and not needs_review("negative", 5)))
    checks.append(("модерация: режим off — ничего не задерживаем",
                   not needs_review("off", 1) and not needs_review("off", 5)))
    checks.append(("модерация: без оценки — на проверку, а не мимо владельца",
                   needs_review("negative", None)))

    _settings = {}
    _real_get = db.get_setting
    try:
        db.get_setting = lambda conn, key, default="": _settings.get(key, default)
        checks.append(("модерация: пустая настройка -> режим по умолчанию",
                       moderation_mode(None) == MODERATION_DEFAULT))
        _settings["moderation"] = "negative"
        checks.append(("модерация: настройка читается", moderation_mode(None) == "negative"))
        _settings["moderation"] = "мусор"
        checks.append(("модерация: мусор в настройке -> безопасный режим",
                       moderation_mode(None) == MODERATION_DEFAULT))
    finally:
        db.get_setting = _real_get

    # --- сквозная проверка модерации на временной БД ---
    # Самое дорогое место, где можно ошибиться: клиент не должен получить текст,
    # которого владелец не видел, и не должен получить его дважды.
    import tempfile
    from pathlib import Path

    _real_path, _real_send = db.DB_PATH, tg.send_message
    _tmp = tempfile.TemporaryDirectory()
    sent = []

    async def _fake_send(token, chat_id, text, reply_markup=None):
        sent.append((str(chat_id), text, bool(reply_markup)))
        return {}

    try:
        db.DB_PATH = Path(_tmp.name) / "t.db"
        tg.send_message = _fake_send
        conn = db.connect()
        db.set_setting(conn, "bot_token", "T")
        cid = db.add_company(conn, "111", "https://2gis.kz", "almaty", "u")
        db.update_company_info(conn, cid, name="Клиника", status=db.ST_OK, error="")
        db.add_chat(conn, "1", "Мой чат")        # первый чат становится админом
        db.add_chat(conn, "2", "Чат клиента")
        db.set_subscriptions(conn, cid, ["2"])

        rv = {"review_id": "r1", "rating": 5, "text": "Спасибо!"}
        asyncio.run(hold_for_review(conn, cid, "Клиника", rv, "ТЕЛО"))
        checks.append(("модерация: задержанное ушло только владельцу, с кнопками",
                       [c for c, _, _ in sent] == ["1"] and sent[0][2] is True
                       and "На проверку" in sent[0][1] and "ТЕЛО" in sent[0][1]))
        checks.append(("модерация: в шапке назван чат-получатель",
                       "Чат клиента" in sent[0][1]))

        pid = db.list_pending(conn, cid)[0]["id"]
        sent.clear()
        ok_pub, note = asyncio.run(publish_pending(conn, pid))
        checks.append(("модерация: подтверждение уходит только клиенту",
                       ok_pub and [c for c, _, _ in sent] == ["2"]
                       and sent[0][1] == "ТЕЛО"))
        sent.clear()
        ok_again, note2 = asyncio.run(publish_pending(conn, pid))
        checks.append(("модерация: второе нажатие не шлёт дубль",
                       ok_again is False and sent == []
                       and "Уже отправлено" in note2))

        # --- получатели отчёта: «только мне» не должно задевать чат клиента ---
        docs = []

        async def _fake_doc(token, chat_id, filename, content, caption=""):
            docs.append(str(chat_id))
            return {}

        _real_doc = tg.send_document
        try:
            tg.send_document = _fake_doc
            asyncio.run(notify_document(conn, "f.html", b"x", "c",
                                        company_id=cid, to="me"))
            checks.append(("отчёт «только мне»: ушло в админ-чат и никуда больше",
                           docs == ["1"]))
            docs.clear()
            asyncio.run(notify_document(conn, "f.html", b"x", "c",
                                        company_id=cid, to="all"))
            checks.append(("отчёт «мне и чатам компании»: оба получателя",
                           sorted(docs) == ["1", "2"]))
        finally:
            tg.send_document = _real_doc

        _settings2 = {}
        _real_get2 = db.get_setting
        try:
            db.get_setting = lambda conn, key, default="": _settings2.get(key, default)
            checks.append(("авто-отчёт: по умолчанию только мне",
                           report_to_mode(None) == "me"
                           and REPORT_TO_DEFAULT == "me"))
            _settings2["report_to"] = "мусор"
            checks.append(("авто-отчёт: мусор в настройке -> только мне",
                           report_to_mode(None) == "me"))
            _settings2["report_to"] = "all"
            checks.append(("авто-отчёт: настройка читается",
                           report_to_mode(None) == "all"))
        finally:
            db.get_setting = _real_get2

        db.set_subscriptions(conn, cid, [])   # чат клиента отписали
        pid2 = db.add_pending(conn, cid, "r2", 1, "ТЕЛО 2")
        sent.clear()
        ok_none, note3 = asyncio.run(publish_pending(conn, pid2))
        checks.append(("модерация: некому слать -> честная ошибка, не «успех»",
                       ok_none is False and "Не ушло" in note3))
        checks.append(("модерация: сорвавшаяся отправка возвращается в очередь",
                       db.get_pending(conn, pid2)["status"] == db.PENDING))
        conn.close()
    finally:
        db.DB_PATH, tg.send_message = _real_path, _real_send
        _tmp.cleanup()

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
