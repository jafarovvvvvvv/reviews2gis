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
import hashlib
import hmac
import html
import os
import secrets
import socket
import sys
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import ai_reply
import bot
import db
import insights
import monitor
import tg

load_dotenv()

# Адрес прослушивания: локально 127.0.0.1; в Docker за Caddy — 0.0.0.0 (PANEL_BIND)
BIND = os.getenv("PANEL_BIND") or "127.0.0.1"
HOST = "127.0.0.1"  # для подсказок/проверки занятости порта
PORT = int(os.getenv("PORT") or 8765)
DEMO = "--demo" in sys.argv  # витрина для встреч на изолированной demo.db

# Логин: если PANEL_PASSWORD задан — панель требует вход (для VPS). Пусто — открыта (локально).
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD") or ""
AUTH_ON = bool(PANEL_PASSWORD)
# Разрешённые Host: localhost + домены из PANEL_HOSTS (через запятую) — для VPS
ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {
    h.strip().lower() for h in (os.getenv("PANEL_HOSTS") or "").split(",") if h.strip()}
OPEN_PATHS = {"/login", "/healthz"}  # доступны без логина
_SECRET = ""  # ключ подписи сессий; ставится в lifespan
SESSION_TTL = 14 * 24 * 3600  # сессия живёт 2 недели

# фоновые задачи держим в живых (иначе их соберёт GC)
_BG_TASKS: set = set()


