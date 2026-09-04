"""
Генерация материалов для рассылки (outreach) из собранных лидов.

Вход — список Company после прогона (leadgen.py). Выход — «пакет для рассылки»
в папке output/outreach_<город>_<категория>_<дата>/:
  index.html       — дашборд: лиды по приоритету, тексты первого касания под
                     каждый канал (кнопка «копировать»), ссылка wa.me с уже
                     подставленным текстом, ссылка на аудит
  audit_<id>.html  — одностраничный аудит карточки: метрики, худшие отзывы,
                     сравнение с конкурентом, два шага (печать в PDF — Ctrl+P)
  outreach.csv     — Название;Приоритет;Телефон;WhatsApp + текст на каждый канал

Что продаём (от этого зависят все тексты здесь): не инструмент, а снятую
работу. «Слежу за карточкой в 2ГИС. Появился отзыв, особенно негативный —
присылаю готовый ответ в WhatsApp в тот же день, вы вставляете. Раз в месяц
отчёт: что изменилось и на что жалуются чаще всего.» Мониторинг как ценность
не продаётся (у 2ГИС свои бесплатные уведомления), рост рейтинга по срокам
не обещается — см. build_steps/rating_note.

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

import os

import ai_reply
import metrics
from lead_scoring import Company, NA, sort_key

# Блок сравнения с конкурентом в разборе. Выключен по умолчанию: он подставлял
# разных конкурентов от прогона к прогону. Одно определение на проект —
# leadgen берёт флаг отсюда же.
AUDIT_COMPETITOR = (os.getenv("AUDIT_COMPETITOR") or "0").strip().lower() in (
    "1", "true", "yes")

# Черновики ответов под цитатами негатива (см. ai_reply.draft_audit_replies).
# ВКЛЮЧЕНО по умолчанию: готовый ответ под конкретной цитатой — единственное
# место, где директор видит нашу работу, а не обещание, и шаг «закрыть негатив»
# опирается именно на них (build_steps). Цена: до MAX_AUDIT_DRAFTS запросов к
# OpenAI на карточку — для пакета на 100+ лидов это заметно, выключайте
# DRAFT_REPLIES=0 на больших прогонах. Без ключа OpenAI флаг ничего не меняет:
# разбор просто строится без черновиков.
DRAFT_REPLIES = (os.getenv("DRAFT_REPLIES") or "1").strip().lower() in (
    "1", "true", "yes")

# Минимальный разрыв рейтинга, при котором упоминаем конкурента
COMPETITOR_GAP = 0.2
# Полоса «рискованного» рейтинга (как в lead_scoring)
RATING_BAND = (3.5, 4.4)
MAX_BULLETS = 3  # пунктов боли в сообщении


# ---------- утилиты ----------

def fmt_rating(r: Optional[float]) -> str:
    """4.7 -> «4,7». Тонкая обёртка над единым слоем — формат один на проект."""
    return metrics.fmt_rating(r)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение. Тонкая обёртка над единым слоем — как fmt_rating выше."""
    return metrics.plural(n, one, few, many)


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
        out.append(f"рейтинг {fmt_rating(c.rating)} — ниже «зелёной зоны» 4,5+, по "
                   f"которой люди отсеивают варианты прямо в выдаче на карте")

    # 2. отзывы без ответа. Число здесь — то же самое поле, что в шапке
    # (unanswered_count); негатив и свежесть называются своими словами, а не
    # подставляются под ту же подпись. Раньше шапка показывала одно число,
    # а текст — другое, и оба назывались «без ответа».
    if c.unanswered_count:
        n = c.unanswered_count
        detail = []
        if c.negative_unanswered:
            detail.append(f"{c.negative_unanswered} из них негативные")
        if c.unanswered_fresh:
            detail.append(f"{c.unanswered_fresh} — за последний месяц")
        tail = (", " + ", ".join(detail)) if detail else ""
        out.append(f"{n} {plural(n, 'отзыв остался', 'отзыва остались', 'отзывов остались')} "
                   f"без ответа из последних {c.loaded_reviews or metrics.WINDOW}"
                   f"{tail} — это видит каждый, кто открывает карточку")

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


# ---------- сообщения первого касания ----------

# Задача первого касания — НЕ вручить разбор, а выйти на директора: узнать имя
# и прямой контакт. Разбор отправляем потом, уже конкретному человеку.
# Поэтому текст короткий, начинается с наблюдения по их карточке и кончается
# вопросом про директора.
#
# Правила этих текстов (нарушишь одно — сообщение читается как рассылка):
#   • не начинать с «бесплатный разбор» и вообще с оффера;
#   • живой язык, без канцелярита и маркетинговых оборотов;
#   • 3–4 предложения, одним сообщением;
#   • без длинных тире: в переписке они выдают копипасту (проверяется самотестом).
TOUCH_CHANNELS = ("whatsapp", "instagram", "admin")
TOUCH_LABELS = {
    "whatsapp": "WhatsApp",
    "instagram": "Instagram Direct",
    "admin": "Если ответил администратор",
}


def city_prepositional(city: str) -> str:
    """«Костанай» -> «в Костанае», «Алматы» -> «в Алматы».

    В холодном сообщении «работаю в 2ГИС в Костанай» сразу выдаёт шаблон,
    поэтому город склоняем. Правила покрывают города из config.CITY_SLUGS;
    неузнанное окончание оставляем как есть — это хуже, чем правильный падеж,
    но лучше, чем выдуманная форма.
    """
    city = (city or "").strip()
    if not city:
        return ""
    low = city.lower()
    if low.endswith(("ый", "ий")):        # Рудный -> в Рудном
        return f"в {city[:-2]}ом"
    if low.endswith("а"):                 # Астана -> в Астане
        return f"в {city[:-1]}е"
    if low.endswith("й"):                 # Костанай -> в Костанае
        return f"в {city[:-1]}е"
    if low.endswith(("ы", "е", "о", "у", "и", "э", "ю", "я")):
        return f"в {city}"                # Алматы, Актобе, Атырау — не склоняем
    if low[-1:].isalpha():                # согласная: Шымкент -> в Шымкенте
        return f"в {city}е"
    return f"в {city}"


