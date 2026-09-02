"""
Модель данных компании + логика приоритизации лида.

Чистая логика без браузера и сети — самотесты: python lead_scoring.py

РЕЙТИНГ И СЧЁТЧИКИ РАЗДЕЛЕНЫ (иначе таблица и разбор расходятся):
  rating         — рейтинг филиала от 2ГИС. НИКОГДА не считается нами
  ratings_count  — сколько всего ОЦЕНОК (по ним 2ГИС и считает рейтинг)
  reviews_count  — сколько ТЕКСТОВЫХ отзывов (их всегда меньше)
  branch_scope   — по чему собраны данные: филиал или вся сеть
  org_*          — те же числа по всей сети, если у компании несколько филиалов

Базовое правило приоритета:
  🔥 Высокий = отзывов >=30 И свежие И (рейтинг 3.5-4.4 ИЛИ есть без ответа)
  🟡 Средний = поток есть (>=30, свежие), без триггеров выше
  ⚪ Низкий  = отзывов <30 ИЛИ нет свежих отзывов

ПОНИЖАЮЩЕЕ ПРАВИЛО поверх базового (добавлено после разбора DoctorDos —
рейтинг 5,0 при 778 отзывах, галочка, номинант, онлайн-запись, владелец
отвечает на бо́льшую часть отзывов; базовое правило дало ему Высокий только
из-за 9 отзывов без ответа, хотя продавать там нечего):

  Если rating >= DOWNGRADE_RATING_MIN И response_rate >= DOWNGRADE_RESPONSE_RATE_MIN
  -> Высокий понижается до Среднего, даже если unanswered_count большой.

Смысл: высокий рейтинг + владелец реально отвечает на отзывы = боли нет,
компания не чувствует проблему. А высокий рейтинг при response_rate == 0 —
наоборот, хороший лид: владелец не отвечает вообще, хотя мог бы.

Решение принимает одна чистая функция decide_priority — она же объясняет,
какое условие сработало и сработало ли понижение (колонка «Причина
приоритета»); причина всегда начинается с метки приоритета, поэтому её можно
читать в отрыве от остальной строки: «Высокий: рейтинг 4,2 в проблемной зоне,
39 без ответа», «Средний: рейтинг 5,0, владелец отвечает на 63% — боли нет».
"""
from dataclasses import dataclass, field
from typing import Optional

import metrics

NA = metrics.NA  # значение для не найденных полей

PRIORITY_HIGH = "🔥 Высокий"
PRIORITY_MEDIUM = "🟡 Средний"
PRIORITY_LOW = "⚪ Низкий"

# Причина приоритета печатается с меткой приоритета, но без эмодзи — так она
# читается как отдельное предложение и в CSV, и в Telegram.
_PLAIN_LABEL = {PRIORITY_HIGH: "Высокий", PRIORITY_MEDIUM: "Средний", PRIORITY_LOW: "Низкий"}

# Для сортировки: чем меньше число — тем выше в таблице
_PRIORITY_RANK = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}

MIN_REVIEWS = 30             # «поток есть» — столько отзывов и больше
PROBLEM_BAND = (3.5, 4.4)    # рейтинг в этой полосе — сам по себе тревожный сигнал

# Понижающее правило: высокий рейтинг + владелец реально отвечает -> боли нет
DOWNGRADE_RATING_MIN = 4.8
DOWNGRADE_RESPONSE_RATE_MIN = 0.2


