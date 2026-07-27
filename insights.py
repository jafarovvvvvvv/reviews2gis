"""
Аналитический отчёт болей по отзывам (этап 2 ROADMAP).

Пайплайн build_report():
  1. глубокий сбор отзывов той же фирмы (Scraper.fetch_reviews_deep, до 300);
  2. классификация батчами через OpenAI (модель из настроек, см. ai_reply):
     каждому отзыву — темы из отраслевой таксономии, тяжесть 1–3, цитата-улика,
     плюс «что хвалят»;
  3. агрегация — чистый Python (частота × тяжесть × свежесть, тренды 90/90 дней,
     метрики реакции владельца) — тестируется без сети;
  4. резюме и рекомендации — второй вызов LLM («пиши как аудитор, без воды»);
  5. динамика к прошлому отчёту (если был) — чистый Python;
  6. рендер HTML (печать в PDF — Ctrl+P), сохранение в insights_reports.

Самотесты без сети: python insights.py
"""
import asyncio
import html
import json
import re
from datetime import datetime, timedelta, timezone

import httpx

import ai_reply
import db
import fetcher

BATCH_SIZE = 25          # отзывов на один запрос классификации
TEXT_LIMIT = 350         # обрезка текста отзыва в промпте
FRESH_WINDOW_DAYS = 90   # окно «свежего» периода для трендов
MAX_QUOTES = 3           # цитат-улик на тему в отчёте
TOP_THEMES = 6           # тем в карте болей

# Отраслевые таксономии болей (ключевые слова категории -> набор тем)
TAXONOMIES = {
    "медицина": ["качество лечения", "боль и осложнения", "цены и навязывание услуг",
                 "очереди и ожидание", "запись и дозвон", "врачи и общение",
                 "ресепшн и администраторы", "чистота и комфорт",
                 "возврат денег и документы"],
    "красота": ["качество работы", "мастера и общение", "цены",
                "запись и ожидание", "чистота и стерильность",
                "администраторы", "возврат и претензии"],
    "общепит": ["вкус и качество еды", "скорость обслуживания", "персонал",
                "цены и порции", "чистота", "доставка", "атмосфера и шум"],
    "авто": ["качество ремонта", "сроки", "цены и допработы",
             "честность и общение", "запись и очередь", "гарантия и возврат"],
    "универсальная": ["качество услуги", "цены", "скорость и сроки",
                      "персонал и общение", "запись и дозвон",
                      "чистота и комфорт", "возврат и претензии"],
}
_INDUSTRY_KEYWORDS = [
    ("медицина", ["стоматолог", "клиник", "медицин", "врач", "диагност", "лаборатор"]),
    ("красота", ["салон", "барбер", "маникюр", "косметол", "парикмахер", "брови",
                 "ресниц", "спа", "массаж"]),
    ("общепит", ["кафе", "ресторан", "кофе", "бар", "пицц", "суши", "столов",
                 "доставка еды", "фастфуд"]),
    ("авто", ["автосервис", "сто", "шиномонтаж", "автомойк", "детейлинг",
              "автосалон", "запчаст"]),
]


class InsightsError(Exception):
    """Ошибка построения отчёта (текст — для показа админу)."""


def detect_industry(category: str) -> str:
    cat = (category or "").lower()
    for industry, keywords in _INDUSTRY_KEYWORDS:
        # \b — иначе «ломбард» ловится на «бар», «стоматолог» на «сто» и т.п.
        if any(re.search(r"\b" + re.escape(k), cat) for k in keywords):
            return industry
    return "универсальная"


# ---------- классификация (LLM) ----------

