"""
Разведочный скрипт: открывает 2GIS, поиск по категории в городе,
сохраняет HTML + скриншоты результатов / карточки / отзывов в explore_dump/,
чтобы изучить реальную структуру DOM перед написанием парсера.

Запуск:  python explore.py
"""
import asyncio
import os
import random
from pathlib import Path

from playwright.async_api import async_playwright

CITY_SLUG = "almaty"          # Алматы
CATEGORY = "стоматология"
DUMP = Path("explore_dump")
DUMP.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Скрипт, маскирующий автоматизацию (2GIS проверяет navigator.webdriver)
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}};
"""


async def save(page, name: str):
    html = await page.content()
    (DUMP / f"{name}.html").write_text(html, encoding="utf-8")
    try:
        await page.screenshot(path=str(DUMP / f"{name}.png"), full_page=False)
    except Exception as e:
        print(f"  screenshot fail {name}: {e}")
    print(f"  saved {name}.html ({len(html)} bytes)")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="ru-RU",
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        url = f"https://2gis.kz/{CITY_SLUG}/search/{CATEGORY}"
        print(f"[1] GET {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

        # Если показалась заглушка «обновите браузер» — кликаем «Пропустить»
        try:
            skip = page.get_by_text("Пропустить обновление", exact=False)
            if await skip.count() > 0:
                print("  interstitial detected -> clicking skip")
                await skip.first.click()
                await asyncio.sleep(5)
        except Exception as e:
            print(f"  skip handling: {e}")

        await save(page, "01_search")

        # Заголовок + признаки капчи
        title = await page.title()
        print(f"  title: {title!r}")
        body_text = (await page.inner_text("body"))[:500]
        print(f"  body[:500]: {body_text!r}")

        # Ищем ссылки на карточки фирм
        hrefs = await page.eval_on_selector_all(
            "a[href*='/firm/']", "els => els.map(e => e.getAttribute('href'))"
        )
        uniq = sorted(set(hrefs))
        print(f"  firm links found: {len(uniq)}")
        for h in uniq[:10]:
            print(f"    {h}")

        if not uniq:
            print("  !! карточек не найдено — сохраняю и выхожу")
            await browser.close()
            return

        # Открываем первую карточку
        first = uniq[0]
        firm_url = first if first.startswith("http") else f"https://2gis.kz{first}"
        print(f"[2] GET firm {firm_url}")
        await page.goto(firm_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        await save(page, "02_firm")

        # Пытаемся открыть вкладку отзывов
        firm_id = firm_url.split("/firm/")[1].split("/")[0].split("?")[0]
        reviews_url = f"https://2gis.kz/{CITY_SLUG}/firm/{firm_id}/tab/reviews"
        print(f"[3] GET reviews {reviews_url}")
        await page.goto(reviews_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
        await save(page, "03_reviews")

        await browser.close()
        print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
