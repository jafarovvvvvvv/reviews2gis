"""
AI-черновики ответов на отзывы (этап 1 ROADMAP).

Генерация через OpenAI API голым HTTPS (без SDK — в духе tg.py). Ключ и модель:
настройки openai_api_key/openai_model в БД (панель) либо .env OPENAI_API_KEY /
OPENAI_MODEL (по умолчанию gpt-5.4-mini).

Правила ответов 2ГИС зашиты в системный промпт (по официальной инструкции
help.2gis: вежливо, без рекламы/конкурентов/споров, персонализация, контакт,
публичное решение проблемы). Язык ответа = язык отзыва (ru/kk — детект по
казахским буквам). Автопубликации нет — владелец копирует ответ в кабинет сам.

Деградация: нет ключа -> generate_replies возвращает [] и алерт остаётся со
статичным шаблоном; ошибка API -> AiReplyError (вызывающий решает, глушить ли).
Чистые функции — без сети, самотесты: python ai_reply.py
"""
import json
import os
import re
from typing import Optional

import httpx
from dotenv import load_dotenv

import db

load_dotenv()  # OPENAI_API_KEY/OPENAI_MODEL доступны и вне app.py (бот, скрипты)

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.4-mini"
MAX_VARIANTS = 2

# буквы, встречающиеся в казахском и отсутствующие в русском
_KK_LETTERS = set("әғқңөұүһі")

SYSTEM_PROMPT = """Ты — специалист по работе с отзывами локального бизнеса в Казахстане.
Готовишь черновики ПУБЛИЧНЫХ ответов официального представителя компании на отзыв в 2ГИС.

Строго соблюдай правила площадки 2ГИС:
- вежливо и спокойно; никаких оскорблений, сарказма, споров с автором и оправдательной перепалки;
- никакой рекламы, акций, промокодов и упоминаний конкурентов;
- персонализируй ответ по содержанию отзыва — шаблонная отписка недопустима;
- не обещай компенсаций, которых компания не давала;
- не выдумывай факты и имена; пиши 2–4 предложения, без капса, максимум один уместный эмодзи.

Разнообразие — критично. Компания публикует такие ответы под отзывами каждый день,
и если все они построены одинаково ("нам жаль... разберёмся... свяжитесь с нами"),
любой читатель 2ГИС сразу узнаёт шаблон бота, а не живого человека. Поэтому:
- НЕ начинай ответ с "нам жаль" или "приносим извинения" в обоих вариантах сразу —
  как минимум один из двух должен открываться иначе: благодарностью за отзыв,
  реакцией на конкретную деталь из текста, прямым обращением по имени автора,
  констатацией факта из отзыва — без вводного извинения;
- строй структуру и порядок мыслей под суть жалобы, а не по единому шаблону:
  * жалоба на оплату/цену/чек — уточни, что разберёшь именно расчёт/счёт, без общих
    фраз и без обещания скидок или возврата;
  * жалоба на сроки/ожидание/опоздание — признай именно потерю времени клиента,
    не подменяй это общим "нам жаль за неудобства";
  * жалоба на качество услуги или товара — отвечай на конкретный недостаток, который
    назвал автор, а не абстрактным "постараемся стать лучше";
  * жалоба на грубость или хамство персонала — самый чувствительный случай: признай
    недопустимость тона без оправданий сотрудника, предложи разобраться в ситуации
    лично, не растворяй это в общей фразе;
  * позитивный отзыв — благодари за ту деталь, которую отметил автор, не используй
    одну и ту же фразу-клише в начале каждый раз;
- если уместно предложить связаться напрямую — делай это разными словами и не в
  каждом ответе одинаковой конструкцией ("напишите нам /свяжитесь с нами /
  будем рады обсудить лично" — не повторяй один и тот же оборот из раза в раз);
- два варианта в одном ответе должны различаться не только словами, но и структурой:
  разный порядок мыслей, разная длина, разное открытие — представь, что их написали
  два разных сотрудника, а не один бот дважды.

Ответ пиши на языке: {lang_name}.
Верни строго JSON без пояснений: {{"replies": ["вариант 1", "вариант 2"]}} —
два разных по формулировке и структуре варианта."""