CLASSIFY_SYSTEM = """Ты — аналитик клиентского опыта. Размечаешь отзывы о компании
(сфера: {industry}). Для КАЖДОГО отзыва из списка определи:
- pains: 0–3 темы-боли строго из списка: {themes}. Тема указывается только если
  клиент реально жалуется на это. severity: 1 — упомянул мимоходом, 2 — явная
  жалоба, 3 — серьёзный ущерб (деньги/здоровье/обман). quote — дословная цитата
  из отзыва до 140 символов, доказывающая боль.
- praise: 0–2 темы из того же списка, которые клиент ХВАЛИТ.
Если боль не подходит ни под одну тему — используй тему "другое".
Верни строго JSON: {{"items":[{{"id":"<id>","pains":[{{"theme":"...","severity":2,
"quote":"..."}}],"praise":["..."]}}]}} — по одному элементу на каждый отзыв."""


def build_classify_messages(reviews: list, industry: str) -> list:
    themes = ", ".join(f"«{t}»" for t in TAXONOMIES[industry])
    lines = []
    for r in reviews:
        text = re.sub(r"\s+", " ", (r.get("text") or "")).strip()[:TEXT_LIMIT]
        lines.append(f"[id={r['review_id']}] оценка {r.get('rating', '?')}/5: {text}")
    return [
        {"role": "system",
         "content": CLASSIFY_SYSTEM.format(industry=industry, themes=themes)},
        {"role": "user", "content": "\n".join(lines)},
    ]


def parse_classify_response(content: str, valid_ids: set) -> list:
    """JSON модели -> [{review_id, pains:[{theme,severity,quote}], praise:[...]}]."""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in data.get("items", []):
        rid = str(item.get("id", ""))
        if rid not in valid_ids:
            continue
        pains = []
        for p in (item.get("pains") or [])[:3]:
            theme = str(p.get("theme", "")).strip().lower()
            if not theme:
                continue
            try:
                severity = max(1, min(3, int(p.get("severity", 1))))
            except (TypeError, ValueError):
                severity = 1
            pains.append({"theme": theme, "severity": severity,
                          "quote": str(p.get("quote", ""))[:160]})
        praise = [str(t).strip().lower() for t in (item.get("praise") or [])[:2]
                  if str(t).strip()]
        out.append({"review_id": rid, "pains": pains, "praise": praise})
    return out


async def classify_reviews(reviews: list, industry: str, api_key: str,
                           model: str, progress=None) -> list:
    """Классифицировать все отзывы с текстом, батчами. Возвращает разметку."""
    todo = [r for r in reviews if (r.get("text") or "").strip()]
    marked = []
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        messages = build_classify_messages(batch, industry)
        payload = {"model": model, "messages": messages,
                   "response_format": {"type": "json_object"},
                   "max_completion_tokens": 6000}
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    ai_reply.API_URL, json=payload,
                    headers={"Authorization": f"Bearer {api_key}"})
                data = resp.json()
        except Exception as e:
            raise InsightsError(f"OpenAI недоступен: {e}") from e
        if resp.status_code != 200:
            err = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            raise InsightsError(f"OpenAI: {err[:200]}")
        content = data["choices"][0]["message"]["content"]
        marked += parse_classify_response(content, {r["review_id"] for r in batch})
        if progress:
            progress(f"классифицировано {min(i + BATCH_SIZE, len(todo))}/{len(todo)}")
    return marked


# ---------- агрегация (чистый Python) ----------

