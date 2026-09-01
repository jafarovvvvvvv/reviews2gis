"""
HTTP-способ чтения карточки 2GIS — без браузера (httpx).

Зачем: Playwright грузит страницу целиком (~685 КБ HTML + скрипты, шрифты,
картинки) и на слабом канале регулярно падает с `Page.goto: Timeout 60000ms`.
Все нужные данные при этом лежат в самом HTML и в публичном reviews-API:

  • страница /{slug}/search/{категория}/page/{N} -> ссылки /firm/{id}
    (13 фирм на страницу; проверено: обычный GET, HTTP 200, ~830 КБ);
  • страница /{slug}/firm/{id}  -> og:title (название, категория),
    og:description (рейтинг, кол-во), "reviewApiKey" (живой ключ API),
    "type":"phone" (телефон), прямые ссылки на соцсети;
  • public-api.reviews.2gis.com/3.0/branches/{id}/reviews -> отзывы,
    рейтинг филиала, число отзывов, официальные ответы.

Замер на 10 реальных карточках: HTTP — 861 мс и 10/10 успеха,
браузер — 8600 мс. Поэтому HTTP основной, браузер (scraper.py) — резерв.

Чистые парсеры (parse_card_html, apply_reviews) общие для обоих способов —
scraper.py импортирует их отсюда, чтобы поля считались одинаково.

Самотесты (без сети): python fetcher.py
"""
import asyncio
import os
import random
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

import metrics
from lead_scoring import Company, NA

# Ключ reviews-API, зашитый в бандл 2GIS. Стартовый — дальше подхватываем
# живой из HTML карточки (_KEY_CACHE).
DEFAULT_REVIEW_KEY = "6e7e1929-4ea9-4a5d-8c05-d601860389bd"
DEFAULT_API_BASE = "https://public-api.reviews.2gis.com/3.0/"

# Определения «негатив»/«без ответа»/окно и все подсчёты по отзывам — в
# metrics.py (единый слой). Здесь их больше нет: раньше те же правила были
# продублированы в tg.py и insights.py и разъезжались.

# Таймауты: карточка отдаётся за ~0.6 с, 20 с с запасом хватает всем.
# (У браузера стояло 60 с — и он всё равно не успевал.)
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
RETRIES = 2              # повторов на сетевую ошибку/5xx (сверх первой попытки)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
]

# Признаки капчи/блока. Ищем только в КОРОТКОМ ответе: в полноценной странице
# 2GIS слово «captcha» встречается внутри бандла и само по себе ничего не значит.
BLOCK_SIGNALS = [
    "подтвердите, что вы не робот", "доступ ограничен", "проверка безопасности",
    "вы не робот", "too many requests",
]
BLOCK_BODY_MAX = 120_000  # больше этого — нормальная страница, а не заглушка

# трекеры/служебные пути, которые НЕ являются ссылками бизнеса
SOCIAL_BLOCKLIST = ("facebook.com/tr", "facebook.com/plugins", "vk.com/js",
                    "vk.com/widget", "instagram.com/embed", "instagram.com/static")

# Поиск: ID фирмы в ссылке. 6+ цифр — чтобы не хватать служебные короткие числа.
FIRM_ID_RE = re.compile(r"/firm/(\d{6,})")
SEARCH_PAGE_MAX = 30      # предохранитель от бесконечной пагинации
SEARCH_EMPTY_STOP = 2     # столько страниц без новых ID подряд -> конец выдачи
SEARCH_PAUSE = (0.6, 1.4)  # пауза между страницами поиска, сек


class BlockedError(Exception):
    """Похоже на капчу/блокировку — нужно остановиться."""


class FetchError(Exception):
    """Не удалось получить данные этим способом — можно пробовать следующий."""


def _log(msg: str):
    print(msg, flush=True)