class AiReplyError(Exception):
    """Ошибка генерации (текст — для показа админу)."""


def detect_lang(text: str) -> str:
    """'kk' если в тексте есть казахские буквы (≥2), иначе 'ru'."""
    hits = sum(1 for ch in (text or "").lower() if ch in _KK_LETTERS)
    return "kk" if hits >= 2 else "ru"


def get_ai_settings(conn) -> tuple:
    """(api_key, model) из настроек БД с фолбэком на переменные окружения."""
    key = db.get_setting(conn, "openai_api_key") or os.getenv("OPENAI_API_KEY", "")
    model = (db.get_setting(conn, "openai_model") or os.getenv("OPENAI_MODEL", "")
             or DEFAULT_MODEL)
    return key.strip(), model.strip()


def build_messages(company_name: str, review: dict, tone: str = "",
                   signature: str = "", category: str = "") -> list:
    """Сообщения для chat-completions. Чистая функция."""
    text = (review.get("text") or "").strip() or "(отзыв без текста, только оценка)"
    lang = detect_lang(text)
    lang_name = "казахский" if lang == "kk" else "русский"
    rating = review.get("rating")
    rating_s = f"{rating}/5" if rating is not None else "без оценки"
    author = review.get("author") or "клиент"

    user = (f"Компания: «{company_name}»"
            + (f", сфера: {category}" if category else "") + ".\n"
            f"Тон бренда: {tone or 'нейтрально-доброжелательный'}.\n"
            + (f"Подпись/контакт для связи: {signature} — аккуратно используй в конце ответа.\n"
               if signature else "")
            + f"Отзыв (оценка {rating_s}, автор: {author}):\n«{text}»")
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(lang_name=lang_name)},
        {"role": "user", "content": user},
    ]


def parse_replies(content: str) -> list:
    """Достать варианты из ответа модели. Терпит код-фенсы и мусор вокруг JSON."""
    content = (content or "").strip()
    if not content:
        return []
    m = re.search(r"\{.*\}", content, re.S)  # JSON может быть обёрнут в ```…```
    if m:
        try:
            data = json.loads(m.group(0))
            replies = [str(r).strip() for r in data.get("replies", []) if str(r).strip()]
            if replies:
                return replies[:MAX_VARIANTS]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    # модель ответила просто текстом — используем как единственный вариант
    return [content[:900]]


async def _chat_completion(payload: dict, api_key: str, timeout: float = 90) -> str:
    """POST на /chat/completions -> content первого варианта. Единственное место
    в проекте, которое реально ходит в OpenAI — все функции этого модуля вызывают
    только этот метод, свой HTTP-клиент никто не пишет."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(API_URL, json=payload,
                                     headers={"Authorization": f"Bearer {api_key}"})
            data = resp.json()
    except Exception as e:
        raise AiReplyError(f"OpenAI недоступен: {e}") from e
    if resp.status_code != 200:
        err = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise AiReplyError(f"OpenAI: {err[:200]}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise AiReplyError(f"OpenAI: неожиданный формат ответа ({e})") from e


async def generate_replies(company_name: str, review: dict, tone: str = "",
                           signature: str = "", category: str = "",
                           api_key: str = "", model: str = "") -> list:
    """0–2 варианта ответа. Пустой список — ключ не задан (тихая деградация)."""
    if not api_key:
        return []
    messages = build_messages(company_name, review, tone, signature, category)
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        # лимит с запасом: у reasoning-моделей сюда входят и токены размышлений
        "max_completion_tokens": 2000,
    }
    content = await _chat_completion(payload, api_key)
    return parse_replies(content)


MAX_AUDIT_DRAFTS = 2  # разбор показывает готовые черновики только под первыми N цитатами

_VALID_CLAIM_TYPES = {"service", "treatment"}

AUDIT_DRAFT_PROMPT = """Ты помогаешь медицинской клинике в Казахстане подготовить черновик
ПУБЛИЧНОГО ответа на негативный отзыв в 2ГИС для бесплатного разбора карточки —
это демонстрация качества нашей работы владельцу клиники, он сам решает, публиковать
ли этот текст.

