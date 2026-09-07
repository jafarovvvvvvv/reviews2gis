"""Конфигурация: чтение .env + аргументов CLI, перевод города в slug 2GIS."""
import argparse
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Название сервиса — одно на проект: им подписаны панель, кабинет клиента и
# отчёты. Раньше константа жила только в app.py, и отчёт, собранный по
# расписанию (monitor.py), уходил без подписи, а собранный кнопкой — с ней.
BRAND = os.getenv("BRAND") or "Bedel"

# Города Казахстана -> slug в URL 2GIS (2gis.kz/<slug>/...)
CITY_SLUGS = {
    "алматы": "almaty", "астана": "astana", "нур-султан": "astana", "нурсултан": "astana",
    "шымкент": "shymkent", "караганда": "karaganda", "актобе": "aktobe", "тараз": "taraz",
    "павлодар": "pavlodar", "усть-каменогорск": "ust-kamenogorsk", "семей": "semey",
    "атырау": "atyrau", "костанай": "kostanay", "кызылорда": "kyzylorda", "уральск": "uralsk",
    "петропавловск": "petropavl", "актау": "aktau", "темиртау": "temirtau",
    "туркестан": "turkestan", "кокшетау": "kokshetau", "талдыкорган": "taldykorgan",
    "экибастуз": "ekibastuz", "рудный": "rudny", "жезказган": "zhezkazgan",
    "кентау": "kentau", "балхаш": "balkhash", "сатпаев": "satpaev",
}

# Минимальная транслитерация для городов вне таблицы
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", " ": "-", "-": "-",
}


def city_to_slug(city: str) -> str:
    key = city.strip().lower()
    if key in CITY_SLUGS:
        return CITY_SLUGS[key]
    # запасной вариант — транслитерация (может не совпасть со слагом 2GIS)
    slug = "".join(_TRANSLIT.get(ch, ch) for ch in key)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


@dataclass
class Config:
    city: str
    category: str
    count: int
    city_slug: str
    headless: bool
    output_dir: str
    sender_name: str = ""     # имя в сообщении-открывашке (SENDER_NAME)
    sender_contact: str = ""  # телефон/Instagram для подписи аудита (SENDER_CONTACT)
    mode: str = "auto"        # способ чтения: auto | http | browser (см. fetcher.FETCH_MODES)

    @property
    def domain(self) -> str:
        return "https://2gis.kz"


def load_config() -> Config:
    p = argparse.ArgumentParser(
        description="Парсер карточек 2GIS — сбор лид-данных локального бизнеса.")
    # `or default` — чтобы пустое значение в .env не перекрывало дефолт пустой строкой
    p.add_argument("--city", default=os.getenv("CITY") or "Алматы", help="Город (например: Алматы)")
    p.add_argument("--category", default=os.getenv("CATEGORY") or "стоматология",
                   help="Категория (например: стоматология)")
    p.add_argument("--count", type=int, default=int(os.getenv("COUNT") or 30),
                   help="Сколько компаний собрать")
    p.add_argument("--no-headless", action="store_true",
                   help="Показать окно браузера (по умолчанию headless)")
    p.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR") or "output")
    p.add_argument("--sender-name", default=os.getenv("SENDER_NAME") or "",
                   help="Ваше имя — подставляется в сообщение-открывашку")
    p.add_argument("--sender-contact", default=os.getenv("SENDER_CONTACT") or "",
                   help="Ваш контакт (телефон/Instagram) — подпись в аудите")
    p.add_argument("--firm", default=None,
                   help="Режим одной карточки: ID филиала 2GIS (для отладки)")
    p.add_argument("--mode", default=os.getenv("FETCH_MODE") or "auto",
                   choices=("auto", "http", "browser"),
                   help="Способ чтения: auto (HTTP, браузер в резерве), http, browser")
    args = p.parse_args()

    env_headless = os.getenv("HEADLESS", "true").lower() not in ("false", "0", "no")
    headless = env_headless and not args.no_headless

    cfg = Config(
        city=args.city,
        category=args.category,
        count=max(1, args.count),
        city_slug=city_to_slug(args.city),
        headless=headless,
        output_dir=args.output_dir,
        sender_name=args.sender_name.strip(),
        sender_contact=args.sender_contact.strip(),
        mode=args.mode,
    )
    # пробросим firm отдельно (не часть основного конфига)
    cfg.firm = args.firm  # type: ignore[attr-defined]
    return cfg