# --- живой ключ reviews-API: общий на весь процесс -------------------------
# Один раз добытый со страницы ключ годится для всех фирм, поэтому храним его
# на уровне модуля — следующая проверка сможет пойти сразу в API, минуя HTML.
_KEY_CACHE = {"key": DEFAULT_REVIEW_KEY, "base": DEFAULT_API_BASE, "from_page": False}


def review_key() -> str:
    return _KEY_CACHE["key"]


def api_base() -> str:
    return _KEY_CACHE["base"]


def remember_key(key: str | None, base: str | None = None):
    """Запомнить живой ключ/базовый URL reviews-API (из HTML или из трафика)."""
    if key and key != _KEY_CACHE["key"]:
        _KEY_CACHE["key"] = key
        _log(f"    [key] новый ключ reviews-API: …{key[-6:]}")
    if key:
        _KEY_CACHE["from_page"] = True
    if base:
        _KEY_CACHE["base"] = base


def proxy_url() -> str | None:
    """Прокси для запросов к 2GIS. Пусто = ходим со своего IP.

    Заложено на будущее: если 2GIS начнёт банить по IP, достаточно задать
    SCRAPE_PROXY (http://user:pass@host:port или socks5://...) — код готов.
    """
    return (os.getenv("SCRAPE_PROXY") or "").strip() or None


# ============================ чистые парсеры ==============================
# Общие для HTTP и браузера: и тот и другой в итоге имеют HTML страницы и
# JSON отзывов, поэтому поля считаются одним и тем же кодом.

def _meta(html: str, prop: str) -> str | None:
    m = (re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % prop, html)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]+property="%s"' % prop, html))
    return m.group(1) if m else None


def parse_card_html(html: str) -> dict:
    """HTML карточки -> поля. Ничего не бросает: чего нет, то None.

    Рейтинг и счётчики берутся из инлайн-JSON карточки — это ровно те числа,
    что видит человек на странице 2ГИС:

        "reviews":{"general_rating":5,              -> rating (филиал)
                   "general_review_count":778,       -> reviews_count (тексты)
                   "general_review_count_with_stars":1174,  -> ratings_count
                   "org_rating":4.9,                 -> рейтинг всей сети
                   "org_review_count":967,
                   "org_review_count_with_stars":1452}

    Различать их обязательно: 2ГИС считает рейтинг по ОЦЕНКАМ (1174), а не по
    текстовым отзывам (778), и у сети из нескольких филиалов рейтинг другой
    (4.9 против 5.0 у филиала). Раньше всё это сваливалось в два поля и
    расходилось между таблицей и разбором.
    """
    out = {"name": None, "category": None, "rating": None, "reviews_count": None,
           "ratings_count": None, "org_rating": None, "org_reviews_count": None,
           "org_ratings_count": None, "branches_count": None,
           "phone": None, "website": None, "key": None, "api_base": None}

    title = _meta(html, "og:title")
    if title:
        parts = [p.strip() for p in re.sub(r"\s+—\s+2ГИС.*$", "", title).split(",")]
        if parts and parts[0]:
            out["name"] = parts[0]
        if len(parts) > 1 and parts[1]:
            out["category"] = parts[1]

    desc = _meta(html, "og:description")
    if desc:
        mr = re.search(r"Оценк[аи]\s+(\d+(?:[.,]\d+)?)", desc)
        if mr:
            out["rating"] = float(mr.group(1).replace(",", "."))
        mc = re.search(r"(\d+)\s+отзыв", desc)
        if mc:
            out["reviews_count"] = int(mc.group(1))

    # Инлайн-JSON карточки — авторитетнее og:*: только он различает оценки и
    # текстовые отзывы и даёт цифры всей сети. Перезаписывает og-значения.
    def _num(pat, cast):
        m = re.search(pat, html)
        return cast(m.group(1)) if m else None

    for key, pat, cast in (
            ("rating", r'"general_rating":\s*([\d.]+)', float),
            ("reviews_count", r'"general_review_count":\s*(\d+)', int),
            ("ratings_count", r'"general_review_count_with_stars":\s*(\d+)', int),
            ("org_rating", r'"org_rating":\s*([\d.]+)', float),
            ("org_reviews_count", r'"org_review_count":\s*(\d+)', int),
            ("org_ratings_count", r'"org_review_count_with_stars":\s*(\d+)', int)):
        val = _num(pat, cast)
        if val is not None:
            out[key] = val

    # «2 филиала» рядом со ссылкой /branches/<id> — сколько всего точек у сети
    mb = re.search(r'/branches/\d+[^>]*>\s*(\d+)\s+филиал', html)
    if mb:
        out["branches_count"] = int(mb.group(1))

    mk = re.search(r'"reviewApiKey":"([0-9a-f-]{36})"', html)
    if mk:
        out["key"] = mk.group(1)
    mu = re.search(r'"reviewApi3Url":"(https://[^"]+?/3\.0/?)"', html)
    if mu:
        out["api_base"] = mu.group(1)

    # телефон: типизированный контакт в инлайн-JSON, иначе tel:-ссылка
    phones = re.findall(r'"type":"phone","value":"(\+?[\d\-() ]{6,20})"', html)
    if not phones:
        phones = re.findall(r'href="tel:(\+?[\d\-() ]{6,20})"', html)
    uniq = list(dict.fromkeys(p.strip() for p in phones if p.strip()))
    if uniq:
        out["phone"] = ", ".join(uniq[:3])

    out["website"] = _socials_from_html(html)
    return out


