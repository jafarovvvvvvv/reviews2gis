"""
Модель данных компании + логика приоритизации лида.

Чистая логика без браузера и сети — самотесты: python lead_scoring.py

РЕЙТИНГ И СЧЁТЧИКИ РАЗДЕЛЕНЫ (иначе таблица и разбор расходятся):
  rating         — рейтинг филиала от 2ГИС. НИКОГДА не считается нами
  ratings_count  — сколько всего ОЦЕНОК (по ним 2ГИС и считает рейтинг)
  reviews_count  — сколько ТЕКСТОВЫХ отзывов (их всегда меньше)
  branch_scope   — по чему собраны данные: филиал или вся сеть
  org_*          — те же числа по всей сети, если у компании несколько филиалов

Правила приоритета (из ТЗ):
  🔥 Высокий = отзывов >=30 И свежие И (рейтинг 3.5-4.4 ИЛИ негатив без ответа
               в топ-5 ИЛИ владелец не отвечает)
  🟡 Средний = поток есть (>=30, свежие), рейтинг 4.5+, владелец отвечает
  ⚪ Низкий  = отзывов <30 ИЛИ нет свежих отзывов
Решение принимает одна чистая функция decide_priority — она же объясняет,
какое именно условие сработало (колонка «Причина приоритета»).
"""
from dataclasses import dataclass, field
from typing import Optional

import metrics

NA = metrics.NA  # значение для не найденных полей

PRIORITY_HIGH = "🔥 Высокий"
PRIORITY_MEDIUM = "🟡 Средний"
PRIORITY_LOW = "⚪ Низкий"

# Для сортировки: чем меньше число — тем выше в таблице
_PRIORITY_RANK = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}

MIN_REVIEWS = 30           # «поток есть» — столько отзывов и больше
RISK_BAND = (3.5, 4.4)     # полоса рискового рейтинга


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
    negative_count: Optional[int] = None        # из них негативных (<=3)
    negative_unanswered: Optional[int] = None   # негативных без ответа
    fresh_count: Optional[int] = None           # отзывов за 30 дней
    has_fresh_reviews: Optional[bool] = None    # есть отзыв за последний месяц
    owner_responds: Optional[bool] = None       # владелец отвечает на отзывы
    negative_without_response: Optional[bool] = None  # негатив без ответа в топ-5
    last_review_date: Optional[str] = None      # ISO-дата новейшего отзыва
    worst_reviews: list = field(default_factory=list)  # [{rating,date,text,answered}]

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
            negative_without_response=self.negative_without_response,
            owner_responds=self.owner_responds)
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
            f"Негатива без ответа (окно {metrics.WINDOW})": n(self.negative_unanswered),
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
    f"Негатива без ответа (окно {metrics.WINDOW})",
    "Дата последнего отзыва", "Данные собраны",
    "Телефон", "Сайт/соцсети", "Ссылка 2GIS",
]


def decide_priority(reviews_count, has_fresh, rating,
                    negative_without_response, owner_responds) -> tuple:
    """Правило приоритета -> (приоритет, причина). Чистая функция.

    Причина объясняет, какое условие сработало: без неё «почему 🔥» приходилось
    выяснять по цифрам вручную, и правило легко было счесть сломанным.
    """
    if reviews_count is None:
        return PRIORITY_LOW, "нет данных о количестве отзывов"
    if reviews_count < MIN_REVIEWS:
        return PRIORITY_LOW, f"отзывов меньше {MIN_REVIEWS} ({reviews_count})"
    if not has_fresh:
        return PRIORITY_LOW, "нет отзывов за последний месяц"

    # поток есть: отзывов достаточно и они свежие
    triggers = []
    if rating is not None and RISK_BAND[0] <= rating <= RISK_BAND[1]:
        triggers.append(f"рейтинг {metrics.fmt_rating(rating)} в полосе риска "
                        f"{metrics.fmt_rating(RISK_BAND[0])}–{metrics.fmt_rating(RISK_BAND[1])}")
    if negative_without_response is True:
        triggers.append(f"негатив без ответа в топ-{metrics.TOP_N}")
    if owner_responds is False:
        triggers.append("владелец не отвечает на отзывы")

    if triggers:
        return PRIORITY_HIGH, "; ".join(triggers)
    return PRIORITY_MEDIUM, (f"поток есть, рейтинг "
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

    # (rc, fresh, rating, neg_no_reply, owner_responds, ожидаемый приоритет, что в причине)
    cases = [
        (50, True, 4.0, False, True, PRIORITY_HIGH, "полосе риска"),
        (50, True, 4.8, True, True, PRIORITY_HIGH, "негатив без ответа"),
        (50, True, 4.8, False, False, PRIORITY_HIGH, "не отвечает"),
        (50, True, 4.8, False, True, PRIORITY_MEDIUM, "владелец отвечает"),
        (50, True, 5.0, False, True, PRIORITY_MEDIUM, "владелец отвечает"),
        (20, True, 4.0, True, False, PRIORITY_LOW, "меньше 30"),
        (100, False, 4.0, True, False, PRIORITY_LOW, "за последний месяц"),
        (None, None, None, None, None, PRIORITY_LOW, "нет данных"),
        # границы полосы риска
        (30, True, 3.5, False, True, PRIORITY_HIGH, "полосе риска"),
        (30, True, 4.4, False, True, PRIORITY_HIGH, "полосе риска"),
        (30, True, 4.5, False, True, PRIORITY_MEDIUM, "владелец отвечает"),
        (30, True, 3.4, False, True, PRIORITY_MEDIUM, "владелец отвечает"),
        # ровно порог по количеству
        (29, True, 4.8, False, True, PRIORITY_LOW, "меньше 30"),
    ]
    for rc, fresh, rating, neg, owner, expect, reason_part in cases:
        got, reason = decide_priority(rc, fresh, rating, neg, owner)
        ok = got == expect and reason_part in reason
        checks.append((f"приоритет rc={rc} fresh={fresh} r={rating} neg={neg} "
                       f"owner={owner} -> {got} ({reason})", ok))

    # несколько условий сразу — перечисляются все
    _, reason = decide_priority(50, True, 4.0, True, False)
    checks.append(("причина: перечислены все сработавшие условия",
                   reason.count(";") == 2))

    # Company.compute_priority делегирует в decide_priority
    c = Company(reviews_count=50, has_fresh_reviews=True, rating=4.0,
                negative_without_response=False, owner_responds=True)
    checks.append(("Company: приоритет и причина проставлены",
                   c.compute_priority() == PRIORITY_HIGH and "полосе риска" in c.priority_reason))

    # контроль из ТЗ: DoctorDos — рейтинг 5,0, владелец отвечает, негатива в топ-5 нет
    doctordos = Company(name="DoctorDos", rating=5.0, ratings_count=1174,
                        reviews_count=778, has_fresh_reviews=True,
                        owner_responds=True, negative_without_response=False)
    checks.append(("контроль ТЗ: DoctorDos -> Средний",
                   doctordos.compute_priority() == PRIORITY_MEDIUM))

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
