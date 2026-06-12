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
import socket
import sys
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import ai_reply
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
    if not db.get_setting(conn, "openai_api_key") and os.getenv("OPENAI_API_KEY"):
        db.set_setting(conn, "openai_api_key", os.getenv("OPENAI_API_KEY"))
    if not db.get_setting(conn, "openai_model"):
        db.set_setting(conn, "openai_model",
                       os.getenv("OPENAI_MODEL") or ai_reply.DEFAULT_MODEL)
    conn.close()
    tasks = [asyncio.create_task(monitor.scheduler_loop()),
             asyncio.create_task(bot.bot_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

# Рабочее имя продукта для витрины (поменять — одна строка)
BRAND = "Пульс Карты"


@app.middleware("http")
async def local_guard(request: Request, call_next):
    """Защита локальной панели: DNS rebinding (чужой Host) и CSRF
    (form-POST с чужого сайта на 127.0.0.1). Браузер при кросс-сайтовом
    POST всегда шлёт Origin — чужой Origin отклоняем; отсутствие Origin
    означает same-origin или не-браузерный клиент (curl) — пропускаем."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost"):
        return PlainTextResponse("Forbidden: unknown host", status_code=403)
    if request.method == "POST":
        source = request.headers.get("origin") or request.headers.get("referer") or ""
        if source:
            src_host = (urllib.parse.urlparse(source).hostname or "").lower()
            if src_host not in ("127.0.0.1", "localhost"):
                return PlainTextResponse("Forbidden: cross-site POST", status_code=403)
    return await call_next(request)


# ---------- HTML-каркас ----------

CSS = """
:root{
  --paper:#F1F4F0; --card:#FFFFFF; --ink:#122620; --muted:#5C7064;
  --line:#DFE7DF; --green:#1E7A46; --green-deep:#155C34; --green-soft:#E2F0E6;
  --red:#D8472B; --red-soft:#FBE9E4; --amber:#B97E14; --amber-soft:#F7EED7;
  --blue:#2B5FD9; --blue-soft:#E8EEFB;
}
*{box-sizing:border-box}
body{font-family:Manrope,'Segoe UI',Arial,sans-serif;margin:0;background:var(--paper);
     color:var(--ink);line-height:1.55;font-size:15.5px}
.mono{font-family:'JetBrains Mono',Consolas,monospace}
header{background:var(--ink);padding:13px 26px;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.brand{font-family:Unbounded,Manrope,sans-serif;font-weight:700;font-size:.98em;
       letter-spacing:.1em;color:#fff;text-transform:uppercase;text-decoration:none}
.brand b{color:#7BD389}
header nav{display:flex;gap:18px;margin-left:auto}
header nav a{color:#B9CCBE;text-decoration:none;font-weight:600;padding:4px 2px}
header nav a:hover{color:#fff}
main{max-width:1080px;margin:24px auto 60px;padding:0 18px}
h1{font-size:1.45em;font-weight:700;letter-spacing:-.01em;margin:6px 0 14px}
h2{font-family:'JetBrains Mono',Consolas,monospace;font-size:.76em;font-weight:600;
   letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:2px 0 12px}
a{color:var(--green-deep)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
      padding:18px 20px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.company{display:flex;gap:14px;align-items:flex-start;background:var(--card);
         border:1px solid var(--line);border-radius:14px;padding:16px;
         transition:border-color .15s,transform .15s}
.company:hover{border-color:var(--green);transform:translateY(-1px)}
.pin{flex:0 0 56px;height:56px;display:flex;align-items:center;justify-content:center;
     border-radius:16px 16px 16px 4px;color:#fff;font-family:Unbounded,Manrope,sans-serif;
     font-size:1.08em;font-weight:700;letter-spacing:-.02em}
.pin.g{background:var(--green)} .pin.a{background:var(--amber)}
.pin.r{background:var(--red)} .pin.n{background:#8A9A8E}
.pin.big{flex-basis:72px;height:72px;font-size:1.5em}
.company .body{min-width:0;flex:1}
.company .name{font-weight:700;text-decoration:none;color:var(--ink);font-size:1.05em}
.company .name:hover{color:var(--green-deep)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0 9px}
.badge,.chip{display:inline-flex;align-items:center;gap:6px;border-radius:99px;
      padding:2px 11px;font-size:.8em;font-weight:700}
.badge::before,.chip::before{content:'';width:6px;height:6px;border-radius:50%;
      background:currentColor;flex:0 0 6px}
.chip.ok,.badge.b-ok{background:var(--green-soft);color:var(--green-deep)}
.chip.err,.badge.b-err{background:var(--red-soft);color:var(--red)}
.chip.busy,.badge.b-busy{background:var(--amber-soft);color:var(--amber)}
.chip.new,.badge.b-new{background:var(--blue-soft);color:var(--blue)}
.meta,.muted{color:var(--muted);font-size:.88em}
button,.btn{cursor:pointer;border:1.5px solid var(--line);border-radius:99px;
      background:var(--card);padding:7px 16px;font-size:.92em;font-weight:600;
      color:var(--ink);font-family:inherit;text-decoration:none;display:inline-block;
      transition:border-color .15s,background .15s}
button:hover,.btn:hover{border-color:var(--ink)}
button.primary{background:var(--green);border-color:var(--green);color:#fff}
button.primary:hover{background:var(--green-deep);border-color:var(--green-deep)}
button.danger{color:var(--red);border-color:#EBC4B9}
button.danger:hover{border-color:var(--red);background:var(--card)}
button.small{padding:4px 13px;font-size:.84em}
:focus-visible{outline:2.5px solid var(--blue);outline-offset:2px;border-radius:8px}
input[type=text],input[type=number]{padding:9px 12px;border:1.5px solid var(--line);
      border-radius:10px;font-size:1em;width:100%;font-family:inherit;background:#fff;
      color:var(--ink)}
input[type=text]:focus,input[type=number]:focus{border-color:var(--green);outline:none}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;vertical-align:top}
th{font-family:'JetBrains Mono',Consolas,monospace;font-size:.7em;letter-spacing:.12em;
   text-transform:uppercase;color:var(--muted);font-weight:600}
.flash{border-radius:12px;padding:11px 16px;margin-bottom:14px;font-weight:600}
.flash.ok{background:var(--green-soft);color:var(--green-deep)}
.flash.err{background:var(--red-soft);color:var(--red)}
.addform{display:flex;gap:10px}
.addform input{flex:1}
blockquote{background:#F7FAF7;border-left:3px solid var(--line);margin:10px 0;
      padding:10px 14px;border-radius:0 10px 10px 0}
blockquote.neg{background:var(--red-soft);border-color:var(--red)}
blockquote .meta{margin-top:5px}
pre{white-space:pre-wrap;background:#F4F7F4;border-radius:10px;padding:12px;
    font-family:inherit;font-size:.95em}
.kv{display:flex;gap:30px;flex-wrap:wrap;margin:10px 0 14px}
.kv b{font-size:1.5em;font-family:'JetBrains Mono',Consolas,monospace;font-weight:600}
.kv span{display:block;color:var(--muted);font-size:.82em}
form.inline{display:inline}
.sysbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
ol.help li{margin:6px 0}
.links a{margin-right:10px;white-space:nowrap}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:640px){header{padding:12px 16px}header nav{margin-left:0}
  main{margin-top:14px}.addform{flex-direction:column}}
"""

FAVICON = ("data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 "
           "viewBox=%220 0 100 100%22><path d=%22M50 6C29 6 13 22 13 43c0 27 37 51 "
           "37 51s37-24 37-51C87 22 71 6 50 6z%22 fill=%22%231E7A46%22/>"
           "<circle cx=%2250%22 cy=%2242%22 r=%2215%22 fill=%22white%22/></svg>")

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700'
         '&family=Manrope:wght@400;600;700&family=JetBrains+Mono:wght@400;600'
         '&display=swap" rel="stylesheet">')

ST_BADGE = {
    db.ST_OK: '<span class="chip ok">ок</span>',
    db.ST_ERROR: '<span class="chip err">ошибка</span>',
    db.ST_ADDING: '<span class="chip busy">загружается…</span>',
    db.ST_CHECKING: '<span class="chip busy">проверяется…</span>',
}


def health_pin(rating, status: str, extra_cls: str = "") -> str:
    """Рейтинг в пине-маркере, цвет = здоровье карточки (подпись дизайна)."""
    if status == db.ST_ERROR:
        return f'<div class="pin n {extra_cls}">⚠</div>'
    if rating is None:
        return f'<div class="pin n {extra_cls}">—</div>'
    cls = "g" if rating >= 4.5 else ("a" if rating >= 4.0 else "r")
    return (f'<div class="pin {cls} {extra_cls}">'
            f'{f"{rating:.1f}".replace(".", ",")}</div>')


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
    brand = e(BRAND).upper().replace(" ", "<b>·</b>", 1)
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href='{FAVICON}'>{FONTS}
<title>{e(title)} — {e(BRAND)}</title><style>{CSS}</style></head><body>
<header><a class="brand" href="/">{brand}</a>
<nav><a href="/">Карточки</a><a href="/settings">Настройки</a></nav></header>
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
        ai_key, ai_model = ai_reply.get_ai_settings(conn)
        cards = []
        busy = False
        for c in companies:
            busy = busy or c["status"] in (db.ST_ADDING, db.ST_CHECKING)
            new7 = db.new_reviews_count(conn, c["id"])
            new_chip = (f'<span class="chip new">+{new7} за 7 дн</span>' if new7 else "")
            count = "—" if c["reviews_count"] is None else c["reviews_count"]
            err_note = (f'<div class="meta" style="color:var(--red)">⚠ {e(c["error"][:120])}</div>'
                        if c["status"] == db.ST_ERROR and c["error"] else "")
            cards.append(f"""<div class="company">
 {health_pin(c['rating'], c['status'])}
 <div class="body">
  <a class="name" href="/company/{c['id']}">{e(c['name'])}</a>
  <div class="meta">{e(c['city_slug'])} · {e(c['category'] or '2ГИС')} ·
    <span class="mono">{count} отз.</span></div>
  <div class="chips">{ST_BADGE.get(c['status'], e(c['status']))}{new_chip}</div>
  <div class="meta">проверено: <span class="mono">{fmt_dt(c['last_checked'])}</span></div>
  {err_note}
  <div style="margin-top:10px">
   <form class="inline" method="post" action="/companies/{c['id']}/check">
     <button class="small">Проверить</button></form>
   <form class="inline" method="post" action="/companies/{c['id']}/delete"
         onsubmit="return confirm('Удалить «{e(c['name'])}» и все её отзывы?')">
     <button class="small danger">Удалить</button></form>
  </div>
 </div></div>""")
    finally:
        conn.close()

    ai_chip = ('<span class="chip ok">AI-ответы включены</span>' if ai_key
               else '<span class="chip err">AI-ответы: нет ключа</span>')
    grid = (f'<div class="grid">{"".join(cards)}</div>' if cards else
            '<div class="card"><p class="muted">Пока нет ни одной карточки. Вставьте '
            'выше ссылку на компанию в 2ГИС — и её отзывы будут под присмотром.</p></div>')
    check_all = (f"""<div style="margin-top:14px">
  <form class="inline" method="post" action="/check-all">
    <button>Проверить все сейчас</button></form></div>""" if cards else "")
    body = f"""
<h1>Карточки под наблюдением</h1>
<div class="sysbar">{tg_line} {ai_chip}
 <span class="muted">автопроверка каждые <span class="mono">{e(interval)}</span> мин</span></div>
<div class="card">
 <form class="addform" method="post" action="/companies/add">
  <input type="text" name="url" required aria-label="Ссылка на карточку 2ГИС"
         placeholder="Вставьте ссылку на карточку 2ГИС — https://2gis.kz/almaty/firm/…">
  <button class="primary">Добавить карточку</button>
 </form>
</div>
{grid}{check_all}"""
    return page("Карточки", body, request, refresh=busy)


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
        ai = []
        try:
            ai = await ai_reply.generate_for_company(conn, c, dict(rv))
        except ai_reply.AiReplyError as ex:
            print(f"[ai] тест: {ex}", flush=True)
        errors = await monitor.notify_chats(
            conn, tg.fmt_review_alert(c["name"], c["url"], dict(rv), test=True,
                                      ai_replies=ai),
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
 {e(rv['author'] or 'аноним')}{badges} ·
 <a href="/company/{cid}/reply/{rv['id']}">AI-ответ →</a></div></blockquote>""")
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
 <div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap">
  {health_pin(c['rating'], c['status'], 'big')}
  <div style="flex:1;min-width:240px">
   <h1 style="margin:0 0 4px">{e(c['name'])}</h1>
   <div class="meta">{e(c['category'] or '')} · {e(c['city_slug'])} ·
     <a href="{e(c['url'])}">карточка в 2ГИС</a></div>
   <div class="chips" style="margin-top:8px">{ST_BADGE.get(c['status'], '')}</div>
  </div>
 </div>
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
<div class="card"><h2>Тон AI-ответов</h2>
 <p class="muted">Стиль, в котором AI готовит ответы на отзывы этой компании,
 и подпись/контакт в конце ответа (например, WhatsApp администратора).</p>
 <form method="post" action="/companies/{cid}/reply-settings">
  <p><input type="text" name="tone" value="{e(c['tone'] or '')}"
        placeholder="Тон: тёплый и заботливый / деловой и краткий / молодёжный…"></p>
  <p><input type="text" name="signature" value="{e(c['reply_signature'] or '')}"
        placeholder="Подпись/контакт: С уважением, клиника N · WhatsApp +7 …"></p>
  <button class="primary">Сохранить тон</button>
 </form>
</div>
<div class="card"><h2>Отзывы (последние {len(reviews)})</h2>{reviews_html}</div>
<p><a href="/">← ко всем компаниям</a></p>"""
    return page(c["name"], body, request, refresh=busy)


@app.post("/companies/{cid}/reply-settings")
async def companies_reply_settings(cid: int, request: Request):
    form = await request.form()
    conn = db.connect()
    try:
        if db.get_company(conn, cid) is None:
            return redirect("/", err="Компания не найдена")
        conn.execute("UPDATE companies SET tone=?, reply_signature=? WHERE id=?",
                     (str(form.get("tone", "")).strip(),
                      str(form.get("signature", "")).strip(), cid))
        conn.commit()
    finally:
        conn.close()
    return redirect(f"/company/{cid}", msg="Тон AI-ответов сохранён")


@app.get("/company/{cid}/reply/{review_id}", response_class=HTMLResponse)
async def company_reply_page(cid: int, review_id: int, request: Request):
    """Генерация AI-ответов на конкретный отзыв (синхронно, ~5–10 сек)."""
    e = html.escape
    conn = db.connect()
    try:
        c = db.get_company(conn, cid)
        rv = db.get_review(conn, review_id)
        if c is None or rv is None or rv["company_id"] != cid:
            return redirect("/", err="Отзыв не найден")
        api_key, _ = ai_reply.get_ai_settings(conn)
        if not api_key:
            return redirect(f"/company/{cid}",
                            err="AI не настроен: добавьте ключ OpenAI в «Настройках»")
        try:
            replies = await ai_reply.generate_for_company(conn, c, dict(rv))
        except ai_reply.AiReplyError as ex:
            return redirect(f"/company/{cid}", err=str(ex))
    finally:
        conn.close()

    variants = "".join(f"""<div class="card"><b>Вариант {i}</b>
 <button style="float:right" onclick="copyMsg('v{i}',this)">Копировать</button>
 <pre id="v{i}">{e(r)}</pre></div>""" for i, r in enumerate(replies, 1)) \
        or '<p class="muted">Модель не вернула вариантов — попробуйте ещё раз.</p>'
    body = f"""
<div class="card">
 <h1>AI-ответ на отзыв — {e(c['name'])}</h1>
 <blockquote class="{'neg' if tg.is_negative(rv['rating']) else ''}">{e(rv['text'] or '(без текста)')}
  <div class="meta">{tg.stars(rv['rating'])} · {e((rv['date_created'] or '')[:10])} ·
  {e(rv['author'] or 'аноним')}</div></blockquote>
 <p class="muted">Скопируйте вариант и опубликуйте в кабинете 2ГИС от имени компании.
 Тон настраивается на странице компании.</p>
</div>
{variants}
<p><a href="/company/{cid}/reply/{review_id}">↻ Сгенерировать ещё</a> ·
 <a href="/company/{cid}">← к компании</a></p>
<script>
function copyMsg(id, btn){{
  const t = document.getElementById(id).innerText;
  const done = () => {{ btn.textContent = 'Скопировано ✓';
                        setTimeout(() => btn.textContent = 'Копировать', 1500); }};
  if (navigator.clipboard && navigator.clipboard.writeText)
    navigator.clipboard.writeText(t).then(done);
  else {{ const ta = document.createElement('textarea'); ta.value = t;
         document.body.appendChild(ta); ta.select();
         document.execCommand('copy'); ta.remove(); done(); }}
}}
</script>"""
    return page(f"AI-ответ — {c['name']}", body, request)


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
        ai_key, ai_model = ai_reply.get_ai_settings(conn)
    finally:
        conn.close()
    ai_status = (f'<span class="badge b-ok">подключены · {e(ai_model)}</span>'
                 if ai_key else
                 '<span class="badge b-err">нет ключа — в алертах статичный шаблон</span>')
    ai_key_hint = f"сохранён: …{e(ai_key[-6:])} — введите новый, чтобы заменить" \
        if ai_key else "sk-…"
    token_hint = (f"сохранён: …{e(token[-6:])} — введите новый, чтобы заменить"
                  if token else "1234567890:AA…")

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
   <span class="muted">Создайте бота у @BotFather командой /newbot и вставьте токен.
   Пустое поле — текущий токен сохранится.</span><br>
   <input type="text" name="bot_token" value="" placeholder="{token_hint}"
          autocomplete="off"></p>
  <p><b>Интервал автопроверки, минут</b><br>
   <input type="number" name="interval" value="{e(interval)}" min="30" style="width:120px">
   <span class="muted">не ставьте меньше 30 — частые проверки повышают риск блокировки 2ГИС</span></p>
  <p><label><input type="checkbox" name="notify_all" {'checked' if notify_all else ''}>
   уведомлять обо <b>всех</b> новых отзывах (иначе — только о негативе, оценка ≤3)</label></p>
  <p><b>AI-ответы на отзывы</b> {ai_status}<br>
   <span class="muted">Ключ OpenAI API; модель менять только осознанно.</span><br>
   <input type="text" name="openai_key" value="" placeholder="{ai_key_hint}"
          autocomplete="off" style="margin-bottom:6px">
   <input type="text" name="openai_model" value="{e(ai_model)}" style="width:240px"></p>
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
    new_token = str(form.get("bot_token", "")).strip()
    interval = str(form.get("interval", "360")).strip()
    notify_all = "1" if form.get("notify_all") else "0"
    try:
        interval = str(max(30, int(interval)))
    except ValueError:
        interval = "360"

    openai_key = str(form.get("openai_key", "")).strip()
    openai_model = str(form.get("openai_model", "")).strip() or ai_reply.DEFAULT_MODEL

    conn = db.connect()
    try:
        if new_token:  # пустое поле = оставить прежний токен (он не светится в HTML)
            db.set_setting(conn, "bot_token", new_token)
        token = db.get_setting(conn, "bot_token")
        db.set_setting(conn, "check_interval_min", interval)
        db.set_setting(conn, "notify_all", notify_all)
        if openai_key:  # пустое поле = оставить прежний ключ
            db.set_setting(conn, "openai_api_key", openai_key)
        db.set_setting(conn, "openai_model", openai_model)
        if not token:
            db.set_setting(conn, "bot_username", "")
            return redirect("/settings", msg="Сохранено (токена нет — уведомления выключены)")
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


def _port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    if _port_busy():
        print(f"Похоже, панель уже запущена: http://{HOST}:{PORT} — просто "
              f"откройте эту ссылку.\nЕсли нужен перезапуск, закройте старый "
              f"процесс (Ctrl+C в его окне) и запустите снова.", flush=True)
        sys.exit(0)
    # Proactor-loop обязателен: Playwright запускает браузер субпроцессом.
    # Поэтому uvicorn запускаем внутри своего цикла (server.serve), а не
    # uvicorn.run(), который мог бы переключить event loop policy.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
