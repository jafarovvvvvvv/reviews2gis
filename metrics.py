"""
ЕДИНЫЙ СЛОЙ ЧИСЕЛ. Все счётчики по отзывам считаются здесь и только здесь.

Зачем модуль появился: таблица лидов и разбор (аудит) показывали по одной
компании разные числа. Каждый потребитель считал по-своему, определения
«негатив» и «без ответа» были продублированы в fetcher.py, tg.py, insights.py
и monitor.py, а окна выборки различались. Теперь определение одно:

    negative   = оценка <= NEGATIVE_MAX (3)
    unanswered = нет official_answer
    окно       = WINDOW (50 новейших отзывов) — ОДНО на все счётчики,
                 подписывается в тексте разбора словами WINDOW_LABEL

Рейтинг здесь НЕ вычисляется: 2ГИС считает его по подтверждённым ОЦЕНКАМ
(general_review_count_with_stars, у DoctorDos их 1174), а не по текстовым
отзывам (778) — средний балл по нашей выборке дал бы 4.34 вместо реальных 5.0.
Поэтому rating приходит только из данных 2ГИС (fetcher.parse_card_html).

Потребители (таблица панели, CSV, аудит, Telegram) берут числа функцией
`company_numbers` — одной для всех.

Самотесты (без сети): python metrics.py
"""
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# --- определения (единственное место) ---
NEGATIVE_MAX = 3       # оценка <= 3 — негатив
FRESH_DAYS = 30        # «свежий» отзыв — за последние 30 дней
TOP_N = 5              # «топ-5» новейших — окно для флага «негатив без ответа»

# Окно выборки для ВСЕХ счётчиков. В конфиге (.env REVIEW_WINDOW), потому что
# от него зависит смысл каждого числа в документе — и оно печатается рядом с
# числом, чтобы «19 без ответа» нельзя было прочитать как «19 из всех отзывов».
WINDOW = max(1, int(os.getenv("REVIEW_WINDOW") or 50))
WINDOW_LABEL = f"последние {WINDOW} отзывов"

MAX_QUOTES = 3         # сколько цитат негатива показывать в разборе

NA = "н/д"

# область данных: филиал или вся сеть
SCOPE_BRANCH = "филиал"
SCOPE_ORG = "все филиалы"


def is_negative(rating) -> bool:
    """Негатив = оценка <= NEGATIVE_MAX. None (нет оценки) негативом не считаем."""
    return rating is not None and rating <= NEGATIVE_MAX


def is_unanswered(review: dict) -> bool:
    """Без ответа = у отзыва нет official_answer."""
    return not review.get("official_answer")


def review_created(r: dict) -> datetime:
    """Дата отзыва; нечитаемая дата -> минимальная (такой отзыв не «свежий»)."""
    try:
        return datetime.fromisoformat(r["date_created"])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class ReviewStats:
    """Счётчики по окну отзывов. Все — по одному и тому же окну WINDOW."""
    loaded: int = 0                 # сколько отзывов реально в окне
    unanswered: int = 0             # из них без ответа
    negative: int = 0               # из них негативных (<=3)
    negative_unanswered: int = 0    # негативных без ответа
    unanswered_fresh: int = 0       # без ответа среди свежих (за FRESH_DAYS)
    fresh: int = 0                  # за последние FRESH_DAYS
    has_fresh: Optional[bool] = None        # есть ли отзыв за FRESH_DAYS
    owner_responds: Optional[bool] = None   # есть ли хоть один ответ владельца
    negative_in_top: Optional[bool] = None  # негатив без ответа в топ-TOP_N
    last_date: Optional[str] = None         # ISO-дата новейшего отзыва
    # Цитаты для разбора: ТОЛЬКО негатив БЕЗ ОТВЕТА, свежие сверху. Отвеченным
    # отзывам здесь не место: документ призывает «ответить на весь негатив»,
    # и показывать рядом отзыв с ответом владельца — противоречие.
    negative_quotes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def review_stats(reviews: list, now: datetime = None) -> ReviewStats:
    """Отзывы (сырые из reviews-API) -> все счётчики. Чистая функция.

    reviews обрезается до WINDOW: окно у всех потребителей одинаковое, даже
    если вызывающий принёс больше (например, deep-сбор аналитики).
    """
    now = now or datetime.now(timezone.utc)
    st = ReviewStats()
    if not reviews:
        # отзывов нет — это факт, а не «нет данных»: свежих точно нет
        st.has_fresh = False
        return st

    window = sorted(reviews, key=review_created, reverse=True)[:WINDOW]
    st.loaded = len(window)
    st.unanswered = sum(1 for r in window if is_unanswered(r))
    st.negative = sum(1 for r in window if is_negative(r.get("rating")))
    st.negative_unanswered = sum(1 for r in window
                                 if is_negative(r.get("rating")) and is_unanswered(r))
    st.fresh = sum(1 for r in window
                   if (now - review_created(r)) <= timedelta(days=FRESH_DAYS))
    st.unanswered_fresh = sum(
        1 for r in window
        if is_unanswered(r) and (now - review_created(r)) <= timedelta(days=FRESH_DAYS))
    st.has_fresh = (now - review_created(window[0])) <= timedelta(days=FRESH_DAYS)
    st.owner_responds = any(not is_unanswered(r) for r in window)
    st.negative_in_top = any(is_negative(r.get("rating")) and is_unanswered(r)
                             for r in window[:TOP_N])
    st.last_date = review_created(window[0]).date().isoformat()

    # Цитаты: негатив БЕЗ ОТВЕТА, свежие сверху, не больше MAX_QUOTES.
    # Отвеченные негативные отзывы отсеиваются здесь, а не в шаблоне.
    unanswered_negative = [r for r in window
                           if is_negative(r.get("rating")) and is_unanswered(r)]
    unanswered_negative.sort(key=review_created, reverse=True)
    for r in unanswered_negative[:MAX_QUOTES]:
        text = re.sub(r"\s+", " ", (r.get("text") or "")).strip()
        st.negative_quotes.append({"rating": r.get("rating"),
                                   "date": review_created(r).date().isoformat(),
                                   "text": (text[:220] + "…") if len(text) > 220 else text})
    return st