@dataclass
class Company:
    firm_id: str = NA
    name: str = NA
    category: str = NA
    url: str = NA              # ссылка на карточку 2GIS
    phone: str = NA
    website: str = NA          # сайт / соцсети

    # --- числа от 2ГИС (никогда не вычисляются нами) ---
    rating: Optional[float] = None           # рейтинг филиала
    rating_confirmed: Optional[bool] = None  # «на основе подтверждённых оценок»
    ratings_count: Optional[int] = None      # всего оценок (база рейтинга)
    reviews_count: Optional[int] = None      # текстовых отзывов
    branch_scope: str = metrics.SCOPE_BRANCH  # филиал | все филиалы
    branches_count: Optional[int] = None     # сколько филиалов у сети
    org_rating: Optional[float] = None       # рейтинг всей сети
    org_reviews_count: Optional[int] = None
    org_ratings_count: Optional[int] = None

    # --- счётчики по окну отзывов (metrics.WINDOW), считает metrics ---
    loaded_reviews: Optional[int] = None        # сколько отзывов в окне
    unanswered_count: Optional[int] = None      # из них без ответа
    unanswered_fresh: Optional[int] = None      # без ответа среди свежих (30 дней)
    negative_count: Optional[int] = None        # из них негативных (<=3)
    negative_unanswered: Optional[int] = None   # негативных без ответа
    response_rate: Optional[float] = None       # доля отзывов в окне с ответом; None = окно пустое
    fresh_count: Optional[int] = None           # отзывов за 30 дней
    has_fresh_reviews: Optional[bool] = None    # есть отзыв за последний месяц
    owner_responds: Optional[bool] = None       # владелец отвечает на отзывы
    negative_without_response: Optional[bool] = None  # негатив без ответа в топ-5
    last_review_date: Optional[str] = None      # ISO-дата новейшего отзыва
    # цитаты для разбора: негатив БЕЗ ОТВЕТА, свежие сверху [{rating,date,text}]
    negative_quotes: list = field(default_factory=list)

    # когда собраны данные (ISO, UTC) — печатается в разборе как «данные собраны»
    collected_at: Optional[str] = None

    priority: str = field(default=PRIORITY_LOW)
    priority_reason: str = ""

    def compute_priority(self) -> str:
        """Назначить приоритет и причину. Возвращает приоритет."""
        self.priority, self.priority_reason = decide_priority(
            reviews_count=self.reviews_count,
            has_fresh=self.has_fresh_reviews,
            rating=self.rating,
            unanswered_count=self.unanswered_count,
            response_rate=self.response_rate)
        return self.priority

    # ---- сериализация для CSV ----
    @staticmethod
    def _fmt_bool(v: Optional[bool]) -> str:
        if v is True:
            return "да"
        if v is False:
            return "нет"
        return NA

    def to_row(self) -> dict:
        n = metrics.fmt_int
        return {
            "Приоритет": self.priority,
            "Причина приоритета": self.priority_reason or NA,
            "Название": self.name or NA,
            "Категория": self.category or NA,
            "Рейтинг": metrics.fmt_rating(self.rating),
            "Оценок всего": n(self.ratings_count),
            "Отзывов (текстовых)": n(self.reviews_count),
            "Область данных": self.branch_scope,
            "Филиалов": n(self.branches_count),
            "Свежие отзывы (мес)": self._fmt_bool(self.has_fresh_reviews),
            "Владелец отвечает": self._fmt_bool(self.owner_responds),
            "Негатив без ответа (топ-5)": self._fmt_bool(self.negative_without_response),
            f"Отзывов без ответа (окно {metrics.WINDOW})": n(self.unanswered_count),
            f"Из них негативных (окно {metrics.WINDOW})": n(self.negative_unanswered),
            f"Из них за {metrics.FRESH_DAYS} дней (окно {metrics.WINDOW})":
                n(self.unanswered_fresh),
            f"Владелец отвечает, % (окно {metrics.WINDOW})":
                metrics.fmt_pct(self.response_rate),
            "Дата последнего отзыва": self.last_review_date or NA,
            "Данные собраны": self.collected_at or NA,
            "Телефон": self.phone or NA,
            "Сайт/соцсети": self.website or NA,
            "Ссылка 2GIS": self.url or NA,
        }


CSV_HEADERS = [
    "Приоритет", "Причина приоритета", "Название", "Категория", "Рейтинг",
    "Оценок всего", "Отзывов (текстовых)", "Область данных", "Филиалов",
    "Свежие отзывы (мес)", "Владелец отвечает", "Негатив без ответа (топ-5)",
    f"Отзывов без ответа (окно {metrics.WINDOW})",
    f"Из них негативных (окно {metrics.WINDOW})",
    f"Из них за {metrics.FRESH_DAYS} дней (окно {metrics.WINDOW})",
    f"Владелец отвечает, % (окно {metrics.WINDOW})",
    "Дата последнего отзыва", "Данные собраны",
    "Телефон", "Сайт/соцсети", "Ссылка 2GIS",
]


def _downgrade_applies(rating, response_rate) -> bool:
    """Высокий рейтинг + владелец реально отвечает -> боли нет, лид не тот.

    response_rate=None (данных нет) НЕ считается «отвечает» — понижение просто
    не применяется, а не падает и не понижает вслепую."""
    return (rating is not None and rating >= DOWNGRADE_RATING_MIN
            and response_rate is not None and response_rate >= DOWNGRADE_RESPONSE_RATE_MIN)


