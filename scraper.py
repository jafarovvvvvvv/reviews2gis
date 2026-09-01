"""
Скрапер 2GIS на Playwright (headless Chromium) — РЕЗЕРВНЫЙ способ.

Основной способ — fetcher.py (чистый HTTP, без браузера): он в ~10 раз быстрее
и не падает по таймауту загрузки. Браузер остаётся на случай, если 2GIS
перестанет отдавать данные простым запросом (JS-рендер, обязательные куки).
Разбор полей общий: parse_card_html / apply_reviews импортируются из fetcher —
чтобы оба способа давали одинаковый результат.

Подход (проверен на реальной структуре 2GIS, см. explore*.py):
  • Список фирм      -> /{slug}/search/{категория}/page/{N}  (~12 фирм/страница)
  • Карточка фирмы   -> /{slug}/firm/{id}:
        - название/категория -> мета-тег og:title
        - рейтинг/кол-во (запас) -> og:description "Оценка X, N отзывов"
        - телефон -> клик «Показать телефон» -> a[href^=tel:]
        - сайт/соцсети -> инлайн-JSON ("type":"website") + прямые ссылки
  • Отзывы           -> reviews-API public-api.reviews.2gis.com/3.0/branches/{id}/reviews
        даёт рейтинг филиала, число отзывов, и по каждому отзыву: оценку,
        дату, официальный ответ владельца (official_answer).

Антибот: свежий случайный UA, маскировка navigator.webdriver, случайные паузы,
имитация скролла, одна вкладка. При капче/блоке — исключение BlockedError.
"""
import asyncio
import random
import re
import urllib.parse
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from lead_scoring import Company, NA
from fetcher import (BlockedError, api_base, apply_card_fields, apply_reviews,
                     parse_card_html, remember_key, review_key)

# BlockedError импортируется из fetcher (общий тип для обоих способов) —
# main.py и monitor.py ловят его по-прежнему из scraper.
__all__ = ["Scraper", "BlockedError"]

