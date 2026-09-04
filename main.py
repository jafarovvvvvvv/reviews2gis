"""
Парсер карточек 2GIS — сбор лид-данных локального бизнеса (CLI).

То же самое умеет веб-панель: `python app.py` -> страница «Поиск лидов»
(форма, прогресс, таблица лидов, кнопка «в мониторинг»). Общая логика прогона —
в leadgen.py; здесь только разбор аргументов и вывод в консоль.

Примеры запуска:
  python main.py                                  # параметры из .env
  python main.py --city Алматы --category стоматология --count 30
  python main.py --firm 70000001026831262         # отладка одной карточки
  python main.py --mode browser                   # принудительно через браузер

Результат: CSV в папке output/ + пакет для рассылки, оба отсортированы по
приоритету лида.
"""
import asyncio
import sys

# консоль Windows -> UTF-8 (иначе падает на эмодзи/кириллице)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

import leadgen
from config import load_config
from fetcher import BlockedError
from lead_scoring import Company
from outreach import TOUCH_LABELS, build_touch_messages


def log(msg=""):
    print(msg, flush=True)


def print_summary(companies):
    counts = leadgen.priority_counts(companies)
    log("\n=== ИТОГ ===")
    log(f"  Всего компаний: {len(companies)}")
    for priority, n in counts.items():
        log(f"  {priority}: {n}")


def print_company(c: Company):
    r = c.to_row()
    log(f"    {r['Приоритет']} | {r['Название']} | рейтинг {r['Рейтинг']} | "
        f"отзывов {r['Кол-во отзывов']} | свежие={r['Свежие отзывы (мес)']} | "
        f"владелец отвечает={r['Владелец отвечает']} | негатив без ответа={r['Негатив без ответа (топ-5)']}")
    log(f"      тел: {r['Телефон']} | сайт: {r['Сайт/соцсети']}")
    log(f"      {r['Ссылка 2GIS']}")


async def run_single(cfg):
    """Отладочный режим: одна карточка."""
    log(f"Режим одной карточки: firm={cfg.firm}")
    c, way = await leadgen.fetch_one(cfg, cfg.firm, cfg.mode)
    log(f"(способ: {way})")
    print_company(c)
    for channel, text in build_touch_messages(c, cfg).items():
        log(f"\n--- первое касание: {TOUCH_LABELS[channel]} ---")
        log(text)


async def run_full(cfg):
    log("=" * 60)
    log(f"2GIS парсер лидов | город: {cfg.city} ({cfg.city_slug}) | "
        f"категория: {cfg.category} | нужно: {cfg.count} | способ: {cfg.mode}")
    log("=" * 60)

    def report(stage, done=0, total=0):
        log(f"[{done}/{total or '?'}] {stage}")

    try:
        res = await leadgen.run_leads(cfg, cfg.mode, report)
    except BlockedError as e:
        log(f"\n[!] ОСТАНОВКА: {e}")
        log("    2GIS показал капчу/блок. Попробуйте позже или с другого IP, "
            "уменьшите количество.")
        return
    except Exception as e:
        log(f"\n[!] Не удалось собрать лидов: {e}")
        return

    for c in res["companies"]:
        print_company(c)
    print_summary(res["companies"])
    log(f"\nСпособ, который сработал: {res['way']}")
    log(f"CSV сохранён: {res['csv']}")
    log(f"Пакет для рассылки: {res['pack'] / 'index.html'}")
    log("  (откройте в браузере: готовые сообщения, ссылки WhatsApp, аудиты)")


def main():
    cfg = load_config()
    if getattr(cfg, "firm", None):
        asyncio.run(run_single(cfg))
    else:
        asyncio.run(run_full(cfg))


if __name__ == "__main__":
    main()