def _parse_dt(iso: str):
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def aggregate(reviews: list, marked: list, now=None) -> dict:
    """Свести разметку в данные отчёта. reviews: [{review_id,rating,text,author,
    date_created,answered}], marked: результат classify_reviews."""
    now = now or datetime.now(timezone.utc)
    fresh_after = now - timedelta(days=FRESH_WINDOW_DAYS)
    prior_after = now - timedelta(days=FRESH_WINDOW_DAYS * 2)
    by_id = {r["review_id"]: r for r in reviews}

    themes: dict = {}
    praises: dict = {}
    for item in marked:
        rv = by_id.get(item["review_id"])
        if rv is None:
            continue
        dt = _parse_dt(rv.get("date_created") or "")
        is_fresh = dt is not None and dt >= fresh_after
        is_prior = dt is not None and prior_after <= dt < fresh_after
        for p in item["pains"]:
            t = themes.setdefault(p["theme"], {
                "count": 0, "fresh": 0, "prior": 0, "score": 0.0, "quotes": []})
            t["count"] += 1
            t["fresh"] += int(is_fresh)
            t["prior"] += int(is_prior)
            t["score"] += p["severity"] * (1.0 if is_fresh else 0.5)
            quote = p["quote"] or (rv.get("text") or "")[:140]
            t["quotes"].append({"q": quote, "rating": rv.get("rating"),
                                "date": (rv.get("date_created") or "")[:10],
                                "sev": p["severity"]})
        for theme in item["praise"]:
            praises[theme] = praises.get(theme, 0) + 1

    for t in themes.values():
        # цитаты-улики: сначала тяжёлые, при равенстве — свежие
        t["quotes"] = sorted(t["quotes"],
                             key=lambda q: (-q["sev"], q["date"] or ""),
                             )[:MAX_QUOTES]
        t["score"] = round(t["score"], 1)
        t["trend"] = ("растёт" if t["fresh"] > t["prior"] else
                      "затухает" if t["fresh"] < t["prior"] else "стабильно")

    # реакция владельца
    total = len(reviews)
    answered = sum(1 for r in reviews if r.get("answered"))
    negative = [r for r in reviews if (r.get("rating") or 5) <= 3]
    neg_answered = sum(1 for r in negative if r.get("answered"))
    dates = sorted(d for d in ((r.get("date_created") or "")[:10] for r in reviews) if d)

    return {
        "themes": dict(sorted(themes.items(), key=lambda kv: -kv[1]["score"])),
        "praises": dict(sorted(praises.items(), key=lambda kv: -kv[1])),
        "reaction": {
            "total": total, "answered": answered,
            "answered_pct": round(100 * answered / total) if total else 0,
            "negative": len(negative), "neg_answered": neg_answered,
            "neg_answered_pct": (round(100 * neg_answered / len(negative))
                                 if negative else 100),
        },
        "period": (f"{dates[0]} — {dates[-1]}" if dates else ""),
    }


def diff_with_previous(current: dict, prev_json: str) -> list:
    """Динамика тем к прошлому отчёту: [{theme, before, now}]. Пусто, если нет."""
    if not prev_json:
        return []
    try:
        prev = json.loads(prev_json)
    except json.JSONDecodeError:
        return []
    prev_themes = prev.get("themes", {})
    out = []
    for theme in set(list(current.get("themes", {})) + list(prev_themes)):
        before = prev_themes.get(theme, {}).get("count", 0)
        now_n = current.get("themes", {}).get(theme, {}).get("count", 0)
        if before != now_n:
            out.append({"theme": theme, "before": before, "now": now_n})
    out.sort(key=lambda d: -(abs(d["now"] - d["before"])))
    return out[:8]


# ---------- резюме и рекомендации (LLM) ----------

SUMMARY_SYSTEM = """Ты — жёсткий аудитор клиентского опыта. По агрегированным данным
об отзывах компании напиши БЕЗ воды, маркетинговых оборотов и утешений:
1) summary: 2–4 предложения — главные боли с числами (сколько жалоб, тренд),
   что это значит для выручки/рейтинга;
2) actions: ровно 3 конкретных действия на ближайший месяц (каждое: что сделать,
   кто делает, как через месяц проверить эффект числом);
3) recommendations: по каждой из переданных тем-болей — 1–2 предложения:
   конкретное действие и ожидаемый эффект. Не выдумывай факты сверх данных.
Пиши на русском. Верни строго JSON:
{"summary":"...","actions":["...","...","..."],
 "recommendations":{"<тема>":"...", ...}}"""


