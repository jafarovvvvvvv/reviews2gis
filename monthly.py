"""
Ежемесячный отчёт клиенту — то, что мы обещаем в оффере: «раз в месяц отчёт:
что изменилось и на что жалуются чаще всего».

Это НЕ «мы ответили на N отзывов» (работа исполнителя клиенту неинтересна) и
не разбор болей из insights.py (тот глубокий, до 300 отзывов, с рекомендациями
и вторым вызовом LLM). Здесь — короткая сводка за период:
  • сколько новых отзывов, сколько из них негативных;
  • ТЕМЫ жалоб с частотой (очередь/запись/цены/персонал/лечение) — разметка
    той же моделью, что пишет черновики ответов (ai_reply.get_ai_settings);
  • динамика к прошлому периоду: рейтинг из снимков (rating_history) и те же
    счётчики за предыдущие 30 дней;
  • что осталось без ответа.

Данные берутся из БД, без похода в 2ГИС: отзывы туда кладёт мониторинг
(monitor.check_company), снимки рейтинга — он же. Единственный внешний вызов —
классификация тем; без ключа OpenAI отчёт строится, но блок тем заменяется
честной строкой, а не пустотой.

Рендер — тот же генератор, что у разбора лида: outreach.AUDIT_CSS, одна
страница HTML, печать в PDF по Ctrl+P.

Агрегация — чистые функции, самотесты без сети: python monthly.py
"""
import html
import json
from datetime import datetime, timedelta, timezone

import ai_reply
import db
import insights
import metrics
import outreach

PERIOD_DAYS = 30    # окно отчёта и окно сравнения (предыдущие 30 дней)
TOP_THEMES = 6      # тем в списке жалоб
MAX_UNANSWERED = 10  # сколько неотвеченных показываем поимённо


class MonthlyError(Exception):
    """Отчёт не построен (текст — для показа админу)."""


# ---------- период и выборка (чистый Python) ----------

def _dt(iso):
    """ISO-строка -> aware datetime (UTC) или None. Мусор не роняет отчёт."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def split_periods(reviews: list, now=None, days: int = PERIOD_DAYS) -> dict:
    """Отзывы -> {'current': [...], 'previous': [...], 'since', 'until', 'prev_since'}.

    current — за последние `days` дней, previous — за `days` дней до этого.
    Сравнивать можно только одинаковые окна, поэтому режем строго по датам
    отзывов, а не по тому, когда мы их увидели.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)
    current, previous = [], []
    for r in reviews:
        d = _dt(r.get("date_created"))
        if d is None:
            continue
        if d >= since:
            current.append(r)
        elif d >= prev_since:
            previous.append(r)
    return {"current": current, "previous": previous,
            "since": since, "until": now, "prev_since": prev_since}


def fmt_period(since, until) -> str:
    """Границы окна -> «05.08.2026 — 04.09.2026» по времени проекта."""
    return (f"{metrics.to_local(since).strftime('%d.%m.%Y')} — "
            f"{metrics.to_local(until).strftime('%d.%m.%Y')}")


def theme_counts(marked: list, limit: int = TOP_THEMES) -> list:
    """Разметка insights.classify_reviews -> [{theme, count, share}] по убыванию.

    Считаем ЖАЛОБЫ (pains), а не упоминания: одна тема у одного отзыва — одна
    жалоба, даже если модель вернула её дважды. share — доля от размеченных
    отзывов, чтобы «5 жалоб» читалось в масштабе периода.
    """
    counts = {}
    for item in marked:
        for theme in {p["theme"] for p in item.get("pains") or []}:
            counts[theme] = counts.get(theme, 0) + 1
    total = len(marked) or 1
    rows = [{"theme": t, "count": n, "share": round(100 * n / total)}
            for t, n in counts.items()]
    rows.sort(key=lambda r: (-r["count"], r["theme"]))
    return rows[:limit]


def aggregate(reviews: list, marked: list, rating_now=None, rating_prev=None,
              now=None) -> dict:
    """Всё, что показывает отчёт. Чистая функция — тестируется без сети и БД."""
    periods = split_periods(reviews, now)
    cur, prev = periods["current"], periods["previous"]
    neg = [r for r in cur if metrics.is_negative(r.get("rating"))]
    unanswered = [r for r in cur if not r.get("answered")]
    return {
        "period": fmt_period(periods["since"], periods["until"]),
        "prev_period": fmt_period(periods["prev_since"], periods["since"]),
        "new": len(cur),
        "negative": len(neg),
        "prev_new": len(prev),
        "prev_negative": sum(1 for r in prev
                             if metrics.is_negative(r.get("rating"))),
        "unanswered": sorted(unanswered,
                             key=lambda r: r.get("date_created") or "",
                             reverse=True)[:MAX_UNANSWERED],
        "unanswered_count": len(unanswered),
        "unanswered_negative": sum(1 for r in unanswered
                                   if metrics.is_negative(r.get("rating"))),
        "themes": theme_counts(marked),
        "classified": len(marked),
        "rating_now": rating_now,
        "rating_prev": rating_prev,
    }


