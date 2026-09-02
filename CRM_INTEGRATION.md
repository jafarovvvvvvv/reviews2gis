# Интеграция с CRM Bedel

У этого проекта появился внешний потребитель кода: **CRM Bedel**
(`C:\Users\Emil\Desktop\CRM Bedel`) — отдельное FastAPI-приложение для ведения
холодных B2B-продаж (стоматологии и локальный бизнес в Казахстане). Оно не
парсит 2ГИС само — за метрики карточки отвечает reviews2gis, вызываемый
напрямую из процесса CRM.

> ⚠️ Как и в README.md этого проекта: документация — часть кода. Если меняете
> `Company` (`lead_scoring.py`), `HttpFetcher.parse_firm` (`fetcher.py`) или
> `Config` (`config.py`) — **обновите этот файл в том же коммите** и проверьте
> `C:\Users\Emil\Desktop\CRM Bedel\parser_adapter.py`
> (класс `Reviews2GisParserAdapter`). Расхождение здесь означает, что CRM
> тихо сломается или начнёт показывать неверные метрики — она не в этом
> репозитории, отдельным CI её не проверить.

## Как устроена связь

У reviews2gis нет ни пакета, ни HTTP API с метриками — только модули в этом
каталоге. Поэтому CRM Bedel подключается напрямую: добавляет этот каталог в
`sys.path` и загружает `fetcher.py` + `config.py` через `importlib.util` (под
именами `reviews2gis_fetcher` / `reviews2gis_config` — просто `import config`
внутри CRM конфликтует с её собственным модулем `config.py`). Дальше
вызывается ровно та же функция, что использует собственный лидген/мониторинг
этого проекта:

```python
async with fetcher.HttpFetcher(cfg) as f:
    company = await f.parse_firm(firm_id)   # -> lead_scoring.Company
```

Это реальный сетевой запрос к 2ГИС (не чтение `monitor.db`) — CRM получает
свежие данные при каждом «Добавить лида» / «Обновить из 2ГИС», а не то, что
когда-то долетело в базу мониторинга. Оба проекта работают на одном системном
Python 3.12, `httpx` уже установлен — версии совместимы
(reviews2gis: `httpx==0.28.1`, CRM Bedel: `httpx>=0.27.0`).

## Контракт полей

CRM Bedel ожидает от `parse_firm()` объект `Company` со следующими полями —
переименование, удаление или смена типа любого из них ломает адаптер:

| `Company` (этот проект) | `LeadMetrics` (CRM Bedel) | Смысл |
|---|---|---|
| `rating` | `rating` | Рейтинг филиала — готовый от 2ГИС |
| `ratings_count` | `ratings_count` | Всего оценок (база рейтинга) |
| `reviews_count` | `reviews_count` | Текстовых отзывов |
| `unanswered_count` | `unanswered_count` | Без ответа владельца (в окне `metrics.WINDOW`) |
| `fresh_count` | `fresh_reviews_30d` | Отзывов за последние 30 дней |

Также важны сигнатуры и поля, от которых зависит вызов:
- `fetcher.HttpFetcher.__init__(self, cfg)` — принимает объект с атрибутами
  `city`, `category`, `count`, `city_slug`, `headless`, `output_dir` (CRM
  создаёт `config.Config(...)` напрямую, а не через `load_config()`).
- `config.Config` (dataclass) и `config.city_to_slug(city: str) -> str` —
  CRM передаёт название города лида (например, «Алматы») и получает slug для
  URL карточки.
- `fetcher.HttpFetcher.parse_firm(self, firm_id: str) -> Company` — должен
  оставаться асинхронным и принимать `firm_id` как единственный позиционный
  аргумент.

`card_id` со стороны CRM — это `firm_id` 2ГИС (число из `.../firm/<firm_id>`),
хранится в поле `Lead.card_id`. Город лида (`Lead.city`) передаётся в адаптер
и используется только для построения URL — на сам поиск компании не влияет.

## Известное ограничение (не баг, но нужно знать)

Если `firm_id` не существует или карточка недоступна, `parse_firm` не всегда
бросает исключение: если reviews-API уже закэшировало рабочий ключ с
предыдущего успешного вызова в этом же процессе (`remember_key` /
`_KEY_CACHE`), оно может вернуть пустой, но валидный ответ вместо ошибки —
и CRM получит `LeadMetrics` с нулями по всем полям вместо явной ошибки. Это
осознанное поведение `fetcher.py` («страница не открылась — это ещё не
провал, отзывы можно взять из API по уже известному ключу»), не то, что
стоит менять ради одного внешнего потребителя — но если вы правите эту логику,
имейте в виду, что молчаливые нули на стороне CRM могут быть последствием.

## Где смотреть подробнее

- Сторона CRM: `C:\Users\Emil\Desktop\CRM Bedel\parser_adapter.py`
  (класс `Reviews2GisParserAdapter`) и его
  `C:\Users\Emil\Desktop\CRM Bedel\ARCHITECTURE.md` (раздел «Адаптер парсера
  2ГИС»).
- Настройка на стороне CRM: `.env` → `PARSER_BACKEND=reviews2gis` +
  `REVIEWS2GIS_PATH=C:\Users\Emil\reviews2gis` (по умолчанию `stub` —
  детерминированная заглушка без сети, используется в тестах CRM независимо
  от этого флага).
