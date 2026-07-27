"""
Генерация материалов для рассылки (outreach) из собранных лидов.

Вход — список Company после прогона (leadgen.py). Выход — «пакет для рассылки»
в папке output/outreach_<город>_<категория>_<дата>/:
  index.html       — дашборд: лиды по приоритету, готовое персональное
                     сообщение (кнопка «копировать»), ссылка wa.me с уже
                     подставленным текстом, ссылка на аудит
  audit_<id>.html  — одностраничный аудит карточки: метрики, худшие отзывы,
                     сравнение с конкурентом, «3 шага» (печать в PDF — Ctrl+P)
  outreach.csv     — Название;Телефон;WhatsApp;Сообщение (для своего учёта)

Конкурент для сравнения берётся из этого же прогона (та же категория и город):
лучший рейтинг при >=30 отзывах. Дополнительных запросов к 2GIS нет.

Чистая логика без браузера и сети — самотесты: python outreach.py
"""
import csv
import html
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from lead_scoring import Company, NA, sort_key

# Минимальный разрыв рейтинга, при котором упоминаем конкурента
COMPETITOR_GAP = 0.2
# Полоса «рискованного» рейтинга (как в lead_scoring)
RATING_BAND = (3.5, 4.4)
MAX_BULLETS = 3  # пунктов боли в сообщении


# ---------- утилиты ----------

def fmt_rating(r: Optional[float]) -> str:
    """4.7 -> «4,7» (русская запись)."""
    return NA if r is None else f"{r:.1f}".replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: plural(3, 'отзыв', 'отзыва', 'отзывов') -> 'отзыва'."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def phone_digits(phone: str) -> Optional[str]:
    """Первый телефон из строки «+7..., +7...» -> цифры для wa.me (7XXXXXXXXXX)."""
    if not phone or phone == NA:
        return None
    digits = re.sub(r"\D", "", phone.split(",")[0])
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits if 11 <= len(digits) <= 15 else None


def wa_link(phone: str, message: str = "") -> Optional[str]:
    digits = phone_digits(phone)
    if not digits:
        return None
    link = f"https://wa.me/{digits}"
    if message:
        link += "?text=" + urllib.parse.quote(message)
    return link


