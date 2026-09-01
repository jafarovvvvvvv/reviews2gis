"""
Приёмочная проверка на ЖИВЫХ данных: таблица лидов и разбор (аудит) обязаны
показывать по одной компании одни и те же числа.

Что делает:
  1. собирает N реальных компаний (по умолчанию 3, Костанай/стоматология);
  2. для каждой берёт числа таблицы (единый слой metrics.company_numbers) и
     строит разбор (leadgen.build_audit);
  3. вытаскивает числа ИЗ ОТРЕНДЕРЕННОГО HTML разбора и сверяет с таблицей —
     любое расхождение валит проверку с понятным сообщением;
  4. проверяет согласованность ВНУТРИ самого разбора: число «без ответа» в
     шапке обязано совпасть с числом в тексте, а в блоке цитат не может быть
     отзыва с ответом владельца;
  5. повторяет разбор ещё раз и требует полного совпадения (детерминизм:
     те же числа и тот же конкурент).

Запуск:
  python check_live.py                    # 3 компании, Костанай, стоматология
  python check_live.py --count 5 --city Алматы --category автосервис
  python check_live.py --selftest         # проверка самой логики сравнения (без сети)

Код возврата 0 — всё сошлось, 1 — есть расхождения.
"""
import argparse
import asyncio
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

import leadgen
import metrics

# Числа, которые обязаны совпасть. Слева — поле слоя данных, справа — подпись
# метрики в разборе, по которой ищем значение в HTML.
CHECKED = (
    ("rating", "рейтинг 2ГИС", metrics.fmt_rating),
    ("reviews_count", "текстовых отзывов", metrics.fmt_int),
    ("unanswered_count", "без ответа", metrics.fmt_int),
    ("fresh_count", f"новых за {metrics.FRESH_DAYS} дней", metrics.fmt_int),
)

# «на основе 1174 оценок» в подписи рейтинга
RATINGS_RE = re.compile(r"на основе\s+(\d+)\s+оценок")
# блок метрики: <div class="v …">ЗНАЧЕНИЕ</div><div class="l">ПОДПИСЬ…
METRIC_RE = re.compile(
    r'<div class="v[^"]*">([^<]*)</div>\s*<div class="l">(.*?)</div>', re.S)


def audit_numbers(html: str) -> dict:
    """Числа, реально напечатанные в разборе: {подпись: значение}.

    Читаем именно HTML, а не объект: так проверка ловит случай, когда шаблон
    посчитал что-то сам вместо того, чтобы взять из слоя данных.
    """
    out = {}
    for value, label in METRIC_RE.findall(html):
        # подпись может содержать <br> и пояснение мелким шрифтом
        clean = re.sub(r"<[^>]+>", " ", label)
        clean = re.sub(r"\s+", " ", clean).strip()
        out[clean] = value.strip()
    m = RATINGS_RE.search(html)
    if m:
        out["оценок всего"] = m.group(1)
    return out


# «14 отзывов остались без ответа», «19 без ответа» — числа, поданные читателю
# как «без ответа». Все они обязаны быть одним и тем же числом.
UNANSWERED_IN_TEXT_RE = re.compile(
    r"(\d+)\s+отзыв\w*\s+остал\w*\s+без ответа")
# метка ответа владельца в блоке цитат — её там быть не должно
ANSWERED_MARK = "есть ответ владельца"
# блок цитат = от своего заголовка до следующего заголовка/блока/конца документа
QUOTES_BLOCK_RE = re.compile(
    r"<h2>Что видят клиенты.*?"
    r"(?=<h2>|<ol class=\"steps\"|<div class=\"vs\"|<div class=\"footer\"|\Z)", re.S)


def check_internal(numbers: dict, html: str) -> list:
    """Согласованность внутри одного документа -> список проблем.

    Ловит то, чего не видит сверка с таблицей: два разных числа под одной
    подписью и отвеченные отзывы в блоке цитат негатива.
    """
    problems = []
    head = audit_numbers(html)
    head_value = next((v for k, v in head.items() if k.startswith("без ответа")), None)
    in_text = UNANSWERED_IN_TEXT_RE.findall(html)

    if head_value is None:
        problems.append("в шапке разбора нет метрики «без ответа»")
    elif not in_text:
        problems.append(f"в шапке «без ответа» = {head_value}, "
                        f"а в тексте это число не названо вовсе")
    else:
        разные = {v for v in in_text if v != head_value}
        if разные:
            problems.append(
                f"«без ответа»: в шапке {head_value}, в тексте "
                f"{', '.join(sorted(разные))} — одно число, поданное как два")

    expected = numbers.get("unanswered_count")
    if expected is not None and head_value is not None and head_value != str(expected):
        problems.append(f"«без ответа»: слой данных {expected}, шапка {head_value}")

    block = QUOTES_BLOCK_RE.search(html)
    if block and ANSWERED_MARK in block.group(0):
        problems.append("в блоке цитат есть отзыв с ответом владельца "
                        "(«есть ответ владельца») — сюда попадает только негатив "
                        "без ответа")
    if block and "Ответить на весь негатив" not in html:
        problems.append("блок цитат негатива есть, а шага «ответить на весь "
                        "негатив» в документе нет")
    if not block and "Ответить на весь негатив" in html:
        problems.append("шаг «ответить на весь негатив» есть, а негатива без "
                        "ответа в документе нет — документ противоречит себе")
    return problems


