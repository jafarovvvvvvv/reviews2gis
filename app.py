"""
Веб-панель мониторинга отзывов 2GIS + уведомления в Telegram.

Запуск:  python app.py  ->  http://127.0.0.1:8765

Что умеет:
  • добавить компанию по ссылке на карточку 2GIS (первичная загрузка — в фоне);
  • фоновый мониторинг: каждые N минут проверяет карточки, новые отзывы
    сохраняет и шлёт в привязанные Telegram-чаты (негатив — с черновиком ответа);
  • страница компании: карточка + лента увиденных отзывов;
  • настройки: токен бота, привязка чатов через getUpdates, интервал проверки,
    «уведомлять обо всех / только о негативе», тестовая отправка.

Серверный HTML без шаблонизатора (в духе остального проекта), хранилище —
SQLite (db.py), проверки — monitor.py, Telegram — tg.py.
"""
import asyncio
import html
import os
import sys
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import bot
import db
import monitor
import tg

load_dotenv()

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT") or 8765)

# фоновые задачи держим в живых (иначе их соберёт GC)
_BG_TASKS: set = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@asynccontextmanager
async def lifespan(app):
    # стартовые настройки из .env (только если в БД ещё пусто)
    conn = db.connect()
    if not db.get_setting(conn, "bot_token") and os.getenv("TELEGRAM_BOT_TOKEN"):
        db.set_setting(conn, "bot_token", os.getenv("TELEGRAM_BOT_TOKEN"))
    conn.close()
    tasks = [asyncio.create_task(monitor.scheduler_loop()),
             asyncio.create_task(bot.bot_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

# ---------- HTML-каркас ----------

CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;margin:0;background:#f4f6fa;color:#1a1a2e}
header{background:#1a1a2e;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center}
header a{color:#cdd3ff;text-decoration:none;font-weight:600}
header a:hover{color:#fff}
header .brand{color:#fff;font-size:1.1em}
main{max-width:980px;margin:20px auto;padding:0 16px}
.card{background:#fff;border-radius:12px;padding:18px 20px;margin-bottom:16px;
      box-shadow:0 1px 3px rgba(20,20,60,.08)}
h1{font-size:1.3em;margin:4px 0 12px}
h2{font-size:1.05em}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid #eceef5;padding:8px 10px;text-align:left;vertical-align:top}
th{color:#666;font-weight:600;font-size:.9em}
tr:hover td{background:#fafbff}
input[type=text],input[type=number]{padding:8px 10px;border:1px solid #ccd;border-radius:8px;
      font-size:1em;width:100%;box-sizing:border-box}
button,.btn{cursor:pointer;border:1px solid #ccd;border-radius:8px;background:#fff;
      padding:7px 14px;font-size:.95em;text-decoration:none;color:#1a1a2e;display:inline-block}
button:hover,.btn:hover{background:#eef}
button.primary{background:#3b5bdb;border-color:#3b5bdb;color:#fff}
button.primary:hover{background:#2f4ec0}
button.danger{color:#c0392b;border-color:#e3b4ac}
.badge{display:inline-block;border-radius:20px;padding:2px 10px;font-size:.82em;font-weight:600}
.b-ok{background:#e6f7ed;color:#1d7a40}.b-err{background:#fdeaea;color:#c0392b}
.b-busy{background:#fff4d6;color:#9a6b00}.b-new{background:#e8edff;color:#3b5bdb}
.muted{color:#888;font-size:.9em}
.flash{border-radius:10px;padding:10px 14px;margin-bottom:14px}
.flash.ok{background:#e6f7ed;color:#1d7a40}.flash.err{background:#fdeaea;color:#c0392b}
.addform{display:flex;gap:10px}
.addform input{flex:1}
blockquote{background:#f8f9fd;border-left:4px solid #ccd;margin:8px 0;padding:8px 12px;
      border-radius:0 8px 8px 0}
blockquote.neg{background:#fff6f6;border-color:#c0392b}
blockquote .meta{color:#888;font-size:.85em;margin-top:4px}
form.inline{display:inline}
.kv{display:flex;gap:28px;flex-wrap:wrap;margin:8px 0}
.kv div b{font-size:1.25em}
.kv div span{display:block;color:#888;font-size:.85em}
ol.help li{margin:6px 0}
"""

ST_BADGE = {
    db.ST_OK: '<span class="badge b-ok">ок</span>',
    db.ST_ERROR: '<span class="badge b-err">ошибка</span>',
    db.ST_ADDING: '<span class="badge b-busy">загружается…</span>',
    db.ST_CHECKING: '<span class="badge b-busy">проверяется…</span>',
}


def page(title: str, body: str, request: Request, refresh: bool = False) -> HTMLResponse:
    e = html.escape
    msg = request.query_params.get("msg", "")
    err = request.query_params.get("err", "")
    flash = ""
    if msg:
        flash += f'<div class="flash ok">{e(msg)}</div>'
    if err:
        flash += f'<div class="flash err">{e(err)}</div>'
    meta = '<meta http-equiv="refresh" content="7">' if refresh else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">{meta}
<title>{e(title)}</title><style>{CSS}</style></head><body>
<header><span class="brand">📍 Отзывы 2ГИС → Telegram</span>
<a href="/">Компании</a><a href="/settings">Настройки</a></header>
<main>{flash}{body}</main></body></html>""")


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=303)


def fmt_dt(iso: str) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d.%m %H:%M")
    except ValueError:
        return iso


def tg_status_line(conn) -> str:
    token = db.get_setting(conn, "bot_token")
    chats = db.list_chats(conn)
    admins = sum(1 for c in chats if c["is_admin"])
    if token and admins:
        return (f'<span class="badge b-ok">Telegram подключён · чатов: {len(chats)}, '
                f'админских: {admins}</span>')
    if token and chats:
        return ('<span class="badge b-err">нет админ-чата</span> '
                '<a href="/settings">назначить →</a> (админ получает все уведомления)')
    return ('<span class="badge b-err">Telegram не настроен</span> '
            '<a href="/settings">настроить →</a> (без этого уведомления не уходят)')


# ---------- компании ----------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    e = html.escape
    conn = db.connect()
    try:
        companies = db.list_companies(conn)
        interval = db.get_setting(conn, "check_interval_min", "360")
        tg_line = tg_status_line(conn)
        rows = []
        busy = False
        for c in companies:
            busy = busy or c["status"] in (db.ST_ADDING, db.ST_CHECKING)
            new7 = db.new_reviews_count(conn, c["id"])
            new_badge = (f' <span class="badge b-new">+{new7} за 7 дн</span>'
                         if new7 else "")
            rating = "—" if c["rating"] is None else f"{c['rating']:.1f}".replace(".", ",")
            count = "—" if c["reviews_count"] is None else c["reviews_count"]
            err_note = (f'<div class="muted">⚠ {e(c["error"])}</div>'
                        if c["status"] == db.ST_ERROR and c["error"] else "")
            rows.append(f"""<tr>
 <td><a href="/company/{c['id']}"><b>{e(c['name'])}</b></a>{new_badge}
     <div class="muted">{e(c['city_slug'])} · {e(c['category'] or '')}</div>{err_note}</td>
 <td>{rating} / {count} отз.</td>
 <td>{ST_BADGE.get(c['status'], e(c['status']))}<div class="muted">{fmt_dt(c['last_checked'])}</div></td>
 <td>
   <form class="inline" method="post" action="/companies/{c['id']}/check"><button>Проверить</button></form>
   <form class="inline" method="post" action="/companies/{c['id']}/delete"
         onsubmit="return confirm('Удалить «{e(c['name'])}» и все её отзывы?')">
     <button class="danger">Удалить</button></form>
 </td></tr>""")
    finally:
        conn.close()

    table = (f"<table><tr><th>Компания</th><th>Рейтинг</th><th>Статус</th><th></th></tr>"
             f"{''.join(rows)}</table>" if rows else
             '<p class="muted">Пока нет компаний — вставьте ссылку на карточку 2ГИС выше.</p>')
    body = f"""
<div class="card">
 <h1>Мониторинг отзывов</h1>
 <div style="margin-bottom:10px">{tg_line}
   <span class="muted"> · автопроверка каждые {e(interval)} мин</span></div>
 <form class="addform" method="post" action="/companies/add">
  <input type="text" name="url" required
         placeholder="Ссылка на карточку 2ГИС, например https://2gis.kz/almaty/firm/70000001062690247">
  <button class="primary">Добавить</button>
 </form>
</div>
<div class="card">{table}
 <div style="margin-top:10px">
  <form class="inline" method="post" action="/check-all"><button>Проверить все сейчас</button></form>
 </div>
</div>"""
    return page("Компании — мониторинг 2ГИС", body, request, refresh=busy)


@app.post("/companies/add")
async def companies_add(request: Request):
    form = await request.form()
    parsed = monitor.parse_2gis_url(str(form.get("url", "")))
    if not parsed:
        return redirect("/", err="Не похоже на ссылку карточки 2ГИС. Нужна ссылка вида "
                                  "https://2gis.kz/<город>/firm/<номер>")
    domain, slug, firm_id = parsed
    url = f"{domain}/{slug}/firm/{firm_id}"
    conn = db.connect()
    try:
        if db.find_company_by_firm(conn, firm_id):
            return redirect("/", err="Эта компания уже добавлена")
        cid = db.add_company(conn, firm_id, domain, slug, url)
    finally:
        conn.close()
    spawn(monitor.check_company(cid, baseline=True))
    return redirect("/", msg="Компания добавляется: загружаем карточку и запоминаем "
                             "текущие отзывы (≈15–30 сек). Уведомления пойдут о новых.")


@app.post("/companies/{cid}/check")
async def companies_check(cid: int):
    spawn(monitor.check_company(cid))
    return redirect(f"/company/{cid}", msg="Проверка запущена")


@app.post("/companies/{cid}/delete")
async def companies_delete(cid: int):
    conn = db.connect()
    try:
        db.delete_company(conn, cid)
    finally:
        conn.close()
    return redirect("/", msg="Компания удалена")


@app.post("/check-all")
async def check_all():
    conn = db.connect()
    try:
        ids = [c["id"] for c in db.list_companies(conn)
               if c["status"] not in (db.ST_ADDING, db.ST_CHECKING)]
    finally:
        conn.close()

    async def run_all():
        for cid in ids:  # последовательно — Playwright один
            await monitor.check_company(cid)

    spawn(run_all())
    return redirect("/", msg=f"Запущена проверка {len(ids)} компаний")


@app.post("/companies/{cid}/test")
async def companies_test(cid: int):
    conn = db.connect()
    try:
        c = db.get_company(conn, cid)
        if c is None:
            return redirect("/", err="Компания не найдена")
        rv = db.latest_review(conn, cid)
        if rv is None:
            return redirect(f"/company/{cid}", err="В базе ещё нет отзывов этой компании")
        errors = await monitor.notify_chats(
            conn, tg.fmt_review_alert(c["name"], c["url"], dict(rv), test=True),
            company_id=cid)
    finally:
        conn.close()
    if errors:
        return redirect(f"/company/{cid}", err="; ".join(errors))
    return redirect(f"/company/{cid}", msg="Тестовое уведомление отправлено в Telegram")


@app.get("/company/{cid}", response_class=HTMLResponse)
async def company_page(cid: int, request: Request):
    e = html.escape
    conn = db.connect()
    try:
        c = db.get_company(conn, cid)
        if c is None:
            return redirect("/", err="Компания не найдена")
        reviews = db.recent_reviews(conn, cid, limit=50)
        all_chats = db.list_chats(conn)
        subs = db.subscribed_chat_ids(conn, cid)
    finally:
        conn.close()

    rating = "—" if c["rating"] is None else f"{c['rating']:.1f}".replace(".", ",")
    count = "—" if c["reviews_count"] is None else c["reviews_count"]
    err_note = (f'<div class="flash err">⚠ {e(c["error"])}</div>'
                if c["status"] == db.ST_ERROR and c["error"] else "")

    quotes = []
    for rv in reviews:
        neg = tg.is_negative(rv["rating"])
        badges = ""
        if rv["is_new"]:
            badges += ' <span class="badge b-new">новый</span>'
        badges += (' <span class="badge b-ok">есть ответ</span>' if rv["answered"]
                   else ' <span class="badge b-err">без ответа</span>')
        text = e(rv["text"] or "(без текста)")
        quotes.append(f"""<blockquote class="{'neg' if neg else ''}">{text}
 <div class="meta">{tg.stars(rv['rating'])} · {e((rv['date_created'] or '')[:10])} ·
 {e(rv['author'] or 'аноним')}{badges}</div></blockquote>""")
    reviews_html = "".join(quotes) or '<p class="muted">Отзывы ещё не загружены.</p>'

    # маршрутизация: админы получают всё, остальных подписывают галочками
    admins = [ch for ch in all_chats if ch["is_admin"]]
    guests = [ch for ch in all_chats if not ch["is_admin"]]
    admin_line = (", ".join(e(ch["title"]) for ch in admins)
                  if admins else "нет — назначьте в «Настройках»")
    if guests:
        boxes = "".join(
            f'<label style="display:block;margin:4px 0">'
            f'<input type="checkbox" name="chat" value="{e(ch["chat_id"])}" '
            f'{"checked" if ch["chat_id"] in subs else ""}> {e(ch["title"])} '
            f'<span class="muted">({e(ch["chat_id"])})</span></label>'
            for ch in guests)
        routing = f"""<form method="post" action="/companies/{cid}/chats">{boxes}
 <button class="primary">Сохранить получателей</button></form>"""
    else:
        routing = ('<p class="muted">Клиентских чатов пока нет: добавьте бота в чат '
                   'клиента и отправьте там /start — чат появится здесь.</p>')

    busy = c["status"] in (db.ST_ADDING, db.ST_CHECKING)
    body = f"""
<div class="card">
 <h1>{e(c['name'])} {ST_BADGE.get(c['status'], '')}</h1>
 <div class="muted">{e(c['category'] or '')} · {e(c['city_slug'])} ·
   <a href="{e(c['url'])}">карточка в 2ГИС</a></div>
 {err_note}
 <div class="kv">
  <div><b>{rating}</b><span>рейтинг</span></div>
  <div><b>{count}</b><span>отзывов</span></div>
  <div><b>{fmt_dt(c['last_checked'])}</b><span>последняя проверка</span></div>
 </div>
 <form class="inline" method="post" action="/companies/{cid}/check"><button class="primary">Проверить сейчас</button></form>
 <form class="inline" method="post" action="/companies/{cid}/test"><button>Тест в Telegram</button></form>
 <form class="inline" method="post" action="/companies/{cid}/delete"
       onsubmit="return confirm('Удалить компанию и все её отзывы?')">
   <button class="danger">Удалить</button></form>
</div>
<div class="card"><h2>Кому слать уведомления</h2>
 <p class="muted">Админ-чаты получают все компании всегда: <b>{admin_line}</b>.
 Ниже — дополнительные получатели именно этой компании (например, чат клиента).</p>
 {routing}
</div>
<div class="card"><h2>Отзывы (последние {len(reviews)})</h2>{reviews_html}</div>
<p><a href="/">← ко всем компаниям</a></p>"""
    return page(c["name"], body, request, refresh=busy)


@app.post("/companies/{cid}/chats")
async def companies_chats(cid: int, request: Request):
    form = await request.form()
    chat_ids = form.getlist("chat")
    conn = db.connect()
    try:
        if db.get_company(conn, cid) is None:
            return redirect("/", err="Компания не найдена")
        # сохраняем только реально существующие неадминские чаты
        valid = {c["chat_id"] for c in db.list_chats(conn) if not c["is_admin"]}
        db.set_subscriptions(conn, cid, [c for c in chat_ids if c in valid])
        n = len([c for c in chat_ids if c in valid])
    finally:
        conn.close()
    return redirect(f"/company/{cid}", msg=f"Получатели сохранены ({n} доп. чатов)")


# ---------- настройки ----------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    e = html.escape
    conn = db.connect()
    try:
        token = db.get_setting(conn, "bot_token")
        bot_username = db.get_setting(conn, "bot_username")
        interval = db.get_setting(conn, "check_interval_min", "360")
        notify_all = db.get_setting(conn, "notify_all", "1") == "1"
        chats = db.list_chats(conn)
    finally:
        conn.close()

    chat_rows = "".join(f"""<tr><td>{e(ch['title'])}</td><td>{e(ch['chat_id'])}</td>
 <td>{'<span class="badge b-new">админ — получает всё</span>' if ch['is_admin']
      else '<span class="muted">по подпискам</span>'}</td>
 <td><form class="inline" method="post" action="/settings/chats/admin">
   <input type="hidden" name="chat_id" value="{e(ch['chat_id'])}">
   <input type="hidden" name="value" value="{0 if ch['is_admin'] else 1}">
   <button>{'Убрать админа' if ch['is_admin'] else 'Сделать админом'}</button></form>
  <form class="inline" method="post" action="/settings/chats/delete"
        onsubmit="return confirm('Отвязать чат {e(ch['title'])}?')">
   <input type="hidden" name="chat_id" value="{e(ch['chat_id'])}">
   <button class="danger">Отвязать</button></form></td></tr>""" for ch in chats)
    chats_html = (f"<table><tr><th>Чат</th><th>ID</th><th>Роль</th><th></th></tr>"
                  f"{chat_rows}</table>"
                  if chats else '<p class="muted">Чаты пока не привязаны.</p>')
    bot_line = (f'Бот: <b>@{e(bot_username)}</b> — напишите ему /start, чат '
                f'привяжется сам (первый чат становится админом).'
                if bot_username else
                'Токен не проверен — сохраните его, и я проверю подключение.')

    body = f"""
<div class="card">
 <h1>Настройки</h1>
 <form method="post" action="/settings/save">
  <p><b>Токен Telegram-бота</b><br>
   <span class="muted">Создайте бота у @BotFather командой /newbot и вставьте токен.</span><br>
   <input type="text" name="bot_token" value="{e(token)}" placeholder="1234567890:AA…"></p>
  <p><b>Интервал автопроверки, минут</b><br>
   <input type="number" name="interval" value="{e(interval)}" min="30" style="width:120px">
   <span class="muted">не ставьте меньше 30 — частые проверки повышают риск блокировки 2ГИС</span></p>
  <p><label><input type="checkbox" name="notify_all" {'checked' if notify_all else ''}>
   уведомлять обо <b>всех</b> новых отзывах (иначе — только о негативе, оценка ≤3)</label></p>
  <button class="primary">Сохранить</button>
 </form>
</div>
<div class="card">
 <h2>Чаты и роли</h2>
 <p>{bot_line}</p>
 <ol class="help muted">
  <li><b>Свой (админский) чат:</b> напишите боту /start в личку — придут все
      уведомления и команды управления (/help — список).</li>
  <li><b>Чат клиента:</b> добавьте бота в группу клиента, отправьте там /start,
      затем на странице компании отметьте этот чат галочкой — туда пойдут
      только отзывы этой компании.</li>
 </ol>
 <form class="inline" method="post" action="/settings/test"><button>Отправить тестовое сообщение (в админ-чаты)</button></form>
 <div style="margin-top:12px">{chats_html}</div>
</div>
<p><a href="/">← к компаниям</a></p>"""
    return page("Настройки", body, request)


@app.post("/settings/chats/admin")
async def settings_chat_admin(request: Request):
    form = await request.form()
    conn = db.connect()
    try:
        db.set_chat_admin(conn, str(form.get("chat_id", "")),
                          form.get("value") == "1")
    finally:
        conn.close()
    return redirect("/settings", msg="Роль чата обновлена")


@app.post("/settings/save")
async def settings_save(request: Request):
    form = await request.form()
    token = str(form.get("bot_token", "")).strip()
    interval = str(form.get("interval", "360")).strip()
    notify_all = "1" if form.get("notify_all") else "0"
    try:
        interval = str(max(30, int(interval)))
    except ValueError:
        interval = "360"

    conn = db.connect()
    try:
        db.set_setting(conn, "bot_token", token)
        db.set_setting(conn, "check_interval_min", interval)
        db.set_setting(conn, "notify_all", notify_all)
        if not token:
            db.set_setting(conn, "bot_username", "")
            return redirect("/settings", msg="Сохранено (токен пуст — уведомления выключены)")
        try:
            me = await tg.get_me(token)
            db.set_setting(conn, "bot_username", me.get("username", ""))
            return redirect("/settings",
                            msg=f"Сохранено. Бот @{me.get('username')} на связи — "
                                f"напишите ему /start, чат привяжется сам")
        except tg.TgError as ex:
            db.set_setting(conn, "bot_username", "")
            return redirect("/settings", err=f"Токен сохранён, но Telegram ответил: {ex}")
    finally:
        conn.close()


@app.post("/settings/chats/delete")
async def settings_chat_delete(request: Request):
    form = await request.form()
    conn = db.connect()
    try:
        db.delete_chat(conn, str(form.get("chat_id", "")))
    finally:
        conn.close()
    return redirect("/settings", msg="Чат отвязан")


@app.post("/settings/test")
async def settings_test():
    conn = db.connect()
    try:
        errors = await monitor.notify_chats(
            conn, "🔔 Тест: уведомления об отзывах 2ГИС работают.")
    finally:
        conn.close()
    if errors:
        return redirect("/settings", err="; ".join(errors))
    return redirect("/settings", msg="Тестовое сообщение отправлено")


# ---------- запуск ----------

async def _serve():
    import uvicorn
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    print(f"Панель мониторинга: http://{HOST}:{PORT}", flush=True)
    await server.serve()


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    # Proactor-loop обязателен: Playwright запускает браузер субпроцессом.
    # Поэтому uvicorn запускаем внутри своего цикла (server.serve), а не
    # uvicorn.run(), который мог бы переключить event loop policy.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