ШАГ 1 — классификация претензии (поле claim_type):
- "service" — жалоба на сервис: очередь, запись, регистратура, ресепшн, цены, счета,
  график работы, обстановка;
- "treatment" — жалоба касается лечения, качества медицинской помощи, конкретного
  врача, диагноза или хода приёма.
Если сомневаешься — выбирай "treatment": это более осторожный вариант.

ШАГ 2 — черновик ответа (поле reply). Правила действуют всегда:
- 2–4 предложения, не больше;
- от лица клиники, обращение на «вы», спокойный тон, без канцелярита и штампов;
- признай проблему, не спорь с автором отзыва, не оправдывайся;
- предложи продолжить разговор в личном канале (WhatsApp или по телефону), а не
  в комментариях;
- никогда не обещай компенсаций, скидок или возвратов;
- не используй имя автора отзыва и не пересказывай детали визита, которые он описал.

Если claim_type == "treatment" — ДОПОЛНИТЕЛЬНОЕ ограничение (врачебная тайна,
это критично и важнее полезности ответа):
- отвечай только общей формулой: сожалеем, просим написать или позвонить напрямую,
  разберём ситуацию индивидуально;
- НИКОГДА не подтверждай и не опровергай, что автор отзыва — пациент клиники;
- НИКОГДА не упоминай диагноз, лечение, врача, симптомы или ход приёма — ни чтобы
  подтвердить, ни чтобы опровергнуть;
- не признавай вину и не описывай, что именно пошло не так в лечении.

Ответ пиши на языке отзыва: {lang_name}.

