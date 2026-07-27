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
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import city_to_slug
from fetcher import BlockedError, FetchError, fetch_ways, fetcher_class
from lead_scoring import (CSV_HEADERS, Company, PRIORITY_HIGH, PRIORITY_LOW,
                          PRIORITY_MEDIUM, sort_key)
from outreach import build_rows, write_outreach_pack

MAX_COUNT = 120   # потолок за прогон: ~1 с на карточку + пауза, дальше долго
DEF_COUNT = 30

# Пауза между карточками, сек. Браузеру нужна человекоподобность, HTTP хватает
# короткой — он и так делает один запрос вместо загрузки всей страницы.
PAUSE = {"http": (0.3, 0.9), "browser": (1.5, 4.0)}

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
    return {"companies": companies, "rows": build_rows(companies, cfg), "way": way,
            "csv": write_csv(companies, cfg), "pack": write_outreach_pack(companies, cfg)}


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

    ok = 0
    for label, passed in checks:
        print(f"  [{'OK ' if passed else 'FAIL'}] {label}")
        ok += passed
    print(f"{ok}/{len(checks)} passed")