def touch_observation(c: Company) -> str:
    """Одно короткое наблюдение по карточке — продолжение фразы «У «X» …».

    Берём самый заметный факт: сначала негатив без ответа, потом весь долг по
    ответам, потом молчащая карточка. Числа — те же поля, что в шапке разбора,
    ничего «на глазок».
    """
    neg = c.negative_unanswered or 0
    if neg:
        return (f"{neg} {plural(neg, 'негативный отзыв', 'негативных отзыва', 'негативных отзывов')} "
                f"без ответа")
    unanswered = c.unanswered_count or 0
    if unanswered:
        return (f"{unanswered} {plural(unanswered, 'отзыв', 'отзыва', 'отзывов')} "
                f"без ответа из последних {c.loaded_reviews or metrics.WINDOW}")
    if c.has_fresh_reviews is False:
        return "за последний месяц ни одного нового отзыва"
    if c.reviews_count is not None and c.reviews_count < 30:
        return f"отзывов всего {c.reviews_count}"
    return "есть пара вещей, которые бросаются в глаза"


def build_touch_message(c: Company, cfg, channel: str = "whatsapp") -> str:
    """Текст первого касания под конкретный канал (TOUCH_CHANNELS)."""
    sender = cfg.sender_name or "[Имя]"
    name = c.name if c.name != NA else "вашей компании"
    obs = touch_observation(c)

    if channel == "instagram":
        # В Direct и так видно, кто пишет: представление не нужно, нужна
        # причина, по которой пишут именно им.
        return (
            f"Здравствуйте! Смотрел карточку «{name}» в 2ГИС, зацепило вот что: "
            f"{obs}. Это читает каждый, кто выбирает, куда пойти.\n"
            f"\n"
            f"У меня по этой части есть что показать, но обсуждать это есть смысл "
            f"с директором, а не в общем аккаунте. Подскажете, как его зовут и "
            f"куда ему написать напрямую?"
        )

    if channel == "admin":
        # Администратор не передаст (или передаст так, что смысл потеряется),
        # поэтому просим не «передайте», а прямой контакт.
        return (
            f"Спасибо! Я не по записи. Смотрю карточку «{name}» в 2ГИС: {obs}, "
            f"и это видит каждый, кто выбирает, куда пойти.\n"
            f"\n"
            f"Передавать ничего не нужно, я сам всё объясню за пару минут. "
            f"Дайте, пожалуйста, прямой номер или WhatsApp директора?"
        )

    where = city_prepositional(getattr(cfg, "city", ""))
    return (
        f"Здравствуйте! Меня зовут {sender}, я занимаюсь отзывами на 2ГИС"
        f"{' ' + where if where else ''}. Открыл карточку «{name}»: {obs}.\n"
        f"\n"
        f"Это видит каждый, кто выбирает, куда пойти, и по этому поводу у меня "
        f"есть что показать. Подскажите, как зовут директора и куда ему лучше "
        f"написать?"
    )


def build_touch_messages(c: Company, cfg) -> dict:
    """Все варианты первого касания: {канал: текст} в порядке TOUCH_CHANNELS."""
    return {ch: build_touch_message(c, cfg, ch) for ch in TOUCH_CHANNELS}


# ---------- аудит (одна страница HTML, печатается в PDF) ----------

AUDIT_CSS = """
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
.note{background:#f4f6fa;border-radius:10px;padding:12px 14px;margin:16px 0;
      color:#444;font-size:.92em}
.footer{margin-top:28px;padding-top:12px;border-top:1px solid #ddd;color:#666;font-size:.9em}
.tiny{font-size:.82em;color:#888}
.vs .up{color:#c0392b}.vs .down{color:#27ae60}
@media print{body{margin:0}.noprint{display:none}}
"""

# Стили черновиков ответов — отдельно от AUDIT_CSS и подключаются отдельным
# <style>, только когда в разборе реально есть хоть один черновик. Иначе при
# DRAFT_REPLIES=0 в документе появлялись бы мёртвые правила и он переставал
# быть побайтово тем же, что и без этой фичи.
_DRAFT_CSS = """
.draft{background:#f2f9f4;border-left:3px solid #27ae60;margin:10px 0 0;
       padding:8px 12px;border-radius:0 8px 8px 0}
.draft .why{color:#666;font-size:.85em;margin-top:4px}
.quote-note{color:#666;font-size:.9em;margin:10px 0}
"""


def fmt_collected(iso) -> str:
    """ISO-момент сбора -> «02.09.2026, 09:28» по Астане (UTC+5), не по Гринвичу.

    Пусто -> «дата неизвестна»."""
    return metrics.fmt_local(iso, empty="дата неизвестна")