def instagram_link(website: str) -> Optional[str]:
    """Ссылка на Instagram из поля «сайт/соцсети» (для outreach в Direct)."""
    m = re.search(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+", website or "")
    return m.group(0) if m else None


def pick_benchmark(companies: list, exclude: Company = None) -> Optional[Company]:
    """Конкурент для сравнения: лучший рейтинг при >=30 отзывах (из этого прогона)."""
    pool = [c for c in companies
            if c is not exclude and c.rating is not None
            and (c.reviews_count or 0) >= 30]
    return max(pool, key=lambda c: (c.rating, c.reviews_count or 0), default=None)


# ---------- пункты «боли» (общие для сообщения и аудита) ----------

def pain_bullets(c: Company, competitor: Optional[Company]) -> list:
    """До MAX_BULLETS самых сильных фактов о карточке, по убыванию силы."""
    out = []

    # 1. проигрыш конкуренту по рейтингу — самая продающая боль
    if (competitor is not None and c.rating is not None
            and competitor.rating - c.rating >= COMPETITOR_GAP):
        out.append(f"рейтинг {fmt_rating(c.rating)} — у «{competitor.name}» рядом "
                   f"{fmt_rating(competitor.rating)}; для людей, выбирающих по карте, "
                   f"это аргумент не в вашу пользу")
    elif c.rating is not None and RATING_BAND[0] <= c.rating <= RATING_BAND[1]:
        out.append(f"рейтинг {fmt_rating(c.rating)} — до «зелёной зоны» 4,5+ не хватает "
                   f"немного, а именно она даёт поток клиентов с карт")

    # 2. негатив без ответа / отзывы без ответа
    if c.negative_unanswered:
        n = c.negative_unanswered
        out.append(f"{n} {plural(n, 'негативный отзыв висит', 'негативных отзыва висят', 'негативных отзывов висят')} "
                   f"без ответа — это видит каждый, кто открывает карточку")
    elif c.unanswered_count and c.loaded_reviews:
        n = c.unanswered_count
        out.append(f"{n} из последних {c.loaded_reviews} отзывов "
                   f"{plural(n, 'остался', 'остались', 'остались')} без ответа")

    # 3. карточка «спит» или отзывов мало
    if c.has_fresh_reviews is False:
        out.append("за последний месяц новых отзывов нет — карточка выглядит "
                   "неактивной, и пара плохих оценок легко сдвинет рейтинг вниз")
    elif c.reviews_count is not None and c.reviews_count < 30:
        out.append(f"отзывов всего {c.reviews_count} — на фоне конкурентов с сотнями "
                   f"отзывов карточка вызывает меньше доверия")

    if not out:
        out.append("есть несколько моментов, из-за которых карточка приводит меньше "
                   "клиентов, чем могла бы")
    return out[:MAX_BULLETS]


# ---------- сообщение-открывашка ----------

def build_message(c: Company, competitor: Optional[Company], cfg) -> str:
    sender = cfg.sender_name or "[Имя]"
    name = c.name if c.name != NA else "вашей компании"
    bullets = "\n".join(f"• {b[0].upper()}{b[1:]}." for b in pain_bullets(c, competitor))
    return (
        f"Здравствуйте! Меня зовут {sender}. Помогаю бизнесу в {cfg.city} получать "
        f"больше клиентов с карт — отзывы и рейтинг на 2ГИС.\n"
        f"\n"
        f"Посмотрел карточку «{name}» на 2ГИС:\n"
        f"{bullets}\n"
        f"\n"
        f"Скинуть полный разбор + 3 конкретных шага, что с этим делать? "
        f"Бесплатно, без обязательств."
    )


# ---------- аудит (одна страница HTML, печатается в PDF) ----------

_AUDIT_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;max-width:760px;margin:24px auto;
     padding:0 16px;color:#1a1a2e;line-height:1.5}
h1{font-size:1.5em;margin-bottom:0}
.sub{color:#666;margin-top:4px}
.metrics{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}
.metric{flex:1 1 140px;background:#f4f6fa;border-radius:10px;padding:14px;text-align:center}
.metric .v{font-size:1.7em;font-weight:700}
.metric .bad{color:#c0392b}.metric .ok{color:#27ae60}
.metric .l{color:#666;font-size:.85em}
ul.pains li{margin:6px 0}
blockquote{background:#fff6f6;border-left:4px solid #c0392b;margin:10px 0;
           padding:10px 14px;border-radius:0 8px 8px 0}
blockquote .meta{color:#888;font-size:.85em}
.vs{background:#f4f6fa;border-radius:10px;padding:14px;margin:16px 0}
.steps li{margin:8px 0}
.footer{margin-top:28px;padding-top:12px;border-top:1px solid #ddd;color:#666;font-size:.9em}
@media print{body{margin:0}.noprint{display:none}}
"""


def render_audit_html(c: Company, competitor: Optional[Company], cfg) -> str:
    e = html.escape
    name = c.name if c.name != NA else "Компания"
    today = datetime.now().strftime("%d.%m.%Y")

    unanswered = c.unanswered_count
    metrics = f"""
<div class="metrics">
 <div class="metric"><div class="v {'bad' if c.rating is not None and c.rating < 4.5 else 'ok'}">{e(fmt_rating(c.rating))}</div><div class="l">рейтинг 2ГИС</div></div>
 <div class="metric"><div class="v">{c.reviews_count if c.reviews_count is not None else e(NA)}</div><div class="l">всего отзывов</div></div>
 <div class="metric"><div class="v {'bad' if unanswered else 'ok'}">{unanswered if unanswered is not None else e(NA)}</div><div class="l">без ответа (из последних {c.loaded_reviews or '—'})</div></div>
 <div class="metric"><div class="v {'ok' if c.fresh_count else 'bad'}">{c.fresh_count if c.fresh_count is not None else e(NA)}</div><div class="l">новых за 30 дней</div></div>
</div>"""

    pains = "\n".join(f"<li>{e(b[0].upper() + b[1:])}.</li>"
                      for b in pain_bullets(c, competitor))

    worst = ""
    if c.worst_reviews:
        quotes = []
        for r in c.worst_reviews:
            stars = "★" * int(r.get("rating") or 0) + "☆" * (5 - int(r.get("rating") or 0))
            answered = "есть ответ владельца" if r.get("answered") else "без ответа"
            text = e(r.get("text") or "(без текста)")
            quotes.append(f'<blockquote>{text}<div class="meta">{stars} · '
                          f'{e(r.get("date") or "")} · {answered}</div></blockquote>')
        worst = "<h2>Что видят клиенты: свежий негатив</h2>" + "\n".join(quotes)

    vs = ""
    if competitor is not None and competitor.rating is not None:
        vs = (f'<div class="vs"><b>Сравнение с конкурентом рядом.</b> '
              f'«{e(competitor.name)}» — рейтинг {e(fmt_rating(competitor.rating))} '
              f'при {competitor.reviews_count or "?"} отзывах. '
              f'Когда человек выбирает по карте между вами и ними, '
              f'рейтинг — первое, что он сравнивает.</div>')

    contact = e(cfg.sender_contact or cfg.sender_name or "")
    footer = (f'Разбор подготовил {e(cfg.sender_name) or "[Имя]"}'
              + (f" · {contact}" if cfg.sender_contact else "")
              + f" · {today}. Данные — открытая карточка 2ГИС.")

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Разбор карточки 2ГИС — {e(name)}</title>
<style>{_AUDIT_CSS}</style></head><body>
<h1>Разбор карточки 2ГИС — {e(name)}</h1>
<div class="sub">{e(c.category)} · {e(cfg.city)} · <a href="{e(c.url)}">карточка в 2ГИС</a></div>
{metrics}
<h2>Что бросается в глаза</h2>
<ul class="pains">{pains}</ul>
{worst}
{vs}
<h2>3 шага, которые можно сделать уже на этой неделе</h2>
<ol class="steps">
 <li><b>Ответить на весь негатив.</b> Спокойный ответ владельца виден каждому будущему
     клиенту и снимает до половины ущерба от плохого отзыва. Мы готовим черновики
     ответов в вашем стиле — остаётся нажать «отправить».</li>
 <li><b>Запустить поток отзывов от довольных клиентов.</b> Ссылка/QR на карточку +
     короткое сообщение клиенту после визита. Рейтинг растёт за 1–2 месяца, недовольных
     сначала выслушиваем приватно — негатив не доходит до карты.</li>
 <li><b>Включить мониторинг.</b> О новом негативе вы узнаёте в тот же день, а не через
     месяц, когда его прочитали сотни людей.</li>
</ol>
<div class="footer">{footer}</div>
</body></html>"""


# ---------- дашборд рассылки ----------

_DASH_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;color:#1a1a2e}
h1{font-size:1.4em}
.sub{color:#666;margin-bottom:16px}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid #e3e3ee;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f4f6fa;position:sticky;top:0}
tr:hover{background:#fafbff}
.links a{margin-right:10px;white-space:nowrap}
details{max-width:520px}
pre{white-space:pre-wrap;background:#f4f6fa;border-radius:8px;padding:10px;font-family:inherit;font-size:.92em}
button{cursor:pointer;border:1px solid #ccd;border-radius:6px;background:#fff;padding:4px 10px}
button:hover{background:#eef}
.warn{background:#fff7e0;border:1px solid #f0d080;border-radius:8px;padding:10px;margin-bottom:14px}
"""

_DASH_JS = """
function copyMsg(id, btn){
  const text = document.getElementById(id).innerText;
  const done = () => { btn.textContent = 'Скопировано ✓';
                       setTimeout(() => btn.textContent = 'Копировать', 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText)
    navigator.clipboard.writeText(text).then(done);
  else { // фолбэк для file:// в старых браузерах
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove(); done();
  }
}
"""


def render_dashboard_html(rows: list, cfg) -> str:
    """rows: список dict(c, message, wa, audit_file)."""
    e = html.escape
    today = datetime.now().strftime("%d.%m.%Y %H:%M")
    warn = ("" if cfg.sender_name else
            '<div class="warn">⚠️ SENDER_NAME в .env не заполнен — в сообщениях стоит '
            'заглушка [Имя]. Заполните и перезапустите, либо правьте при отправке.</div>')

    trs = []
    for i, row in enumerate(rows):
        c = row["c"]
        links = [f'<a href="{e(c.url)}">2ГИС</a>',
                 f'<a href="{e(row["audit_file"])}">аудит</a>']
        if row["wa"]:
            links.append(f'<a href="{e(row["wa"])}">WhatsApp ➤</a>')
        ig = instagram_link(c.website)
        if ig:
            links.append(f'<a href="{e(ig)}">Instagram</a>')
        trs.append(f"""<tr>
 <td>{e(c.priority)}</td>
 <td><b>{e(c.name)}</b><br><span style="color:#888">{e(c.phone)}</span></td>
 <td>{e(fmt_rating(c.rating))} / {c.reviews_count if c.reviews_count is not None else e(NA)} отз.<br>
     без ответа: {c.unanswered_count if c.unanswered_count is not None else e(NA)}</td>
 <td class="links">{' '.join(links)}</td>
 <td><details><summary>Сообщение <button onclick="event.preventDefault();copyMsg('m{i}',this)">Копировать</button></summary>
     <pre id="m{i}">{e(row["message"])}</pre></details></td>
</tr>""")

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Рассылка: {e(cfg.category)} — {e(cfg.city)}</title>
<style>{_DASH_CSS}</style></head><body>
<h1>Пакет для рассылки — {e(cfg.category)}, {e(cfg.city)}</h1>
<div class="sub">{len(rows)} лидов · собрано {today} · кнопка WhatsApp открывает чат
с уже подставленным сообщением; «аудит» — страница для отправки после «да, скиньте»
(Ctrl+P → сохранить как PDF)</div>
{warn}
<table>
<tr><th>Приоритет</th><th>Компания</th><th>Рейтинг</th><th>Ссылки</th><th>Открывашка</th></tr>
{''.join(trs)}
</table>
<script>{_DASH_JS}</script>
</body></html>"""


# ---------- строки рассылки ----------

def audit_filename(c: Company, i: int = 0) -> str:
    """Имя файла аудита лида. Общее для пакета на диске и ссылок в веб-панели."""
    return f"audit_{c.firm_id if c.firm_id != NA else i}.html"


def build_rows(companies: list, cfg) -> list:
    """Лиды -> строки рассылки: сообщение, ссылка WhatsApp, файл аудита.

    Чистая функция (без диска и сети): по ней строится и пакет в output/,
    и таблица результатов на странице «Поиск лидов» в панели — так обе
    поверхности показывают ровно один и тот же текст.
    """
    companies = sorted(companies, key=sort_key)
    rows = []
    for i, c in enumerate(companies):
        competitor = pick_benchmark(companies, exclude=c)
        message = build_message(c, competitor, cfg)
        rows.append({"c": c, "competitor": competitor, "message": message,
                     "wa": wa_link(c.phone, message),
                     "audit_file": audit_filename(c, i)})
    return rows


# ---------- запись пакета ----------

def write_outreach_pack(companies: list, cfg) -> Path:
    """Собрать пакет рассылки. Возвращает путь к папке пакета."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pack = Path(cfg.output_dir) / f"outreach_{cfg.city_slug}_{cfg.category}_{ts}"
    pack.mkdir(parents=True, exist_ok=True)

    rows = build_rows(companies, cfg)
    for row in rows:
        (pack / row["audit_file"]).write_text(
            render_audit_html(row["c"], row["competitor"], cfg), encoding="utf-8")

    (pack / "index.html").write_text(render_dashboard_html(rows, cfg), encoding="utf-8")

    with open(pack / "outreach.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Название", "Приоритет", "Телефон", "WhatsApp", "Сообщение"])
        for row in rows:
            w.writerow([row["c"].name, row["c"].priority, row["c"].phone,
                        row["wa"] or NA, row["message"]])
    return pack


if __name__ == "__main__":
    # консоль Windows -> UTF-8 (иначе кириллица в выводе падает на cp1251)
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    # самотесты без сети/браузера
    from dataclasses import dataclass

    @dataclass
    class _Cfg:
        city = "Алматы"
        city_slug = "almaty"
        category = "стоматология"
        output_dir = "output"
        sender_name = "Эмиль"
        sender_contact = "+7 700 000 00 00"

    cfg = _Cfg()
    lead = Company(firm_id="1", name="Стоматология Аруна", rating=3.9,
                   reviews_count=120, has_fresh_reviews=False, owner_responds=False,
                   negative_without_response=True, phone="+7 (727) 123-45-67",
                   website="https://instagram.com/aruna_dent", url="https://2gis.kz/x",
                   loaded_reviews=50, unanswered_count=14, negative_unanswered=3,
                   fresh_count=0, last_review_date="2026-05-01",
                   worst_reviews=[{"rating": 1, "date": "2026-05-01",
                                   "text": "Ужасно <б>", "answered": False}])
    lead.compute_priority()
    top = Company(firm_id="2", name="Dent Lux", rating=4.8, reviews_count=300)

    checks = []

    msg = build_message(lead, top, cfg)
    checks.append(("конкурент в сообщении", "Dent Lux" in msg and "4,8" in msg))
    checks.append(("негатив без ответа в сообщении", "3 негативных отзыва висят" in msg))
    checks.append(("имя отправителя", "Эмиль" in msg))
    checks.append(("не больше 3 пунктов", msg.count("• ") <= MAX_BULLETS))
    checks.append(("склонение", plural(1, "а", "б", "в") == "а"
                   and plural(3, "а", "б", "в") == "б"
                   and plural(11, "а", "б", "в") == "в"
                   and plural(21, "а", "б", "в") == "а"))
    lead1 = Company(negative_unanswered=1, loaded_reviews=50)
    checks.append(("склонение n=1 в сообщении",
                   "1 негативный отзыв висит" in build_message(lead1, None, cfg)))

    cfg_noname = _Cfg(); cfg_noname.sender_name = ""
    checks.append(("заглушка имени", "[Имя]" in build_message(lead, top, cfg_noname)))

    wa = wa_link(lead.phone, msg)
    checks.append(("wa.me из +7(727)…", wa is not None and "/77271234567?" in wa))
    checks.append(("wa.me из 8-кода", phone_digits("8 701 555-44-33") == "77015554433"))
    checks.append(("wa.me нет телефона", wa_link(NA) is None))

    audit = render_audit_html(lead, top, cfg)
    checks.append(("аудит: имя и конкурент", "Аруна" in audit and "Dent Lux" in audit))
    checks.append(("аудит: HTML экранирован", "<б>" not in audit and "&lt;б&gt;" in audit))

    dash = render_dashboard_html(
        [{"c": lead, "message": msg, "wa": wa, "audit_file": "audit_1.html"}], cfg)
    checks.append(("дашборд: ссылки", "audit_1.html" in dash and "wa.me" in dash))
    checks.append(("дашборд: instagram", "instagram.com/aruna_dent" in dash))

    hot = Company(firm_id="3", name="Смайл", rating=4.0, reviews_count=60,
                  has_fresh_reviews=True, owner_responds=False,
                  phone="+7 (727) 000-11-22")
    hot.compute_priority()  # 🔥 Высокий — должен встать первым
    rows = build_rows([lead, hot, top], cfg)
    checks.append(("строки: отсортированы по приоритету",
                   [r["c"].name for r in rows][0] == "Смайл"))
    checks.append(("строки: файл аудита по firm_id",
                   rows[0]["audit_file"] == "audit_3.html"))
    checks.append(("строки: сообщение и WhatsApp на месте",
                   "«Смайл»" in rows[0]["message"]
                   and "wa.me/77270001122" in (rows[0]["wa"] or "")))
    checks.append(("строки: без телефона WhatsApp пустой",
                   [r for r in rows if r["c"].name == "Dent Lux"][0]["wa"] is None))

    checks.append(("benchmark исключает себя",
                   pick_benchmark([lead, top], exclude=top) is lead))
    checks.append(("benchmark пустой пул", pick_benchmark([lead], exclude=lead) is None))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
