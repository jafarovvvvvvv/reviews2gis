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

import metrics

API = "https://api.telegram.org/bot{token}/{method}"
# Порог негатива один на весь проект — metrics.NEGATIVE_MAX. Раньше он был
# продублирован здесь и в fetcher/insights, и определения разъезжались.
NEGATIVE_MAX = metrics.NEGATIVE_MAX

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


async def send_message(token: str, chat_id: str, text: str, reply_markup=None):
    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=True)
    if reply_markup:
        params["reply_markup"] = reply_markup
    return await call(token, "sendMessage", **params)


async def answer_callback(token: str, callback_id: str, text: str = ""):
    """Погасить «часики» на нажатой inline-кнопке (+опц. всплывашка)."""
    try:
        await call(token, "answerCallbackQuery", callback_query_id=callback_id,
                   text=text)
    except TgError:
        pass


def btn_cb(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def btn_url(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def inline_kb(*rows) -> dict:
    """rows — списки кнопок (btn_cb/btn_url). Пустые ряды отбрасываются."""
    return {"inline_keyboard": [r for r in rows if r]}


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
    """Негатив — по общему определению (metrics.is_negative)."""
    return metrics.is_negative(rating)


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
        lines.append("\n✍ <b>Готовый ответ</b> (тапните по тексту — скопируется):"
                     if len(ai_replies) == 1 else
                     "\n✍ <b>Готовые ответы</b> (тапните по тексту — скопируется):")
        for reply in ai_replies:
            lines.append(f"<pre>{e(reply)}</pre>")
    elif neg:
        lines.append(f"\n✍ Черновик ответа:\n<i>{e(NEGATIVE_DRAFT)}</i>")
    return "\n".join(lines)


def fmt_unanswered_review(company_name: str, company_url: str, rv: dict,
                          ai_replies: list = None) -> str:
    """Карточка неотвеченного отзыва для «догоняющей» рассылки владельцу
    (кнопка «отправить неотвеченные» на странице компании) — в отличие от
    fmt_review_alert это не «новый отзыв», а разбор скопившегося долга,
    заголовок и повод другие, тело то же (звёзды/дата/автор/текст/варианты).
    """
    e = html.escape
    rating = rv.get("rating")
    neg = is_negative(rating)
    head = "📋 <b>Без ответа</b>" + (" · негатив" if neg else "")
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
        lines.append("\n✍ <b>Готовый ответ</b> (тапните по тексту — скопируется):"
                     if len(ai_replies) == 1 else
                     "\n✍ <b>Готовые ответы</b> (тапните по тексту — скопируется):")
        for reply in ai_replies:
            lines.append(f"<pre>{e(reply)}</pre>")
    elif neg:
        lines.append(f"\n✍ Черновик ответа:\n<i>{e(NEGATIVE_DRAFT)}</i>")
    return "\n".join(lines)


def fmt_unanswered_batch_header(company_name: str, company_url: str, count: int) -> str:
    """Заголовок перед серией fmt_unanswered_review — объясняет, что дальше."""
    e = html.escape
    word = metrics.plural(count, "отзыв", "отзыва", "отзывов")
    return (f"📋 <b>Неотвеченные отзывы — {e(company_name)}</b>\n"
            f"{count} {word} без ответа. Дальше — по одному сообщению на отзыв, "
            f"с готовым вариантом ответа, где получилось его подготовить.\n"
            f'<a href="{e(company_url)}">Карточка в 2ГИС</a>')


def fmt_moderation_header(company_name: str, rating, client_chats: list) -> str:
    """Шапка над задержанным уведомлением (уходит только владельцу сервиса).

    Сразу говорит, кому уйдёт текст после подтверждения: если чат клиента не
    подписан, жать «отправить» бессмысленно, и это лучше видеть до нажатия.
    """
    e = html.escape
    kind = "негатив" if is_negative(rating) else "отзыв"
    if client_chats:
        who = ", ".join(e(c["title"] or c["chat_id"]) for c in client_chats)
        tail = f"После подтверждения уйдёт клиенту ({who})."
    else:
        tail = ("Чат клиента не подписан на компанию — отправлять пока некому "
                "(подпишите на странице компании).")
    return (f"🕓 <b>На проверку</b> — {e(company_name)} · {kind}\n"
            f"{tail}\n\n")


def fmt_check_error(company_name: str, error: str) -> str:
    return (f"⚠️ Не удалось проверить отзывы «{html.escape(company_name)}»: "
            f"{html.escape(error)}")


def fmt_weekly_digest(name, rating, delta, new_reviews, neg_unanswered) -> str:
    """Еженедельный дайджест клиенту. delta — изменение рейтинга за неделю (или None)."""
    e = html.escape
    rs = "—" if rating is None else f"{rating:.1f}".replace(".", ",")
    if delta is None or abs(delta) < 0.05:
        trend = "без изменений за неделю"
    elif delta > 0:
        trend = f"📈 +{delta:.1f}".replace(".", ",") + " за неделю"
    else:
        trend = f"📉 {delta:.1f}".replace(".", ",") + " за неделю"
    lines = [
        f"📊 <b>Итоги недели — {e(name)}</b>",
        f"Рейтинг: <b>{rs}</b> ({trend})",
        f"Новых отзывов за 7 дней: {new_reviews}",
    ]
    if neg_unanswered:
        lines.append(f"⚠️ Негатива без ответа: {neg_unanswered} — стоит ответить, "
                     f"мы подготовили варианты.")
    else:
        lines.append("✅ Весь негатив отработан.")
    return "\n".join(lines)


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
    checks.append(("ai: оба варианта в pre, без ярлыков «Вариант N»",
                   ai.count("<pre>") == 2 and "Вариант" not in ai))
    checks.append(("ai: экранирование", "&lt;раз&gt;" in ai))
    checks.append(("ai: статичный шаблон не дублируется", "Черновик ответа" not in ai))
    no_ai = fmt_review_alert("Y", "u", {"rating": 1, "text": "плохо"}, ai_replies=[])
    checks.append(("ai: пустой список -> старый шаблон", "Черновик ответа" in no_ai))

    # --- шапка модерации: видно, кому уйдёт текст после подтверждения ---
    mh = fmt_moderation_header("Клиника <X>", 1, [{"title": "Чат клиента",
                                                   "chat_id": "77"}])
    checks.append(("модерация: заголовок и вид отзыва",
                   "На проверку" in mh and "негатив" in mh))
    checks.append(("модерация: назван чат-получатель",
                   "Чат клиента" in mh and "Клиника &lt;X&gt;" in mh))
    mh_empty = fmt_moderation_header("Y", 5, [])
    checks.append(("модерация: получателя нет -> сказано прямо",
                   "не подписан" in mh_empty and "отзыв" in mh_empty))

    ur = fmt_unanswered_review("Y", "u", {"rating": 1, "date_created": "2026-06-01",
                                          "author": "Иван", "text": "плохо"})
    checks.append(("неотвеченный: заголовок «Без ответа», не «Новый»",
                   "Без ответа" in ur and "Новый отзыв" not in ur))
    checks.append(("неотвеченный: негатив -> черновик", "Черновик ответа" in ur))
    ur_ai = fmt_unanswered_review("Y", "u", {"rating": 5, "text": "супер"},
                                  ai_replies=["Спасибо!"])
    checks.append(("неотвеченный: с AI-вариантом, без черновика",
                   "<pre>Спасибо!</pre>" in ur_ai and "Черновик ответа" not in ur_ai))

    hdr = fmt_unanswered_batch_header("Клиника", "https://2gis.kz/x", 3)
    checks.append(("шапка пачки: число и склонение", "3 отзыва без ответа" in hdr))
    hdr1 = fmt_unanswered_batch_header("Клиника", "u", 1)
    checks.append(("шапка пачки: единственное число", "1 отзыв без ответа" in hdr1))

    dg = fmt_weekly_digest("Клиника", 4.6, 0.2, 5, 2)
    checks.append(("дайджест: рост и негатив", "📈" in dg and "4,6" in dg
                   and "Негатива без ответа: 2" in dg))
    dg2 = fmt_weekly_digest("X", 4.6, 0.0, 0, 0)
    checks.append(("дайджест: без изменений и негатив отработан",
                   "без изменений" in dg2 and "отработан" in dg2))
    dg3 = fmt_weekly_digest("X", 4.0, -0.3, 3, 0)
    checks.append(("дайджест: падение", "📉" in dg3))

    kb = inline_kb([btn_cb("Ещё", "reply:5")], [btn_url("2ГИС", "https://2gis.kz/x")], [])
    checks.append(("inline_kb: 2 ряда (пустой отброшен)",
                   len(kb["inline_keyboard"]) == 2))
    checks.append(("btn_cb: callback_data", kb["inline_keyboard"][0][0]["callback_data"] == "reply:5"))
    checks.append(("btn_url: url", kb["inline_keyboard"][1][0]["url"] == "https://2gis.kz/x"))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