def quotes_of(html: str) -> list:
    """Цитаты из блока негатива: [(дата, помечено ли «без ответа»)]."""
    block = QUOTES_BLOCK_RE.search(html)
    if not block:
        return []
    out = []
    for meta in re.findall(r'<div class="meta">(.*?)</div>', block.group(0), re.S):
        clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", meta)).strip()
        date = re.search(r"\d{4}-\d{2}-\d{2}", clean)
        out.append((date.group(0) if date else "?", "без ответа" in clean))
    return out


def compare(numbers: dict, html: str) -> list:
    """Числа таблицы против чисел разбора -> список расхождений (пустой = ок)."""
    printed = audit_numbers(html)
    problems = []
    for field, label, fmt in CHECKED:
        expected = fmt(numbers.get(field))
        got = next((v for k, v in printed.items() if k.startswith(label)), None)
        if got is None:
            problems.append(f"в разборе нет метрики «{label}» "
                            f"(таблица показывает {expected})")
        elif got != expected:
            problems.append(f"«{label}»: таблица {expected}, разбор {got}")
    if numbers.get("ratings_count") is not None:
        expected = str(numbers["ratings_count"])
        got = printed.get("оценок всего")
        if got != expected:
            problems.append(f"«оценок всего»: таблица {expected}, разбор {got}")
    return problems


async def run(city: str, category: str, count: int) -> int:
    cfg = leadgen.LeadCfg(city=city, category=category, count=count)
    print(f"=== сбор {count} компаний: {category}, {city} ===")
    companies, way = await leadgen.collect_leads(cfg)
    leadgen.save_snapshots(companies, cfg)
    print(f"собрано {len(companies)} (способ: {way})\n")

    failures = []
    for c in companies:
        numbers = metrics.company_numbers(c)
        print(f"--- {c.name} (firm {c.firm_id})")
        print(f"    таблица: рейтинг {metrics.fmt_rating(numbers['rating'])} · "
              f"{metrics.fmt_int(numbers['ratings_count'])} оценок · "
              f"{metrics.fmt_int(numbers['reviews_count'])} отзывов · "
              f"без ответа {metrics.fmt_int(numbers['unanswered_count'])} · "
              f"{c.priority} ({c.priority_reason})")

        try:
            # снимок только что записан — свежий, сети не будет
            html, audited, sid = await leadgen.build_audit(c.firm_id, cfg)
        except leadgen.StaleDataError as e:
            failures.append(f"{c.name}: разбор не построен — {e}")
            print(f"    [FAIL] {e}")
            continue

        problems = compare(numbers, html)
        if problems:
            failures += [f"{c.name}: {p}" for p in problems]
            for p in problems:
                print(f"    [FAIL] {p}")
        else:
            print("    [OK ] числа таблицы и разбора совпали")

        internal = check_internal(numbers, html)
        if internal:
            failures += [f"{c.name}: {p}" for p in internal]
            for p in internal:
                print(f"    [FAIL] {p}")
        else:
            print(f"    [OK ] внутри разбора согласовано: без ответа "
                  f"{metrics.fmt_int(numbers['unanswered_count'])} и в шапке, и в тексте")

        quotes = quotes_of(html)
        print(f"    цитаты негатива без ответа: {len(quotes)}")
        for date, unanswered in quotes:
            print(f"      · {date} — unanswered={unanswered}")
        if any(not u for _, u in quotes):
            failures.append(f"{c.name}: в цитатах есть отвеченный отзыв")
            print("    [FAIL] в цитатах есть отвеченный отзыв")

        # детерминизм: второй разбор подряд обязан быть таким же
        html2, _, _ = await leadgen.build_audit(c.firm_id, cfg)
        if html2 != html:
            failures.append(f"{c.name}: два разбора подряд различаются")
            print("    [FAIL] два разбора подряд различаются")
        else:
            print("    [OK ] повторный разбор идентичен")

        comp1 = leadgen.pick_competitor(cfg, c.firm_id)
        comp2 = leadgen.pick_competitor(cfg, c.firm_id)
        id1 = comp1.firm_id if comp1 else None
        id2 = comp2.firm_id if comp2 else None
        if id1 != id2:
            failures.append(f"{c.name}: конкурент выбран нестабильно ({id1} != {id2})")
            print(f"    [FAIL] конкурент нестабилен: {id1} != {id2}")
        else:
            print(f"    [OK ] конкурент детерминирован: "
                  f"{comp1.name if comp1 else 'нет кандидатов'}")

    print("\n=== ИТОГ ===")
    if failures:
        print(f"РАСХОЖДЕНИЯ ({len(failures)}):")
        for f in failures:
            print(f"  • {f}")
        return 1
    print(f"Все числа совпали по {len(companies)} компаниям, "
          f"повторные прогоны идентичны.")
    return 0