def decide_priority(reviews_count, has_fresh, rating,
                    unanswered_count, response_rate) -> tuple:
    """Правило приоритета -> (приоритет, причина). Чистая функция.

    Причина всегда начинается с метки приоритета («Высокий: …», «Средний: …»)
    и объясняет, какое условие сработало — включая понижающее правило, если
    оно применилось. Без этого «почему 🔥» приходилось выяснять по цифрам
    вручную, и правило легко было счесть сломанным (см. кейс DoctorDos выше).
    """
    if reviews_count is None:
        return PRIORITY_LOW, f"{_PLAIN_LABEL[PRIORITY_LOW]}: нет данных о количестве отзывов"
    if reviews_count < MIN_REVIEWS:
        return (PRIORITY_LOW, f"{_PLAIN_LABEL[PRIORITY_LOW]}: отзывов меньше "
                              f"{MIN_REVIEWS} ({reviews_count})")
    if not has_fresh:
        return PRIORITY_LOW, f"{_PLAIN_LABEL[PRIORITY_LOW]}: нет отзывов за последний месяц"

    # поток есть: отзывов достаточно и они свежие. База: рейтинг в проблемной
    # полосе ИЛИ есть отзывы без ответа — каждое условие даёт свой фрагмент
    # причины, фрагменты всегда открываются текущим рейтингом для контекста.
    fragments = [f"рейтинг {metrics.fmt_rating(rating)}"]
    in_problem_band = rating is not None and PROBLEM_BAND[0] <= rating <= PROBLEM_BAND[1]
    if in_problem_band:
        fragments[0] += " в проблемной зоне"
    if unanswered_count:
        if response_rate == 0:
            fragments.append("но владелец не отвечает вообще")
        else:
            fragments.append(f"{unanswered_count} без ответа")

    if in_problem_band or unanswered_count:
        if _downgrade_applies(rating, response_rate):
            pct = metrics.fmt_pct(response_rate)
            return (PRIORITY_MEDIUM,
                    f"{_PLAIN_LABEL[PRIORITY_MEDIUM]}: рейтинг {metrics.fmt_rating(rating)}, "
                    f"владелец отвечает на {pct} — боли нет")
        return PRIORITY_HIGH, f"{_PLAIN_LABEL[PRIORITY_HIGH]}: " + ", ".join(fragments)

    return (PRIORITY_MEDIUM, f"{_PLAIN_LABEL[PRIORITY_MEDIUM]}: поток есть, рейтинг "
                             f"{metrics.fmt_rating(rating)}, владелец отвечает")


