"""
Telegram-уведомления через Bot API (голый HTTPS, без сторонних SDK).

Поллинг getUpdates и команды живут в bot.py (он — единственный потребитель
getUpdates). Здесь — транспорт (call/send_message/get_me) и чистое
форматирование уведомлений.

Форматирование сообщений — чистые функции, самотесты: python tg.py
"""
import html
from typing import Optional

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
NEGATIVE_MAX = 3  # оценка <=3 — негатив (как NEGATIVE_RATING_MAX в scraper)

# шаблон черновика ответа на негатив (подставляется в уведомление)
NEGATIVE_DRAFT = (
    "Здравствуйте! Спасибо, что написали — нам жаль, что визит прошёл не так, "
    "как должен был. Хотим разобраться и всё исправить: напишите нам, пожалуйста, "
    "[телефон/WhatsApp] — решим вопрос лично."
)


class TgError(Exception):
    """Ошибка Telegram API (текст — description из ответа)."""


async def call(token: str, method: str, http_timeout: float = 20, **params):
    """http_timeout должен превышать timeout long-polling'а getUpdates."""
    url = API.format(token=token.strip(), method=method)
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(url, json=params)
            data = resp.json()
    except Exception as e:
        raise TgError(f"сеть/API недоступны: {e}") from e
    if not data.get("ok"):
        raise TgError(data.get("description") or f"HTTP {resp.status_code}")
    return data["result"]


async def send_message(token: str, chat_id: str, text: str):
    return await call(token, "sendMessage", chat_id=chat_id, text=text,
                      parse_mode="HTML", disable_web_page_preview=True)


async def get_me(token: str) -> dict:
    """Проверка токена; возвращает {username, ...}."""
    return await call(token, "getMe")


async def send_document(token: str, chat_id: str, filename: str,
                        content: bytes, caption: str = ""):
    """Отправить файл (отчёт-HTML и т.п.). multipart, поэтому не через call()."""
    url = API.format(token=token.strip(), method="sendDocument")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url, data={"chat_id": str(chat_id), "caption": caption[:1000]},
                files={"document": (filename, content, "text/html")})
            data = resp.json()
    except Exception as e:
        raise TgError(f"сеть/API недоступны: {e}") from e
    if not data.get("ok"):
        raise TgError(data.get("description") or f"HTTP {resp.status_code}")
    return data["result"]


# ---------- форматирование уведомлений (чистые функции) ----------

def stars(rating: Optional[int]) -> str:
    r = max(0, min(5, int(rating or 0)))
    return "★" * r + "☆" * (5 - r)


def is_negative(rating: Optional[int]) -> bool:
    return rating is not None and rating <= NEGATIVE_MAX


def fmt_review_alert(company_name: str, company_url: str, rv: dict,
                     test: bool = False, ai_replies: list = None) -> str:
    """Уведомление о новом отзыве. rv: rating/date_created/author/text/answered.

    ai_replies — готовые варианты ответа (ai_reply.py); в Telegram <pre>-блок
    копируется одним тапом. Без них на негатив идёт статичный шаблон.
    """
    e = html.escape
    rating = rv.get("rating")
    neg = is_negative(rating)
    head = "🚨 <b>Новый негативный отзыв</b>" if neg else "⭐ <b>Новый отзыв</b>"
    if test:
        head += " (тест — последний отзыв из базы)"
    date = (rv.get("date_created") or "")[:10]
    author = rv.get("author") or "аноним"
    text = (rv.get("text") or "").strip() or "(без текста)"
    if len(text) > 500:
        text = text[:500] + "…"
    lines = [
        f"{head} — {e(company_name)}",
        f"{stars(rating)} · {e(date)} · {e(author)}",
        f"«{e(text)}»",
        f'<a href="{e(company_url)}">Карточка в 2ГИС</a> — ответить можно в кабинете.',
    ]
    if ai_replies:
        lines.append("\n✍ <b>Готовые ответы</b> (тапните по тексту — скопируется):")
        for i, reply in enumerate(ai_replies, 1):
            lines.append(f"Вариант {i}:\n<pre>{e(reply)}</pre>")
    elif neg:
        lines.append(f"\n✍ Черновик ответа:\n<i>{e(NEGATIVE_DRAFT)}</i>")
    return "\n".join(lines)


def fmt_check_error(company_name: str, error: str) -> str:
    return (f"⚠️ Не удалось проверить отзывы «{html.escape(company_name)}»: "
            f"{html.escape(error)}")


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []
    checks.append(("stars", stars(3) == "★★★☆☆" and stars(None) == "☆☆☆☆☆"))
    checks.append(("negative порог", is_negative(3) and not is_negative(4)
                   and not is_negative(None)))

    neg = fmt_review_alert("Клиника <X>", "https://2gis.kz/x",
                           {"rating": 1, "date_created": "2026-06-01T10:00:00",
                            "author": "Иван", "text": "Плохо <b>совсем</b>"})
    checks.append(("негатив: заголовок и черновик",
                   "🚨" in neg and "Черновик ответа" in neg))
    checks.append(("негатив: HTML экранирован",
                   "&lt;b&gt;" in neg and "Клиника &lt;X&gt;" in neg))

    pos = fmt_review_alert("Y", "u", {"rating": 5, "text": "Супер"})
    checks.append(("позитив: без черновика", "⭐" in pos and "Черновик" not in pos))
    checks.append(("тестовая пометка",
                   "(тест" in fmt_review_alert("Y", "u", {"rating": 5}, test=True)))

    long = fmt_review_alert("Y", "u", {"rating": 1, "text": "ы" * 600})
    checks.append(("обрезка длинного текста", "ы" * 501 not in long and "…" in long))

    ai = fmt_review_alert("Y", "u", {"rating": 1, "text": "плохо"},
                          ai_replies=["Ответ <раз>", "Ответ два"])
    checks.append(("ai: оба варианта в pre",
                   ai.count("<pre>") == 2 and "Вариант 2" in ai))
    checks.append(("ai: экранирование", "&lt;раз&gt;" in ai))
    checks.append(("ai: статичный шаблон не дублируется", "Черновик ответа" not in ai))
    no_ai = fmt_review_alert("Y", "u", {"rating": 1, "text": "плохо"}, ai_replies=[])
    checks.append(("ai: пустой список -> старый шаблон", "Черновик ответа" in no_ai))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