def _socials_from_html(html: str) -> str | None:
    found: list[str] = []

    def unwrap(val: str) -> str:
        # реальный URL — после '?' в обёртке-редиректе link.2gis
        real = re.split(r"\?(?=https?://)", val)[-1]
        return urllib.parse.unquote(real).strip()

    for raw in re.findall(r'"type":"website","value":"([^"]+)"', html):
        real = unwrap(raw.encode().decode("unicode_escape", errors="ignore"))
        if real and real not in found:
            found.append(real)
        break

    social_pats = [
        r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+",
        r"https?://t\.me/[A-Za-z0-9_]+",
        r"https?://(?:www\.)?vk\.com/[A-Za-z0-9_.]+",
        r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.]+",
        r"https?://wa\.me/\d+",
    ]
    for pat in social_pats:
        for m in re.findall(pat, html):
            if any(b in m for b in SOCIAL_BLOCKLIST):
                continue
            if m not in found:
                found.append(m)
            break
        if len(found) >= 3:
            break
    return "; ".join(found[:3]) if found else None


def extract_firm_ids(html: str) -> list:
    """HTML страницы поиска -> ID фирм в порядке появления, без повторов.

    Общий парсер для обоих способов: и HTTP, и браузер в итоге имеют один и тот
    же HTML выдачи.
    """
    return list(dict.fromkeys(FIRM_ID_RE.findall(html or "")))


def apply_reviews(c: Company, data: dict | None, now: datetime | None = None):
    """Ответ reviews-API -> поля Company.

    Рейтинг НИКОГДА не вычисляется по отзывам: он приходит от 2ГИС (карточка
    или meta.branch_rating). Средний балл по нашей выборке — не тот рейтинг,
    что показывает 2ГИС: тот считается по подтверждённым оценкам, которых
    больше, чем текстовых отзывов.

    Все счётчики считает metrics.review_stats — один код на всех потребителей.
    data=None — отзывы не получены: рейтинг/счётчики карточки остаются, флаги
    по отзывам — None (нет данных).
    """
    if data is None:
        return

    meta = data.get("meta") or {}
    # Карточка авторитетнее API: там ровно те числа, что видит человек.
    # Из API берём только то, чего карточка не дала.
    if c.rating is None and meta.get("branch_rating") is not None:
        c.rating = meta["branch_rating"]
    if c.reviews_count is None and meta.get("branch_reviews_count") is not None:
        c.reviews_count = meta["branch_reviews_count"]
    if meta.get("branch_rating_is_confirmed") is not None:
        # «рейтинг на основе подтверждённых оценок» — так 2ГИС метит карточку
        c.rating_confirmed = bool(meta["branch_rating_is_confirmed"])

    st = metrics.review_stats(data.get("reviews") or [], now)
    c.loaded_reviews = st.loaded
    c.unanswered_count = st.unanswered
    c.negative_count = st.negative
    c.negative_unanswered = st.negative_unanswered
    c.fresh_count = st.fresh
    c.has_fresh_reviews = st.has_fresh
    c.owner_responds = st.owner_responds
    c.negative_without_response = st.negative_in_top
    c.last_review_date = st.last_date
    c.worst_reviews = st.worst