def render_dynamics_block(dyn) -> str:
    """Блок «что изменилось» по двум снимкам. Нет истории -> пустая строка.

    Заглушек и прочерков быть не должно: если сравнивать не с чем, блока в
    разборе просто нет.
    """
    if not dyn or not dyn.get("changes"):
        return ""
    e = html.escape
    rows = []
    for ch in dyn["changes"]:
        was, now = ch["was"], ch["now"]
        if was is None or now is None or was == now:
            arrow = ""
        elif now > was:
            arrow = ' <span class="up">&#9650;</span>'
        else:
            arrow = ' <span class="down">&#9660;</span>'
        fmt = metrics.fmt_rating if ch["label"] == "рейтинг" else metrics.fmt_int
        rows.append(f"<li>{e(ch['label'])}: <b>{e(fmt(was))}</b> &rarr; "
                    f"<b>{e(fmt(now))}</b>{arrow}</li>")
    return (f'<div class="vs"><b>Что изменилось с прошлого замера '
            f'({e(fmt_collected(dyn["since"]))}).</b>'
            f'<ul class="pains">{"".join(rows)}</ul></div>')


# Шагов ровно два, и это не сокращение ради краткости, а позиционирование.
# Чего здесь больше не будет:
#   • «включить мониторинг» — у 2ГИС есть свои бесплатные уведомления в Telegram
#     и на почту для всех компаний, продавать «вы узнаете о новом отзыве» нечестно.
#     Мониторинг в коде остался и работает, но клиенту он не продаётся;
#   • «запустить поток отзывов» (QR/просьба после визита) — это меняет процессы
#     внутри клиники и держится на дисциплине администраторов. Мы это пока не
#     продаём, значит и не обещаем.
STEP_ANSWER = (
    "<b>Закрыть накопленный негатив.</b> Ответ владельца снимает часть ущерба от "
    "плохого отзыва и виден каждому, кто открывает карточку. Тексты пишем мы — "
    "вам останется вставить их в кабинет 2ГИС.")
STEP_ANSWER_DRAFTS = (
    "<b>Закрыть накопленный негатив.</b> Ответ владельца снимает часть ущерба от "
    "плохого отзыва и виден каждому, кто открывает карточку. Готовые ответы — выше, "
    "под цитатами: их можно вставлять в кабинет 2ГИС как есть.")
STEP_KEEP = (
    "<b>Не копить дальше.</b> Каждый новый отзыв получает ответ: готовый текст "
    "приходит вам в WhatsApp, вы его вставляете. Помнить и отслеживать ничего "
    "не нужно.")


def build_steps(c: Company, has_drafts: bool = False) -> list:
    """Шаги для разбора. Шаг «закрыть негатив» появляется, только если негатив
    без ответа реально есть: иначе документ противоречит сам себе.

    has_drafts — показаны ли в этом же документе готовые ответы под цитатами.
    Обещать «ответы прямо в разборе» там, где их нет, нельзя.
    """
    steps = []
    if getattr(c, "negative_quotes", None):
        steps.append(STEP_ANSWER_DRAFTS if has_drafts else STEP_ANSWER)
    steps.append(STEP_KEEP)
    return steps


def rating_note(c: Company) -> str:
    """Честная приписка про рейтинг под шагами.

    Раньше разбор обещал «рейтинг растёт за 1–2 месяца». При сотнях накопленных
    оценок это неправда: сдвиг на 0,3 требует сотен новых оценок. Обещание
    заменено на то, что ответы действительно дают.
    """
    n = getattr(c, "ratings_count", None)
    base = (f"при {metrics.fmt_int(n)} накопленных оценках" if n
            else "при накопленной базе оценок")
    return (f'<p class="note"><b>Про рейтинг честно.</b> Ответы на отзывы сами по себе '
            f'оценку не поднимают: они стабилизируют репутацию и снимают часть ущерба '
            f'от негатива. Сдвинуть сам рейтинг {base} может только приток новых '
            f'оценок — это отдельная работа, и её мы вам сейчас не продаём.</p>')


async def build_quote_drafts(c: Company) -> list:
    """Черновики ответов под первыми цитатами разбора (DRAFT_REPLIES=1).

    Список из 0–MAX_AUDIT_DRAFTS элементов, выровненный по индексу с
    c.negative_quotes (элемент может быть None — сбой по конкретной цитате).
    DRAFT_REPLIES=0, нет цитат или не задан ключ OpenAI -> [] — вызывающий
    рендерит разбор без блока черновиков, как раньше.
    """
    quotes = getattr(c, "negative_quotes", None) or []
    if not DRAFT_REPLIES or not quotes:
        return []
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    model = os.getenv("OPENAI_MODEL", "").strip()
    category = c.category if c.category and c.category != NA else ""
    return await ai_reply.draft_audit_replies(c.name, category, quotes, api_key, model)


def render_draft(d: dict) -> str:
    """Блок «Как бы ответили мы» под одной цитатой. d — {claim_type, reply, why}."""
    e = html.escape
    why = f'<div class="why"><i>{e(d["why"])}</i></div>' if d.get("why") else ""
    return f'<div class="draft"><b>Как бы ответили мы:</b> {e(d["reply"])}{why}</div>'


def render_quotes(c: Company, drafts: list = None) -> str:
    """Блок цитат: негатив БЕЗ ОТВЕТА, свежие сверху (их отобрал слой данных).

    Пусто -> блока нет совсем. Метки «есть ответ владельца» здесь быть не может:
    отвеченные отзывы сюда не попадают по построению.

    drafts: результат build_quote_drafts — черновики для первых цитат.
    Пусто/None (DRAFT_REPLIES=0 или сбой генерации целиком) -> ни одного
    блока черновика, ни строки «остальные N» — разбор рендерится так же,
    как до появления этой фичи.
    """
    quotes = getattr(c, "negative_quotes", None) or []
    if not quotes:
        return ""
    e = html.escape
    drafts = drafts or []
    has_drafts = any(drafts)
    items = []
    for i, r in enumerate(quotes):
        rating = int(r.get("rating") or 0)
        stars = "★" * rating + "☆" * (5 - rating)
        text = e(r.get("text") or "(без текста)")
        block = (f'<blockquote>{text}<div class="meta">{stars} · '
                 f'{e(r.get("date") or "")} · без ответа</div>')
        if i < len(drafts) and drafts[i]:
            block += render_draft(drafts[i])
        block += "</blockquote>"
        items.append(block)
        if i == 2 and has_drafts:
            remaining = (c.negative_unanswered or 0) - 2
            if remaining > 0:
                items.append(
                    f'<p class="quote-note">Остальные {remaining} '
                    f'{plural(remaining, "отзыв", "отзыва", "отзывов")} без ответа '
                    f'разбираем в рамках пилота.</p>')
    n = len(quotes)
    title = (f"Что видят клиенты: негатив без ответа "
             f"({n} {plural(n, 'отзыв', 'отзыва', 'отзывов')} "
             f"из последних {metrics.WINDOW})")
    return f"<h2>{e(title)}</h2>" + "\n".join(items)


