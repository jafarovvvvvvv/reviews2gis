"""
Модель данных компании + логика приоритизации лида.

Чистая логика без браузера — её можно тестировать отдельно (см. блок __main__).

Правила приоритета (из ТЗ):
  🔥 Высокий = отзывов >=30 И свежие И (рейтинг 3.5-4.4 ИЛИ негатив без ответа в топ-5 ИЛИ владелец не отвечает)
  🟡 Средний = поток есть (>=30, свежие), рейтинг 4.5+, владелец отвечает
  ⚪ Низкий  = отзывов <30 ИЛИ нет свежих отзывов
"""
from dataclasses import dataclass, field, asdict
from typing import Optional

NA = "н/д"  # значение для не найденных полей

PRIORITY_HIGH = "🔥 Высокий"
PRIORITY_MEDIUM = "🟡 Средний"
PRIORITY_LOW = "⚪ Низкий"

# Для сортировки: чем меньше число — тем выше в таблице
_PRIORITY_RANK = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}


@dataclass
class Company:
    firm_id: str = NA
    name: str = NA
    category: str = NA
    rating: Optional[float] = None          # None -> н/д
    reviews_count: Optional[int] = None     # None -> н/д
    has_fresh_reviews: Optional[bool] = None    # отзыв за последний месяц
    owner_responds: Optional[bool] = None       # владелец отвечает на отзывы
    negative_without_response: Optional[bool] = None  # негатив без ответа в топ-5
    phone: str = NA
    website: str = NA          # сайт / соцсети
    url: str = NA              # ссылка на карточку 2GIS

    # --- детали для аудита и сообщений (по загруженным отзывам, до 50 новейших) ---
    loaded_reviews: Optional[int] = None        # сколько отзывов реально загружено
    unanswered_count: Optional[int] = None      # из них без официального ответа
    negative_unanswered: Optional[int] = None   # негатив (<=3) без ответа
    fresh_count: Optional[int] = None           # отзывов за последние 30 дней
    last_review_date: Optional[str] = None      # ISO-дата новейшего отзыва
    worst_reviews: list = field(default_factory=list)  # [{rating, date, text, answered}]

    priority: str = field(default=PRIORITY_LOW)

    def compute_priority(self) -> str:
        """Назначить приоритет по правилам ТЗ. Возвращает и сохраняет в self.priority."""
        rc = self.reviews_count
        fresh = self.has_fresh_reviews

        # ⚪ Низкий: отзывов мало или нет свежих (None трактуем консервативно как "нет данных" -> низкий)
        if rc is None or rc < 30 or not fresh:
            self.priority = PRIORITY_LOW
            return self.priority

        # Здесь гарантировано: rc >= 30 И есть свежие отзывы
        rating_in_band = self.rating is not None and 3.5 <= self.rating <= 4.4
        owner_silent = self.owner_responds is False
        neg_no_reply = self.negative_without_response is True

        if rating_in_band or neg_no_reply or owner_silent:
            self.priority = PRIORITY_HIGH
        else:
            # поток есть, рейтинг 4.5+ (или неизвестен но не в полосе риска), владелец отвечает
            self.priority = PRIORITY_MEDIUM
        return self.priority

    # ---- сериализация для CSV ----
    @staticmethod
    def _fmt_bool(v: Optional[bool]) -> str:
        if v is True:
            return "да"
        if v is False:
            return "нет"
        return NA

    @staticmethod
    def _fmt_num(v) -> str:
        return NA if v is None else str(v)

    def to_row(self) -> dict:
        return {
            "Приоритет": self.priority,
            "Название": self.name or NA,
            "Категория": self.category or NA,
            "Рейтинг": self._fmt_num(self.rating),
            "Кол-во отзывов": self._fmt_num(self.reviews_count),
            "Свежие отзывы (мес)": self._fmt_bool(self.has_fresh_reviews),
            "Владелец отвечает": self._fmt_bool(self.owner_responds),
            "Негатив без ответа (топ-5)": self._fmt_bool(self.negative_without_response),
            "Отзывов без ответа": self._fmt_num(self.unanswered_count),
            "Дата последнего отзыва": self.last_review_date or NA,
            "Телефон": self.phone or NA,
            "Сайт/соцсети": self.website or NA,
            "Ссылка 2GIS": self.url or NA,
        }


CSV_HEADERS = [
    "Приоритет", "Название", "Категория", "Рейтинг", "Кол-во отзывов",
    "Свежие отзывы (мес)", "Владелец отвечает", "Негатив без ответа (топ-5)",
    "Отзывов без ответа", "Дата последнего отзыва",
    "Телефон", "Сайт/соцсети", "Ссылка 2GIS",
]


def sort_key(c: Company):
    """Сортировка: сначала приоритет, внутри — по убыванию числа отзывов."""
    return (_PRIORITY_RANK.get(c.priority, 99), -(c.reviews_count or 0))


if __name__ == "__main__":
    # консоль Windows -> UTF-8 (иначе вывод эмодзи приоритетов падает на cp1251)
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    # быстрые проверки логики приоритета
    cases = [
        # (rc, fresh, rating, neg_no_reply, owner_responds, expect)
        (50, True, 4.0, False, True, PRIORITY_HIGH),    # рейтинг в полосе 3.5-4.4
        (50, True, 4.8, True, True, PRIORITY_HIGH),     # негатив без ответа
        (50, True, 4.8, False, False, PRIORITY_HIGH),   # владелец молчит
        (50, True, 4.8, False, True, PRIORITY_MEDIUM),  # всё хорошо -> средний
        (20, True, 4.0, True, False, PRIORITY_LOW),     # отзывов <30
        (100, False, 4.0, True, False, PRIORITY_LOW),   # нет свежих
        (None, None, None, None, None, PRIORITY_LOW),   # ничего не нашли
    ]
    ok = 0
    for rc, fresh, rating, neg, owner, expect in cases:
        c = Company(reviews_count=rc, has_fresh_reviews=fresh, rating=rating,
                    negative_without_response=neg, owner_responds=owner)
        got = c.compute_priority()
        status = "OK " if got == expect else "FAIL"
        ok += got == expect
        print(f"  [{status}] rc={rc} fresh={fresh} r={rating} neg={neg} owner={owner} -> {got}")
    print(f"{ok}/{len(cases)} passed")