def apply_card_fields(c: Company, fields: dict):
    """Разобранные поля карточки -> Company. Общий код для HTTP и браузера.

    Числа кладутся по назначению: оценки отдельно от текстовых отзывов,
    сеть отдельно от филиала. Ничего не вычисляется.
    """
    if fields.get("name"):
        c.name = fields["name"]
    if fields.get("category"):
        c.category = fields["category"]
    for attr in ("rating", "ratings_count", "reviews_count",
                 "org_rating", "org_reviews_count", "org_ratings_count",
                 "branches_count"):
        if fields.get(attr) is not None:
            setattr(c, attr, fields[attr])
    if fields.get("phone"):
        c.phone = fields["phone"]
    if fields.get("website"):
        c.website = fields["website"]


def looks_blocked(body: str, status: int = 200) -> bool:
    """Похоже ли на капчу/блок. Длинную настоящую страницу не трогаем."""
    if status in (403, 429):
        return True
    if len(body) > BLOCK_BODY_MAX:
        return False
    low = body.lower()
    return any(sig in low for sig in BLOCK_SIGNALS)


# ========================= каскад способов ================================
# Способы чтения в порядке предпочтения; первый успешный побеждает. Живёт
# здесь, а не в monitor.py, чтобы каскадом могли пользоваться и мониторинг,
# и лидогенерация (leadgen.py), и аналитика (insights.py) — без общего импорта
# тяжёлого monitor. Выбор способа — настройка «fetch_mode» в панели.

FETCH_MODES = {
    "auto": ("http", "browser"),   # по умолчанию: быстрый HTTP, браузер в резерв
    "http": ("http",),             # только HTTP (для отладки)
    "browser": ("browser",),       # только браузер (старое поведение)
}


def fetch_ways(mode: str) -> tuple:
    """Имя режима -> кортеж способов. Неизвестный режим -> «auto»."""
    return FETCH_MODES.get(mode) or FETCH_MODES["auto"]


def fetcher_class(way: str):
    """Способ -> класс-читатель. Браузер импортируется лениво: пока HTTP
    справляется, процесс не тянет Playwright вообще."""
    if way == "http":
        return HttpFetcher
    if way == "browser":
        from scraper import Scraper  # локально: scraper импортирует этот модуль
        return Scraper
    raise KeyError(f"неизвестный способ чтения: {way}")


# ============================ HTTP-способ =================================

