"""
Разведка №2: перехват сетевых JSON-ответов 2GIS (reviews API / catalog API),
раскрытие телефона, осмотр формата дат и официальных ответов.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

CITY_SLUG = "almaty"
FIRM_ID = "70000001026831262"
DUMP = Path("explore_dump")
DUMP.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome={runtime:{}};
"""

captured = []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="ru-RU",
                                        viewport={"width": 1366, "height": 900})
        await ctx.add_init_script(STEALTH)
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if "reviews.2gis" in url or ("catalog.api.2gis" in url and "byid" in url.lower()):
                try:
                    body = await resp.json()
                    captured.append((url, body))
                    print(f"  [JSON] {url[:90]}...")
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # карточка фирмы — раскрыть телефон
        firm_url = f"https://2gis.kz/{CITY_SLUG}/firm/{FIRM_ID}"
        print(f"[1] firm {firm_url}")
        await page.goto(firm_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        # Кнопка "Показать телефон"
        try:
            btn = page.get_by_text("Показать телефон", exact=False)
            if await btn.count() > 0:
                await btn.first.click()
                await asyncio.sleep(2)
                print("  clicked 'Показать телефон'")
        except Exception as e:
            print(f"  phone click: {e}")

        # tel: ссылки после раскрытия
        tels = await page.eval_on_selector_all(
            "a[href^='tel:']", "els=>els.map(e=>e.getAttribute('href'))")
        print(f"  tel links: {tels}")

        # внешние сайты/соцсети (исключаем 2gis/служебные)
        links = await page.eval_on_selector_all(
            "a[href^='http']",
            "els=>els.map(e=>e.getAttribute('href')).filter(h=>h && !h.includes('2gis') && !h.includes('api.'))")
        print(f"  external links (first 10): {links[:10]}")

        # вкладка отзывов
        reviews_url = f"https://2gis.kz/{CITY_SLUG}/firm/{FIRM_ID}/tab/reviews"
        print(f"[2] reviews {reviews_url}")
        await page.goto(reviews_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(7)

        # сохранить перехваченный JSON
        for i, (url, body) in enumerate(captured):
            (DUMP / f"api_{i}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  saved api_{i}.json from {url[:80]}")

        await browser.close()
        print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
