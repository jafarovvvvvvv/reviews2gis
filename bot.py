"""
Telegram-бот: длинный поллинг getUpdates + админ-команды.

Привязка чата — самообслуживание: любой чат пишет боту /start и появляется
в панели. Самый первый чат становится админом (это владелец); админ-чаты
получают уведомления по всем компаниям и могут управлять ботом командами.
Остальные чаты (клиентские группы) получают только компании, на которые их
подписали на странице компании в панели.

Команды (только из админ-чатов, кроме /start и /help):
  /list            — компании со статусами
  /add <ссылка>    — добавить компанию по ссылке 2GIS
  /del <N>         — удалить компанию №N из /list
  /check [N]       — проверить все компании (или одну №N)
  /weekly <N> [all] — отчёт за неделю (weekly_report.py); без аргумента —
                     только в админ-чаты, all — ещё и чатам компании
  /report <N>      — глубокий разбор болей (insights.py)
  /status          — настройки и состояние
  /interval <мин>  — интервал автопроверки (минимум 30)
  /mode all|neg    — все отзывы или только негатив
  /mod all|neg|off — что уходит клиенту только после моего подтверждения
  /test            — тестовое сообщение в этот чат

Уведомление, задержанное на проверку (monitor.hold_for_review), приходит
владельцу сервиса с кнопками «Отправить клиенту» / «Не отправлять» —
callback-действия send:<id> и drop:<id>.

Поллинг — единственный потребитель getUpdates (кнопки «Найти чаты» в панели
больше нет — она конфликтовала бы по 409). Разбор команд — чистая функция
parse_command, самотесты: python bot.py
"""
import asyncio
import html

import ai_reply
import db
import insights
import monitor
import tg

HELP_ADMIN = (
    "Команды:\n"
    "/list — компании со статусами\n"
    "/add <ссылка 2ГИС> — добавить компанию\n"
    "/del <N> — удалить компанию №N\n"
    "/check [N] — проверить все (или №N)\n"
    "/reply <N> — AI-ответ на последний отзыв компании №N\n"
    "/weekly <N> [all] — отчёт за неделю по компании №N (all — и чатам компании)\n"
    "/report <N> — глубокий разбор болей по компании №N\n"
    "/digest <N> — еженедельный дайджест по компании №N сейчас\n"
    "/status — настройки и состояние\n"
    "/interval <мин> — интервал автопроверки\n"
    "/mode all|neg — все отзывы / только негатив\n"
    "/mod all|neg|off — что показывать мне до отправки клиенту\n"
    "/test — тест в этот чат"
)

# Подписи режимов модерации (monitor.MODERATION_MODES) — одни и те же
# в /status, /mod и в настройках панели.
MOD_LABELS = {
    "all": "всё сначала мне",
    "negative": "негатив мне, позитив клиенту сразу",
    "off": "всё клиенту сразу",
}
HELP_GUEST = ("Этот чат получает уведомления по компаниям, на которые его "
              "подписали в панели. Управление — из админ-чата или панели.")