class HttpFetcher:
    """Читает карточку и отзывы по HTTP. API совместим с scraper.Scraper в
    той части, которой пользуются monitor.py, insights.py и leadgen.py:
    `collect_firm_ids`, `parse_firm`, `last_reviews_data`, `fetch_reviews_deep`."""

    name = "http"

    def __init__(self, cfg):
        self.cfg = cfg
        self.last_reviews_data = None
        self.client: httpx.AsyncClient | None = None
        self.ua = random.choice(USER_AGENTS)

    async def __aenter__(self):
        kw = {}
        proxy = proxy_url()
        if proxy:
            kw["proxy"] = proxy
            _log(f"  [http] через прокси {proxy.split('@')[-1]}")
        self.client = httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True,
            headers={"User-Agent": self.ua,
                     "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            **kw)
        return self

    async def __aexit__(self, *exc):
        if self.client:
            await self.client.aclose()

    def _headers(self, referer: str | None = None, html: bool = True) -> dict:
        h = {
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                       if html else "application/json, text/plain, */*"),
            "sec-ch-ua": '"Chromium";v="148", "Not?A_Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if referer:
            h["Referer"] = referer
        return h

    async def _get(self, url: str, headers: dict) -> httpx.Response:
        """GET с повторами на сетевых сбоях и 5xx."""
        last = None
        for attempt in range(RETRIES + 1):
            try:
                r = await self.client.get(url, headers=headers)
                if r.status_code < 500:
                    return r
                last = FetchError(f"HTTP {r.status_code}")
            except httpx.HTTPError as e:
                last = FetchError(f"{type(e).__name__}: {e}")
            if attempt < RETRIES:
                await asyncio.sleep(0.8 * (attempt + 1) + random.uniform(0, 0.4))
        raise last or FetchError("не удалось выполнить запрос")

    # ---------- поиск: список фирм по категории ----------
    async def collect_firm_ids(self, count: int) -> list:
        """ID фирм из выдачи /{slug}/search/{категория}/page/{N}.

        Страница поиска отдаётся обычным GET (проверено: HTTP 200, ~830 КБ,
        13 ID на страницу) — браузер для этого не нужен.

        Ошибка на ПЕРВОЙ странице — это провал способа (бросаем, вызывающий
        уйдёт в браузер). Ошибка дальше — отдаём то, что успели собрать.
        """
        ids: list = []
        empty = 0
        cat = urllib.parse.quote(self.cfg.category)
        page = 1
        while len(ids) < count and empty < SEARCH_EMPTY_STOP and page <= SEARCH_PAGE_MAX:
            url = f"{self.cfg.domain}/{self.cfg.city_slug}/search/{cat}/page/{page}"
            try:
                r = await self._get(url, self._headers())
                if looks_blocked(r.text, r.status_code):
                    raise BlockedError(
                        f"поиск: страница похожа на заглушку/капчу (HTTP {r.status_code})")
                if r.status_code != 200:
                    raise FetchError(f"поиск: HTTP {r.status_code}")
            except BlockedError:
                raise
            except FetchError:
                if not ids:
                    raise
                _log(f"  [search] страница {page} не отдалась — беру собранное")
                break
            new = [i for i in extract_firm_ids(r.text) if i not in ids]
            _log(f"  [search] страница {page}: новых компаний {len(new)} "
                 f"(всего {len(ids) + len(new)} из {count})")
            empty = 0 if new else empty + 1
            ids.extend(new)
            page += 1
            if len(ids) < count:
                await asyncio.sleep(random.uniform(*SEARCH_PAUSE))
        return ids[:count]

    # ---------- карточка ----------
    async def parse_firm(self, firm_id: str) -> Company:
        """Карточка + отзывы. Бросает FetchError, если не вышло ничего —
        тогда вызывающий код пробует следующий способ (браузер)."""
        self.last_reviews_data = None
        c = Company()
        c.firm_id = firm_id
        c.category = getattr(self.cfg, "category", "") or NA
        c.url = f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}"

        card_err = ""
        try:
            html = await self._fetch_card_html(c.url)
            fields = parse_card_html(html)
            remember_key(fields["key"], fields["api_base"])
            apply_card_fields(c, fields)
        except BlockedError:
            raise
        except Exception as e:
            # Страница не открылась — это ещё не провал: отзывы (главное для
            # мониторинга) можно взять из API по уже известному ключу.
            card_err = str(e)
            _log(f"    [http] карточка не отдалась ({card_err[:80]}) — иду сразу в API")

        data = await self._fetch_reviews(firm_id)
        self.last_reviews_data = data
        if data is None and c.name == NA:
            raise FetchError(card_err or "ни карточка, ни отзывы не прочитались")

        apply_reviews(c, data)
        # момент сбора: разбор печатает его как «данные собраны …», а слой
        # свежести по нему решает, не пора ли перечитать карточку
        c.collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.compute_priority()
        return c

    async def _fetch_card_html(self, url: str) -> str:
        r = await self._get(url, self._headers())
        if looks_blocked(r.text, r.status_code):
            raise BlockedError(f"страница похожа на заглушку/капчу (HTTP {r.status_code})")
        if r.status_code != 200:
            raise FetchError(f"карточка: HTTP {r.status_code}")
        return r.text

    # ---------- отзывы ----------
    def _reviews_url(self, firm_id: str, offset: int = 0, limit: int = 50) -> str:
        params = {
            "limit": limit, "offset": offset, "is_advertiser": "false",
            "fields": "meta.branch_rating,meta.branch_reviews_count",
            "rated": "true", "sort_by": "date_edited",
            "key": _KEY_CACHE["key"], "locale": "ru_KZ",
        }
        base = _KEY_CACHE["base"].rstrip("/")
        return f"{base}/branches/{firm_id}/reviews?" + urllib.parse.urlencode(params)

    async def _fetch_reviews(self, firm_id: str, offset: int = 0, _retry=True):
        referer = f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}/tab/reviews"
        headers = {**self._headers(referer, html=False), "Origin": self.cfg.domain}
        try:
            r = await self._get(self._reviews_url(firm_id, offset), headers)
        except FetchError as e:
            _log(f"    [http] reviews-API: {e}")
            return None
        if r.status_code == 200:
            try:
                return r.json()
            except Exception as e:
                _log(f"    [http] reviews-API: битый JSON: {e}")
                return None
        _log(f"    [http] reviews-API HTTP {r.status_code}")
        # ключ мог протухнуть — обновим его со страницы карточки и повторим
        if _retry and r.status_code in (401, 403, 404):
            if await self._refresh_key(firm_id):
                return await self._fetch_reviews(firm_id, offset, _retry=False)
        return None

    async def _refresh_key(self, firm_id: str) -> bool:
        before = _KEY_CACHE["key"]
        try:
            html = await self._fetch_card_html(
                f"{self.cfg.domain}/{self.cfg.city_slug}/firm/{firm_id}")
        except Exception:
            return False
        f = parse_card_html(html)
        remember_key(f["key"], f["api_base"])
        return _KEY_CACHE["key"] != before

    async def fetch_reviews_deep(self, firm_id: str, limit_total: int = 300) -> list:
        """Постранично собрать до limit_total отзывов (аналитика/аудит)."""
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
                break
            offset += 50
            await asyncio.sleep(random.uniform(0.3, 0.8))
        return out[:limit_total]