async def generate_summary(agg: dict, company_name: str, industry: str,
                           api_key: str, model: str) -> dict:
    top = {t: {"жалоб": v["count"], "тренд": v["trend"],
               "вес": v["score"], "цитаты": [q["q"] for q in v["quotes"]]}
           for t, v in list(agg["themes"].items())[:TOP_THEMES]}
    user = (f"Компания: {company_name} (сфера: {industry}).\n"
            f"Период данных: {agg['period']}.\n"
            f"Боли: {json.dumps(top, ensure_ascii=False)}\n"
            f"Хвалят: {json.dumps(agg['praises'], ensure_ascii=False)}\n"
            f"Реакция владельца: {json.dumps(agg['reaction'], ensure_ascii=False)}")
    payload = {"model": model,
               "messages": [{"role": "system", "content": SUMMARY_SYSTEM},
                            {"role": "user", "content": user}],
               "response_format": {"type": "json_object"},
               "max_completion_tokens": 4000}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(ai_reply.API_URL, json=payload,
                                     headers={"Authorization": f"Bearer {api_key}"})
            data = resp.json()
    except Exception as e:
        raise InsightsError(f"OpenAI недоступен: {e}") from e
    if resp.status_code != 200:
        err = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise InsightsError(f"OpenAI: {err[:200]}")
    m = re.search(r"\{.*\}", data["choices"][0]["message"]["content"] or "", re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        out = {}
    return {"summary": str(out.get("summary", "")),
            "actions": [str(a) for a in (out.get("actions") or [])[:3]],
            "recommendations": {str(k).lower(): str(v) for k, v in
                                (out.get("recommendations") or {}).items()}}


# ---------- рендер ----------

_REPORT_CSS = """
body{font-family:Manrope,'Segoe UI',Arial,sans-serif;max-width:800px;margin:26px auto;
     padding:0 18px;color:#122620;line-height:1.55;background:#fff}
h1{font-size:1.5em;margin-bottom:2px}
h2{font-family:'JetBrains Mono',Consolas,monospace;font-size:.78em;font-weight:600;
   letter-spacing:.14em;text-transform:uppercase;color:#5C7064;margin:26px 0 10px}
.sub{color:#5C7064;margin:0 0 18px}
.lead{background:#F1F4F0;border-radius:14px;padding:16px 18px;font-size:1.04em}
table{border-collapse:collapse;width:100%;margin:10px 0}
th,td{border-bottom:1px solid #DFE7DF;padding:8px 10px;text-align:left;vertical-align:top}
th{font-family:'JetBrains Mono',monospace;font-size:.7em;letter-spacing:.12em;
   text-transform:uppercase;color:#5C7064}
.up{color:#D8472B;font-weight:700}.down{color:#1E7A46;font-weight:700}
blockquote{background:#FBE9E4;border-left:3px solid #D8472B;margin:8px 0;
   padding:8px 12px;border-radius:0 10px 10px 0;font-size:.95em}
blockquote .meta{color:#5C7064;font-size:.85em;margin-top:3px}
ol li{margin:8px 0}
.rec{background:#F7FAF7;border-radius:10px;padding:10px 14px;margin:8px 0}
.rec b{display:block;margin-bottom:3px}
.footer{margin-top:30px;padding-top:12px;border-top:1px solid #DFE7DF;
        color:#5C7064;font-size:.88em}
@media print{body{margin:0}}
"""


def render_report_html(company_name: str, industry: str, agg: dict, summary: dict,
                       dynamics: list, reviews_used: int, brand: str = "") -> str:
    e = html.escape
    today = datetime.now().strftime("%d.%m.%Y")

    theme_rows = []
    for theme, v in list(agg["themes"].items())[:TOP_THEMES]:
        trend_cls = "up" if v["trend"] == "растёт" else (
            "down" if v["trend"] == "затухает" else "")
        quotes = "".join(
            f'<blockquote>{e(q["q"])}<div class="meta">'
            f'{"★" * int(q["rating"] or 0)}{"☆" * (5 - int(q["rating"] or 0))} · '
            f'{e(q["date"])}</div></blockquote>' for q in v["quotes"])
        rec = summary["recommendations"].get(theme, "")
        rec_html = f'<div class="rec"><b>Что делать</b>{e(rec)}</div>' if rec else ""
        theme_rows.append(f"""<tr><td style="min-width:140px"><b>{e(theme)}</b><br>
 <span class="{trend_cls}">{e(v['trend'])}</span></td>
 <td style="width:90px">{v['count']} жалоб<br>вес {v['score']}</td>
 <td>{quotes}{rec_html}</td></tr>""")

    praises = ", ".join(f"{e(t)} ({n})" for t, n in list(agg["praises"].items())[:6]) \
        or "—"
    actions = "".join(f"<li>{e(a)}</li>" for a in summary["actions"])
    r = agg["reaction"]

    dyn_html = ""
    if dynamics:
        rows = "".join(
            f"<tr><td>{e(d['theme'])}</td><td>{d['before']}</td><td>{d['now']}</td>"
            f"<td class=\"{'down' if d['now'] < d['before'] else 'up'}\">"
            f"{'−' if d['now'] < d['before'] else '+'}{abs(d['now'] - d['before'])}</td></tr>"
            for d in dynamics)
        dyn_html = (f"<h2>Динамика к прошлому отчёту</h2><table><tr><th>Тема</th>"
                    f"<th>Было</th><th>Стало</th><th>Δ</th></tr>{rows}</table>")

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Аналитика отзывов — {e(company_name)}</title>
<style>{_REPORT_CSS}</style></head><body>
<h1>Аналитика отзывов — {e(company_name)}</h1>
<p class="sub">{e(industry)} · период {e(agg['period'])} · отзывов в анализе:
 {reviews_used} · подготовлено {today}</p>
<div class="lead"><b>Резюме.</b> {e(summary['summary'])}</div>
<h2>3 действия на ближайший месяц</h2>
<ol>{actions}</ol>
<h2>Карта болей (по весу: частота × тяжесть × свежесть)</h2>
<table><tr><th>Боль</th><th>Объём</th><th>Доказательства и что делать</th></tr>
{''.join(theme_rows)}</table>
<h2>Что хвалят</h2>
<p>{praises}</p>
<h2>Реакция на отзывы</h2>
<p>Отвечено на {r['answered']} из {r['total']} отзывов ({r['answered_pct']}%).
Негативных: {r['negative']}, из них с ответом — {r['neg_answered']}
({r['neg_answered_pct']}%). Каждый негатив без ответа продолжает работать
против компании у всех, кто открывает карточку.</p>
{dyn_html}
<div class="footer">{e(brand or 'Отчёт по отзывам')} · данные — открытая карточка
2ГИС · {today}. Цитаты приведены дословно из публичных отзывов.</div>
</body></html>"""


# ---------- оркестрация ----------

def _note(progress, msg):
    if progress:
        progress(msg)
    print(f"[insights] {msg}", flush=True)


async def _deep_fetch(domain: str, city_slug: str, firm_id: str, deep_limit: int):
    """Карточка + до deep_limit отзывов. Возвращает (Company, list[dict]).

    Идёт по тому же каскаду способов, что и мониторинг (HTTP -> браузер):
    для 300 отзывов это 6 запросов к API — браузер тут особенно избыточен.
    """
    import monitor  # импорт здесь — против циклов на уровне модулей
    conn = db.connect()
    try:
        mode = db.get_setting(conn, "fetch_mode", "auto")
    finally:
        conn.close()
    ways = fetcher.fetch_ways(mode)
    cfg = monitor.MonitorCfg(city_slug=city_slug, domain=domain)
    errors = []
    async with monitor.SCRAPE_LOCK:
        for way in ways:
            try:
                async with fetcher.fetcher_class(way)(cfg) as s:
                    c = await s.parse_firm(firm_id)  # обновляет ключ reviews-API
                    raw = await s.fetch_reviews_deep(firm_id, deep_limit)
                if raw:
                    return c, [monitor.extract_review(r) for r in raw]
                errors.append(f"{way}: отзывов не собрано")
            except Exception as e:
                errors.append(f"{way}: {e}")
    raise InsightsError("не удалось собрать отзывы (" + "; ".join(errors) + ")")


async def _analyze(reviews: list, name: str, category: str, api_key: str,
                   model: str, progress=None):
    """Классификация + агрегация + резюме. Возвращает (industry, marked, agg, summary)."""
    industry = detect_industry(category)
    _note(progress, f"классификация {len(reviews)} отзывов (сфера: {industry})…")
    marked = await classify_reviews(reviews, industry, api_key, model,
                                    progress=lambda m: _note(progress, m))
    agg = aggregate(reviews, marked)
    _note(progress, "формулирую резюме и рекомендации…")
    summary = await generate_summary(agg, name, industry, api_key, model)
    return industry, marked, agg, summary


async def build_report(company_id: int, deep_limit: int = 300,
                       progress=None, brand: str = "") -> int:
    """Полный цикл отчёта для компании из БД. Возвращает id отчёта."""
    conn = db.connect()
    try:
        row = db.get_company(conn, company_id)
        if row is None:
            raise InsightsError("компания не найдена")
        api_key, model = ai_reply.get_ai_settings(conn)
        if not api_key:
            raise InsightsError("не задан ключ OpenAI (Настройки)")

        _note(progress, f"глубокий сбор отзывов «{row['name']}» (до {deep_limit})…")
        c, reviews = await _deep_fetch(row["domain"], row["city_slug"],
                                       row["firm_id"], deep_limit)
        # запоминаем тексты в БД молча — без алертов
        db.save_reviews(conn, company_id, reviews, mark_new=False)

        industry, marked, agg, summary = await _analyze(
            reviews, row["name"], row["category"], api_key, model, progress)

        by_id = {r["review_id"]: r for r in reviews}
        topics = [{"review_id": item["review_id"], "theme": p["theme"],
                   "severity": p["severity"], "quote": p["quote"],
                   "rating": by_id.get(item["review_id"], {}).get("rating"),
                   "date_created": by_id.get(item["review_id"], {}).get("date_created", "")}
                  for item in marked for p in item["pains"]]
        db.replace_topics(conn, company_id, topics)

        dynamics = diff_with_previous(agg, db.prev_report_data(conn, company_id,
                                                               before_id=10 ** 9))
        html_out = render_report_html(row["name"], industry, agg, summary, dynamics,
                                      reviews_used=len(reviews), brand=brand)
        data_json = json.dumps({"themes": {t: {"count": v["count"]}
                                           for t, v in agg["themes"].items()},
                                "reaction": agg["reaction"]}, ensure_ascii=False)
        report_id = db.save_report(conn, company_id, agg["period"], len(reviews),
                                   data_json, html_out)
        _note(progress, f"отчёт №{report_id} готов")
        return report_id
    finally:
        conn.close()


async def build_adhoc_report(domain: str, city_slug: str, firm_id: str,
                             deep_limit: int = 300, progress=None,
                             brand: str = "") -> tuple:
    """«Аудит по ссылке» для продаж: без постановки на мониторинг и записей в БД.
    Возвращает (имя компании, html отчёта)."""
    conn = db.connect()
    api_key, model = ai_reply.get_ai_settings(conn)
    conn.close()
    if not api_key:
        raise InsightsError("не задан ключ OpenAI (Настройки)")
    _note(progress, f"аудит по ссылке: сбор отзывов firm={firm_id}…")
    c, reviews = await _deep_fetch(domain, city_slug, firm_id, deep_limit)
    name = c.name if c.name != "н/д" else f"Компания {firm_id}"
    industry, _, agg, summary = await _analyze(
        reviews, name, c.category or "", api_key, model, progress)
    html_out = render_report_html(name, industry, agg, summary, [],
                                  reviews_used=len(reviews), brand=brand)
    return name, html_out


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []

    checks.append(("industry: стоматология", detect_industry("стоматологический центр")
                   == "медицина"))
    checks.append(("industry: барбершоп", detect_industry("барбершоп") == "красота"))
    checks.append(("industry: неизвестно", detect_industry("ломбард") == "универсальная"))

    msgs = build_classify_messages(
        [{"review_id": "1", "rating": 1, "text": "Хамят и дорого"}], "медицина")
    checks.append(("classify-промпт: тема и текст",
                   "ресепшн и администраторы" in msgs[0]["content"]
                   and "[id=1]" in msgs[1]["content"]))

    parsed = parse_classify_response(
        '{"items":[{"id":"1","pains":[{"theme":"Цены","severity":5,"quote":"дорого"}],'
        '"praise":["врачи и общение"]},{"id":"чужой","pains":[]}]}', {"1"})
    checks.append(("classify-парсинг: чужой id отброшен", len(parsed) == 1))
    checks.append(("classify-парсинг: severity зажат, тема в lower",
                   parsed[0]["pains"][0]["severity"] == 3
                   and parsed[0]["pains"][0]["theme"] == "цены"))

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    reviews = [
        {"review_id": "a", "rating": 1, "text": "т", "answered": False,
         "date_created": "2026-05-20T10:00:00+00:00"},   # свежий
        {"review_id": "b", "rating": 2, "text": "т", "answered": True,
         "date_created": "2026-01-15T10:00:00+00:00"},   # прошлое окно
        {"review_id": "c", "rating": 5, "text": "т", "answered": False,
         "date_created": "2026-05-25T10:00:00+00:00"},
    ]
    marked = [
        {"review_id": "a", "pains": [{"theme": "цены", "severity": 3, "quote": "д"}],
         "praise": []},
        {"review_id": "b", "pains": [{"theme": "цены", "severity": 2, "quote": "д"}],
         "praise": []},
        {"review_id": "c", "pains": [], "praise": ["персонал и общение"]},
    ]
    agg = aggregate(reviews, marked, now=now)
    t = agg["themes"]["цены"]
    checks.append(("agg: счёт и окна", t["count"] == 2 and t["fresh"] == 1
                   and t["prior"] == 1))
    checks.append(("agg: вес (3*1.0 + 2*0.5)", t["score"] == 4.0))
    checks.append(("agg: тренд стабильно (1 свежая = 1 прошлая)",
                   t["trend"] == "стабильно"))
    only_fresh = aggregate(reviews[:1], marked[:1], now=now)
    checks.append(("agg: тренд растёт без прошлых",
                   only_fresh["themes"]["цены"]["trend"] == "растёт"))
    checks.append(("agg: похвала", agg["praises"] == {"персонал и общение": 1}))
    checks.append(("agg: реакция", agg["reaction"]["answered_pct"] == 33
                   and agg["reaction"]["negative"] == 2
                   and agg["reaction"]["neg_answered_pct"] == 50))
    checks.append(("agg: период", agg["period"] == "2026-01-15 — 2026-05-25"))

    dyn = diff_with_previous(agg, '{"themes":{"цены":{"count":5},"сроки":{"count":2}}}')
    checks.append(("динамика: дельты", {(d["theme"], d["before"], d["now"])
                                        for d in dyn}
                   == {("цены", 5, 2), ("сроки", 2, 0)}))
    checks.append(("динамика: без прошлого", diff_with_previous(agg, "") == []))

    html_out = render_report_html(
        "Клиника <X>", "медицина", agg,
        {"summary": "Главная боль — цены.", "actions": ["Действие 1", "Д2", "Д3"],
         "recommendations": {"цены": "Опубликовать прайс."}},
        dyn, reviews_used=3, brand="Bedel")
    checks.append(("html: экранирование и секции",
                   "Клиника &lt;X&gt;" in html_out and "Карта болей" in html_out
                   and "Опубликовать прайс." in html_out
                   and "Динамика" in html_out))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