# --- единая точка выдачи чисел потребителям -------------------------------

# Поля, которые считаются «числами компании». Таблица, CSV, аудит и Telegram
# берут их ТОЛЬКО отсюда — чтобы одна и та же компания везде выглядела одинаково.
NUMBER_FIELDS = ("rating", "rating_confirmed", "ratings_count", "reviews_count",
                 "branch_scope", "branches_count", "org_rating", "org_reviews_count",
                 "org_ratings_count", "loaded_reviews", "unanswered_count",
                 "unanswered_fresh", "negative_count", "negative_unanswered",
                 "fresh_count", "has_fresh_reviews", "owner_responds",
                 "negative_without_response", "last_review_date", "collected_at",
                 "priority", "priority_reason")


def company_numbers(c) -> dict:
    """Company -> плоский словарь чисел для любого потребителя.

    Ничего не пересчитывает: только достаёт уже посчитанное слоем данных.
    Если потребителю нужно число, которого здесь нет, — его надо добавить
    сюда, а не считать на месте.
    """
    return {name: getattr(c, name, None) for name in NUMBER_FIELDS}


def fmt_rating(r) -> str:
    """4.7 -> «4,7»; None -> «н/д». Форматирование, не вычисление."""
    return NA if r is None else f"{r:.1f}".replace(".", ",")


def fmt_int(v) -> str:
    return NA if v is None else str(v)


def unanswered_note(c) -> str:
    """Подпись к числу «без ответа»: из какого окна и что внутри него.

    Одно число (unanswered_count) — одна подпись. Второе число (негатив) и
    третье (свежие) называются своими словами, а не прячутся под той же
    подписью: «19 без ответа из последних 50 отзывов (всего 204),
    из них 14 негативных и 5 за последние 30 дней».
    """
    n = company_numbers(c)
    if n["unanswered_count"] is None:
        return ""
    parts = [f"из {WINDOW_LABEL.replace('последние', 'последних')}"]
    if n["reviews_count"] is not None:
        parts.append(f"всего у компании {n['reviews_count']}")
    tail = []
    if n["negative_unanswered"]:
        tail.append(f"{n['negative_unanswered']} негативных")
    if n["unanswered_fresh"]:
        tail.append(f"{n['unanswered_fresh']} за последние {FRESH_DAYS} дней")
    note = " · ".join(parts)
    return note + (f" · из них {' и '.join(tail)}" if tail else "")