# ============================== самотесты =================================
if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []

    HTML = (
        '<meta property="og:title" content="Demokrat, стоматологическая клиника,'
        ' ул. Абая 1 — 2ГИС">'
        '<meta property="og:description" content="Оценка 4.9, 73 фото, 885 отзывов.">'
        '<script>window.__cfg={"reviewApiKey":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",'
        '"reviewApi3Url":"https://public-api.reviews.2gis.com/3.0/"};'
        '{"type":"phone","value":"+77072837030"}</script>'
        '<a href="https://instagram.com/demokrat_almaty">ig</a>'
        '<img src="https://facebook.com/tr?id=1">'
    )
    f = parse_card_html(HTML)
    checks.append(("html: название", f["name"] == "Demokrat"))
    checks.append(("html: категория", f["category"] == "стоматологическая клиника"))
    checks.append(("html: рейтинг", f["rating"] == 4.9))
    checks.append(("html: кол-во отзывов", f["reviews_count"] == 885))
    checks.append(("html: телефон", f["phone"] == "+77072837030"))
    checks.append(("html: ключ API", f["key"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    checks.append(("html: инстаграм", "instagram.com/demokrat_almaty" in (f["website"] or "")))
    checks.append(("html: трекер отброшен", "facebook.com/tr" not in (f["website"] or "")))

    CARD_JSON = (
        '<meta property="og:title" content="DoctorDos, стоматология — 2ГИС">'
        '<meta property="og:description" content="Оценка 5, 42 фото, 778 отзывов.">'
        '<script>{"reviews":{"general_rating":5,"general_review_count":778,'
        '"general_review_count_with_stars":1174,"is_reviewable":false,'
        '"org_rating":4.9,"org_review_count":967,"org_review_count_with_stars":1452}}'
        '</script>'
        '<span><a href="/kostanaj/branches/70000001037223551" class="_x">2 филиала</a></span>'
    )
    j = parse_card_html(CARD_JSON)
    checks.append(("карточка: рейтинг филиала из JSON", j["rating"] == 5.0))
    checks.append(("карточка: оценки отдельно от отзывов",
                   j["ratings_count"] == 1174 and j["reviews_count"] == 778))
    checks.append(("карточка: числа сети отдельно",
                   j["org_rating"] == 4.9 and j["org_reviews_count"] == 967
                   and j["org_ratings_count"] == 1452))
    checks.append(("карточка: количество филиалов", j["branches_count"] == 2))
    checks.append(("карточка: счётчик отзывов не спутан с оценками",
                   j["reviews_count"] != j["ratings_count"]))

    cj = Company()
    apply_card_fields(cj, j)
    checks.append(("поля карточки -> Company",
                   cj.rating == 5.0 and cj.ratings_count == 1174
                   and cj.reviews_count == 778 and cj.branches_count == 2))

    # ЗАПРЕТ САМОСТОЯТЕЛЬНОГО РЕЙТИНГА: средний балл по отзывам не должен
    # попасть ни в rating, ни в org_rating (у DoctorDos это 4.34 против 5.0)
    low = [{"id": i, "rating": 4, "date_created": "2026-08-30T10:00:00+00:00",
            "text": "ок", "official_answer": None} for i in range(10)]
    apply_reviews(cj, {"meta": {}, "reviews": low})
    checks.append(("рейтинг не пересчитан по отзывам (среднее 4.0 не подставилось)",
                   cj.rating == 5.0 and cj.org_rating == 4.9))

    empty = parse_card_html("<html><body>ничего</body></html>")
    checks.append(("html: пустой не падает",
                   empty["name"] is None and empty["rating"] is None))

    SEARCH = (
        '<a href="/almaty/firm/70000001026831262">Клиника</a>'
        '<a href="/almaty/firm/70000001026831262/tab/reviews">отзывы той же</a>'
        '<a href="/almaty/firm/70000001099887766?m=1">Вторая</a>'
        '<a href="/almaty/search/стоматология/page/2">дальше</a>'
        '<a href="/firm/12345">короткий id — не фирма</a>'
    )
    sids = extract_firm_ids(SEARCH)
    checks.append(("поиск: id по порядку без повторов",
                   sids == ["70000001026831262", "70000001099887766"]))
    checks.append(("поиск: короткий номер отброшен", "12345" not in sids))
    checks.append(("поиск: пустой HTML", extract_firm_ids("") == []))

    NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)

    def rv(days_ago, rating, answer=None, text="текст"):
        d = (NOW - timedelta(days=days_ago)).isoformat()
        return {"id": f"r{days_ago}", "rating": rating, "date_created": d,
                "text": text, "official_answer": answer}

    c = Company()
    apply_reviews(c, {"meta": {"branch_rating": 4.2, "branch_reviews_count": 120},
                      "reviews": [rv(2, 1), rv(5, 5, {"text": "ответ"}), rv(400, 2)]}, now=NOW)
    checks.append(("reviews: рейтинг из meta", c.rating == 4.2))
    checks.append(("reviews: кол-во из meta", c.reviews_count == 120))
    checks.append(("reviews: есть свежие", c.has_fresh_reviews is True))
    checks.append(("reviews: владелец отвечает", c.owner_responds is True))
    checks.append(("reviews: негатив без ответа в топ-5", c.negative_without_response is True))
    checks.append(("reviews: без ответа посчитаны", c.unanswered_count == 2))
    checks.append(("reviews: негатив без ответа", c.negative_unanswered == 2))
    checks.append(("reviews: свежих за 30 дней", c.fresh_count == 2))
    checks.append(("reviews: худшие отсортированы",
                   [w["rating"] for w in c.worst_reviews] == [1, 2]))

    c2 = Company()
    apply_reviews(c2, {"meta": {}, "reviews": []}, now=NOW)
    checks.append(("reviews: пусто -> нет свежих", c2.has_fresh_reviews is False
                   and c2.loaded_reviews == 0))
    c3 = Company()
    c3.rating = 4.0
    apply_reviews(c3, None, now=NOW)
    checks.append(("reviews: None не затирает", c3.rating == 4.0
                   and c3.has_fresh_reviews is None))

    c_api = Company()
    apply_reviews(c_api, {"meta": {"branch_rating": 4.2, "branch_reviews_count": 120,
                                   "branch_rating_is_confirmed": True},
                          "reviews": [rv(2, 5)]}, now=NOW)
    checks.append(("API: рейтинг взят, когда карточка молчит",
                   c_api.rating == 4.2 and c_api.reviews_count == 120))
    checks.append(("API: флаг подтверждённых оценок", c_api.rating_confirmed is True))

    c_card = Company(rating=5.0, reviews_count=778, ratings_count=1174)
    apply_reviews(c_card, {"meta": {"branch_rating": 4.2, "branch_reviews_count": 120},
                           "reviews": [rv(2, 5)]}, now=NOW)
    checks.append(("API не затирает числа карточки",
                   c_card.rating == 5.0 and c_card.reviews_count == 778))

    c4 = Company()
    apply_reviews(c4, {"reviews": [rv(1, 5), rv(300, 4)]}, now=NOW)
    checks.append(("reviews: старая дата не считается свежей", c4.fresh_count == 1))

    checks.append(("блок: капча в короткой заглушке",
                   looks_blocked("Подтвердите, что вы не робот") is True))
    checks.append(("блок: 429", looks_blocked("ok", 429) is True))
    checks.append(("блок: длинная страница со словом captcha — не блок",
                   looks_blocked("captcha" + "x" * BLOCK_BODY_MAX) is False))
    checks.append(("блок: обычная страница", looks_blocked("<html>привет</html>") is False))

    checks.append(("каскад: auto = http, потом браузер",
                   fetch_ways("auto") == ("http", "browser")))
    checks.append(("каскад: неизвестный режим -> auto",
                   fetch_ways("чепуха") == ("http", "browser")))
    checks.append(("каскад: http -> HttpFetcher", fetcher_class("http") is HttpFetcher))
    try:
        fetcher_class("телепатия")
        bad_way = False
    except KeyError:
        bad_way = True
    checks.append(("каскад: неизвестный способ -> KeyError", bad_way))

    _KEY_CACHE["key"] = DEFAULT_REVIEW_KEY
    remember_key("11111111-2222-3333-4444-555555555555")
    checks.append(("ключ: запомнен", review_key() == "11111111-2222-3333-4444-555555555555"))
    remember_key(None)
    checks.append(("ключ: None не сбрасывает",
                   review_key() == "11111111-2222-3333-4444-555555555555"))
    _KEY_CACHE["key"] = DEFAULT_REVIEW_KEY

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