_BG_TASKS: set = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def parse_command(text: str, bot_username: str = ""):
    """'/check 3' -> ('check', '3'); '/list@OtherBot' чужому боту -> None."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, args = text.partition(" ")
    cmd = head[1:].lower()
    if "@" in cmd:
        cmd, _, mention = cmd.partition("@")
        if bot_username and mention.lower() != bot_username.lower():
            return None  # команда адресована другому боту в группе
    return (cmd, args.strip()) if cmd else None


def _fmt_company_line(i: int, c) -> str:
    e = html.escape
    rating = "—" if c["rating"] is None else f"{c['rating']:.1f}".replace(".", ",")
    count = "—" if c["reviews_count"] is None else c["reviews_count"]
    status = {db.ST_OK: "ок", db.ST_ERROR: "⚠ ошибка",
              db.ST_ADDING: "загружается", db.ST_CHECKING: "проверяется"}.get(
        c["status"], c["status"])
    return f"{i}. <b>{e(c['name'])}</b> — {rating}/{count} отз., {status}"


async def _reply(token: str, chat_id: str, text: str):
    try:
        await tg.send_message(token, chat_id, text)
    except tg.TgError as e:
        print(f"[bot] ответ в {chat_id}: {e}", flush=True)


async def _add_and_report(company_id: int, token: str, chat_id: str):
    """Фоновая первичная загрузка после /add + отчёт в чат."""
    res = await monitor.check_company(company_id, baseline=True)
    conn = db.connect()
    try:
        c = db.get_company(conn, company_id)
    finally:
        conn.close()
    if c is None:
        return
    if c["status"] == db.ST_OK:
        rating = "—" if c["rating"] is None else f"{c['rating']:.1f}".replace(".", ",")
        await _reply(token, chat_id,
                     f"✅ «{html.escape(c['name'])}» добавлена: рейтинг {rating}, "
                     f"отзывов {c['reviews_count']}. Текущие отзывы запомнены, "
                     f"уведомления пойдут о новых.")
    else:
        await _reply(token, chat_id,
                     f"⚠ Компанию добавил, но загрузка не удалась: "
                     f"{html.escape(c['error'] or 'неизвестная ошибка')}")


async def _build_and_send_report(company_id: int, token: str, chat_id: str):
    """Фон: отчёт болей + отправка файлом в чат, откуда попросили."""
    try:
        rid = await insights.build_report(company_id)
    except Exception as ex:
        await _reply(token, chat_id, f"⚠ Отчёт не построился: {html.escape(str(ex)[:300])}")
        return
    conn = db.connect()
    try:
        rep = db.get_report(conn, rid)
        comp = db.get_company(conn, company_id)
    finally:
        conn.close()
    try:
        await tg.send_document(
            token, chat_id, f"otchet_{company_id}_{rep['created_at'][:10]}.html",
            rep["html"].encode("utf-8"),
            f"📊 Аналитика отзывов — {comp['name']} · {rep['period']} · "
            f"в анализе {rep['reviews_used']} отзывов")
    except tg.TgError as e:
        await _reply(token, chat_id, f"⚠ Файл не отправился: {html.escape(str(e))}")


async def _build_and_send_weekly(company_id: int, to: str, token: str, chat_id: str):
    """Фон: отчёт за неделю через общий путь monitor.build_and_send_report.

    Своей рассылки здесь нет намеренно: «только мне» из бота и «только мне» из
    панели должны означать один и тот же список получателей.
    """
    try:
        _, errors = await monitor.build_and_send_report(company_id, to=to)
    except Exception as ex:
        await _reply(token, chat_id,
                     f"⚠ Отчёт за неделю не построился: {html.escape(str(ex)[:300])}")
        return
    note = f"📊 Отчёт за неделю готов и отправлен ({monitor.REPORT_TO_LABELS[to]})."
    if errors:
        note += "\n⚠ " + html.escape("; ".join(errors)[:300])
    await _reply(token, chat_id, note)


async def _check_and_report(ids: list, token: str, chat_id: str):
    """Последовательная проверка (Playwright один) + краткий отчёт."""
    total_new = 0
    total_held = 0
    failed = 0
    for cid in ids:
        res = await monitor.check_company(cid)
        total_new += len(res["new"])
        total_held += res.get("held", 0)
        if res["errors"]:
            failed += 1
    msg = f"Проверка завершена: компаний {len(ids)}, новых отзывов {total_new}"
    if total_held:
        msg += f", из них на проверке у вас {total_held}"
    if failed:
        msg += f", с ошибками {failed} (детали в панели)"
    await _reply(token, chat_id, msg + ".")


async def handle_command(cmd: str, args: str, chat: dict, token: str):
    """Обработать команду из чата. chat: {id, title, is_admin, is_new}."""
    chat_id = chat["id"]
    e = html.escape

    if cmd == "start":
        if chat["is_new"] and chat["is_admin"]:
            await _reply(token, chat_id,
                         "✅ Чат привязан как <b>админ</b>: сюда идут уведомления по "
                         "всем компаниям и доступно управление.\n\n" + HELP_ADMIN)
        elif chat["is_new"]:
            await _reply(token, chat_id,
                         "✅ Чат привязан. Подпишите его на нужные компании на "
                         "странице компании в панели — сюда пойдут их отзывы.")
        else:
            await _reply(token, chat_id, "Чат уже привязан. " +
                         (HELP_ADMIN if chat["is_admin"] else HELP_GUEST))
        return

    if cmd == "help":
        await _reply(token, chat_id, HELP_ADMIN if chat["is_admin"] else HELP_GUEST)
        return

    if not chat["is_admin"]:
        await _reply(token, chat_id, "Команда доступна только из админ-чата. " + HELP_GUEST)
        return

    conn = db.connect()
    try:
        companies = db.list_companies(conn)

        if cmd == "list":
            if not companies:
                await _reply(token, chat_id, "Компаний пока нет. /add <ссылка 2ГИС>")
                return
            lines = [_fmt_company_line(i, c) for i, c in enumerate(companies, 1)]
            await _reply(token, chat_id, "\n".join(lines))

        elif cmd == "add":
            parsed = monitor.parse_2gis_url(args)
            if not parsed:
                await _reply(token, chat_id,
                             "Пришлите ссылку на карточку: /add https://2gis.kz/"
                             "&lt;город&gt;/firm/&lt;номер&gt;")
                return
            domain, slug, firm_id = parsed
            if db.find_company_by_firm(conn, firm_id):
                await _reply(token, chat_id, "Эта компания уже добавлена (/list).")
                return
            cid = db.add_company(conn, firm_id, domain, slug,
                                 f"{domain}/{slug}/firm/{firm_id}")
            await _reply(token, chat_id, "Добавляю: загружаю карточку (~полминуты)…")
            _spawn(_add_and_report(cid, token, chat_id))

        elif cmd == "del":
            c = _company_by_num(companies, args)
            if c is None:
                await _reply(token, chat_id, "Укажите номер из /list: /del 2")
                return
            db.delete_company(conn, c["id"])
            await _reply(token, chat_id, f"🗑 «{e(c['name'])}» удалена вместе с отзывами.")

        elif cmd == "check":
            if args:
                c = _company_by_num(companies, args)
                if c is None:
                    await _reply(token, chat_id, "Укажите номер из /list: /check 2")
                    return
                targets = [c["id"]]
            else:
                targets = [c["id"] for c in companies
                           if c["status"] not in (db.ST_ADDING, db.ST_CHECKING)]
            if not targets:
                await _reply(token, chat_id, "Проверять нечего.")
                return
            await _reply(token, chat_id, f"Проверяю ({len(targets)})… Новые отзывы "
                                         f"придут отдельными сообщениями.")
            _spawn(_check_and_report(targets, token, chat_id))

        elif cmd == "status":
            interval = db.get_setting(conn, "check_interval_min", "360")
            mode = ("все отзывы" if db.get_setting(conn, "notify_all", "1") == "1"
                    else "только негатив (≤3)")
            chats = db.list_chats(conn)
            admins = sum(1 for c in chats if c["is_admin"])
            held = len(db.list_pending(conn))
            await _reply(token, chat_id,
                         f"Компаний: {len(companies)}\n"
                         f"Чатов: {len(chats)} (админских {admins})\n"
                         f"Интервал автопроверки: {interval} мин\n"
                         f"Режим уведомлений: {mode}\n"
                         f"Проверка перед отправкой клиенту: "
                         f"{MOD_LABELS[monitor.moderation_mode(conn)]}\n"
                         f"Ждут вашего решения: {held}")

        elif cmd == "interval":
            try:
                val = max(30, int(args))
            except ValueError:
                await _reply(token, chat_id, "Так: /interval 120 (минут, минимум 30)")
                return
            db.set_setting(conn, "check_interval_min", str(val))
            await _reply(token, chat_id, f"Интервал автопроверки: {val} мин.")

        elif cmd == "mode":
            if args.lower() in ("all", "все"):
                db.set_setting(conn, "notify_all", "1")
                await _reply(token, chat_id, "Режим: уведомлять обо всех новых отзывах.")
            elif args.lower() in ("neg", "негатив"):
                db.set_setting(conn, "notify_all", "0")
                await _reply(token, chat_id, "Режим: только негатив (оценка ≤3).")
            else:
                await _reply(token, chat_id, "Так: /mode all — все, /mode neg — только негатив")

        elif cmd == "mod":
            key = args.lower().strip()
            aliases = {"all": "all", "всё": "all", "все": "all",
                       "neg": "negative", "negative": "negative", "негатив": "negative",
                       "off": "off", "выкл": "off"}
            if key in aliases:
                db.set_setting(conn, "moderation", aliases[key])
                await _reply(token, chat_id,
                             f"Проверка перед отправкой клиенту: "
                             f"{MOD_LABELS[aliases[key]]}.")
            else:
                await _reply(token, chat_id,
                             "Так: /mod all — всё сначала мне, /mod neg — только "
                             "негатив на проверку, /mod off — всё клиенту сразу.\n"
                             f"Сейчас: {MOD_LABELS[monitor.moderation_mode(conn)]}.")

        elif cmd == "reply":
            c = _company_by_num(companies, args)
            if c is None:
                await _reply(token, chat_id, "Укажите номер из /list: /reply 2")
                return
            await _send_reply_variants(c["id"], token, chat_id)

        elif cmd == "report":
            c = _company_by_num(companies, args)
            if c is None:
                await _reply(token, chat_id, "Укажите номер из /list: /report 2")
                return
            api_key, _ = ai_reply.get_ai_settings(conn)
            if not api_key:
                await _reply(token, chat_id, "AI не настроен: добавьте ключ OpenAI "
                                             "в «Настройках» панели.")
                return
            await _reply(token, chat_id,
                         f"Готовлю разбор болей по «{e(c['name'])}»: сбор до 300 "
                         f"отзывов и анализ, ~2–4 минуты…")
            _spawn(_build_and_send_report(c["id"], token, chat_id))

        elif cmd == "weekly":
            # «/weekly 2» — только мне, «/weekly 2 all» — ещё и чатам компании.
            # Умолчание намеренно осторожное: отчёт клиенту уходит, только если
            # об этом попросили явно.
            num, _, rest = args.partition(" ")
            to = "all" if rest.strip().lower() in ("all", "все", "клиенту") else "me"
            c = _company_by_num(companies, num)
            if c is None:
                await _reply(token, chat_id,
                             "Укажите номер из /list: /weekly 2 (только мне) "
                             "или /weekly 2 all (и чатам компании)")
                return
            await _reply(token, chat_id,
                         f"Готовлю отчёт за неделю по «{e(c['name'])}» "
                         f"({monitor.REPORT_TO_LABELS[to]})…")
            _spawn(_build_and_send_weekly(c["id"], to, token, chat_id))

        elif cmd == "digest":
            c = _company_by_num(companies, args)
            if c is None:
                await _reply(token, chat_id, "Укажите номер из /list: /digest 2")
                return
            errs = await monitor.send_weekly_digest(c["id"])
            await _reply(token, chat_id,
                         "Дайджест отправлен." if not errs else "; ".join(errs[:3]))

        elif cmd == "test":
            await _reply(token, chat_id, "🔔 Тест: уведомления работают.")

        else:
            await _reply(token, chat_id, "Не знаю такой команды. /help")
    finally:
        conn.close()


def _company_by_num(companies, args: str):
    """Номер из /list (1-based) -> строка компании или None."""
    try:
        i = int(args)
    except ValueError:
        return None
    return companies[i - 1] if 1 <= i <= len(companies) else None


async def _send_reply_variants(company_id: int, token: str, chat_id: str):
    """Сгенерировать AI-ответы на последний отзыв компании и прислать в чат.
    Общая логика для команды /reply и inline-кнопки «Ещё вариант»."""
    conn = db.connect()
    try:
        c = db.get_company(conn, company_id)
        if c is None:
            await _reply(token, chat_id, "Компания не найдена.")
            return
        rv = db.latest_review(conn, company_id)
        if rv is None:
            await _reply(token, chat_id, "У этой компании ещё нет отзывов в базе.")
            return
        api_key, _ = ai_reply.get_ai_settings(conn)
        if not api_key:
            await _reply(token, chat_id, "AI не настроен: добавьте ключ OpenAI "
                                         "в «Настройках» панели.")
            return
        try:
            variants = await ai_reply.generate_for_company(conn, c, dict(rv))
        except ai_reply.AiReplyError as ex:
            await _reply(token, chat_id, f"⚠ {html.escape(str(ex))}")
            return
    finally:
        conn.close()
    if not variants:
        await _reply(token, chat_id, "Модель не вернула вариантов — повторите.")
        return
    text = (rv["text"] or "(без текста)")[:200]
    parts = [f"Отзыв ({tg.stars(rv['rating'])}): «{html.escape(text)}»"]
    for i, v in enumerate(variants, 1):
        parts.append(f"Вариант {i} (тап — скопируется):\n<pre>{html.escape(v)}</pre>")
    await _reply(token, chat_id, "\n".join(parts))


async def _decide_pending(pending_id: int, send: bool, token: str, chat_id: str):
    """Кнопка под задержанным уведомлением: отпустить клиенту или закрыть."""
    conn = db.connect()
    try:
        if send:
            _, note = await monitor.publish_pending(conn, pending_id)
        elif db.decide_pending(conn, pending_id, db.DROPPED):
            note = "Уведомление закрыто, клиенту ничего не ушло."
        else:
            note = "Это уведомление уже разобрано."
    finally:
        conn.close()
    await _reply(token, chat_id, note)


async def handle_callback(cb: dict, token: str):
    """Нажатие inline-кнопки. Действия — только из админ-чатов."""
    data = cb.get("data") or ""
    chat = (cb.get("message") or {}).get("chat") or {}
    chat_id = str(chat.get("id") or "")
    cb_id = cb.get("id")
    conn = db.connect()
    try:
        row = db.get_chat(conn, chat_id)
        is_admin = bool(row and row["is_admin"])
    finally:
        conn.close()
    if not is_admin:
        await tg.answer_callback(token, cb_id, "Доступно только из админ-чата")
        return
    action, _, arg = data.partition(":")
    if action == "reply" and arg.isdigit():
        await tg.answer_callback(token, cb_id, "Генерирую ещё вариант…")
        _spawn(_send_reply_variants(int(arg), token, chat_id))
    elif action == "check" and arg.isdigit():
        await tg.answer_callback(token, cb_id, "Запускаю проверку…")
        _spawn(_check_and_report([int(arg)], token, chat_id))
    elif action in ("send", "drop") and arg.isdigit():
        # решение по задержанному уведомлению (monitor.hold_for_review):
        # «send» отпускает текст клиенту, «drop» закрывает без отправки
        await tg.answer_callback(token, cb_id,
                                 "Отправляю…" if action == "send" else "Не отправляю")
        _spawn(_decide_pending(int(arg), action == "send", token, chat_id))
    else:
        await tg.answer_callback(token, cb_id)


async def handle_update(update: dict, token: str, bot_username: str):
    cb = update.get("callback_query")
    if cb:
        await handle_callback(cb, token)
        return
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""
    if not chat_id or not text:
        return
    parsed = parse_command(text, bot_username)
    if parsed is None:
        return  # не команда (или команда чужому боту) — молчим
    cmd, args = parsed

    title = chat.get("title") or " ".join(
        x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
        or chat.get("username") or str(chat_id)

    conn = db.connect()
    try:
        row = db.get_chat(conn, chat_id)
        is_new = False
        if row is None:
            if cmd != "start":
                await _reply(token, str(chat_id),
                             "Чат не привязан — напишите /start.")
                return
            is_new = db.add_chat(conn, chat_id, title)
            row = db.get_chat(conn, chat_id)
        info = {"id": str(chat_id), "title": title,
                "is_admin": bool(row["is_admin"]), "is_new": is_new}
    finally:
        conn.close()
    await handle_command(cmd, args, info, token)


async def bot_loop():
    """Длинный поллинг getUpdates. Работает, пока живо приложение."""
    await asyncio.sleep(3)
    while True:
        conn = db.connect()
        try:
            token = db.get_setting(conn, "bot_token")
            offset = int(db.get_setting(conn, "tg_offset", "0"))
            bot_username = db.get_setting(conn, "bot_username", "")
        finally:
            conn.close()
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            updates = await tg.call(token, "getUpdates", http_timeout=40,
                                    offset=offset, timeout=25,
                                    allowed_updates=["message", "callback_query"])
        except asyncio.CancelledError:
            raise
        except tg.TgError as e:
            print(f"[bot] getUpdates: {e}", flush=True)
            await asyncio.sleep(10)
            continue
        new_offset = offset
        for u in updates:
            new_offset = max(new_offset, u.get("update_id", 0) + 1)
            try:
                await handle_update(u, token, bot_username)
            except Exception as e:
                print(f"[bot] обработка апдейта: {e}", flush=True)
        if new_offset != offset:
            conn = db.connect()
            try:
                db.set_setting(conn, "tg_offset", str(new_offset))
            finally:
                conn.close()


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []
    checks.append(("команда с аргументом", parse_command("/check 3") == ("check", "3")))
    checks.append(("команда без аргумента", parse_command("/list") == ("list", "")))
    checks.append(("упоминание своего бота",
                   parse_command("/list@MyBot", "MyBot") == ("list", "")))
    checks.append(("упоминание чужого бота",
                   parse_command("/list@OtherBot", "MyBot") is None))
    checks.append(("регистр и пробелы",
                   parse_command("  /MODE all  ") == ("mode", "all")))
    checks.append(("не команда", parse_command("привет") is None))
    checks.append(("пустая строка", parse_command("") is None))
    checks.append(("одинокий слэш", parse_command("/") is None))

    fake = [{"id": 10, "name": "A"}, {"id": 20, "name": "B"}]
    checks.append(("номер -> компания", _company_by_num(fake, "2")["id"] == 20))
    checks.append(("номер за пределами", _company_by_num(fake, "3") is None))
    checks.append(("номер не число", _company_by_num(fake, "x") is None))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
