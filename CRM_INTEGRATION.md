# Интеграция с CRM Bedel

У этого проекта есть внешний потребитель кода: **CRM Bedel**
(`C:\Users\Emil\Desktop\CRM Bedel`, репозиторий `crm-bedel`) — отдельное
FastAPI-приложение для ведения холодных B2B-продаж (стоматологии и локальный
бизнес в Казахстане). Оно не парсит 2ГИС само — за метрики карточки отвечает
reviews2gis. С сентября 2026 связь двусторонняя:

1. **CRM тянет метрики карточки** — по кнопке «Обновить из 2ГИС» / при
   добавлении лида.
2. **reviews2gis сам отдаёт в CRM новых лидов** — после каждого прогона
   лидогенерации (`leadgen.py`, CLI `main.py` или страница «Поиск лидов»).

> ⚠️ Как и в README.md этого проекта: документация — часть кода. Если меняете
> `Company` (`lead_scoring.py`), `HttpFetcher.parse_firm` (`fetcher.py`),
> `Config`/`city_to_slug` (`config.py`) или сами эндпоинты моста в `app.py`/
> `leadgen.py` — **обновите этот файл в том же коммите** и проверьте
> `C:\Users\Emil\Desktop\CRM Bedel\parser_adapter.py` (класс
> `Reviews2GisParserAdapter`) и `routers/leads.py` (эндпоинт `/leads/push`).
> Расхождение здесь означает, что CRM тихо сломается или начнёт показывать
> неверные метрики — она не в этом репозитории, отдельным CI её не проверить.

## Как устроена связь: HTTP-мост

Оба направления идут по HTTP через `app.py` reviews2gis и `main.py`/
`routers/leads.py` CRM — это работает одинаково локально (Windows, оба
процесса на одной машине) и в проде (два разных Docker-контейнера на
домашнем сервере, разные файловые системы). Мост защищён общим секретом
`BRIDGE_API_KEY` (одно и то же значение в `.env` обоих проектов), передаётся
заголовком `X-Bridge-Key`. Пустой `BRIDGE_API_KEY` выключает соответствующий
эндпоинт (404) — так его не открыть по недосмотру в локальной разработке.

### 1. CRM → reviews2gis: чтение метрик карточки

`GET /api/firm/{firm_id}?city=<город>` (reviews2gis, `app.py`).

Внутри — тот же вызов, что использует собственный лидген/мониторинг:

```python
async with fetcher.HttpFetcher(cfg) as f:
    company = await f.parse_firm(firm_id)   # -> lead_scoring.Company
```

Это реальный сетевой запрос к 2ГИС (не чтение `monitor.db`) — CRM получает
свежие данные при каждом вызове. Ответ — JSON с полями `rating`,
`ratings_count`, `reviews_count`, `unanswered_count`, `fresh_count` — их же
ждёт `LeadMetrics` в CRM.

Со стороны CRM это `Reviews2GisParserAdapter._fetch_via_http()`
(`parser_adapter.py`), включается настройками `.env`: `PARSER_BACKEND=
reviews2gis` + `REVIEWS2GIS_API_URL=http://<host>:8765` +
`REVIEWS2GIS_API_KEY=<тот же BRIDGE_API_KEY>`.

**Локальный фолбэк без HTTP.** Если `REVIEWS2GIS_API_URL` не задан, адаптер
откатывается на прежний способ — прямой импорт `fetcher.py`/`config.py` по
`REVIEWS2GIS_PATH` (`importlib.util`, каталог reviews2gis в `sys.path`, модули
грузятся под именами `reviews2gis_fetcher`/`reviews2gis_config`, чтобы не
конфликтовать с собственным `config.py` CRM). Работает только когда оба
проекта на одной машине — так до сих пор запущено локально на Windows,
специально ничего не меняли, чтобы не ломать текущую разработку.

### 2. reviews2gis → CRM: пуш новых лидов

`POST /leads/push` (CRM, `routers/leads.py`) — JSON-список лидов, заголовок
`X-Bridge-Key`. Вызывается из `leadgen.push_to_crm()` (`leadgen.py`), которую
`run_leads()` дёргает в конце каждого прогона — единая точка для CLI и
панели. Управляется `.env`-параметром `CRM_PUSH_URL` в reviews2gis (пусто =
фича выключена, лиды остаются только в `output/*.csv`, как раньше).

Payload — по одному объекту на компанию:

| Поле payload | Источник (`Company`/`cfg`) |
|---|---|
| `firm_id` | `Company.firm_id` |
| `name`, `category`, `url`, `phone` | `Company.name/category/url/phone` |
| `city` | `cfg.city` (на `Company` этого поля нет — прогон один на весь город) |
| `rating`, `ratings_count`, `reviews_count` | те же поля `Company` |
| `unanswered_count`, `fresh_count` | те же поля `Company` |
| `priority`, `priority_reason` | `Company.priority/priority_reason` (справочно — CRM пересчитывает приоритет своей формулой, см. ниже) |

**Дедуп на стороне CRM — по `card_id`** (это и есть `firm_id`, стабильный и
уникальный):
- лида с таким `card_id` ещё нет → создаётся `Lead` (`status="Новый"`) +
  начальный `Snapshot`; приоритет считает `logic.compute_priority()` — CRM не
  доверяет строке `priority` из payload, у неё своя формула (сейчас совпадает
  с `lead_scoring.decide_priority`, но это два независимых места, могут
  разойтись — см. предупреждение выше);
- лид уже есть → обновляются только метрики-колонки + добавляется новый
  `Snapshot`; `status`/`contact_*`/`phone`/`instagram`/`whatsapp`/`notes`/
  `next_touch_date` **не трогаются** — это ручная работа менеджера в CRM,
  автопуш её не перезаписывает.

Пуш — **best-effort**: сбой (CRM недоступна, сеть) ловится в
`leadgen.push_to_crm`, пишется `[warn]` в лог и **не роняет и не блокирует**
прогон — CSV и пакет рассылки формируются как обычно, лиды просто не попадут
в CRM автоматически в этот раз.

## Известное ограничение (не баг, но нужно знать)

Если `firm_id` не существует или карточка недоступна, `parse_firm` не всегда
бросает исключение: если reviews-API уже закэшировало рабочий ключ с
предыдущего успешного вызова в этом же процессе (`remember_key` /
`_KEY_CACHE`), оно может вернуть пустой, но валидный ответ вместо ошибки —
и запрос к `/api/firm/{id}` отдаст CRM нули по всем полям вместо явной
ошибки. Это осознанное поведение `fetcher.py` («страница не открылась — это
ещё не провал, отзывы можно взять из API по уже известному ключу»), не то,
что стоит менять ради одного внешнего потребителя — но если вы правите эту
логику, имейте в виду, что молчаливые нули на стороне CRM могут быть
последствием.

## Где смотреть подробнее

- Сторона CRM: `C:\Users\Emil\Desktop\CRM Bedel\parser_adapter.py` (класс
  `Reviews2GisParserAdapter`), `routers/leads.py` (эндпоинт `/leads/push`) и
  `ARCHITECTURE.md` там же.
- Деплой на сервер (оба проекта рядом, `host.docker.internal` между
  контейнерами) — [[deploy-home-server]] (скилл), сервер 100.85.183.12.
