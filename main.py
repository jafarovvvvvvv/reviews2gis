"""
Парсер карточек 2GIS — сбор лид-данных локального бизнеса.

Примеры запуска:
  python main.py                                  # параметры из .env
  python main.py --city Алматы --category стоматология --count 30
  python main.py --firm 70000001026831262         # отладка одной карточки

Результат: CSV в папке output/, отсортированный по приоритету лида.
"""
import asyncio
import csv
import io
import sys
from datetime import datetime
from pathlib import Path

# консоль Windows -> UTF-8 (иначе падает на эмодзи/кириллице)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from config import load_config
from lead_scoring import (Company, CSV_HEADERS, sort_key,
                          PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)
from outreach import build_message, write_outreach_pack
from scraper import Scraper, BlockedError


def log(msg=""):
    print(msg, flush=True)


def write_csv(companies, cfg) -> Path:
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = Path(cfg.output_dir) / f"2gis_{cfg.city_slug}_{cfg.category}_{ts}.csv"
    # utf-8-sig (BOM) — чтобы Excel корректно открыл кириллицу
    with open(fname, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS, delimiter=";")
        w.writeheader()
        for c in companies:
            w.writerow(c.to_row())
    return fname


def print_summary(companies):
    counts = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 0, PRIORITY_LOW: 0}
    for c in companies:
        counts[c.priority] = counts.get(c.priority, 0) + 1
    log("\n=== ИТОГ ===")
    log(f"  Всего компаний: {len(companies)}")
    log(f"  {PRIORITY_HIGH}: {counts[PRIORITY_HIGH]}")
    log(f"  {PRIORITY_MEDIUM}: {counts[PRIORITY_MEDIUM]}")
    log(f"  {PRIORITY_LOW}: {counts[PRIORITY_LOW]}")


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
    async with Scraper(cfg) as s:
        c = await s.parse_firm(cfg.firm)
    print_company(c)
    log("\n--- сообщение-открывашка ---")
    log(build_message(c, None, cfg))


async def run_full(cfg):
    log("=" * 60)
    log(f"2GIS парсер лидов | город: {cfg.city} ({cfg.city_slug}) | "
        f"категория: {cfg.category} | нужно: {cfg.count}")
    log("=" * 60)

    companies: list[Company] = []
    blocked = False
    async with Scraper(cfg) as s:
        # 1. собрать список ID
        try:
            ids = await s.collect_firm_ids(cfg.count)
        except BlockedError as e:
            log(f"\n[!] ОСТАНОВКА: {e}")
            log("    2GIS показал капчу/блок на этапе поиска. Попробуйте позже "
                "или с другого IP, уменьшите частоту.")
            return
        log(f"\nСобрано ID компаний: {len(ids)} (цель {cfg.count})\n")
        if not ids:
            log("[!] Не найдено ни одной компании. Проверьте город/категорию.")
            return

        total = len(ids)
        for i, fid in enumerate(ids, 1):
            log(f"[{i}/{total}] Компания id={fid}")
            try:
                c = await s.parse_firm(fid)
            except BlockedError as e:
                log(f"\n[!] ОСТАНОВКА на компании {i}/{total}: {e}")
                log("    Сохраняю то, что уже собрано, и выхожу.")
                blocked = True
                break
            companies.append(c)
            print_company(c)
            if i < total:
                await s._human_pause()  # пауза между компаниями

    if not companies:
        log("\n[!] Нечего сохранять.")
        return

    # 2. сортировка по приоритету
    companies.sort(key=sort_key)

    # 3. CSV + пакет для рассылки
    out = write_csv(companies, cfg)
    pack = write_outreach_pack(companies, cfg)
    print_summary(companies)
    log(f"\nCSV сохранён: {out}")
    log(f"Пакет для рассылки: {pack / 'index.html'}")
    log("  (откройте в браузере: готовые сообщения, ссылки WhatsApp, аудиты)")
    if blocked:
        log("(данные неполные — был обнаружен блок)")


def main():
    cfg = load_config()
    if getattr(cfg, "firm", None):
        asyncio.run(run_single(cfg))
    else:
        asyncio.run(run_full(cfg))


if __name__ == "__main__":
    main()