def _sign_session(exp: int) -> str:
    sig = hmac.new(_SECRET.encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _valid_session(token: str) -> bool:
    try:
        exp_s, sig = (token or "").split(".", 1)
    except (ValueError, AttributeError):
        return False
    expected = hmac.new(_SECRET.encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected) and int(exp_s) > int(time.time())


def cabinet_token(company_id: int) -> str:
    """Неугадываемая read-only ссылка на кабинет клиента для одной компании.
    Привязана к _SECRET — смена секрета отзывает все ссылки."""
    sig = hmac.new(_SECRET.encode(), f"cab:{company_id}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{company_id}-{sig}"


def cabinet_verify(token: str):
    """Токен -> company_id или None."""
    try:
        cid_s, sig = (token or "").split("-", 1)
        cid = int(cid_s)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_SECRET.encode(), f"cab:{cid}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    return cid if hmac.compare_digest(sig, expected) else None

# фоновые задачи держим в живых (иначе их соберёт GC)
_BG_TASKS: set = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@asynccontextmanager
async def lifespan(app):
    if DEMO:
        import demo
        db.DB_PATH = Path(__file__).parent / "demo.db"
        conn = db.connect()
        if demo.seed(conn):
            print("  [demo] демо-данные созданы (demo.db)", flush=True)
        conn.close()
    # стартовые настройки из .env (только если в БД ещё пусто)
    conn = db.connect()
    global _SECRET
    _SECRET = os.getenv("PANEL_SECRET") or db.get_setting(conn, "panel_secret")
    if not _SECRET:  # сгенерировать и сохранить (сессии переживут перезапуск)
        _SECRET = secrets.token_hex(32)
        db.set_setting(conn, "panel_secret", _SECRET)
    if not db.get_setting(conn, "bot_token") and os.getenv("TELEGRAM_BOT_TOKEN"):
        db.set_setting(conn, "bot_token", os.getenv("TELEGRAM_BOT_TOKEN"))
    if not db.get_setting(conn, "openai_api_key") and os.getenv("OPENAI_API_KEY"):
        db.set_setting(conn, "openai_api_key", os.getenv("OPENAI_API_KEY"))
    if not db.get_setting(conn, "openai_model"):
        db.set_setting(conn, "openai_model",
                       os.getenv("OPENAI_MODEL") or ai_reply.DEFAULT_MODEL)
    conn.close()
    # в демо фоновые задачи не нужны (компания не скрапится, токена может не быть)
    tasks = [] if DEMO else [asyncio.create_task(monitor.scheduler_loop()),
                             asyncio.create_task(bot.bot_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

# Имя продукта (bedel, каз. — авторитет, репутация, вес в обществе). Домен bedelreviews.kz
BRAND = "Bedel"
TAGLINE = "вес вашей репутации"


@app.middleware("http")
async def guard(request: Request, call_next):
    """Защита панели: (1) Host-allowlist (DNS rebinding), (2) CSRF по Origin для
    POST, (3) логин по сессионной cookie, если задан PANEL_PASSWORD."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in ALLOWED_HOSTS:
        return PlainTextResponse("Forbidden: unknown host", status_code=403)
    if request.method == "POST":
        source = request.headers.get("origin") or request.headers.get("referer") or ""
        if source:
            src_host = (urllib.parse.urlparse(source).hostname or "").lower()
            if src_host not in ALLOWED_HOSTS:
                return PlainTextResponse("Forbidden: cross-site POST", status_code=403)
    path = request.url.path
    public = path in OPEN_PATHS or path.startswith("/c/")  # /c/ — кабинет клиента
    if AUTH_ON and not public:
        if not _valid_session(request.cookies.get("bedel_session", "")):
            if request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return PlainTextResponse("Unauthorized", status_code=401)
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
.tagline{color:#7E9384;font-size:.82em;font-style:italic;letter-spacing:.02em;
         border-left:1px solid #34433B;padding-left:14px}
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
.charts{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:640px){.charts{grid-template-columns:1fr}}
.chart .ct{font-family:'JetBrains Mono',monospace;font-size:11px;fill:#5C7064}
.chartlabel{font-family:'JetBrains Mono',monospace;font-size:.7em;letter-spacing:.1em;
   text-transform:uppercase;color:#5C7064;margin-bottom:2px}
.distrib{display:flex;flex-direction:column;gap:5px;margin-top:4px}
.distrow{display:flex;align-items:center;gap:10px}
.distrow .dl{width:26px;font-size:.85em;color:#5C7064}
.distrow .dbar{flex:1;background:#EEF2EE;border-radius:6px;height:14px;overflow:hidden}
.distrow .dbar i{display:block;height:100%;border-radius:6px}
.distrow .dbar i.g{background:#1E7A46}.distrow .dbar i.a{background:#B97E14}
.distrow .dbar i.r{background:#D8472B}
.distrow .dn{width:30px;text-align:right;font-size:.85em;color:#5C7064}
.kv{display:flex;gap:30px;flex-wrap:wrap;margin:10px 0 14px}
.kv b{font-size:1.5em;font-family:'JetBrains Mono',Consolas,monospace;font-weight:600}
.kv span{display:block;color:var(--muted);font-size:.82em}
form.inline{display:inline}
.sysbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
ol.help li{margin:6px 0}
.links a{margin-right:10px;white-space:nowrap}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:640px){header{padding:12px 16px}header nav{margin-left:auto}
  .tagline{display:none}main{margin-top:14px}.addform{flex-direction:column}}
"""

FAVICON = ("data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 "
           "viewBox=%220 0 100 100%22><path d=%22M50 6C29 6 13 22 13 43c0 27 37 51 "
           "37 51s37-24 37-51C87 22 71 6 50 6z%22 fill=%22%231E7A46%22/>"
           "<path d=%22M50 24L54.7 35.5L67.1 36.4L57.6 44.5L60.6 56.6L50 50"
           "L39.4 56.6L42.4 44.5L32.9 36.4L45.3 35.5Z%22 fill=%22white%22/></svg>")

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


def svg_line_chart(points, color="#1E7A46", w=560, h=160, pad=34,
                   force_min=None, force_max=None, fmt=str) -> str:
    """Линейный график из [(подпись, число)]. Пусто, если точек <2.

    Подписи значений — у левого края (ось Y), даты — отдельной строкой внизу,
    чтобы не накладывались."""
    pts = [(lbl, v) for lbl, v in points if v is not None]
    if len(pts) < 2:
        return ('<p class="muted">Графика пока нет — нужно ≥2 проверки в разные '
                'дни. Появится по мере накопления истории.</p>')
    vals = [v for _, v in pts]
    vmin = force_min if force_min is not None else min(vals)
    vmax = force_max if force_max is not None else max(vals)
    if vmax <= vmin:
        vmax = vmin + 1
    n = len(pts)
    top, floor = 18, h - 34            # вертикальные границы зоны графика
    fx = lambda i: pad + (w - pad - 14) * i / (n - 1)
    fy = lambda v: top + (floor - top) * (1 - (v - vmin) / (vmax - vmin))
    poly = " ".join(f"{fx(i):.1f},{fy(v):.1f}" for i, (_, v) in enumerate(pts))
    area = f"{fx(0):.1f},{floor:.1f} " + poly + f" {fx(n - 1):.1f},{floor:.1f}"
    dots = "".join(f'<circle cx="{fx(i):.1f}" cy="{fy(v):.1f}" r="3" '
                   f'fill="{color}"/>' for i, (_, v) in enumerate(pts))
    e = html.escape
    return f'''<svg viewBox="0 0 {w} {h}" width="100%" role="img" class="chart">
 <polygon points="{area}" fill="{color}" opacity="0.09"/>
 <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.5"
   stroke-linejoin="round" stroke-linecap="round"/>{dots}
 <text x="2" y="{top + 4}" class="ct">{e(fmt(vmax))}</text>
 <text x="2" y="{floor}" class="ct">{e(fmt(vmin))}</text>
 <text x="{pad}" y="{h - 8}" class="ct">{e(pts[0][0])}</text>
 <text x="{w - 14}" y="{h - 8}" class="ct" text-anchor="end">{e(pts[-1][0])}</text>
</svg>'''


def rating_distribution(reviews) -> str:
    """5 горизонтальных столбиков: сколько отзывов с каждой оценкой (из окна)."""
    counts = {s: 0 for s in range(1, 6)}
    for r in reviews:
        rt = r["rating"]
        if rt in counts:
            counts[rt] += 1
    total = sum(counts.values())
    if not total:
        return '<p class="muted">Отзывы ещё не загружены.</p>'
    rows = []
    for s in range(5, 0, -1):
        n = counts[s]
        pct = round(100 * n / total)
        cls = "g" if s >= 5 else ("a" if s == 4 else "r")
        rows.append(
            f'<div class="distrow"><span class="dl">{s}★</span>'
            f'<span class="dbar"><i class="{cls}" style="width:{pct}%"></i></span>'
            f'<span class="dn mono">{n}</span></div>')
    return f'<div class="distrib">{"".join(rows)}</div>'


def render_dynamics_card(history, reviews) -> str:
    """Карточка «Динамика»: графики рейтинга/числа отзывов, распределение, % ответов.
    Общая для страницы компании и кабинета клиента."""
    def short_date(iso):
        try:
            return datetime.fromisoformat(iso).strftime("%d.%m")
        except (ValueError, TypeError):
            return ""
    rating_pts = [(short_date(h["ts"]), h["rating"]) for h in history]
    count_pts = [(short_date(h["ts"]), h["reviews_count"]) for h in history]
    rating_chart = svg_line_chart(rating_pts, color="#1E7A46", force_min=3.0,
                                  force_max=5.0, fmt=lambda v: f"{v:.1f}".replace(".", ","))
    count_chart = svg_line_chart(count_pts, color="#2B5FD9", fmt=lambda v: str(int(v)))
    answered_now = (history[-1]["answered_pct"] if history
                    and history[-1]["answered_pct"] is not None else None)
    answered_line = (f'<div class="kv" style="margin-top:6px"><div>'
                     f'<b>{answered_now}%</b><span>ответов владельца (окно)</span>'
                     f'</div></div>' if answered_now is not None else "")
    return f"""<div class="card"><h2>Динамика</h2>
 <div class="charts">
  <div><div class="chartlabel">Рейтинг (3,0–5,0)</div>{rating_chart}</div>
  <div><div class="chartlabel">Всего отзывов</div>{count_chart}</div>
 </div>
 <div style="margin-top:16px"><div class="chartlabel">Распределение оценок
   (последние {len(reviews)})</div>{rating_distribution(reviews)}</div>
 {answered_line}
</div>"""


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
    brand = f'{e(BRAND).upper()}<b>.</b>'
    demo_chip = ('<span class="chip" style="background:#F7EED7;color:#B97E14">'
                 'демо-режим</span>' if DEMO else "")
    logout_link = ('<form class="inline" method="post" action="/logout">'
                   '<button class="small" style="margin-left:6px">Выйти</button>'
                   '</form>') if AUTH_ON else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href='{FAVICON}'>{FONTS}
<title>{e(title)} — {e(BRAND)}</title><style>{CSS}</style></head><body>
<header><a class="brand" href="/">{brand}</a>
<span class="tagline">{e(TAGLINE)}</span>{demo_chip}
<nav><a href="/">Карточки</a><a href="/health">Состояние</a>
<a href="/settings">Настройки</a>{logout_link}</nav></header>
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


# ---------- вход / здоровье ----------

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    """Открытый пинг для аптайм-мониторинга (без секретов)."""
    return "ok"


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not AUTH_ON or _valid_session(request.cookies.get("bedel_session", "")):
        return RedirectResponse("/", status_code=303)
    bad = "<div class='flash err'>Неверный пароль</div>" if request.query_params.get("e") else ""
    body = f"""<div class="card" style="max-width:380px;margin:60px auto">
 <h1>Вход в {html.escape(BRAND)}</h1>
 <p class="muted">Панель защищена паролем.</p>{bad}
 <form method="post" action="/login">
  <input type="password" name="password" autofocus aria-label="Пароль"
         placeholder="Пароль" style="margin-bottom:10px">
  <button class="primary">Войти</button>
 </form></div>"""
    return page("Вход", body, request)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    pwd = str(form.get("password", ""))
    # сравниваем в байтах: hmac.compare_digest для str падает на не-ASCII паролях
    if AUTH_ON and hmac.compare_digest(pwd.encode("utf-8"), PANEL_PASSWORD.encode("utf-8")):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("bedel_session", _sign_session(int(time.time()) + SESSION_TTL),
                        max_age=SESSION_TTL, httponly=True, samesite="lax")
        return resp
    return RedirectResponse("/login?e=1", status_code=303)


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("bedel_session")
    return resp


# ---------- кабинет клиента (read-only по токену) ----------

def client_page(title: str, body: str) -> HTMLResponse:
    """Минимальный публичный шаблон без навигации/управления — для клиента."""
    e = html.escape
    brand = f'{e(BRAND).upper()}<b>.</b>'
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href='{FAVICON}'>{FONTS}
<title>{e(title)} — {e(BRAND)}</title><style>{CSS}</style></head><body>
<header><span class="brand">{brand}</span>
<span class="tagline">{e(TAGLINE)}</span></header>
<main>{body}</main></body></html>""")


@app.get("/c/{token}", response_class=HTMLResponse)
async def client_cabinet(token: str):
    e = html.escape
    cid = cabinet_verify(token)
    if cid is None:
        return HTMLResponse("<h1>Ссылка недействительна</h1>", status_code=404)
    conn = db.connect()
    try:
        c = db.get_company(conn, cid)
        if c is None:
            return HTMLResponse("<h1>Компания не найдена</h1>", status_code=404)
        reviews = db.recent_reviews(conn, cid, limit=30)
        history = db.rating_history(conn, cid)
        reports = db.list_reports(conn, cid)
    finally:
        conn.close()

    rating = "—" if c["rating"] is None else f"{c['rating']:.1f}".replace(".", ",")
    count = "—" if c["reviews_count"] is None else c["reviews_count"]
    quotes = []
    for rv in reviews:
        neg = tg.is_negative(rv["rating"])
        ans = ('<span class="badge b-ok">есть ответ</span>' if rv["answered"]
               else '<span class="badge b-err">без ответа</span>')
        quotes.append(f"""<blockquote class="{'neg' if neg else ''}">{e(rv['text'] or '(без текста)')}
 <div class="meta">{tg.stars(rv['rating'])} · {e((rv['date_created'] or '')[:10])} ·
 {e(rv['author'] or 'аноним')} · {ans}</div></blockquote>""")
    reviews_html = "".join(quotes) or '<p class="muted">Отзывы ещё не загружены.</p>'

    report_link = ""
    if reports:
        rid = reports[0]["id"]
        report_link = (f'<div class="card"><h2>Аналитический отчёт</h2>'
                       f'<p class="muted">Разбор болей клиентов и рекомендации '
                       f'за {e(reports[0]["period"])}.</p>'
                       f'<a class="btn" href="/c/{token}/report/{rid}" '
                       f'target="_blank" rel="noopener">Открыть отчёт →</a></div>')

    body = f"""
<div class="card">
 <div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap">
  {health_pin(c['rating'], c['status'], 'big')}
  <div style="flex:1;min-width:240px">
   <h1 style="margin:0 0 4px">{e(c['name'])}</h1>
   <div class="meta">{e(c['category'] or '')} · {e(c['city_slug'])} ·
     <a href="{e(c['url'])}">карточка в 2ГИС</a></div>
  </div>
 </div>
 <div class="kv">
  <div><b>{rating}</b><span>рейтинг</span></div>
  <div><b>{count}</b><span>отзывов</span></div>
  <div><b>{fmt_dt(c['last_checked'])}</b><span>обновлено</span></div>
 </div>
</div>
{render_dynamics_card(history, reviews)}
{report_link}
<div class="card"><h2>Последние отзывы</h2>{reviews_html}</div>
<p class="muted">Эта страница обновляется автоматически. Подготовлено {e(BRAND)}.</p>"""
    return client_page(c["name"], body)


@app.get("/c/{token}/report/{rid}", response_class=HTMLResponse)
async def client_report(token: str, rid: int):
    cid = cabinet_verify(token)
    if cid is None:
        return HTMLResponse("<h1>Ссылка недействительна</h1>", status_code=404)
    conn = db.connect()
    try:
        rep = db.get_report(conn, rid)
    finally:
        conn.close()
    if rep is None or rep["company_id"] != cid:  # отчёт только своей компании
        return HTMLResponse("<h1>Отчёт не найден</h1>", status_code=404)
    return HTMLResponse(rep["html"])


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    e = html.escape
    conn = db.connect()
    try:
        companies = db.list_companies(conn)
        interval = db.get_setting(conn, "check_interval_min", "360")
        cap = db.get_setting(conn, "max_checks_per_hour", "40")
    finally:
        conn.close()
    errored = [c for c in companies if c["status"] == db.ST_ERROR]
    dead = [c for c in companies if c["consecutive_errors"] >= monitor.DEAD_THRESHOLD]
    health = ('<span class="chip err">парсер под угрозой</span>' if dead else
              '<span class="chip ok">парсер жив</span>' if companies else
              '<span class="chip busy">нет компаний</span>')
    rows = "".join(
        f'<tr><td>{e(c["name"])}</td><td>{ST_BADGE.get(c["status"], "")}</td>'
        f'<td class="mono">{c["consecutive_errors"]}</td>'
        f'<td class="mono">{fmt_dt(c["last_checked"])}</td>'
        f'<td class="meta">{e((c["error"] or "")[:80])}</td></tr>'
        for c in companies)
    body = f"""
<h1>Состояние системы</h1>
<div class="sysbar">{health}
 <span class="muted">компаний: {len(companies)} · с ошибкой: {len(errored)} ·
 интервал <span class="mono">{e(interval)}</span> мин · лимит
 <span class="mono">{e(cap)}</span>/час</span></div>
<div class="card"><h2>Карточки</h2>
 <table><tr><th>Компания</th><th>Статус</th><th>Ошибок подряд</th>
 <th>Проверено</th><th>Последняя ошибка</th></tr>{rows or ''}</table>
 {'<p class="muted">Нет компаний.</p>' if not companies else ''}
</div>
<p><a href="/">← к карточкам</a></p>"""
    return page("Состояние", body, request)


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
 <form class="addform" method="post" action="/audit" style="margin-top:10px">
  <input type="text" name="url" required aria-label="Ссылка для аудита"
         placeholder="Аудит по ссылке без мониторинга — отчёт болей придёт в Telegram (~3 мин)">
  <button>Сделать аудит</button>
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
        reports = db.list_reports(conn, cid)
        history = db.rating_history(conn, cid)
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

    dynamics_card = render_dynamics_card(history, reviews)
    cab_url = f"/c/{cabinet_token(cid)}"

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
{dynamics_card}
<div class="card"><h2>Ссылка для клиента</h2>
 <p class="muted">Read-only страница: рейтинг, графики, отзывы и последний отчёт.
 Без управления и настроек — можно смело отдавать клиенту.</p>
 <p><a class="btn" href="{cab_url}" target="_blank" rel="noopener">Открыть кабинет клиента →</a></p>
 <pre id="caburl" style="font-size:.85em">{e(cab_url)}</pre>
 <button onclick="(function(){{var u=location.origin+document.getElementById('caburl').innerText;
   navigator.clipboard&&navigator.clipboard.writeText(u);}})()">Копировать полную ссылку</button>
</div>
<div class="card"><h2>Кому слать уведомления</h2>
 <p class="muted">Админ-чаты получают все компании всегда: <b>{admin_line}</b>.
 Ниже — дополнительные получатели именно этой компании (например, чат клиента).</p>
 {routing}
</div>
<div class="card"><h2>Аналитический отчёт болей</h2>
 <p class="muted">Глубокий сбор до 300 отзывов, карта болей с цитатами-уликами и
 конкретные рекомендации. Готовый отчёт уходит в Telegram файлом (печать в PDF —
 Ctrl+P). Со второго отчёта появляется динамика «было → стало».</p>
 <form class="inline" method="post" action="/companies/{cid}/report">
   <button class="primary">Сгенерировать отчёт</button></form>
 {('<table><tr><th>Когда</th><th>Период данных</th><th>Объём</th><th></th></tr>'
   + ''.join(f'<tr><td class="mono">{fmt_dt(r["created_at"])}</td>'
             f'<td>{e(r["period"])}</td><td>{r["reviews_used"]} отз.</td>'
             f'<td class="links"><a href="/report/{r["id"]}" target="_blank" rel="noopener">открыть</a> '
             f'<form class="inline" method="post" action="/report/{r["id"]}/send">'
             f'<button class="small">в Telegram</button></form></td></tr>'
             for r in reports) + '</table>') if reports else
  '<p class="muted" style="margin-top:10px">Отчётов ещё нет.</p>'}
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


async def _send_report(report_id: int) -> list:
    """Отправить сохранённый отчёт получателям компании. Список ошибок."""
    conn = db.connect()
    try:
        rep = db.get_report(conn, report_id)
        if rep is None:
            return ["отчёт не найден"]
        comp = db.get_company(conn, rep["company_id"])
        name = comp["name"] if comp else "компания"
        fname = f"otchet_{rep['company_id']}_{rep['created_at'][:10]}.html"
        caption = (f"📊 Аналитика отзывов — {name} · {rep['period']} · "
                   f"в анализе {rep['reviews_used']} отзывов")
        return await monitor.notify_document(conn, fname,
                                             rep["html"].encode("utf-8"),
                                             caption, company_id=rep["company_id"])
    finally:
        conn.close()


async def _report_task(cid: int):
    """Фон: построить отчёт и разослать; об ошибке сказать админам в TG."""
    try:
        rid = await insights.build_report(cid, brand=BRAND)
    except Exception as ex:
        conn = db.connect()
        try:
            await monitor.notify_chats(
                conn, f"⚠ Отчёт не построился: {html.escape(str(ex)[:300])}")
        finally:
            conn.close()
        return
    for err in await _send_report(rid):
        print(f"[report] {err}", flush=True)


async def _audit_task(domain: str, slug: str, firm_id: str):
    """Фон: аудит по ссылке (без мониторинга) -> файл в output/ + админам в TG."""
    conn = db.connect()
    try:
        try:
            name, html_out = await insights.build_adhoc_report(
                domain, slug, firm_id, brand=BRAND)
        except Exception as ex:
            await monitor.notify_chats(
                conn, f"⚠ Аудит по ссылке не построился: {html.escape(str(ex)[:300])}")
            return
        fname = f"audit_insights_{firm_id}.html"
        out = Path("output")
        out.mkdir(exist_ok=True)
        (out / fname).write_text(html_out, encoding="utf-8")
        for err in await monitor.notify_document(
                conn, fname, html_out.encode("utf-8"),
                f"📊 Аудит по ссылке — {name} (копия: output/{fname})"):
            print(f"[audit] {err}", flush=True)
    finally:
        conn.close()


@app.post("/companies/{cid}/report")
async def companies_report(cid: int):
    conn = db.connect()
    try:
        if db.get_company(conn, cid) is None:
            return redirect("/", err="Компания не найдена")
        api_key, _ = ai_reply.get_ai_settings(conn)
    finally:
        conn.close()
    if not api_key:
        return redirect(f"/company/{cid}",
                        err="AI не настроен: добавьте ключ OpenAI в «Настройках»")
    spawn(_report_task(cid))
    return redirect(f"/company/{cid}",
                    msg="Отчёт готовится (~2–4 мин): сбор до 300 отзывов и анализ. "
                        "Придёт в Telegram и появится в списке отчётов.")


@app.get("/report/{rid}", response_class=HTMLResponse)
async def report_view(rid: int):
    conn = db.connect()
    try:
        rep = db.get_report(conn, rid)
    finally:
        conn.close()
    if rep is None:
        return redirect("/", err="Отчёт не найден")
    return HTMLResponse(rep["html"])


@app.post("/report/{rid}/send")
async def report_send(rid: int):
    errors = await _send_report(rid)
    conn = db.connect()
    try:
        rep = db.get_report(conn, rid)
    finally:
        conn.close()
    back = f"/company/{rep['company_id']}" if rep else "/"
    if errors:
        return redirect(back, err="; ".join(errors))
    return redirect(back, msg="Отчёт отправлен в Telegram")


@app.post("/audit")
async def audit_by_link(request: Request):
    form = await request.form()
    parsed = monitor.parse_2gis_url(str(form.get("url", "")))
    if not parsed:
        return redirect("/", err="Не похоже на ссылку карточки 2ГИС")
    conn = db.connect()
    try:
        api_key, _ = ai_reply.get_ai_settings(conn)
    finally:
        conn.close()
    if not api_key:
        return redirect("/", err="AI не настроен: добавьте ключ OpenAI в «Настройках»")
    domain, slug, firm_id = parsed
    spawn(_audit_task(domain, slug, firm_id))
    return redirect("/", msg="Аудит готовится (~2–4 мин) — придёт файлом в "
                             "админ-чаты Telegram, копия ляжет в output/.")


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
        max_per_hour = db.get_setting(conn, "max_checks_per_hour", "40")
        notify_all = db.get_setting(conn, "notify_all", "1") == "1"
        auto_digest = db.get_setting(conn, "auto_digest", "0") == "1"
        auto_report = db.get_setting(conn, "auto_report", "0") == "1"
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
  <p><b>Лимит проверок в час</b><br>
   <input type="number" name="max_per_hour" value="{e(max_per_hour)}" min="1" style="width:120px">
   <span class="muted">потолок под масштаб: при многих компаниях остаток едет на следующий час</span></p>
  <p><label><input type="checkbox" name="notify_all" {'checked' if notify_all else ''}>
   уведомлять обо <b>всех</b> новых отзывах (иначе — только о негативе, оценка ≤3)</label></p>
  <p><label><input type="checkbox" name="auto_digest" {'checked' if auto_digest else ''}>
   слать клиентам <b>еженедельный дайджест</b> (рейтинг, новые отзывы, негатив)</label></p>
  <p><label><input type="checkbox" name="auto_report" {'checked' if auto_report else ''}>
   строить <b>ежемесячный отчёт болей</b> автоматически и слать получателям</label></p>
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
    max_per_hour = str(form.get("max_per_hour", "40")).strip()
    notify_all = "1" if form.get("notify_all") else "0"
    auto_digest = "1" if form.get("auto_digest") else "0"
    auto_report = "1" if form.get("auto_report") else "0"
    try:
        interval = str(max(30, int(interval)))
    except ValueError:
        interval = "360"
    try:
        max_per_hour = str(max(1, int(max_per_hour)))
    except ValueError:
        max_per_hour = "40"

    openai_key = str(form.get("openai_key", "")).strip()
    openai_model = str(form.get("openai_model", "")).strip() or ai_reply.DEFAULT_MODEL

    conn = db.connect()
    try:
        if new_token:  # пустое поле = оставить прежний токен (он не светится в HTML)
            db.set_setting(conn, "bot_token", new_token)
        token = db.get_setting(conn, "bot_token")
        db.set_setting(conn, "check_interval_min", interval)
        db.set_setting(conn, "max_checks_per_hour", max_per_hour)
        db.set_setting(conn, "notify_all", notify_all)
        db.set_setting(conn, "auto_digest", auto_digest)
        db.set_setting(conn, "auto_report", auto_report)
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
    config = uvicorn.Config(app, host=BIND, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    auth = "с логином" if AUTH_ON else "без логина (локально)"
    print(f"Панель {BRAND} ({auth}): http://{HOST}:{PORT}", flush=True)
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
