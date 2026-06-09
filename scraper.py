"""
Скрапер 2GIS на Playwright (headless Chromium).

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
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from lead_scoring import Company, NA

# Публичный ключ reviews-API, зашитый в бандл 2GIS. Используется как стартовый;
# при ротации подхватывается свежий из живого трафика (см. _refresh_review_key).
DEFAULT_REVIEW_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"

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

FRESH_DAYS = 30          # «свежий» отзыв — за последние 30 дней
NEGATIVE_RATING_MAX = 3  # оценка <=3 считается негативной
TOP_N = 5                # «топ-5» отзывов (новейших) для проверки негатива без ответа

BLOCK_SIGNALS = [
    "подтвердите, что вы не робот", "captcha", "êàï÷à", "доступ ограничен",
    "too many requests", "проверка безопасности", "вы не робот",
]


class BlockedError(Exception):
    """Похоже на капчу/блокировку — нужно остановиться."""


def _log(msg: str):
    print(msg, flush=True)


class Scraper:
    def __init__(self, cfg):
        self.cfg = cfg
        # Стартовые значения — лишь фолбэк. Реальные ключ и URL извлекаются
        # из конфига каждой открытой карточки (_update_review_config).
        self.review_key = DEFAULT_REVIEW_KEY
        self.review_api_base = "https://public-api.reviews.2gis.com/3.0/"
        self._key_from_page = False  # был ли ключ хоть раз получен со страницы
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    # ---------- жизненный цикл ----------
    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.cfg.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ua = random.choice(USER_AGENTS)
        self.ctx = await self.browser.new_context(
            user_agent=ua,
            locale="ru-RU",
            viewport={"width": random.randint(1320, 1460), "height": random.randint(840, 960)},
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        )
        await self.ctx.add_init_script(STEALTH_JS)
        # ловим актуальный ключ reviews-API из живого трафика
        self.ctx.on("request", self._sniff_key)
        self.page = await self.ctx.new_page()  # одна вкладка на всю сессию
        _log(f"  [browser] UA: {ua.split(') ')[-1]}")
        return self

    async def __aexit__(self, *exc):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    def _sniff_key(self, request):
        if "public-api.reviews.2gis" in request.url:
            m = re.search(r"[?&]key=([0-9a-f-]{36})", request.url)
            if m and m.group(1) != self.review_key:
                self.review_key = m.group(1)

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
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
        c = Company()
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
        c.compute_priority()
        return c

    async def _extract_card_fields(self, c: Company, firm_id: str):
        # название + категория из og:title; рейтинг/кол-во (запас) из og:description
        try:
            og_title = await self._meta("og:title")
            if og_title:
                parts = [p.strip() for p in re.sub(r"\s+—\s+2ГИС.*$", "", og_title).split(",")]
                if parts and parts[0]:
                    c.name = parts[0]
                if len(parts) > 1 and parts[1]:
                    c.category = parts[1]
        except Exception as e:
            _log(f"    [warn] og:title: {e}")

        try:
            og_desc = await self._meta("og:description")
            if og_desc:
                mr = re.search(r"Оценк[аи]\s+(\d+(?:[.,]\d+)?)", og_desc)
                if mr and c.rating is None:
                    c.rating = float(mr.group(1).replace(",", "."))
                mc = re.search(r"(\d+)\s+отзыв", og_desc)
                if mc and c.reviews_count is None:
                    c.reviews_count = int(mc.group(1))
        except Exception as e:
            _log(f"    [warn] og:description: {e}")

        # телефон
        try:
            c.phone = await self._extract_phone()
        except Exception as e:
            _log(f"    [warn] телефон: {e}")

        # один раз берём HTML карточки -> из него и ключ reviews-API, и соцсети
        try:
            html = await self.page.content()
            self._update_review_config(html)   # ДИНАМИЧЕСКИЙ ключ из конфига страницы
            c.website = self._socials_from_html(html)
        except Exception as e:
            _log(f"    [warn] сайт/соцсети/ключ: {e}")

    async def _meta(self, prop: str):
        el = await self.page.query_selector(f'meta[property="{prop}"]')
        if el:
            return await el.get_attribute("content")
        return None

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

    # трекеры/служебные пути, которые НЕ являются ссылками бизнеса
    _SOCIAL_BLOCKLIST = ("facebook.com/tr", "facebook.com/plugins", "vk.com/js",
                         "vk.com/widget", "instagram.com/embed", "instagram.com/static")

    def _update_review_config(self, html: str):
        """Достаём актуальный ключ и базовый URL reviews-API из конфига страницы.

        Конфиг 2GIS содержит:
          "reviewApi3Url":"https://public-api.reviews.2gis.com/3.0/",
          "reviewApiKey":"<uuid>"
        Это источник истины: ключ может быть ротирован — берём его со страницы,
        а не из хардкода. DEFAULT_REVIEW_KEY используется лишь если не нашли.
        """
        mk = re.search(r'"reviewApiKey":"([0-9a-f-]{36})"', html)
        if mk:
            if mk.group(1) != self.review_key:
                self.review_key = mk.group(1)
            if not self._key_from_page:
                self._key_from_page = True
                _log(f"    [key] ключ reviews-API получен со страницы: …{self.review_key[-6:]}")
        mu = re.search(r'"reviewApi3Url":"(https://[^"]+?/3\.0/?)"', html)
        if mu:
            self.review_api_base = mu.group(1)

    def _socials_from_html(self, html: str) -> str:
        found: list[str] = []

        def unwrap(val: str) -> str:
            # реальный URL — после '?' в обёртке-редиректе link.2gis
            real = re.split(r"\?(?=https?://)", val)[-1]
            return urllib.parse.unquote(real).strip()

        # сайт из типизированного контакта
        for raw in re.findall(r'"type":"website","value":"([^"]+)"', html):
            real = unwrap(raw.encode().decode("unicode_escape", errors="ignore"))
            if real and real not in found:
                found.append(real)
            break

        # прямые соцсети (исключаем трекинг-пиксели)
        social_pats = [
            r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+",
            r"https?://t\.me/[A-Za-z0-9_]+",
            r"https?://(?:www\.)?vk\.com/[A-Za-z0-9_.]+",
            r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.]+",
            r"https?://wa\.me/\d+",
        ]
        for pat in social_pats:
            for m in re.findall(pat, html):
                if any(b in m for b in self._SOCIAL_BLOCKLIST):
                    continue
                if m not in found:
                    found.append(m)
                break
            if len(found) >= 3:
                break
        return "; ".join(found[:3]) if found else NA

    # ---------- отзывы через API ----------
    async def _extract_reviews(self, c: Company, firm_id: str):
        data = await self._fetch_reviews(firm_id)
        if data is None:
            return  # оставляем рейтинг/кол-во из og:*, а review-флаги — None (н/д)

        meta = data.get("meta", {})
        if meta.get("branch_rating") is not None:
            c.rating = meta["branch_rating"]
        if meta.get("branch_reviews_count") is not None:
            c.reviews_count = meta["branch_reviews_count"]

        reviews = data.get("reviews", []) or []
        if not reviews:
            # отзывов нет вовсе
            c.has_fresh_reviews = False
            c.owner_responds = None
            c.negative_without_response = None
            return

        # сортируем по дате создания (новейшие первыми) — не зависим от серверной сортировки
        def created(r):
            try:
                return datetime.fromisoformat(r["date_created"])
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        reviews.sort(key=created, reverse=True)
        now = datetime.now(timezone.utc)

        # свежесть: новейший отзыв за последние FRESH_DAYS дней
        newest = created(reviews[0])
        c.has_fresh_reviews = (now - newest) <= timedelta(days=FRESH_DAYS)

        # владелец отвечает: есть хотя бы один официальный ответ среди загруженных
        c.owner_responds = any(r.get("official_answer") for r in reviews)

        # негатив без ответа в топ-5 (новейших)
        top = reviews[:TOP_N]
        c.negative_without_response = any(
            (r.get("rating") or 5) <= NEGATIVE_RATING_MAX and not r.get("official_answer")
            for r in top
        )

    async def _fetch_reviews(self, firm_id: str, _retry=True):
        params = {
            "limit": 50, "offset": 0, "is_advertiser": "false",
            "fields": "meta.branch_rating,meta.branch_reviews_count",
            "rated": "true", "sort_by": "date_edited",
            "key": self.review_key, "locale": "ru_KZ",
        }
        base = self.review_api_base.rstrip("/")
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
                return await self._fetch_reviews(firm_id, _retry=False)
        return None

    async def _refresh_review_key(self, firm_id: str) -> bool:
        """Открыть вкладку отзывов и перехватить актуальный ключ (трафик + конфиг)."""
        before = self.review_key
        try:
            await self._goto(f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}/tab/reviews")
            await self._human_pause(2.0, 3.5)
            self._update_review_config(await self.page.content())
        except Exception:
            return False
        return self.review_key != before