Верни строго JSON без пояснений:
{{"claim_type": "service" или "treatment",
  "reply": "черновик ответа на языке {lang_name}, 2-4 предложения",
  "why": "одна короткая фраза по-русски — почему такой ответ работает"}}"""


def build_draft_messages(company_name: str, category: str, quote: dict) -> list:
    """Сообщения для классификации+черновика по одной цитате {rating,date,text}."""
    text = (quote.get("text") or "").strip() or "(отзыв без текста, только оценка)"
    lang = detect_lang(text)
    lang_name = "казахский" if lang == "kk" else "русский"
    rating = quote.get("rating")
    rating_s = f"{rating}/5" if rating is not None else "без оценки"
    user = (f"Клиника: «{company_name}»" + (f", профиль: {category}" if category else "")
            + f".\nНегативный отзыв на 2ГИС (оценка {rating_s}):\n«{text}»")
    return [
        {"role": "system", "content": AUDIT_DRAFT_PROMPT.format(lang_name=lang_name)},
        {"role": "user", "content": user},
    ]


def parse_draft(content: str) -> Optional[dict]:
    """{claim_type, reply, why} из ответа модели, либо None — реплики не нашлось.

    claim_type неизвестен/битый -> 'treatment': безопасный дефолт, если модель не
    вернула валидную классификацию, публичный ответ не должен становиться менее
    осторожным."""
    content = (content or "").strip()
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    claim_type = str(data.get("claim_type") or "").strip().lower()
    if claim_type not in _VALID_CLAIM_TYPES:
        claim_type = "treatment"
    why = str(data.get("why") or "").strip()
    return {"claim_type": claim_type, "reply": reply[:600], "why": why[:200]}


async def draft_public_reply(company_name: str, category: str, quote: dict,
                             api_key: str, model: str = "") -> Optional[dict]:
    """Классификация + черновик ответа на одну цитату разбора.

    None — ключа нет или модель не вернула пригодный JSON (вызывающий решает,
    считать ли это ошибкой). Сетевые/HTTP-сбои бросают AiReplyError."""
    if not api_key:
        return None
    messages = build_draft_messages(company_name, category, quote)
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 700,
    }
    content = await _chat_completion(payload, api_key, timeout=60)
    return parse_draft(content)


async def draft_audit_replies(company_name: str, category: str, quotes: list,
                              api_key: str, model: str = "", log=print) -> list:
    """До MAX_AUDIT_DRAFTS черновиков для первых цитат разбора.

    По каждой цитате — свой try/except: одна неудача не должна тушить остальные.
    Полный сбой (нет ключа, недоступен API) -> [], разбор рендерится без
    черновиков, как раньше. Совпадает по духу с leadgen.push_to_crm."""
    drafts = []
    for q in quotes[:MAX_AUDIT_DRAFTS]:
        try:
            d = await draft_public_reply(company_name, category, q, api_key, model)
        except AiReplyError as e:
            log(f"  [warn] черновик ответа не получен ({company_name}): {e}")
            d = None
        drafts.append(d)
    return drafts


async def generate_for_company(conn, company_row, review: dict) -> list:
    """Удобная обёртка: настройки из БД + поля тона из карточки компании."""
    api_key, model = get_ai_settings(conn)
    keys = company_row.keys() if hasattr(company_row, "keys") else []
    return await generate_replies(
        company_name=company_row["name"],
        review=review,
        tone=company_row["tone"] if "tone" in keys else "",
        signature=company_row["reply_signature"] if "reply_signature" in keys else "",
        category=company_row["category"] if "category" in keys else "",
        api_key=api_key, model=model)


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []

    checks.append(("lang: русский", detect_lang("Очень плохо, не рекомендую") == "ru"))
    checks.append(("lang: казахский", detect_lang("Өте жақсы дәрігер, рахмет!") == "kk"))
    checks.append(("lang: пусто -> ru", detect_lang("") == "ru"))
    checks.append(("lang: одна буква -> ru", detect_lang("сәлем по-русски") == "ru"))

    msgs = build_messages("Клиника Аруна", {"rating": 1, "author": "Айка",
                                            "text": "Хамят на ресепшн"},
                          tone="тёплый", signature="WhatsApp +7 700 000 00 00",
                          category="стоматология")
    sys_c, user_c = msgs[0]["content"], msgs[1]["content"]
    checks.append(("промпт: правила 2ГИС", "2ГИС" in sys_c and "конкурент" in sys_c))
    checks.append(("промпт: язык русский", "русский" in sys_c))
    checks.append(("промпт: JSON-формат", '"replies"' in sys_c))
    checks.append(("промпт: компания/тон/подпись",
                   "Аруна" in user_c and "тёплый" in user_c and "WhatsApp" in user_c))
    checks.append(("промпт: текст отзыва", "Хамят" in user_c))

    kk = build_messages("X", {"text": "Дәрігер өте жақсы, рахмет үлкен!"})
    checks.append(("промпт: казахский детект", "казахский" in kk[0]["content"]))
    no_text = build_messages("X", {"rating": 2, "text": ""})
    checks.append(("промпт: отзыв без текста", "без текста" in no_text[1]["content"]))

    checks.append(("parse: чистый JSON",
                   parse_replies('{"replies": ["Ответ один", "Ответ два"]}')
                   == ["Ответ один", "Ответ два"]))
    checks.append(("parse: код-фенс",
                   parse_replies('```json\n{"replies": ["A", "B"]}\n```') == ["A", "B"]))
    checks.append(("parse: обрезка до 2",
                   len(parse_replies('{"replies": ["1", "2", "3"]}')) == 2))
    checks.append(("parse: голый текст -> 1 вариант",
                   parse_replies("Просто текст ответа") == ["Просто текст ответа"]))
    checks.append(("parse: пусто", parse_replies("") == []))
    checks.append(("parse: битый JSON -> текст",
                   parse_replies('{"replies": [не json') == ['{"replies": [не json']))

    # ---- разбор: черновики под цитатами негатива ----
    dmsgs = build_draft_messages("Клиника Аруна", "стоматология",
                                 {"rating": 1, "date": "2026-08-01", "text": "Хамят на ресепшн"})
    dsys, duser = dmsgs[0]["content"], dmsgs[1]["content"]
    checks.append(("draft-промпт: врачебная тайна упомянута", "врачебная тайна" in dsys))
    checks.append(("draft-промпт: claim_type service/treatment",
                   '"service"' in dsys and '"treatment"' in dsys))
    checks.append(("draft-промпт: без компенсаций и без имени автора",
                   "компенсац" in dsys and "имя автора" in dsys))
    checks.append(("draft-промпт: неуверенность -> treatment (безопасный дефолт)",
                   "выбирай \"treatment\"" in dsys))
    checks.append(("draft-промпт: компания и текст цитаты в user",
                   "Аруна" in duser and "Хамят" in duser))
    checks.append(("draft-промпт: язык казахский определяется так же, как в build_messages",
                   "казахский" in build_draft_messages(
                       "X", "", {"text": "Дәрігер өте жақсы, рахмет үлкен!"})[0]["content"]))

    checks.append(("parse_draft: чистый JSON",
                   parse_draft('{"claim_type": "service", "reply": "Ответ", "why": "коротко"}')
                   == {"claim_type": "service", "reply": "Ответ", "why": "коротко"}))
    checks.append(("parse_draft: неизвестный claim_type -> безопасный дефолт treatment",
                   parse_draft('{"claim_type": "хз", "reply": "Ответ"}')["claim_type"]
                   == "treatment"))
    checks.append(("parse_draft: claim_type отсутствует -> treatment",
                   parse_draft('{"reply": "Ответ"}')["claim_type"] == "treatment"))
    checks.append(("parse_draft: нет reply -> None",
                   parse_draft('{"claim_type": "service"}') is None))
    checks.append(("parse_draft: пусто -> None", parse_draft("") is None))
    checks.append(("parse_draft: битый JSON -> None", parse_draft("не json вовсе") is None))
    checks.append(("parse_draft: код-фенс",
                   parse_draft('```json\n{"claim_type":"service","reply":"X"}\n```')
                   == {"claim_type": "service", "reply": "X", "why": ""}))

    import asyncio

    async def _fake_draft_ok(company_name, category, quote, api_key, model=""):
        return {"claim_type": "service", "reply": f"ответ на: {quote['text']}", "why": "коротко"}

    async def _fake_draft_boom(company_name, category, quote, api_key, model=""):
        raise AiReplyError("таймаут")

    _orig_draft_public_reply = draft_public_reply
    quotes3 = [{"rating": 1, "text": "раз"}, {"rating": 2, "text": "два"},
              {"rating": 1, "text": "три"}]

    globals()["draft_public_reply"] = _fake_draft_ok
    drafts_ok = asyncio.run(draft_audit_replies("Аруна", "", quotes3, "sk-test"))
    checks.append(("draft_audit_replies: не больше MAX_AUDIT_DRAFTS",
                   len(drafts_ok) == MAX_AUDIT_DRAFTS))
    checks.append(("draft_audit_replies: только первые цитаты",
                   drafts_ok[0]["reply"] == "ответ на: раз"
                   and drafts_ok[1]["reply"] == "ответ на: два"))

    globals()["draft_public_reply"] = _fake_draft_boom
    logs = []
    drafts_fail = asyncio.run(draft_audit_replies("Аруна", "", quotes3, "sk-test",
                                                   log=logs.append))
    checks.append(("draft_audit_replies: сбой API -> None вместо падения",
                   drafts_fail == [None, None]))
    checks.append(("draft_audit_replies: сбой залогирован как [warn]",
                   len(logs) == 2 and all("[warn]" in l for l in logs)))
    globals()["draft_public_reply"] = _orig_draft_public_reply

    checks.append(("draft_public_reply: без ключа -> None",
                   asyncio.run(draft_public_reply("X", "", {"text": "т"}, "")) is None))
    checks.append(("draft_audit_replies: пустой список цитат -> пустой список",
                   asyncio.run(draft_audit_replies("X", "", [], "sk-test")) == []))

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
