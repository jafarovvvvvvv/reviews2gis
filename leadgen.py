"""
Лидогенерация: поиск компаний 2GIS по городу и категории -> карточки лидов ->
CSV + пакет для рассылки (outreach.py).

Один и тот же прогон запускают CLI (`main.py`) и веб-панель (`app.py`,
страница «Поиск лидов»), поэтому логика живёт здесь, а не в main.py.

СПОСОБ ЧТЕНИЯ — каскад из fetcher.py (настройка «fetch_mode»):
  1. HTTP (fetcher.HttpFetcher) — основной, браузер не нужен: страница поиска
     /{slug}/search/{категория}/page/{N} отдаётся обычным GET (13 фирм/страница);
  2. браузер (scraper.Scraper) — резерв.
Каскад переключается ЦЕЛИКОМ: если способ не собрал ни одной карточки, весь
прогон повторяется следующим способом. Отдельная нечитаемая карточка прогон
не срывает — она пропускается.

Журнал прогонов панели (RUNS) живёт в памяти процесса и обрезается до
RUNS_KEEP последних; сами результаты — CSV и пакет рассылки — лежат в output/
и переживают перезапуск (см. list_packs).

Самотесты (без сети): python leadgen.py
"""
import asyncio
import csv
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import metrics
from config import city_to_slug
from fetcher import BlockedError, FetchError, fetch_ways, fetcher_class
from lead_scoring import (CSV_HEADERS, Company, PRIORITY_HIGH, PRIORITY_LOW,
                          PRIORITY_MEDIUM, sort_key)
from outreach import (AUDIT_COMPETITOR as _AUDIT_COMPETITOR, build_rows,
                      render_audit_html, write_outreach_pack)


def outreach_competitor_flag() -> bool:
    """Флаг блока конкурента (задан в outreach.py, читается из .env)."""
    return _AUDIT_COMPETITOR

MAX_COUNT = 120   # потолок за прогон: ~1 с на карточку + пауза, дальше долго
DEF_COUNT = 30

# Пауза между карточками, сек. Браузеру нужна человекоподобность, HTTP хватает
# короткой — он и так делает один запрос вместо загрузки всей страницы.
PAUSE = {"http": (0.3, 0.9), "browser": (1.5, 4.0)}

# Разбор (аудит) не должен показывать данные месячной давности: если снимок
# старше этого срока — компания перечитывается с 2ГИС до рендера.
AUDIT_MAX_AGE_HOURS = int(os.getenv("AUDIT_MAX_AGE_HOURS") or 24)

# Блок сравнения с конкурентом выключен по умолчанию (AUDIT_COMPETITOR=1
# включает): он не раз подставлял в разбор разных конкурентов от прогона к
# прогону. Флаг определён в outreach — там же, где рендерится разбор.
SHOW_COMPETITOR = outreach_competitor_flag()

# статусы прогона
ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"

RUNS_KEEP = 20  # сколько прогонов держать в журнале панели

# Имя пакета: outreach_<slug>_<категория>_<дата> — категория кириллицей,
# поэтому проверяем не «только латиница», а отсутствие разделителей пути.
PACK_NAME_RE = re.compile(r"^outreach_[^\\/:*?\"<>|]+$")
PACK_FILE_RE = re.compile(r"^(index\.html|outreach\.csv|audit_[A-Za-z0-9]+\.html)$")


def _log(msg: str):
    print(msg, flush=True)


# ============================== конфиг ====================================

@dataclass
class LeadCfg:
    """Конфиг прогона. Утиный аналог config.Config: панели argparse не нужен,
    а поля — те же, их читают outreach.py и fetcher.py."""
    city: str
    category: str
    count: int = DEF_COUNT
    city_slug: str = ""
    output_dir: str = "output"
    sender_name: str = ""
    sender_contact: str = ""
    headless: bool = True   # для резервного способа (браузер)

    def __post_init__(self):
        self.city = (self.city or "").strip()
        self.category = (self.category or "").strip()
        self.count = clean_count(self.count)
        self.city_slug = (self.city_slug or "").strip() or city_to_slug(self.city)

    @property
    def domain(self) -> str:
        return "https://2gis.kz"