# Пул свежих User-Agent (Chrome 146–148: старые версии 2GIS отдаёт заглушку «обновите браузер»)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}};
"""

BLOCK_SIGNALS = [
    "подтвердите, что вы не робот", "captcha", "êàï÷à", "доступ ограничен",
    "too many requests", "проверка безопасности", "вы не робот",
]

# Ресурсы, которые для разбора карточки не нужны: картинки, шрифты, видео,
# стили, аналитика. Именно их загрузка съедала 60-секундный таймаут на слабом
# канале (`Page.goto: Timeout 60000ms exceeded` в логах прода).
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

GOTO_TIMEOUT_MS = 35000  # без тяжёлых ресурсов страница приходит за секунды


def _log(msg: str):
    print(msg, flush=True)


class Scraper:
    def __init__(self, cfg):
        self.cfg = cfg
        # Ключ и базовый URL reviews-API живут в fetcher (общий кэш на процесс):
        # добытый браузером ключ достаётся и HTTP-способу, и наоборот.
        # сырой ответ reviews-API последней разобранной фирмы (нужен monitor.py
        # для дифа отзывов; сбрасывается в начале parse_firm)
        self.last_reviews_data = None
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    # ---------- жизненный цикл ----------
    async def __aenter__(self):
        self._pw = await async_playwright().start()
        try:
            launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            try:
                self.browser = await self._pw.chromium.launch(
                    headless=self.cfg.headless, args=launch_args)
            except Exception as e:
                # headless shell может быть не виден процессу (наблюдалось на
                # Windows) — полный Chromium ставится рядом, пробуем его
                if "Executable doesn't exist" not in str(e):
                    raise
                _log("  [browser] headless shell недоступен — пробую полный Chromium")
                self.browser = await self._pw.chromium.launch(
                    headless=self.cfg.headless, channel="chromium", args=launch_args)
            ua = random.choice(USER_AGENTS)
            self.ctx = await self.browser.new_context(
                user_agent=ua,
                locale="ru-RU",
                viewport={"width": random.randint(1320, 1460), "height": random.randint(840, 960)},
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            )
            await self.ctx.add_init_script(STEALTH_JS)
            # не тянем картинки/шрифты/видео/стили — нам нужен только HTML
            await self.ctx.route("**/*", self._filter_resource)
            # ловим актуальный ключ reviews-API из живого трафика
            self.ctx.on("request", self._sniff_key)
            self.page = await self.ctx.new_page()  # одна вкладка на всю сессию
            _log(f"  [browser] UA: {ua.split(') ')[-1]}")
            return self
        except Exception:
            await self.__aexit__()  # не оставляем подвисший драйвер Playwright
            raise

    async def __aexit__(self, *exc):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _filter_resource(self, route):
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass  # маршрут мог закрыться вместе со страницей — не наша беда

    def _sniff_key(self, request):
        if "public-api.reviews.2gis" in request.url:
            m = re.search(r"[?&]key=([0-9a-f-]{36})", request.url)
            if m:
                remember_key(m.group(1))

    # ---------- человекоподобность ----------
    async def _human_pause(self, lo=1.5, hi=4.0):
        await asyncio.sleep(random.uniform(lo, hi))

    async def _human_scroll(self, steps=None):
        """Имитация прокрутки панели результатов/страницы."""
        steps = steps or random.randint(3, 6)
        for _ in range(steps):
            await self.page.mouse.wheel(0, random.randint(300, 900))
            await asyncio.sleep(random.uniform(0.4, 1.1))

    # ---------- навигация + защита ----------
    async def _goto(self, url: str):
        await self.page.goto(url, wait_until="domcontentloaded",
                             timeout=GOTO_TIMEOUT_MS)
        await self._human_pause()
        await self._maybe_skip_interstitial()
        await self._check_block()

    async def _maybe_skip_interstitial(self):
        try:
            skip = self.page.get_by_text("Пропустить обновление", exact=False)
            if await skip.count() > 0:
                await skip.first.click()
                await self._human_pause(1.0, 2.0)
        except Exception:
            pass

    async def _check_block(self):
        try:
            body = (await self.page.inner_text("body"))[:3000].lower()
        except Exception:
            return
        for sig in BLOCK_SIGNALS:
            if sig in body:
                raise BlockedError(f"обнаружен сигнал блокировки: «{sig}»")

    # ---------- сбор ID фирм из поиска ----------
    async def collect_firm_ids(self, count: int):
        ids: list[str] = []
        page_num = 1
        empty_pages = 0
        cat = urllib.parse.quote(self.cfg.category)
        while len(ids) < count and empty_pages < 2 and page_num <= 30:
            url = f"{self.cfg.domain}/{self.cfg.city_slug}/search/{cat}/page/{page_num}"
            _log(f"  [search] страница {page_num}: {url}")
            await self._goto(url)
            await self._human_scroll()  # подгрузить/доскроллить список
            await self._human_pause(1.0, 2.0)
            page_ids = await self._extract_firm_ids()
            new = [i for i in page_ids if i not in ids]
            _log(f"  [search] найдено на странице: {len(page_ids)}, новых: {len(new)}")
            if not new:
                empty_pages += 1
            else:
                empty_pages = 0
            ids.extend(new)
            page_num += 1
        return ids[:count]

    async def _extract_firm_ids(self):
        hrefs = await self.page.eval_on_selector_all(
            "a[href*='/firm/']", "els => els.map(e => e.getAttribute('href'))")
        out = []
        for h in hrefs or []:
            m = re.search(r"/firm/(\d+)", h or "")
            if m and m.group(1) not in out:
                out.append(m.group(1))
        return out

    # ---------- парсинг одной карточки ----------
    async def parse_firm(self, firm_id: str) -> Company:
        self.last_reviews_data = None  # не отдать данные предыдущей фирмы
        c = Company()
        c.firm_id = firm_id
        c.category = self.cfg.category
        c.url = f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}"

        try:
            await self._goto(c.url)
        except BlockedError:
            raise
        except Exception as e:
            _log(f"    [warn] не удалось открыть карточку: {e}")
            return c  # вернём пустую (всё н/д), но не упадём

        await self._extract_card_fields(c, firm_id)
        await self._extract_reviews(c, firm_id)
        c.collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.compute_priority()
        return c

    async def _extract_card_fields(self, c: Company, firm_id: str):
        """Один раз берём HTML страницы и разбираем его общим парсером
        (fetcher.parse_card_html) — тем же, что и HTTP-способ."""
        try:
            html = await self.page.content()
        except Exception as e:
            _log(f"    [warn] не удалось получить HTML карточки: {e}")
            return

        f = parse_card_html(html)
        remember_key(f["key"], f["api_base"])  # ДИНАМИЧЕСКИЙ ключ из конфига страницы
        apply_card_fields(c, f)  # тот же разбор полей, что и у HTTP-способа

        # телефон: обычно уже есть в HTML; если нет — жмём «Показать телефон»
        # (единственное, что умеет браузер и не умеет простой запрос)
        if c.phone == NA:
            try:
                c.phone = await self._extract_phone()
            except Exception as e:
                _log(f"    [warn] телефон: {e}")

    async def _extract_phone(self) -> str:
        async def tels():
            return await self.page.eval_on_selector_all(
                "a[href^='tel:']", "els=>els.map(e=>e.getAttribute('href').replace('tel:',''))")
        nums = await tels()
        if not nums:
            # телефон скрыт за кнопкой «Показать телефон»
            try:
                btn = self.page.get_by_text("Показать телефон", exact=False)
                if await btn.count() > 0:
                    await btn.first.click()
                    await self._human_pause(1.0, 2.0)
                    nums = await tels()
            except Exception:
                pass
        uniq = list(dict.fromkeys(n for n in nums if n))
        return ", ".join(uniq[:3]) if uniq else NA

    # ---------- отзывы через API ----------
    async def _extract_reviews(self, c: Company, firm_id: str):
        data = await self._fetch_reviews(firm_id)
        self.last_reviews_data = data
        # разбор общий с HTTP-способом; data=None -> рейтинг/кол-во из og:*
        apply_reviews(c, data)

    async def fetch_reviews_deep(self, firm_id: str, limit_total: int = 300) -> list:
        """Постранично собрать до limit_total отзывов (аналитика/аудит).

        Вызывать после parse_firm той же фирмы — ключ reviews-API уже получен.
        Пагинация той же сессией, с паузами; обрыв страницы = отдаём что есть.
        """
        out: list = []
        offset = 0
        while len(out) < limit_total:
            data = await self._fetch_reviews(firm_id, offset=offset)
            reviews = (data or {}).get("reviews") or []
            if not reviews:
                break
            out.extend(reviews)
            _log(f"    [deep] собрано отзывов: {len(out)}")
            if len(reviews) < 50:
                break  # последняя страница
            offset += 50
            await self._human_pause(1.0, 2.2)
        return out[:limit_total]

    async def _fetch_reviews(self, firm_id: str, _retry=True, offset: int = 0):
        params = {
            "limit": 50, "offset": offset, "is_advertiser": "false",
            "fields": "meta.branch_rating,meta.branch_reviews_count",
            "rated": "true", "sort_by": "date_edited",
            "key": review_key(), "locale": "ru_KZ",
        }
        base = api_base().rstrip("/")
        url = f"{base}/branches/{firm_id}/reviews?" + urllib.parse.urlencode(params)
        # Запрос идёт через контекст браузера (та же сессия/cookies/UA/IP, что и
        # навигация) — НЕ «голый» requests. Referer указывает на вкладку отзывов
        # этой же фирмы, чтобы запрос выглядел естественно. Небольшой джиттер.
        referer = f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}/tab/reviews"
        await asyncio.sleep(random.uniform(0.6, 1.6))
        try:
            resp = await self.ctx.request.get(url, headers={
                "Origin": "https://2gis.kz", "Referer": referer}, timeout=30000)
            if resp.ok:
                return await resp.json()
            _log(f"    [warn] reviews-API HTTP {resp.status}")
        except Exception as e:
            _log(f"    [warn] reviews-API: {e}")

        # Фолбэк, если ключ из конфига карточки всё же не подошёл (ротация в
        # середине прогона): открываем вкладку отзывов — она и сама дёргает
        # reviews-API (ключ ловит _sniff_key), и содержит свежий конфиг.
        if _retry:
            if await self._refresh_review_key(firm_id):
                return await self._fetch_reviews(firm_id, _retry=False, offset=offset)
        return None

    async def _refresh_review_key(self, firm_id: str) -> bool:
        """Открыть вкладку отзывов и перехватить актуальный ключ (трафик + конфиг)."""
        before = review_key()
        try:
            await self._goto(f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}/tab/reviews")
            await self._human_pause(2.0, 3.5)
            f = parse_card_html(await self.page.content())
            remember_key(f["key"], f["api_base"])
        except Exception:
            return False
        return review_key() != before