def scope_note(c) -> str:
    """Подпись об области данных для разбора: филиал или вся сеть."""
    scope = getattr(c, "branch_scope", None) or SCOPE_BRANCH
    n = getattr(c, "branches_count", None)
    if scope == SCOPE_BRANCH and n and n > 1:
        return (f"данные по одному филиалу из {n} — 2ГИС считает рейтинг "
                f"отдельно по каждому")
    if scope == SCOPE_ORG:
        return "данные по всем филиалам компании"
    return "данные по этому филиалу"


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []
    NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def rv(days_ago, rating, answer=None, text="текст"):
        return {"id": f"r{days_ago}", "rating": rating,
                "date_created": (NOW - timedelta(days=days_ago)).isoformat(),
                "text": text, "official_answer": answer}

    checks.append(("негатив: порог 3", is_negative(3) and not is_negative(4)))
    checks.append(("негатив: нет оценки — не негатив", not is_negative(None)))
    checks.append(("без ответа: пусто", is_unanswered({}) is True))
    checks.append(("без ответа: есть ответ",
                   is_unanswered({"official_answer": {"text": "ok"}}) is False))

    st = review_stats([rv(1, 1), rv(2, 5, {"text": "спасибо"}), rv(3, 2),
                       rv(400, 1), rv(5, 4)], now=NOW)
    checks.append(("счётчики: загружено", st.loaded == 5))
    checks.append(("счётчики: без ответа", st.unanswered == 4))
    checks.append(("счётчики: негатив", st.negative == 3))
    checks.append(("счётчики: негатив без ответа", st.negative_unanswered == 3))
    checks.append(("счётчики: свежих за 30 дней", st.fresh == 4))
    checks.append(("счётчики: есть свежие", st.has_fresh is True))
    checks.append(("счётчики: владелец отвечает", st.owner_responds is True))
    checks.append(("счётчики: негатив в топ-5", st.negative_in_top is True))
    checks.append(("счётчики: дата последнего", st.last_date == "2026-08-31"))
    checks.append(("счётчики: без ответа среди свежих", st.unanswered_fresh == 3))

    # --- цитаты: только негатив БЕЗ ОТВЕТА, свежие сверху ---
    quoted = review_stats([rv(1, 2, {"text": "ответили"}), rv(3, 1), rv(10, 2),
                           rv(20, 5), rv(30, 1, {"text": "ответили"})], now=NOW)
    checks.append(("цитаты: отвеченный негатив не попадает",
                   len(quoted.negative_quotes) == 2))
    checks.append(("цитаты: свежие сверху",
                   [q["date"] for q in quoted.negative_quotes]
                   == ["2026-08-29", "2026-08-22"]))
    checks.append(("цитаты: нет флага answered — сюда попадают только без ответа",
                   all("answered" not in q for q in quoted.negative_quotes)))
    checks.append(("цитаты: не больше MAX_QUOTES",
                   len(review_stats([rv(i, 1) for i in range(1, 9)],
                                    now=NOW).negative_quotes) == MAX_QUOTES))
    checks.append(("цитаты: весь негатив отвечен -> цитат нет",
                   review_stats([rv(1, 1, {"text": "ответ"}),
                                 rv(2, 2, {"text": "ответ"})],
                                now=NOW).negative_quotes == []))
    checks.append(("цитаты: негатива нет вовсе -> цитат нет",
                   review_stats([rv(1, 5), rv(2, 4)], now=NOW).negative_quotes == []))
    checks.append(("цитаты: меньше трёх — показываем сколько есть",
                   len(review_stats([rv(1, 1)], now=NOW).negative_quotes) == 1))

    old = review_stats([rv(100, 5, {"text": "ok"})], now=NOW)
    checks.append(("счётчики: старый отзыв — не свежий",
                   old.has_fresh is False and old.fresh == 0))
    checks.append(("счётчики: ответ есть — негатива в топе нет",
                   old.owner_responds is True and old.negative_in_top is False))

    empty = review_stats([], now=NOW)
    checks.append(("счётчики: пусто — свежих нет, счётчики нули",
                   empty.has_fresh is False and empty.loaded == 0
                   and empty.unanswered == 0))

    big = review_stats([rv(i % 30, 5) for i in range(120)], now=NOW)
    checks.append((f"окно: не больше {WINDOW} отзывов", big.loaded == WINDOW))

    mixed = review_stats([rv(1, 1) for _ in range(60)], now=NOW)
    checks.append(("окно: счётчики тоже обрезаны окном",
                   mixed.unanswered == WINDOW and mixed.negative == WINDOW))

    checks.append(("формат: рейтинг по-русски", fmt_rating(4.9) == "4,9"
                   and fmt_rating(5.0) == "5,0" and fmt_rating(None) == NA))
    checks.append(("формат: число", fmt_int(0) == "0" and fmt_int(None) == NA))

    # --- подпись к «без ответа»: числа названы своими именами ---
    class _C:
        unanswered_count = 19
        unanswered_fresh = 5
        negative_unanswered = 14
        reviews_count = 204

        def __getattr__(self, name):
            return None

    note = unanswered_note(_C())
    checks.append(("подпись: указано окно", f"последних {WINDOW}" in note))
    checks.append(("подпись: всего отзывов у компании", "всего у компании 204" in note))
    checks.append(("подпись: негатив назван отдельно", "14 негативных" in note))
    checks.append(("подпись: свежие названы отдельно",
                   f"5 за последние {FRESH_DAYS} дней" in note))
    checks.append(("подпись: само число 19 в подпись не подмешано",
                   "19" not in note))

    checks.append(("окно: берётся из конфига (REVIEW_WINDOW)",
                   WINDOW == max(1, int(os.getenv("REVIEW_WINDOW") or 50))))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