def clean_count(raw, default: int = DEF_COUNT) -> int:
    """«30», 30, мусор -> число в разумных границах 1…MAX_COUNT."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_COUNT, n))


# ============================== прогон ====================================

async def _one_way(way: str, cfg: LeadCfg, report) -> list:
    """Прогон одним способом: поиск ID -> карточки. Бросает, если способ не дал
    ничего — тогда каскад пробует следующий."""
    lo, hi = PAUSE.get(way, PAUSE["http"])
    companies: list = []
    async with fetcher_class(way)(cfg) as s:
        report(f"ищу компании: {cfg.category}, {cfg.city}", 0, cfg.count)
        ids = await s.collect_firm_ids(cfg.count)
        if not ids:
            raise FetchError("поиск не дал ни одной компании — "
                             "проверьте город и категорию")
        total = len(ids)
        _log(f"  [lead] собрано ID компаний: {total} (цель {cfg.count})")
        for i, fid in enumerate(ids, 1):
            report(f"читаю карточку {i} из {total}", i - 1, total)
            try:
                c = await s.parse_firm(fid)
            except BlockedError:
                raise
            except Exception as e:  # одна карточка не должна срывать прогон
                _log(f"  [lead] карточка {fid} пропущена: {str(e)[:120]}")
                continue
            companies.append(c)
            if i < total:
                await asyncio.sleep(random.uniform(lo, hi))
        report(f"собрано карточек: {len(companies)}", total, total)
    if not companies:
        raise FetchError("ни одна карточка не прочиталась")
    return companies


async def collect_leads(cfg: LeadCfg, mode: str = "auto", report=None):
    """Каскад способов. Возвращает (список Company, сработавший способ).

    BlockedError не лечится сменой способа (тот же IP) — пробрасываем сразу.
    """
    report = report or (lambda *a, **kw: None)
    ways = fetch_ways(mode)
    errors = []
    for i, way in enumerate(ways):
        try:
            companies = await _one_way(way, cfg, report)
        except BlockedError:
            raise
        except Exception as e:
            errors.append(f"{way}: {e}")
            if i + 1 < len(ways):
                _log(f"  [lead] способ «{way}» не смог ({str(e)[:120]}) — "
                     f"пробую «{ways[i + 1]}»")
            continue
        if i:  # выручил резервный способ — это стоит видеть в логах
            _log(f"  [lead] способ «{way}» выручил после отказа «{ways[0]}»")
        return companies, way
    raise FetchError("; ".join(errors) or "не осталось способов")


async def fetch_one(cfg: LeadCfg, firm_id: str, mode: str = "auto"):
    """Одна карточка по её ID (отладочный режим CLI). -> (Company, способ)."""
    ways = fetch_ways(mode)
    errors = []
    for way in ways:
        try:
            async with fetcher_class(way)(cfg) as s:
                return await s.parse_firm(firm_id), way
        except BlockedError:
            raise
        except Exception as e:
            errors.append(f"{way}: {e}")
    raise FetchError("; ".join(errors) or "не осталось способов")


def write_csv(companies: list, cfg) -> Path:
    """CSV лидов по приоритету. utf-8-sig + «;» — Excel открывает как есть."""
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = Path(cfg.output_dir) / f"2gis_{cfg.city_slug}_{cfg.category}_{ts}.csv"
    with open(fname, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS, delimiter=";")
        w.writeheader()
        for c in companies:
            w.writerow(c.to_row())
    return fname


async def run_leads(cfg: LeadCfg, mode: str = "auto", report=None) -> dict:
    """Полный прогон: поиск -> карточки -> CSV + пакет рассылки.

    Возвращает {companies, rows, way, csv, pack} (пути — Path).
    """
    companies, way = await collect_leads(cfg, mode, report)
    companies.sort(key=sort_key)
    report = report or (lambda *a, **kw: None)
    report("готовлю пакет для рассылки…", len(companies), len(companies))
    # снимки пишем до рендера: разбор и динамика читают их из БД
    save_snapshots(companies, cfg)
    return {"companies": companies, "rows": build_rows(companies, cfg), "way": way,
            "csv": write_csv(companies, cfg), "pack": write_outreach_pack(companies, cfg)}


# ==================== снимки, свежесть, динамика ==========================

class StaleDataError(Exception):
    """Свежих данных нет и получить не удалось — разбор рендерить нельзя."""


def save_snapshots(companies: list, cfg) -> int:
    """Сохранить снимки прогона (история для динамики и выбора конкурента)."""
    conn = db.connect()
    try:
        for c in companies:
            db.add_lead_snapshot(conn, c, cfg.city_slug, cfg.category)
    finally:
        conn.close()
    return len(companies)


def snapshot_age_hours(row) -> float:
    """Возраст снимка в часах. Нечитаемая дата -> «бесконечно старый»."""
    try:
        ts = datetime.fromisoformat(row["collected_at"])
    except (TypeError, ValueError, KeyError):
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def company_from_snapshot(row) -> Company:
    """Строка снимка -> Company (числа те же, без похода в сеть)."""
    import json
    extra = {}
    try:
        extra = json.loads(row["data_json"] or "{}")
    except (ValueError, TypeError):
        pass
    c = Company(firm_id=row["firm_id"], name=row["name"] or metrics.NA,
                category=row["category"] or metrics.NA, url=row["url"] or metrics.NA)
    for f in db.SNAPSHOT_FIELDS:
        val = row[f]
        if f == "rating_confirmed" and val is not None:
            val = bool(val)
        setattr(c, f, val)
    c.branch_scope = row["branch_scope"] or metrics.SCOPE_BRANCH
    c.collected_at = row["collected_at"]
    c.negative_quotes = extra.get("negative_quotes") or []
    c.last_review_date = extra.get("last_review_date")
    c.has_fresh_reviews = extra.get("has_fresh_reviews")
    c.owner_responds = extra.get("owner_responds")
    c.negative_without_response = extra.get("negative_without_response")
    return c


async def fresh_company(firm_id: str, cfg, mode: str = "auto",
                        max_age_hours: int = None, force: bool = False):
    """Компания со СВЕЖИМИ данными: (Company, id снимка).

    Разбор обязан показывать актуальные числа. Поэтому:
      • есть снимок моложе max_age_hours -> берём его;
      • иначе перечитываем карточку с 2ГИС и пишем новый снимок;
      • перечитать не удалось -> StaleDataError. Молча отдать старое нельзя:
        именно так в разбор и попали данные месячной давности.
    """
    max_age = AUDIT_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    conn = db.connect()
    try:
        last = db.last_lead_snapshot(conn, firm_id)
    finally:
        conn.close()

    if last is not None and not force:
        age = snapshot_age_hours(last)
        if age <= max_age:
            _log(f"  [lead] снимок свежий ({age:.1f} ч <= {max_age} ч) — беру его")
            return company_from_snapshot(last), last["id"]
        _log(f"  [lead] снимку {age:.1f} ч (порог {max_age} ч) — перечитываю карточку")

    try:
        c, way = await fetch_one(cfg, firm_id, mode)
    except Exception as e:
        raise StaleDataError(
            f"не удалось получить свежие данные по компании {firm_id}: {e}. "
            f"Разбор не построен — показывать устаревшие числа нельзя.") from e
    if c.name == metrics.NA and c.rating is None:
        raise StaleDataError(
            f"карточка {firm_id} прочиталась пустой — разбор не построен.")

    conn = db.connect()
    try:
        sid = db.add_lead_snapshot(conn, c, cfg.city_slug, cfg.category)
    finally:
        conn.close()
    _log(f"  [lead] карточка перечитана способом «{way}», снимок #{sid}")
    return c, sid


def dynamics(firm_id: str, snapshot_id: int) -> dict:
    """Сравнение с предыдущим снимком: {было -> стало}. None, если истории нет.

    Блок динамики в разборе рисуется только при непустом результате — заглушки
    и прочерки не показываем.
    """
    conn = db.connect()
    try:
        prev = db.prev_lead_snapshot(conn, firm_id, snapshot_id)
        cur = conn.execute("SELECT * FROM lead_snapshots WHERE id=?",
                           (snapshot_id,)).fetchone()
    finally:
        conn.close()
    if prev is None or cur is None:
        return None
    fields = (("rating", "рейтинг"), ("unanswered_count", "отзывов без ответа"),
              ("negative_unanswered", "негатива без ответа"),
              ("reviews_count", "текстовых отзывов"))
    changes = []
    for key, label in fields:
        was, now = prev[key], cur[key]
        if was is None and now is None:
            continue
        changes.append({"label": label, "was": was, "now": now,
                        "delta": (None if was is None or now is None
                                  else round(now - was, 2))})
    if not changes:
        return None
    return {"since": prev["collected_at"], "until": cur["collected_at"],
            "changes": changes}


def pick_competitor(cfg, exclude_firm_id: str):
    """Конкурент для сравнения — детерминированно и из общего слоя данных.

    Топ-1 по рейтингу среди последних снимков той же категории того же города,
    сама компания исключается. При равном рейтинге — больше оценок, затем
    меньший firm_id: два прогона подряд дают один и тот же выбор.
    """
    conn = db.connect()
    try:
        pool = db.latest_snapshots_in(conn, cfg.city_slug, cfg.category)
    finally:
        conn.close()
    cands = [r for r in pool
             if r["firm_id"] != exclude_firm_id and r["rating"] is not None]
    if not cands:
        return None
    best = sorted(cands, key=lambda r: (-r["rating"], -(r["ratings_count"] or 0),
                                        r["firm_id"]))[0]
    return company_from_snapshot(best)


async def build_audit(firm_id: str, cfg, mode: str = "auto",
                      max_age_hours: int = None, with_competitor: bool = None) -> tuple:
    """Разбор компании: (HTML, Company, id снимка).

    Данные гарантированно свежие (см. fresh_company) — иначе StaleDataError и
    никакого HTML. Динамика добавляется, если есть предыдущий снимок.
    """
    c, sid = await fresh_company(firm_id, cfg, mode, max_age_hours)
    show = SHOW_COMPETITOR if with_competitor is None else with_competitor
    competitor = pick_competitor(cfg, firm_id) if show else None
    html_out = render_audit_html(c, cfg, competitor=competitor,
                                 dynamics=dynamics(firm_id, sid))
    return html_out, c, sid


def priority_counts(companies: list) -> dict:
    """{приоритет: сколько} в порядке 🔥 / 🟡 / ⚪ (для сводки)."""
    return {p: sum(1 for c in companies if c.priority == p)
            for p in (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)}


# ==================== журнал прогонов веб-панели ==========================

@dataclass
class LeadRun:
    """Один прогон, запущенный из панели. Живёт в памяти процесса: страница
    «Поиск лидов» читает отсюда статус, прогресс и результат."""
    id: int
    cfg: LeadCfg
    status: str = ST_RUNNING
    stage: str = "запуск…"
    done: int = 0
    total: int = 0
    way: str = ""
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""
    rows: list = field(default_factory=list)   # строки рассылки (outreach.build_rows)
    pack: str = ""                             # имя папки пакета в output/
    csv_name: str = ""

    @property
    def busy(self) -> bool:
        return self.status == ST_RUNNING

    @property
    def percent(self) -> int:
        """Прогресс в процентах; пока неизвестен объём — 0."""
        if self.status == ST_DONE:
            return 100
        if not self.total:
            return 0
        return max(0, min(100, round(100 * self.done / self.total)))

    @property
    def title(self) -> str:
        return f"{self.cfg.category}, {self.cfg.city}"

    def counts(self) -> dict:
        return priority_counts([r["c"] for r in self.rows])

    def report(self, stage: str, done: int = 0, total: int = 0):
        """Колбэк прогресса для run_leads."""
        self.stage, self.done, self.total = stage, done, total

    def finish(self, res: dict):
        self.rows = res["rows"]
        self.way = res["way"]
        self.pack = Path(res["pack"]).name
        self.csv_name = Path(res["csv"]).name
        self.done = self.total = len(self.rows)
        self.stage = f"готово: {len(self.rows)} лидов"
        self.status = ST_DONE
        self.finished_at = datetime.now().isoformat(timespec="seconds")

    def fail(self, err: str):
        self.error = err
        self.stage = "не получилось"
        self.status = ST_ERROR
        self.finished_at = datetime.now().isoformat(timespec="seconds")


RUNS: dict = {}
_LAST_ID = [0]


def new_run(cfg: LeadCfg) -> LeadRun:
    """Создать прогон в журнале (журнал обрезается до RUNS_KEEP последних)."""
    _LAST_ID[0] += 1
    run = LeadRun(id=_LAST_ID[0], cfg=cfg)
    RUNS[run.id] = run
    for rid in sorted(RUNS)[:-RUNS_KEEP]:
        RUNS.pop(rid, None)
    return run


def get_run(run_id) -> LeadRun:
    try:
        return RUNS.get(int(run_id))
    except (TypeError, ValueError):
        return None


def list_runs() -> list:
    """Прогоны, новые сверху."""
    return [RUNS[rid] for rid in sorted(RUNS, reverse=True)]


def any_busy() -> bool:
    return any(r.busy for r in RUNS.values())


async def run_task(run: LeadRun, mode: str = "auto", lock=None):
    """Фоновая задача панели: прогон + запись результата в сам LeadRun.

    lock — общий с мониторингом замок на обращения к 2ГИС (monitor.SCRAPE_LOCK):
    пока идёт прогон, плановые проверки ждут, и наоборот. Ошибку наружу не
    поднимает — она видна на странице прогона как статус.
    """
    if lock is not None:
        run.stage = "жду очереди к 2ГИС…"
        async with lock:
            await _run_into(run, mode)
    else:
        await _run_into(run, mode)


async def _run_into(run: LeadRun, mode: str):
    try:
        res = await run_leads(run.cfg, mode, run.report)
    except BlockedError as e:
        run.fail(f"2ГИС показал капчу или блок ({e}). Попробуйте позже, "
                 f"уменьшите количество или смените IP.")
    except Exception as e:
        run.fail(str(e)[:400])
    else:
        run.finish(res)
    _log(f"[lead] прогон #{run.id} «{run.title}»: {run.status} — {run.stage}")


# ===================== пакеты рассылки на диске ===========================

def list_packs(output_dir: str = "output", limit: int = RUNS_KEEP) -> list:
    """Готовые пакеты рассылки в output/ — новые сверху.

    Журнал прогонов живёт в памяти и пропадает при перезапуске панели, а сами
    пакеты остаются на диске: их и показываем.
    """
    root = Path(output_dir)
    if not root.is_dir():
        return []
    packs = [p for p in root.iterdir()
             if p.is_dir() and PACK_NAME_RE.match(p.name)
             and (p / "index.html").is_file()]
    packs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name,
             "leads": len(list(p.glob("audit_*.html"))),
             "ts": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
            for p in packs[:limit]]


def pack_file(pack: str, filename: str, output_dir: str = "output"):
    """Путь к файлу внутри пакета — или None.

    Панель отдаёт эти файлы наружу, поэтому: имя пакета и имя файла — по белому
    списку, и итоговый путь обязан лежать внутри output/ (защита от «../»).
    """
    if not PACK_NAME_RE.match(pack or "") or ".." in (pack or ""):
        return None
    if not PACK_FILE_RE.match(filename or ""):
        return None
    root = Path(output_dir).resolve()
    p = (root / pack / filename).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    return p


# ============================== самотесты =================================
if __name__ == "__main__":
    import sys
    import tempfile

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    checks = []

    # --- конфиг ---
    cfg = LeadCfg(city="  Алматы ", category=" стоматология ", count="45")
    checks.append(("конфиг: slug из города", cfg.city_slug == "almaty"))
    checks.append(("конфиг: пробелы обрезаны",
                   cfg.city == "Алматы" and cfg.category == "стоматология"))
    checks.append(("конфиг: количество из строки", cfg.count == 45))
    checks.append(("конфиг: домен", cfg.domain == "https://2gis.kz"))
    checks.append(("количество: потолок", clean_count(10 ** 6) == MAX_COUNT))
    checks.append(("количество: пол", clean_count(0) == 1 and clean_count(-5) == 1))
    checks.append(("количество: мусор -> по умолчанию", clean_count("много") == DEF_COUNT))

    # --- прогресс прогона ---
    run = new_run(LeadCfg(city="Алматы", category="стоматология", count=10))
    checks.append(("прогон: в журнале", get_run(run.id) is run and run.busy))
    checks.append(("прогресс: объём неизвестен -> 0%", run.percent == 0))
    run.report("читаю карточку 5 из 10", 5, 10)
    checks.append(("прогресс: половина", run.percent == 50))
    run.fail("сеть недоступна")
    checks.append(("прогон: ошибка", run.status == ST_ERROR and not run.busy
                   and bool(run.finished_at)))
    checks.append(("прогон: неизвестный id", get_run(999999) is None
                   and get_run("чепуха") is None))

    # --- полный прогон без сети: способ подменён фейком ---
    def _fake_company(fid, name, rating, rc, fresh, owner):
        c = Company(firm_id=fid, name=name, category="стоматология", rating=rating,
                    reviews_count=rc, has_fresh_reviews=fresh, owner_responds=owner,
                    phone="+7 (727) 111-22-33", url=f"https://2gis.kz/almaty/firm/{fid}",
                    loaded_reviews=50, unanswered_count=7, negative_unanswered=2,
                    fresh_count=3 if fresh else 0)
        c.compute_priority()
        return c

    FAKE = [_fake_company("111111", "Спящая клиника", 4.1, 80, True, False),
            _fake_company("222222", "Топ Дент", 4.8, 300, True, True),
            _fake_company("333333", "Малыш", 4.9, 5, False, True)]

    async def _fake_one_way(way, cfg, report):
        report("ищу компании", 0, len(FAKE))
        return list(FAKE)

    with tempfile.TemporaryDirectory() as tmp:
        real_one_way = _one_way
        globals()["_one_way"] = _fake_one_way
        try:
            run2 = new_run(LeadCfg(city="Алматы", category="стоматология",
                                   count=3, output_dir=tmp, sender_name="Эмиль"))
            asyncio.run(run_task(run2))
        finally:
            globals()["_one_way"] = real_one_way

        checks.append(("прогон: завершён", run2.status == ST_DONE and run2.percent == 100))
        checks.append(("прогон: лиды по приоритету",
                       [r["c"].name for r in run2.rows][0] == "Спящая клиника"))
        checks.append(("прогон: сводка по приоритетам",
                       run2.counts()[PRIORITY_HIGH] == 1
                       and run2.counts()[PRIORITY_LOW] == 1))
        pack_dir = Path(tmp) / run2.pack
        checks.append(("прогон: пакет на диске",
                       (pack_dir / "index.html").is_file()
                       and (pack_dir / "audit_111111.html").is_file()))
        checks.append(("прогон: CSV на диске", (Path(tmp) / run2.csv_name).is_file()))

        packs = list_packs(tmp)
        checks.append(("пакеты: найден на диске",
                       len(packs) == 1 and packs[0]["name"] == run2.pack
                       and packs[0]["leads"] == 3))
        checks.append(("пакеты: нет папки -> пусто",
                       list_packs(str(Path(tmp) / "нет-такой")) == []))

        checks.append(("файл пакета: index.html отдаётся",
                       pack_file(run2.pack, "index.html", tmp) is not None))
        checks.append(("файл пакета: аудит отдаётся",
                       pack_file(run2.pack, "audit_111111.html", tmp) is not None))
        checks.append(("файл пакета: чужое имя файла отклонено",
                       pack_file(run2.pack, "secret.env", tmp) is None))
        checks.append(("файл пакета: выход по ../ отклонён",
                       pack_file(run2.pack, "../../monitor.db", tmp) is None
                       and pack_file("outreach_../..", "index.html", tmp) is None))
        checks.append(("файл пакета: чужая папка отклонена",
                       pack_file("output", "index.html", tmp) is None))
        checks.append(("файл пакета: несуществующий -> None",
                       pack_file(run2.pack, "audit_999999.html", tmp) is None))

    # --- каскад: первый способ упал, второй справился ---
    calls = []

    async def _flaky(way, cfg, report):
        calls.append(way)
        if way == "http":
            raise FetchError("поиск не дал ни одной компании")
        return list(FAKE)

    globals()["_one_way"] = _flaky
    try:
        got, used = asyncio.run(collect_leads(LeadCfg(city="Алматы", category="кафе")))
        checks.append(("каскад: браузер выручил",
                       used == "browser" and calls == ["http", "browser"] and len(got) == 3))

        async def _blocked(way, cfg, report):
            raise BlockedError("капча")

        globals()["_one_way"] = _blocked
        calls.clear()
        try:
            asyncio.run(collect_leads(LeadCfg(city="Алматы", category="кафе")))
            blocked_ok = False
        except BlockedError:
            blocked_ok = True
        checks.append(("каскад: блок не лечится сменой способа", blocked_ok))
    finally:
        globals()["_one_way"] = real_one_way

    # ============ свежесть данных, динамика, конкурент ============
    # всё на временной БД и без сети: парсер подменяется фейком
    with tempfile.TemporaryDirectory() as tmpdb:
        real_db_path = db.DB_PATH
        db.DB_PATH = Path(tmpdb) / "t.db"
        real_fetch_one = fetch_one
        fetched = []

        def _mk(firm_id, name, rating, unans, neg_unans, ratings=1000, revs=700):
            c = Company(firm_id=firm_id, name=name, category="стоматология",
                        url=f"https://2gis.kz/kostanay/firm/{firm_id}",
                        rating=rating, ratings_count=ratings, reviews_count=revs,
                        unanswered_count=unans, negative_unanswered=neg_unans,
                        negative_count=neg_unans, loaded_reviews=50, fresh_count=4,
                        has_fresh_reviews=True, owner_responds=True,
                        negative_without_response=False)
            c.compute_priority()
            return c

        async def _fake_fetch_one(cfg, firm_id, mode="auto"):
            fetched.append(firm_id)
            return _mk(firm_id, "Свежая клиника", 5.0, 10, 9), "http"

        cfg_k = LeadCfg(city="Костанай", category="стоматология", output_dir=tmpdb)
        globals()["fetch_one"] = _fake_fetch_one
        try:
            # 1. снимка нет -> идём в сеть
            c1, sid1 = asyncio.run(fresh_company("111", cfg_k))
            checks.append(("свежесть: без снимка читаем карточку",
                           fetched == ["111"] and c1.rating == 5.0))
            checks.append(("свежесть: снимок записан", sid1 > 0))

            # 2. снимок свежий -> сети быть не должно
            fetched.clear()
            c2, sid2 = asyncio.run(fresh_company("111", cfg_k))
            checks.append(("свежесть: свежий снимок берётся без сети",
                           fetched == [] and sid2 == sid1 and c2.rating == 5.0))
            checks.append(("свежесть: числа снимка не потеряны",
                           c2.ratings_count == 1000 and c2.reviews_count == 700
                           and c2.unanswered_count == 10))

            # 3. состарим снимок -> обязано перечитать
            conn = db.connect()
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
            conn.execute("UPDATE lead_snapshots SET collected_at=? WHERE id=?",
                         (old_ts, sid1))
            conn.commit()
            conn.close()
            fetched.clear()
            c3, sid3 = asyncio.run(fresh_company("111", cfg_k))
            checks.append(("свежесть: устаревший снимок -> перечитали",
                           fetched == ["111"] and sid3 != sid1))

            # 4. динамика: было -> стало
            dyn = dynamics("111", sid3)
            checks.append(("динамика: есть предыдущий снимок",
                           dyn is not None and dyn["since"] == old_ts))
            rating_change = [ch for ch in dyn["changes"] if ch["label"] == "рейтинг"]
            checks.append(("динамика: рейтинг было->стало",
                           rating_change and rating_change[0]["was"] == 5.0
                           and rating_change[0]["now"] == 5.0))

            # 5. динамики нет, если снимок первый
            fetched.clear()
            _, sid_new = asyncio.run(fresh_company("222", cfg_k))
            checks.append(("динамика: истории нет -> None",
                           dynamics("222", sid_new) is None))

            # 6. падение парсера -> разбор не рендерится
            async def _boom(cfg, firm_id, mode="auto"):
                raise FetchError("2ГИС не ответил")

            globals()["fetch_one"] = _boom
            conn = db.connect()
            conn.execute("UPDATE lead_snapshots SET collected_at=?", (old_ts,))
            conn.commit()
            conn.close()
            try:
                asyncio.run(build_audit("111", cfg_k))
                stale_ok = False
            except StaleDataError as ex:
                stale_ok = "не удалось получить свежие данные" in str(ex)
            checks.append(("свежесть: парсинг упал -> StaleDataError, разбора нет",
                           stale_ok))

            # 7. конкурент выбирается детерминированно
            globals()["fetch_one"] = _fake_fetch_one
            conn = db.connect()
            for fid, nm, rt, rc in (("aaa", "Топ-1", 4.9, 500),
                                    ("bbb", "Топ-2", 4.9, 900),
                                    ("ccc", "Слабый", 4.1, 100)):
                comp = _mk(fid, nm, rt, 1, 1, ratings=rc, revs=rc)
                db.add_lead_snapshot(conn, comp, "kostanay", "стоматология")
            conn.close()
            first = pick_competitor(cfg_k, "111")
            second = pick_competitor(cfg_k, "111")
            checks.append(("конкурент: выбор детерминирован",
                           first is not None and second is not None
                           and first.firm_id == second.firm_id))
            checks.append(("конкурент: сама компания исключена",
                           pick_competitor(cfg_k, "bbb").firm_id != "bbb"))
            checks.append(("конкурент: рейтинг берётся из снимка (общий слой)",
                           first.rating is not None and first.rating >= 4.9))

            # тай-брейк при равном рейтинге — на отдельной категории,
            # чтобы в пуле были только две сравниваемые компании
            conn = db.connect()
            for fid, nm, rc in (("bar1", "Бар-1", 500), ("bar2", "Бар-2", 900)):
                db.add_lead_snapshot(conn, _mk(fid, nm, 4.9, 1, 1, ratings=rc, revs=rc),
                                     "kostanay", "барбершоп")
            conn.close()
            cfg_b = LeadCfg(city="Костанай", category="барбершоп", output_dir=tmpdb)
            checks.append(("конкурент: при равном рейтинге — больше оценок",
                           pick_competitor(cfg_b, "zzz").firm_id == "bar2"))
            checks.append(("конкурент: повторный вызов даёт тот же выбор",
                           pick_competitor(cfg_b, "zzz").firm_id
                           == pick_competitor(cfg_b, "zzz").firm_id))

            # 8. блок конкурента по умолчанию выключен
            checks.append(("конкурент: по умолчанию выключен", SHOW_COMPETITOR is False))
            html_off, _, _ = asyncio.run(build_audit("111", cfg_k, max_age_hours=99999))
            checks.append(("разбор: без флага блока сравнения нет",
                           "Сравнение с конкурентом" not in html_off))
            html_on, _, _ = asyncio.run(
                build_audit("111", cfg_k, max_age_hours=99999, with_competitor=True))
            checks.append(("разбор: с флагом блок появляется",
                           "Сравнение с конкурентом" in html_on))
            checks.append(("разбор: в подвале дата сбора",
                           "данные собраны" in html_off.lower()))
        finally:
            globals()["fetch_one"] = real_fetch_one
            db.DB_PATH = real_db_path

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