# ---------- рендер (тот же генератор, что у разбора) ----------

_MONTHLY_CSS = """
table.themes{border-collapse:collapse;width:100%;margin:12px 0}
table.themes td{padding:7px 0;border-bottom:1px solid #eee;vertical-align:middle}
table.themes td.n{text-align:right;white-space:nowrap;color:#666;width:90px}
.bar{background:#e9edf5;border-radius:6px;height:9px;margin-top:5px}
.bar span{display:block;height:9px;border-radius:6px;background:#c0392b}
"""


def _delta_span(now, was, better_when: str = "up") -> str:
    """«было -> стало» со стрелкой. Нет одного из чисел -> пустая строка."""
    if now is None or was is None or now == was:
        return ""
    up = now > was
    good = up if better_when == "up" else not up
    arrow = "&#9650;" if up else "&#9660;"
    return f' <span class="{"down" if good else "up"}">{arrow}</span>'


def render_report_html(company_name: str, agg: dict, url: str = "",
                       brand: str = "") -> str:
    """Одна страница отчёта. Все числа приходят готовыми из aggregate()."""
    e = html.escape
    fi = metrics.fmt_int

    rating_line = ""
    if agg["rating_now"] is not None:
        was = ("" if agg["rating_prev"] is None else
               f' <span class="tiny">месяц назад {e(metrics.fmt_rating(agg["rating_prev"]))}</span>')
        rating_line = (f'<div class="metric"><div class="v">'
                       f'{e(metrics.fmt_rating(agg["rating_now"]))}'
                       f'{_delta_span(agg["rating_now"], agg["rating_prev"])}</div>'
                       f'<div class="l">рейтинг 2ГИС{was}</div></div>')

    metrics_html = f"""
<div class="metrics">
 {rating_line}
 <div class="metric"><div class="v">{e(fi(agg["new"]))}
   {_delta_span(agg["new"], agg["prev_new"])}</div>
   <div class="l">новых отзывов<br><span class="tiny">за прошлый период {e(fi(agg["prev_new"]))}</span></div></div>
 <div class="metric"><div class="v {'bad' if agg["negative"] else 'ok'}">{e(fi(agg["negative"]))}
   {_delta_span(agg["negative"], agg["prev_negative"], better_when="down")}</div>
   <div class="l">из них негативных<br><span class="tiny">за прошлый период {e(fi(agg["prev_negative"]))}</span></div></div>
 <div class="metric"><div class="v {'bad' if agg["unanswered_count"] else 'ok'}">{e(fi(agg["unanswered_count"]))}</div>
   <div class="l">осталось без ответа<br><span class="tiny">негатива из них {e(fi(agg["unanswered_negative"]))}</span></div></div>
</div>"""

    if agg["themes"]:
        top = agg["themes"][0]["count"] or 1
        rows = "".join(
            f'<tr><td>{e(t["theme"][0].upper() + t["theme"][1:])}'
            f'<div class="bar"><span style="width:{round(100 * t["count"] / top)}%"></span></div></td>'
            f'<td class="n">{t["count"]} '
            f'{e(metrics.plural(t["count"], "жалоба", "жалобы", "жалоб"))}<br>'
            f'<span class="tiny">{t["share"]}% отзывов</span></td></tr>'
            for t in agg["themes"])
        themes_html = (f'<h2>На что жаловались чаще всего</h2>'
                       f'<table class="themes">{rows}</table>'
                       f'<p class="tiny">Разобрано отзывов с текстом: '
                       f'{e(fi(agg["classified"]))}. Одна тема на отзыв считается '
                       f'один раз.</p>')
    elif agg["classified"]:
        themes_html = ('<h2>На что жаловались чаще всего</h2>'
                       '<p>За период жалоб по темам не нашлось: в отзывах '
                       'претензий нет.</p>')
    else:
        themes_html = ('<h2>На что жаловались чаще всего</h2>'
                       '<p>Разметка тем за этот период не выполнялась — '
                       'отзывов с текстом не было.</p>')

    if agg["unanswered"]:
        items = "".join(
            f'<blockquote>{e((r.get("text") or "(без текста)")[:400])}'
            f'<div class="meta">{"★" * int(r.get("rating") or 0)}'
            f'{"☆" * (5 - int(r.get("rating") or 0))} · '
            f'{e((r.get("date_created") or "")[:10])} · без ответа</div></blockquote>'
            for r in agg["unanswered"])
        tail = ""
        if agg["unanswered_count"] > len(agg["unanswered"]):
            rest = agg["unanswered_count"] - len(agg["unanswered"])
            tail = (f'<p class="tiny">И ещё {rest} '
                    f'{e(metrics.plural(rest, "отзыв", "отзыва", "отзывов"))} '
                    f'без ответа за этот же период.</p>')
        unanswered_html = (f'<h2>Осталось без ответа</h2>{items}{tail}'
                           f'<p>Ответы на них подготовим и пришлём — вставить '
                           f'нужно будет только текст.</p>')
    else:
        unanswered_html = ('<h2>Осталось без ответа</h2>'
                           '<p>Ничего: на все отзывы периода ответ есть.</p>')

    head = f'<a href="{e(url)}">карточка в 2ГИС</a> · ' if url else ""
    signed = f' Подготовлено {e(brand)}.' if brand else ""

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Отчёт за месяц — {e(company_name)}</title>
<style>{outreach.AUDIT_CSS}</style><style>{_MONTHLY_CSS}</style></head><body>
<h1>Отчёт за месяц — {e(company_name)}</h1>
<div class="sub">{head}период {e(agg["period"])}</div>
{metrics_html}
{themes_html}
{unanswered_html}
<div class="footer">Данные по отзывам карточки 2ГИС за период {e(agg["period"])};
сравнение — с периодом {e(agg["prev_period"])}.{signed}</div>
</body></html>"""


# ---------- оркестрация ----------

async def classify_period(reviews: list, category: str, conn) -> list:
    """Разметить темы жалоб той же моделью, что пишет черновики ответов.

    Нет ключа или нет отзывов с текстом -> [] (отчёт строится без блока тем).
    Сбой классификации отчёт не роняет: сводка по числам всё равно ценна.
    """
    with_text = [r for r in reviews if (r.get("text") or "").strip()]
    if not with_text:
        return []
    api_key, model = ai_reply.get_ai_settings(conn)
    if not api_key:
        return []
    industry = insights.detect_industry(category or "")
    try:
        return await insights.classify_reviews(with_text, industry, api_key, model)
    except insights.InsightsError as e:
        print(f"[monthly] темы не размечены: {e}", flush=True)
        return []


async def build_report(company_id: int, brand: str = "", now=None) -> int:
    """Собрать ежемесячный отчёт по компании из БД. Возвращает id отчёта."""
    conn = db.connect()
    try:
        row = db.get_company(conn, company_id)
        if row is None:
            raise MonthlyError("компания не найдена")
        # окно выборки с запасом: нужны и текущий период, и предыдущий
        reviews = [dict(r) for r in db.recent_reviews(conn, company_id, limit=500)]
        periods = split_periods(reviews, now)
        marked = await classify_period(periods["current"], row["category"], conn)
        agg = aggregate(reviews, marked, rating_now=row["rating"],
                        rating_prev=db.rating_days_ago(conn, company_id,
                                                       PERIOD_DAYS),
                        now=now)
        html_out = render_report_html(row["name"], agg, url=row["url"], brand=brand)
        data_json = json.dumps({"new": agg["new"], "negative": agg["negative"],
                                "unanswered": agg["unanswered_count"],
                                "themes": {t["theme"]: t["count"]
                                           for t in agg["themes"]}},
                               ensure_ascii=False)
        return db.save_report(conn, company_id, agg["period"], agg["new"],
                              data_json, html_out, kind=db.KIND_MONTHLY)
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
    NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    def rv(days_ago, rating, text="норм", answered=False, rid=None):
        d = NOW - timedelta(days=days_ago)
        return {"review_id": rid or f"r{days_ago}_{rating}",
                "rating": rating, "date_created": d.isoformat(timespec="seconds"),
                "text": text, "answered": answered}

    reviews = [
        rv(1, 1, "Очередь на час и хамство"), rv(3, 2, "Дорого и навязали"),
        rv(10, 5, "Всё отлично", answered=True), rv(20, 4, "Нормально"),
        rv(35, 1, "Плохо"), rv(40, 5, "Супер"), rv(50, 3, "Так себе"),
        rv(100, 1, "Древний отзыв"),
    ]

    p = split_periods(reviews, NOW)
    checks.append(("период: текущее окно — последние 30 дней", len(p["current"]) == 4))
    checks.append(("период: предыдущее окно — 30 дней до него", len(p["previous"]) == 3))
    checks.append(("период: старее двух окон не берём",
                   all("Древний" not in r["text"]
                       for r in p["current"] + p["previous"])))
    checks.append(("период: битая дата не роняет и не попадает в выборку",
                   len(split_periods([{"date_created": "мусор", "rating": 1}],
                                     NOW)["current"]) == 0))

    marked = [
        {"review_id": "a", "pains": [{"theme": "очереди и ожидание", "severity": 2},
                                     {"theme": "ресепшн и администраторы", "severity": 2}]},
        {"review_id": "b", "pains": [{"theme": "очереди и ожидание", "severity": 3}]},
        {"review_id": "c", "pains": [{"theme": "цены и навязывание услуг", "severity": 2}]},
        {"review_id": "d", "pains": []},
    ]
    themes = theme_counts(marked)
    checks.append(("темы: частота по убыванию",
                   [t["theme"] for t in themes][0] == "очереди и ожидание"
                   and themes[0]["count"] == 2))
    checks.append(("темы: доля считается от размеченных отзывов",
                   themes[0]["share"] == 50))
    checks.append(("темы: одна тема у одного отзыва считается один раз",
                   theme_counts([{"review_id": "a",
                                  "pains": [{"theme": "цены"}, {"theme": "цены"}]}]
                                )[0]["count"] == 1))
    checks.append(("темы: пустая разметка -> пустой список", theme_counts([]) == []))

    agg = aggregate(reviews, marked, rating_now=4.3, rating_prev=4.5, now=NOW)
    checks.append(("сводка: новые и негатив за период",
                   agg["new"] == 4 and agg["negative"] == 2))
    checks.append(("сводка: те же числа за прошлый период",
                   agg["prev_new"] == 3 and agg["prev_negative"] == 2))
    checks.append(("сводка: без ответа — только текущий период",
                   agg["unanswered_count"] == 3
                   and agg["unanswered_negative"] == 2))
    checks.append(("сводка: границы периода подписаны датами",
                   "05.08.2026" in agg["period"] and "04.09.2026" in agg["period"]))

    doc = render_report_html("Клиника <X>", agg, url="https://2gis.kz/x",
                             brand="Bedel")
    checks.append(("отчёт: имя экранировано",
                   "Клиника &lt;X&gt;" in doc and "<X>" not in doc))
    checks.append(("отчёт: это не «мы ответили на N отзывов»",
                   "мы ответили" not in doc.lower()))
    checks.append(("отчёт: темы жалоб с частотой",
                   "Очереди и ожидание" in doc and "2 жалобы" in doc))
    checks.append(("отчёт: динамика к прошлому периоду",
                   "месяц назад 4,5" in doc and "за прошлый период 3" in doc))
    checks.append(("отчёт: рейтинг вниз помечен как ухудшение",
                   '<span class="up">&#9650;</span>' not in doc.split("рейтинг")[0]))
    checks.append(("отчёт: блок «осталось без ответа»",
                   "Осталось без ответа" in doc and "без ответа</div>" in doc))
    checks.append(("отчёт: тот же генератор, что у разбора (общий CSS)",
                   outreach.AUDIT_CSS in doc))
    checks.append(("отчёт: мониторинг клиенту не продаётся",
                   "мониторинг" not in doc.lower()
                   and "узнаете" not in doc.lower()))

    clean = aggregate([rv(2, 5, "Спасибо!", answered=True)], [], now=NOW)
    doc_clean = render_report_html("Y", clean)
    checks.append(("отчёт: без долгов — честная строка, а не пустой блок",
                   "Ничего: на все отзывы периода ответ есть." in doc_clean))
    checks.append(("отчёт: нет разметки -> блок тем не врёт",
                   "Разметка тем за этот период не выполнялась" in doc_clean))

    many = [rv(i % 28 + 1, 1, f"жалоба {i}", rid=f"u{i}") for i in range(15)]
    agg_many = aggregate(many, [], now=NOW)
    doc_many = render_report_html("Z", agg_many)
    checks.append(("отчёт: длинный хвост без ответа свёрнут в «и ещё N»",
                   agg_many["unanswered_count"] == 15
                   and len(agg_many["unanswered"]) == MAX_UNANSWERED
                   and "И ещё 5 отзывов" in doc_many))

    checks.append(("рейтинга нет -> плитки рейтинга нет",
                   "рейтинг 2ГИС" not in render_report_html(
                       "Y", aggregate([], [], now=NOW))))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