def sort_key(c: Company):
    """Сортировка: сначала приоритет, внутри — по убыванию числа отзывов."""
    return (_PRIORITY_RANK.get(c.priority, 99), -(c.reviews_count or 0))


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []

    # (rc, fresh, rating, unanswered_count, response_rate, ожидаемый приоритет,
    #  подстрока причины) — шесть обязательных кейсов из ТЗ + границы
    cases = [
        # 1. rating 4.2, unanswered 39, response_rate 0.10 -> Высокий
        (50, True, 4.2, 39, 0.10, PRIORITY_HIGH,
         "Высокий: рейтинг 4,2 в проблемной зоне, 39 без ответа"),
        # 2. rating 5.0, unanswered 9, response_rate 0.60 -> Средний (понижение)
        (50, True, 5.0, 9, 0.60, PRIORITY_MEDIUM,
         "Средний: рейтинг 5,0, владелец отвечает на 60% — боли нет"),
        # 3. rating 4.9, unanswered 50, response_rate 0.00 -> Высокий
        (50, True, 4.9, 50, 0.00, PRIORITY_HIGH,
         "Высокий: рейтинг 4,9, но владелец не отвечает вообще"),
        # 4. rating 4.8, unanswered 20, response_rate 0.20 -> Средний (граница включительно)
        (50, True, 4.8, 20, 0.20, PRIORITY_MEDIUM,
         "Средний: рейтинг 4,8, владелец отвечает на 20% — боли нет"),
        # 5. rating 4.8, unanswered 20, response_rate 0.19 -> Высокий (чуть ниже границы)
        (50, True, 4.8, 20, 0.19, PRIORITY_HIGH, "Высокий: рейтинг 4,8, 20 без ответа"),
        # 6. reviews_count 12 -> Низкий независимо от остальных полей
        (12, True, 5.0, 50, 0.00, PRIORITY_LOW, f"{_PLAIN_LABEL[PRIORITY_LOW]}: отзывов меньше"),
        # 7. response_rate None -> правило не падает, понижение не применяется
        (50, True, 4.9, 10, None, PRIORITY_HIGH, "Высокий: рейтинг 4,9, 10 без ответа"),

        # --- прежние ветки и границы (сохранены под новые поля) ---
        (50, True, 4.8, 0, None, PRIORITY_MEDIUM, "поток есть, рейтинг"),
        (20, True, 4.0, 5, None, PRIORITY_LOW, "меньше 30"),
        (100, False, 4.0, 5, None, PRIORITY_LOW, "за последний месяц"),
        (None, None, None, None, None, PRIORITY_LOW, "нет данных"),
        (30, True, 3.5, 0, None, PRIORITY_HIGH, "в проблемной зоне"),   # нижняя граница
        (30, True, 4.4, 0, None, PRIORITY_HIGH, "в проблемной зоне"),   # верхняя граница
        (30, True, 4.5, 0, None, PRIORITY_MEDIUM, "владелец отвечает"),  # чуть выше зоны
        (30, True, 3.4, 0, None, PRIORITY_MEDIUM, "владелец отвечает"),  # чуть ниже зоны
        (29, True, 4.8, 20, 0.10, PRIORITY_LOW, "меньше 30"),           # ровно порог по count
    ]
    for rc, fresh, rating, unans, rr, expect, reason_part in cases:
        got, reason = decide_priority(rc, fresh, rating, unans, rr)
        ok = got == expect and reason_part in reason
        checks.append((f"приоритет rc={rc} fresh={fresh} r={rating} unans={unans} "
                       f"rr={rr} -> {got} ({reason})", ok))

    # понижение — граница строго снаружи по рейтингу (4.79 < 4.8 -> не понижается)
    got, reason = decide_priority(50, True, 4.79, 20, 0.90)
    checks.append(("понижение: рейтинг чуть ниже 4,8 -> не применяется",
                   got == PRIORITY_HIGH))

    # response_rate=None не роняет и не понижает даже при высоком рейтинге
    got, reason = decide_priority(50, True, 5.0, 30, None)
    checks.append(("понижение: response_rate=None -> правило не падает, "
                   "не понижает", got == PRIORITY_HIGH))

    # Company.compute_priority делегирует в decide_priority
    c = Company(reviews_count=50, has_fresh_reviews=True, rating=4.0)
    checks.append(("Company: приоритет и причина проставлены",
                   c.compute_priority() == PRIORITY_HIGH
                   and "в проблемной зоне" in c.priority_reason))

    # контроль из ТЗ: DoctorDos — рейтинг 5,0, 778 отзывов, владелец отвечает
    # на бо́льшую часть (response_rate высокий) — было ошибочно Высокий из-за
    # 9-10 отзывов без ответа, должно стать Средний
    doctordos = Company(name="DoctorDos", rating=5.0, ratings_count=1174,
                        reviews_count=778, has_fresh_reviews=True,
                        unanswered_count=10, response_rate=0.8)
    checks.append(("контроль ТЗ: DoctorDos -> Средний (понижающее правило сработало)",
                   doctordos.compute_priority() == PRIORITY_MEDIUM
                   and "боли нет" in doctordos.priority_reason))

    # разделение счётчиков
    row = doctordos.to_row()
    checks.append(("CSV: оценки и отзывы — разные колонки",
                   row["Оценок всего"] == "1174" and row["Отзывов (текстовых)"] == "778"))
    checks.append(("CSV: рейтинг из 2ГИС", row["Рейтинг"] == "5,0"))
    checks.append(("CSV: есть колонка причины", "Причина приоритета" in CSV_HEADERS
                   and row["Причина приоритета"] != NA))
    checks.append(("CSV: заголовки совпадают с полями строки",
                   set(CSV_HEADERS) == set(row)))

    # сортировка
    hi = Company(reviews_count=40, has_fresh_reviews=True, rating=4.0)
    hi.compute_priority()
    lo = Company(reviews_count=500, has_fresh_reviews=False)
    lo.compute_priority()
    checks.append(("сортировка: приоритет важнее количества",
                   sorted([lo, hi], key=sort_key)[0] is hi))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