def render_audit_html(c: Company, cfg, competitor=None, dynamics=None,
                      drafts: list = None) -> str:
    """Разбор карточки. ВСЕ числа приходят готовыми (metrics.company_numbers) —
    в шаблоне ничего не вычисляется, иначе разбор снова разойдётся с таблицей.

    competitor: блок сравнения рисуется, только если конкурент передан явно
    (решение принимает leadgen по флагу SHOW_COMPETITOR).
    drafts: черновики ответов под цитатами — см. build_quote_drafts/render_quotes.
    """
    e = html.escape
    n = metrics.company_numbers(c)   # единственный источник чисел
    name = c.name if c.name != NA else "Компания"

    rating_cls = "bad" if n["rating"] is not None and n["rating"] < 4.5 else "ok"
    unanswered = n["unanswered_count"]
    ratings_line = (f'на основе {metrics.fmt_int(n["ratings_count"])} оценок'
                    if n["ratings_count"] is not None else "оценка 2ГИС")
    metrics_html = f"""
<div class="metrics">
 <div class="metric"><div class="v {rating_cls}">{e(metrics.fmt_rating(n["rating"]))}</div>
   <div class="l">рейтинг 2ГИС<br><span class="tiny">{e(ratings_line)}</span></div></div>
 <div class="metric"><div class="v">{e(metrics.fmt_int(n["reviews_count"]))}</div>
   <div class="l">текстовых отзывов</div></div>
 <div class="metric"><div class="v {'bad' if unanswered else 'ok'}">{e(metrics.fmt_int(unanswered))}</div>
   <div class="l">без ответа<br><span class="tiny">{e(metrics.unanswered_note(c))}</span></div></div>
 <div class="metric"><div class="v {'ok' if n["fresh_count"] else 'bad'}">{e(metrics.fmt_int(n["fresh_count"]))}</div>
   <div class="l">новых за {metrics.FRESH_DAYS} дней</div></div>
</div>"""

    pains = "\n".join(f"<li>{e(b[0].upper() + b[1:])}.</li>"
                      for b in pain_bullets(c, competitor))

    worst = render_quotes(c, drafts)
    has_drafts = 'class="draft"' in worst
    draft_style = f"<style>{_DRAFT_CSS}</style>" if has_drafts else ""
    steps_html = ("<h2>Что можно сделать уже на этой неделе</h2>\n<ol class=\"steps\">"
                  + "".join(f"<li>{step}</li>" for step in build_steps(c, has_drafts))
                  + "</ol>\n" + rating_note(c))

    vs = ""
    if competitor is not None and competitor.rating is not None:
        cn = metrics.company_numbers(competitor)
        vs = (f'<div class="vs"><b>Сравнение с конкурентом рядом.</b> '
              f'«{e(competitor.name)}» — рейтинг {e(metrics.fmt_rating(cn["rating"]))} '
              f'при {e(metrics.fmt_int(cn["reviews_count"]))} отзывах. '
              f'Когда человек выбирает по карте между вами и ними, '
              f'рейтинг — первое, что он сравнивает.</div>')

    contact = e(cfg.sender_contact or cfg.sender_name or "")
    collected = fmt_collected(n["collected_at"])
    scope = metrics.scope_note(c)
    footer = (f'Разбор подготовил {e(cfg.sender_name) or "[Имя]"}'
              + (f" · {contact}" if cfg.sender_contact else "")
              + f'. Данные собраны {e(collected)} — открытая карточка 2ГИС, '
              + f'{e(scope)}.')

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Разбор карточки 2ГИС — {e(name)}</title>
<style>{AUDIT_CSS}</style>{draft_style}</head><body>
<h1>Разбор карточки 2ГИС — {e(name)}</h1>
<div class="sub">{e(c.category)} · {e(cfg.city)} · <a href="{e(c.url)}">карточка в 2ГИС</a>
 · <span class="tiny">данные собраны {e(collected)}</span></div>
{metrics_html}
<h2>Что бросается в глаза</h2>
<ul class="pains">{pains}</ul>
{worst}
{render_dynamics_block(dynamics)}
{vs}
{steps_html}
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
.variant{margin:10px 0}
.variant b{font-size:.9em;color:#555}
pre{white-space:pre-wrap;background:#f4f6fa;border-radius:8px;padding:10px;font-family:inherit;font-size:.92em}
button{cursor:pointer;border:1px solid #ccd;border-radius:6px;background:#fff;padding:4px 10px}
button:hover{background:#eef}
.warn{background:#fff7e0;border:1px solid #f0d080;border-radius:8px;padding:10px;margin-bottom:14px}
"""

# Дашборд открывают и как файл (file://), и из панели по http://<ip>:8765 —
# ни там, ни там navigator.clipboard недоступен (нужен https или localhost),
# а в части браузеров он есть, но writeText отклоняется. Поэтому сначала API,
# при любой осечке — execCommand, и честная подсказка, если не вышло вообще.
_DASH_JS = """
function copyText(text){
  const legacy = () => { const ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.top='-1000px';
    document.body.appendChild(ta); ta.select();
    let ok=false; try { ok = document.execCommand('copy'); } catch(e) { ok=false; }
    ta.remove(); return ok; };
  if (navigator.clipboard && window.isSecureContext)
    return navigator.clipboard.writeText(text).then(() => true).catch(() => legacy());
  return Promise.resolve(legacy());
}
function copyMsg(id, btn){
  const text = document.getElementById(id).innerText;
  copyText(text).then(ok => {
    btn.textContent = ok ? 'Скопировано ✓' : 'Выделите и Ctrl+C';
    setTimeout(() => btn.textContent = 'Копировать', ok ? 1500 : 2500);
  });
}
"""


def render_dashboard_html(rows: list, cfg) -> str:
    """rows: список dict(c, message, wa, audit_file)."""
    e = html.escape
    today = metrics.now_local().strftime("%d.%m.%Y %H:%M")
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
        # все каналы касания рядом: под рукой должен быть тот текст, в котором
        # реально пишешь, а не один «универсальный»
        variants = "".join(
            f'<div class="variant"><b>{e(TOUCH_LABELS[ch])}</b> '
            f'<button onclick="event.preventDefault();copyMsg(\'m{i}_{ch}\',this)">Копировать</button>'
            f'<pre id="m{i}_{ch}">{e(row["messages"][ch])}</pre></div>'
            for ch in TOUCH_CHANNELS)
        trs.append(f"""<tr>
 <td>{e(c.priority)}</td>
 <td><b>{e(c.name)}</b><br><span style="color:#888">{e(c.phone)}</span></td>
 <td>{e(fmt_rating(c.rating))} / {c.reviews_count if c.reviews_count is not None else e(NA)} отз.<br>
     без ответа: {c.unanswered_count if c.unanswered_count is not None else e(NA)}</td>
 <td class="links">{' '.join(links)}</td>
 <td><details><summary>Первое касание ({len(TOUCH_CHANNELS)} варианта)</summary>
     {variants}</details></td>
</tr>""")

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Рассылка: {e(cfg.category)} — {e(cfg.city)}</title>
<style>{_DASH_CSS}</style></head><body>
<h1>Пакет для рассылки — {e(cfg.category)}, {e(cfg.city)}</h1>
<div class="sub">{len(rows)} лидов · собрано {today} · цель первого касания — выйти на
директора (имя и прямой контакт), а не вручить разбор; кнопка WhatsApp открывает чат
с уже подставленным текстом, «аудит» — страница для отправки уже директору
(Ctrl+P → сохранить как PDF)</div>
{warn}
<table>
<tr><th>Приоритет</th><th>Компания</th><th>Рейтинг</th><th>Ссылки</th><th>Первое касание</th></tr>
{''.join(trs)}
</table>
<script>{_DASH_JS}</script>
</body></html>"""


# ---------- строки рассылки ----------

def audit_filename(c: Company, i: int = 0) -> str:
    """Имя файла аудита лида. Общее для пакета на диске и ссылок в веб-панели."""
    return f"audit_{c.firm_id if c.firm_id != NA else i}.html"


def build_rows(companies: list, cfg) -> list:
    """Лиды -> строки рассылки: тексты касаний, ссылка WhatsApp, файл аудита.

    Чистая функция (без диска и сети): по ней строится и пакет в output/,
    и таблица результатов на странице «Поиск лидов» в панели — так обе
    поверхности показывают ровно один и тот же текст.

    messages — все варианты касания ({канал: текст}); message — вариант для
    WhatsApp, он же уходит в ссылку wa.me и в CSV.
    """
    companies = sorted(companies, key=sort_key)
    rows = []
    for i, c in enumerate(companies):
        competitor = pick_benchmark(companies, exclude=c)
        messages = build_touch_messages(c, cfg)
        message = messages["whatsapp"]
        rows.append({"c": c, "competitor": competitor, "message": message,
                     "messages": messages, "wa": wa_link(c.phone, message),
                     "audit_file": audit_filename(c, i)})
    return rows


# ---------- запись пакета ----------

async def write_outreach_pack(companies: list, cfg) -> Path:
    """Собрать пакет рассылки. Возвращает путь к папке пакета."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pack = Path(cfg.output_dir) / f"outreach_{cfg.city_slug}_{cfg.category}_{ts}"
    pack.mkdir(parents=True, exist_ok=True)

    rows = build_rows(companies, cfg)
    for row in rows:
        # конкурент в файле разбора — только при включённом флаге (как и в панели)
        competitor = row["competitor"] if AUDIT_COMPETITOR else None
        drafts = await build_quote_drafts(row["c"])
        (pack / row["audit_file"]).write_text(
            render_audit_html(row["c"], cfg, competitor=competitor, drafts=drafts),
            encoding="utf-8")

    (pack / "index.html").write_text(render_dashboard_html(rows, cfg), encoding="utf-8")

    with open(pack / "outreach.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Название", "Приоритет", "Телефон", "WhatsApp"]
                   + [TOUCH_LABELS[ch] for ch in TOUCH_CHANNELS])
        for row in rows:
            w.writerow([row["c"].name, row["c"].priority, row["c"].phone,
                        row["wa"] or NA]
                       + [row["messages"][ch] for ch in TOUCH_CHANNELS])
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
                   ratings_count=310, reviews_count=120, branches_count=2,
                   has_fresh_reviews=False, owner_responds=False,
                   negative_without_response=True, phone="+7 (727) 123-45-67",
                   website="https://instagram.com/aruna_dent", url="https://2gis.kz/x",
                   loaded_reviews=50, unanswered_count=14, negative_unanswered=3,
                   negative_count=5, fresh_count=0, last_review_date="2026-05-01",
                   unanswered_fresh=2,
                   collected_at="2026-09-01T07:28:16+00:00",
                   negative_quotes=[{"rating": 1, "date": "2026-05-01",
                                     "text": "Ужасно <б>"}])
    lead.compute_priority()
    top = Company(firm_id="2", name="Dent Lux", rating=4.8, reviews_count=300)

    checks = []

    msg = build_touch_message(lead, cfg)
    touch = build_touch_messages(lead, cfg)

    # --- первое касание: задача — выйти на директора, а не вручить разбор ---
    checks.append(("касание: варианты под все каналы",
                   set(touch) == set(TOUCH_CHANNELS) and all(touch.values())))
    checks.append(("касание: WhatsApp — это whatsapp-вариант",
                   touch["whatsapp"] == msg))
    checks.append(("касание: наблюдение по их карточке, а не про нас",
                   all("3 негативных отзыва без ответа" in t for t in touch.values())))
    checks.append(("касание: заканчивается вопросом про директора",
                   all(t.rstrip().endswith("?") for t in touch.values())))
    checks.append(("касание: во всех вариантах спрашиваем про директора",
                   all("директор" in t.lower() for t in touch.values())))
    checks.append(("касание: не начинается с оффера «бесплатный разбор»",
                   all("бесплатн" not in t.lower() for t in touch.values())
                   and all("разбор" not in t.lower() for t in touch.values())))
    checks.append(("касание: без длинных тире",
                   all("—" not in t and "–" not in t for t in touch.values())))
    checks.append(("касание: коротко — не больше 4 предложений",
                   all(t.count(".") + t.count("?") + t.count("!") <= 5
                       for t in touch.values())))
    checks.append(("касание: администратора не просим передать, просим контакт",
                   "Передавать ничего не нужно" in touch["admin"]
                   and "прямой номер" in touch["admin"]))
    checks.append(("касание: в Instagram не представляемся по второму разу",
                   "Меня зовут" not in touch["instagram"]))
    checks.append(("касание: мониторинг клиенту не продаётся",
                   all("мониторинг" not in t.lower() and "узнаете" not in t.lower()
                       and "узнаёте" not in t.lower() for t in touch.values())))
    checks.append(("имя отправителя", "Эмиль" in msg))
    checks.append(("касание: город склоняется, а не «в Костанай»",
                   city_prepositional("Костанай") == "в Костанае"
                   and city_prepositional("Астана") == "в Астане"
                   and city_prepositional("Шымкент") == "в Шымкенте"
                   and city_prepositional("Рудный") == "в Рудном"))
    checks.append(("касание: несклоняемые города не ломаем",
                   city_prepositional("Алматы") == "в Алматы"
                   and city_prepositional("Актобе") == "в Актобе"
                   and city_prepositional("Атырау") == "в Атырау"))
    checks.append(("касание: города нет -> фраза не разваливается",
                   city_prepositional("") == ""
                   and "на 2ГИС. Открыл карточку" in build_touch_message(
                       lead, type("C", (), {"city": "", "sender_name": "Э"})())))
    checks.append(("склонение", plural(1, "а", "б", "в") == "а"
                   and plural(3, "а", "б", "в") == "б"
                   and plural(11, "а", "б", "в") == "в"
                   and plural(21, "а", "б", "в") == "а"))
    lead1 = Company(unanswered_count=1, negative_unanswered=1, loaded_reviews=50)
    checks.append(("склонение n=1 в касании",
                   "1 негативный отзыв без ответа" in build_touch_message(lead1, cfg)))
    lead_no_neg = Company(unanswered_count=9, negative_unanswered=0, loaded_reviews=50)
    checks.append(("наблюдение: нет негатива -> весь долг по ответам",
                   touch_observation(lead_no_neg) == "9 отзывов без ответа из последних 50"))
    checks.append(("наблюдение: нечего сказать -> без выдуманных чисел",
                   touch_observation(Company()) == "есть пара вещей, которые бросаются в глаза"))

    cfg_noname = _Cfg(); cfg_noname.sender_name = ""
    checks.append(("заглушка имени", "[Имя]" in build_touch_message(lead, cfg_noname)))

    wa = wa_link(lead.phone, msg)
    checks.append(("wa.me из +7(727)…", wa is not None and "/77271234567?" in wa))
    checks.append(("wa.me из 8-кода", phone_digits("8 701 555-44-33") == "77015554433"))
    checks.append(("wa.me нет телефона", wa_link(NA) is None))

    audit = render_audit_html(lead, cfg, competitor=top)
    checks.append(("аудит: имя и конкурент", "Аруна" in audit and "Dent Lux" in audit))
    checks.append(("аудит: HTML экранирован", "<б>" not in audit and "&lt;б&gt;" in audit))

    # --- дата сбора, а не дата генерации файла ---
    checks.append(("аудит: в подвале дата СБОРА данных",
                   "данные собраны" in audit.lower() and "01.09.2026" in audit))
    checks.append(("аудит: подписано окно счётчиков",
                   f"последних {metrics.WINDOW}" in audit))
    checks.append(("аудит: подпись «без ответа» — число сразу в начале, без двусмысленности",
                   "14 без ответа из последних" in audit
                   and "3 из них — негативные" in audit
                   and "2 — за последние 30 дней" in audit
                   and "Всего у компании 120 отзывов." in audit))

    # --- БАГ 1: цитаты только без ответа ---
    answered_only = Company(name="Всё отвечено", rating=4.0, reviews_count=100,
                            loaded_reviews=50, unanswered_count=0,
                            negative_unanswered=0, negative_quotes=[],
                            collected_at="2026-09-01T07:00:00+00:00")
    html_ans = render_audit_html(answered_only, cfg)
    checks.append(("цитаты: нет негатива без ответа -> блока нет",
                   "негатив без ответа" not in html_ans.lower()))
    checks.append(("шаги: без негатива без ответа шаг «закрыть негатив» убран",
                   "Закрыть накопленный негатив" not in html_ans))
    checks.append(("шаги: второй шаг на месте",
                   "Не копить дальше" in html_ans))

    checks.append(("цитаты: есть негатив без ответа -> шаг «закрыть негатив» есть",
                   "Закрыть накопленный негатив" in audit))
    checks.append(("цитаты: метки «есть ответ владельца» в блоке нет",
                   "есть ответ владельца" not in audit))
    checks.append(("цитаты: каждая помечена «без ответа»",
                   audit.count("· без ответа</div>") == 1))

    many = Company(name="Много", rating=4.0, reviews_count=100, loaded_reviews=50,
                   unanswered_count=9, negative_unanswered=2,
                   collected_at="2026-09-01T07:00:00+00:00",
                   negative_quotes=[{"rating": 1, "date": "2026-08-30", "text": "раз"},
                                    {"rating": 2, "date": "2026-08-20", "text": "два"}])
    html_many = render_audit_html(many, cfg)
    checks.append(("цитаты: в заголовке — сколько их и из какого окна",
                   "2 отзыва" in html_many and f"из последних {metrics.WINDOW}" in html_many))

    # --- черновики ответов под цитатами (DRAFT_REPLIES) ---
    draft1 = {"claim_type": "service", "reply": "Спасибо, разберём с администратором.",
             "why": "Признаём проблему, не спорим."}
    draft2 = {"claim_type": "treatment", "reply": "Сожалеем, напишите нам напрямую.",
             "why": "Общая формула, без деталей визита."}
    many3 = Company(name="Много", rating=4.0, reviews_count=100, loaded_reviews=50,
                    unanswered_count=9, negative_unanswered=9,
                    collected_at="2026-09-01T07:00:00+00:00",
                    negative_quotes=[{"rating": 1, "date": "2026-08-30", "text": "раз"},
                                     {"rating": 2, "date": "2026-08-20", "text": "два"},
                                     {"rating": 1, "date": "2026-08-10", "text": "три"}])

    html_no_drafts = render_audit_html(many3, cfg)
    checks.append(("черновики: drafts не передан -> документ как раньше, ни следа фичи",
                   "class=\"draft\"" not in html_no_drafts
                   and "Как бы ответили" not in html_no_drafts
                   and "разбираем в рамках пилота" not in html_no_drafts))
    checks.append(("черновики: пустой список drafts=[] -> тот же результат, что и None",
                   render_audit_html(many3, cfg, drafts=[]) == html_no_drafts))

    html_drafts = render_audit_html(many3, cfg, drafts=[draft1, draft2])
    checks.append(("черновики: под первой и второй цитатой ровно два блока",
                   html_drafts.count("Как бы ответили мы:") == 2))
    checks.append(("черновики: текст черновика на месте",
                   "Спасибо, разберём с администратором." in html_drafts))
    checks.append(("черновики: пояснение — курсивом",
                   "<i>Признаём проблему, не спорим.</i>" in html_drafts))
    checks.append(("черновики: под третьей цитатой черновика нет, но есть «остальные N»",
                   "Остальные 7 отзывов без ответа разбираем в рамках пилота." in html_drafts))

    # меньше 3 цитат вовсе -> «остальные» упоминать нечего, строки нет
    html_2q = render_audit_html(lead, cfg, drafts=[draft1])  # у lead всего 1 цитата
    checks.append(("черновики: меньше 3 цитат -> строки «остальные» нет",
                   "разбираем в рамках пилота" not in html_2q))

    # частичный сбой: черновик для второй цитаты не пришёл (None)
    html_partial = render_audit_html(many3, cfg, drafts=[draft1, None])
    checks.append(("черновики: частичный сбой — один блок черновика, не два",
                   html_partial.count("Как бы ответили мы:") == 1))
    checks.append(("черновики: частичный сбой всё равно показывает «остальные N»",
                   "разбираем в рамках пилота" in html_partial))

    # полный сбой генерации (все None) -> будто черновиков не заказывали вовсе
    html_all_none = render_audit_html(many3, cfg, drafts=[None, None])
    checks.append(("черновики: полный сбой -> ни блоков, ни строки «остальные»",
                   "Как бы ответили" not in html_all_none
                   and "разбираем в рамках пилота" not in html_all_none))

    import asyncio

    async def _fake_batch(company_name, category, quotes, api_key, model="", log=print):
        return [draft1, draft2][:min(len(quotes), 2)]

    _orig_batch = ai_reply.draft_audit_replies
    _orig_flag = DRAFT_REPLIES
    _saved_key = os.environ.get("OPENAI_API_KEY")
    try:
        ai_reply.draft_audit_replies = _fake_batch

        globals()["DRAFT_REPLIES"] = False
        os.environ["OPENAI_API_KEY"] = "sk-test"
        checks.append(("build_quote_drafts: DRAFT_REPLIES=0 -> [] без похода в ai_reply",
                       asyncio.run(build_quote_drafts(many3)) == []))

        globals()["DRAFT_REPLIES"] = True
        os.environ.pop("OPENAI_API_KEY", None)
        checks.append(("build_quote_drafts: DRAFT_REPLIES=1, но нет ключа -> []",
                       asyncio.run(build_quote_drafts(many3)) == []))

        os.environ["OPENAI_API_KEY"] = "sk-test"
        checks.append(("build_quote_drafts: флаг включён и ключ есть -> зовёт ai_reply",
                       asyncio.run(build_quote_drafts(many3)) == [draft1, draft2]))
        checks.append(("build_quote_drafts: нет цитат -> [] без похода в ai_reply",
                       asyncio.run(build_quote_drafts(answered_only)) == []))
    finally:
        ai_reply.draft_audit_replies = _orig_batch
        globals()["DRAFT_REPLIES"] = _orig_flag
        if _saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = _saved_key

    # --- БАГ 2: одно число в шапке и в тексте ---
    head = re.search(r'<div class="v[^"]*">(\d+)</div>\s*<div class="l">без ответа',
                     audit)
    checks.append(("шапка: число без ответа найдено", head is not None))
    checks.append(("шапка == текст: одно и то же число без ответа",
                   head is not None and head.group(1) == "14"
                   and f"{head.group(1)} отзывов остались без ответа" in audit))
    checks.append(("текст: negative_unanswered не выдаётся за «без ответа»",
                   "3 отзыва остались без ответа" not in audit))

    # шаги строятся отдельной функцией — её можно проверить в лоб
    checks.append(("build_steps: без цитат — один шаг",
                   len(build_steps(answered_only)) == 1))
    checks.append(("build_steps: с цитатами — два шага",
                   len(build_steps(lead)) == 2))
    checks.append(("build_steps: мониторинг и сбор отзывов из шагов убраны",
                   all("мониторинг" not in s.lower() and "QR" not in s
                       and "поток отзывов" not in s.lower()
                       for s in build_steps(lead, True))))
    checks.append(("build_steps: есть черновики -> шаг ссылается на них",
                   "Готовые ответы — выше" in build_steps(lead, True)[0]))
    checks.append(("build_steps: черновиков нет -> обещания «ответы в разборе» нет",
                   "Готовые ответы — выше" not in build_steps(lead, False)[0]))

    # --- честность про рейтинг: сроков роста не обещаем ---
    checks.append(("рейтинг: обещания «растёт за 1–2 месяца» больше нет",
                   "1–2 месяца" not in audit and "растёт" not in audit))
    checks.append(("рейтинг: честная приписка с реальной базой оценок",
                   "Про рейтинг честно" in audit
                   and "при 310 накопленных оценках" in audit
                   and "приток новых оценок" in audit))
    checks.append(("рейтинг: базы оценок нет -> формулировка без числа",
                   "при накопленной базе оценок" in render_audit_html(
                       Company(name="Без базы", rating=4.0, reviews_count=10,
                               collected_at="2026-09-01T07:00:00+00:00"), cfg)))
    checks.append(("разбор: мониторинг клиенту не продаётся",
                   "мониторинг" not in audit.lower()
                   and "узнаёте" not in audit and "узнаете" not in audit))
    checks.append(("аудит: оценки и отзывы разделены",
                   "на основе 310 оценок" in audit and "текстовых отзывов" in audit))
    checks.append(("аудит: сказано, что данные по одному филиалу",
                   "филиалу из 2" in audit))
    checks.append(("аудит: рейтинг не пересчитан, показан как есть",
                   ">3,9<" in audit.replace(" ", "").replace("\n", "")
                   or "3,9" in audit))

    # --- конкурент только по явной передаче ---
    no_vs = render_audit_html(lead, cfg)
    checks.append(("аудит: без конкурента блока сравнения нет",
                   "Сравнение с конкурентом" not in no_vs))
    checks.append(("аудит: с конкурентом блок есть",
                   "Сравнение с конкурентом" in audit))

    # --- динамика ---
    dyn = {"since": "2026-07-27T22:54:00+00:00", "until": "2026-09-01T07:28:16+00:00",
           "changes": [{"label": "рейтинг", "was": 4.8, "now": 5.0, "delta": 0.2},
                       {"label": "отзывов без ответа", "was": 9, "now": 10, "delta": 1}]}
    with_dyn = render_audit_html(lead, cfg, dynamics=dyn)
    checks.append(("динамика: блок отрисован с датой прошлого замера",
                   "Что изменилось" in with_dyn
                   and fmt_collected(dyn["since"]) in with_dyn))
    checks.append(("динамика: было -> стало",
                   "4,8" in with_dyn and "5,0" in with_dyn))
    checks.append(("динамика: нет истории -> блока нет",
                   "Что изменилось" not in render_audit_html(lead, cfg, dynamics=None)))
    checks.append(("динамика: пустой список изменений -> блока нет",
                   render_dynamics_block({"since": "x", "changes": []}) == ""))

    checks.append(("дата сбора: пусто -> честная подпись",
                   fmt_collected(None) == "дата неизвестна"))

    dash = render_dashboard_html(
        [{"c": lead, "message": msg, "messages": touch, "wa": wa,
          "audit_file": "audit_1.html"}], cfg)
    checks.append(("дашборд: ссылки", "audit_1.html" in dash and "wa.me" in dash))
    checks.append(("дашборд: instagram", "instagram.com/aruna_dent" in dash))
    checks.append(("дашборд: все три варианта касания с кнопкой копирования",
                   all(TOUCH_LABELS[ch] in dash and f"m0_{ch}" in dash
                       for ch in TOUCH_CHANNELS)))

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
    checks.append(("строки: все варианты касания, message — вариант WhatsApp",
                   set(rows[0]["messages"]) == set(TOUCH_CHANNELS)
                   and rows[0]["messages"]["whatsapp"] == rows[0]["message"]))
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