def selftest() -> int:
    """Проверка самой логики сравнения — без сети."""
    checks = []
    html_ok = '''
<div class="metric"><div class="v ok">5,0</div>
  <div class="l">рейтинг 2ГИС<br><span class="tiny">на основе 1174 оценок</span></div></div>
<div class="metric"><div class="v">778</div><div class="l">текстовых отзывов</div></div>
<div class="metric"><div class="v bad">10</div>
  <div class="l">без ответа<br><span class="tiny">последние 50 отзывов</span></div></div>
<div class="metric"><div class="v ok">4</div><div class="l">новых за 30 дней</div></div>'''
    numbers = {"rating": 5.0, "ratings_count": 1174, "reviews_count": 778,
               "unanswered_count": 10, "fresh_count": 4}
    checks.append(("совпадение чисел -> нет расхождений", compare(numbers, html_ok) == []))

    bad_rating = html_ok.replace(">5,0<", ">4,8<")
    problems = compare(numbers, bad_rating)
    checks.append(("рейтинг разошёлся -> расхождение поймано",
                   len(problems) == 1 and "рейтинг" in problems[0]))

    swapped = {**numbers, "reviews_count": 1174, "ratings_count": 778}
    checks.append(("оценки и отзывы перепутаны -> поймано",
                   len(compare(swapped, html_ok)) == 2))

    checks.append(("нет метрики в разборе -> поймано",
                   any("нет метрики" in p
                       for p in compare(numbers, '<div class="v">5,0</div>'))))

    # --- согласованность внутри документа ---
    doc_ok = (html_ok + "<ul class=\"pains\"><li>10 отзывов остались без ответа "
              "из последних 50.</li></ul>"
              "<h2>Что видят клиенты: негатив без ответа (1 отзыв)</h2>"
              "<blockquote>плохо<div class=\"meta\">★☆☆☆☆ · 2026-08-30 · без ответа"
              "</div></blockquote>"
              "<ol class=\"steps\"><li>Ответить на весь негатив.</li></ol>")
    checks.append(("внутри документа: одно число -> ок",
                   check_internal(numbers, doc_ok) == []))

    doc_two = doc_ok.replace("10 отзывов остались без ответа",
                             "14 отзывов остались без ответа")
    problems = check_internal(numbers, doc_two)
    checks.append(("внутри документа: два числа под одной подписью -> поймано",
                   any("поданное как два" in p for p in problems)))

    doc_answered = doc_ok.replace("· без ответа", "· есть ответ владельца")
    checks.append(("внутри документа: отвеченный отзыв в цитатах -> поймано",
                   any("ответом владельца" in p
                       for p in check_internal(numbers, doc_answered))))

    doc_no_quotes = (html_ok + "<ul class=\"pains\"><li>10 отзывов остались без "
                     "ответа из последних 50.</li></ul>"
                     "<ol class=\"steps\"><li>Ответить на весь негатив.</li></ol>")
    checks.append(("внутри документа: шаг без блока цитат -> поймано",
                   any("противоречит себе" in p
                       for p in check_internal(numbers, doc_no_quotes))))

    checks.append(("цитаты: разбор блока даёт даты и флаг",
                   quotes_of(doc_ok) == [("2026-08-30", True)]))

    parsed = audit_numbers(html_ok)
    checks.append(("разбор HTML: подписи очищены от тегов",
                   parsed.get("текстовых отзывов") == "778"
                   and parsed.get("оценок всего") == "1174"))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--city", default="Костанай")
    p.add_argument("--category", default="стоматология")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--selftest", action="store_true",
                   help="проверить логику сравнения без обращения к 2ГИС")
    args = p.parse_args()
    sys.exit(selftest() if args.selftest
             else asyncio.run(run(args.city, args.category, args.count)))
