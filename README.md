# ai-ffuf

AI-обёртка над `ffuf`: массовый фаззинг списка целей с авто-очисткой шума и подсказками LLM.

Что делает:
- прогоняет список целей одним или несколькими HTTP-методами;
- параллельно обрабатывает цели (`--workers`);
- сам чистит шум (catch-all / soft-404), добавляя фильтры ffuf и перезапуская скан;
- периодически спрашивает LLM, какие фильтры добавить / не надо ли притормозить или остановиться;
- следит за здоровьем скана (бан 403/429, рост ошибок, зависание) и досрочно останавливает задачу;
- пропускает уже обработанные цели при повторном запуске (resume);
- собирает сводку и различия между методами.

---

## Установка и окружение

Зависимости: `ffuf` в `PATH`; Python-пакеты из `requirements.txt` (`openai`, `requests`, `json-repair` и т.д.).

LLM настраивается через `.env` (основной провайдер + опциональный fallback):

```
# Основной провайдер (OpenAI-совместимый)
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL_NAME=x-ai/grok-4.1-fast

# Fallback (опционально)
VOIDAI_BASE_URL=https://api.voidai.app/v1
VOIDAI_API_KEY=sk-...
VOIDAI_MODEL_NAME=magistral-medium-latest
```

`.env` читается из текущего каталога, либо путь задаётся `--env-file`. При ошибке основного провайдера (timeout/401/429) — автопереключение на fallback. Если ключей нет — LLM просто не вызывается, авто-очистка работает и без него.

---

## Базовый запуск

### 1. Одна цель через `path`-фаззинг

```bash
ai-ffuf \
  --targets targets.txt \
  --wordlist /opt/wordlist-for-fuzz/content_fuzz/common.txt
```

`targets.txt` — список целей по одной в строке. Вместо файла можно передать один URL:

```bash
ai-ffuf --targets https://example.com --wordlist common.txt
```

### 2. Несколько методов (GET + POST)

```bash
ai-ffuf \
  --targets targets.txt \
  --wordlist common.txt \
  --methods GET POST \
  --workers 3
```

Каждый метод — отдельный скан. Если метод не дал результатов, пробуется следующий.

### 3. POST с телом

```bash
ai-ffuf \
  --targets targets.txt \
  --wordlist params.txt \
  --methods POST \
  --post-data "a=1&b=FUZZ"
```

### 4. Свои заголовки

```bash
ai-ffuf \
  --targets targets.txt \
  --wordlist common.txt \
  --header "Cookie: session=abc" \
  --header "X-Pentest: GHACK"
```

Заголовок обязан быть в формате `Name: value`. Битый заголовок (без `:` или с пустым значением) — ошибка на старте.

### 5. Vhost-фаззинг (по заголовку Host)

```bash
ai-ffuf \
  --targets targets.txt \
  --wordlist subdomains.txt \
  --mode vhost \
  --host-suffix .example.com
```

Формирует `Host: FUZZ.example.com`. Если `--host-suffix` не задан — `Host: FUZZ`.

### 6. Ограничение скорости и потоков

```bash
ai-ffuf \
  --targets targets.txt \
  --wordlist common.txt \
  --rate 50 \
  --threads 5 \
  --p 0.1
```

`--rate` — запросов/сек, `--threads` — потоки ffuf (`-t`), `--p` — задержка между запросами (сек).

### 7. Свой `.env`

```bash
ai-ffuf --targets targets.txt --wordlist common.txt --env-file /path/to/.env
```

---

## Параметры CLI

### Основные

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--targets` | **обязательный** | Файл со списком целей (по одной URL в строке) или одна URL |
| `--wordlist` | **обязательный** | Путь к словарю |
| `--workers` | `2` | Число параллельных воркеров (по цели на воркер) |
| `--output-dir` | `results` | Каталог для JSON-вывода ffuf и сводок |
| `--blocked-dir` | `blocked` | Каталог для JSON заблокированных задач |
| `--max-restarts` | `2` | Максимум рестартов на задачу (метод) |
| `--env-file` | — | Путь к `.env` |

### HTTP

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--methods` | `GET` | HTTP-методы (через пробел), порядок важен: `GET POST` |
| `--header` | — | Заголовок `Name: value`; можно указывать несколько раз |
| `--post-data` | — | Тело POST, например `a=1&b=FUZZ` |
| `--mode` | `path` | `path` — путь в URL, `vhost` — заголовок Host |
| `--host-suffix` | — | Суффикс для vhost: `Host: FUZZ<suffix>` |

### Скорость ffuf

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `-t`, `--threads` | — | Число потоков ffuf |
| `--rate` | — | Максимум запросов в секунду |
| `--p` | — | Задержка между запросами, сек |

### LLM

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--llm-model` | `OPENAI_MODEL_NAME` или `x-ai/grok-4.1-fast` | Модель |
| `--llm-trigger` | `50` | Через сколько найденных ответов вызывать LLM (`0` — выключить) |

### Мониторинг (watchdog)

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--monitor-period-seconds` | `10` | Интервал тика watchdog |
| `--monitor-stall-seconds` | `60` | Нет вывода ffuf дольше N сек → `monitor_stall` |
| `--monitor-error-window-seconds` | `120` | Окно подсчёта роста ошибок |
| `--monitor-error-growth` | `20` | Рост ошибок на N за окно → `monitor_error_growth` |
| `--ban-403-rate` | `0.6` | Доля 403 ≥ N → `monitor_ban` |
| `--ban-429-rate` | `0.2` | Доля 429 ≥ N → `monitor_ban` |

### Доп. флаги ffuf

| Флаг | Описание |
|------|----------|
| `--ffuf-extra` | Доп. флаги ffuf строкой, фильтруются по whitelist. Можно несколько раз |
| `--ffuf-allow` | Добавить флаг в whitelist `--ffuf-extra` |
| `--ffuf-allow-reset` | Очистить дефолтный whitelist |

Whitelist `--ffuf-extra` (пример):
```
-ac -acc -ach -ack -acs -D -e -ic -ignore-body -json -maxtime -maxtime-job
-mt -r -raw -recursion -recursion-depth -recursion-strategy -s -sa -se -sf
-silent -split-by-host -timeout -v -x
```

Управляются агентом (в `--ffuf-extra` заблокированы):
```
-u -w -of -o -debug-log -mc -fc -fl -fw -fs -rate -p -t -H -X -d
```

---

## Как это работает

### Команда ffuf

Агент всегда запускает ffuf с `-mc all` (матчить все коды) и сам управляет фильтрами `fc/fl/fw/fs`. Автокалибровка (`-ac`) по умолчанию не используется. Итоговую команду видно в логе: `cmd: ffuf ...`.

### Порядок методов

Для цели методы перебираются по порядку. Нет результатов у текущего — берётся следующий. Если задача заблокирована (`monitor_stall`, `monitor_error_growth`, `monitor_ban`, `llm_drop`) — следующие методы не запускаются.

### Авто-очистка шума (auto-noise)

Агент определяет доминирующие паттерны и добавляет фильтр, после чего перезапускает ffuf.

- **GET/POST**: по сигнатурам `(words, lines, size)`, включая доминирующие `2xx/3xx` (catch-all/soft-404). Паттерн в ≥92% ответов при ≥40 совпадениях → фильтр `fs/fw/fl`.
- **HEAD/OPTIONS**: тело отсутствует, поэтому по статус-кодам → `fc`.
- Стартовые `fc`: `{404}` для `path`, `{400}` для `vhost`.
- Стабилизация: паттерн должен быть кандидатом 2 тика подряд.
- В логе `heartbeat` выводится `top(w/l/s:count)` — топ сигнатур по статусам (диагностика шума).

### LLM-анализ

Каждые `--llm-trigger` найденных ответов вызывается LLM. Ей передаётся: `status_counts`, `body_patterns` (топ-5 сигнатур на статус), текущие фильтры, метрики монитора. LLM может:

- рекомендовать фильтры `fl`/`fw`/`fs`, а также `fc` (только `403`/`404`). Несколько значений за раз применяются в один проход;
  - `fc` применяется только если статус доминирует (≥95% ответов) и его тело однородно (топ-сигнатура ≥95% внутри статуса); иначе `fc` игнорируется — чтобы не скрыть реальные `403`-ресурсы;
- рекомендовать `rate`/`threads`/`delay`;
- вернуть `drop=true` → задача блокируется с причиной `llm_drop`.

### Рестарты

Рестарт (перезапуск ffuf с накопленными фильтрами) происходит при применении авто-фильтра или изменении фильтров LLM. Лимит — `--max-restarts`; при превышении задача блокируется с причиной `restarts`.

### Resume

При повторном запуске уже обработанные цели пропускаются. Цель считается завершённой, если для каждого метода есть валидный `results/{name}.{method}.json` и он не старше `blocked/{name}.{method}.json`. Частичный результат после блокировки за завершённый не считается.

---

## Причины блокировки

| Причина | Описание |
|---------|----------|
| `monitor_stall` | Нет вывода ffuf дольше `--monitor-stall-seconds` |
| `monitor_error_growth` | Рост ошибок на `--monitor-error-growth` за окно |
| `monitor_ban` | Доля 403/429 превысила порог |
| `llm_drop` | LLM вернула `drop=true` |
| `exec` | ffuf завершился с ненулевым кодом без результатов |
| `restarts` | Исчерпан лимит `--max-restarts` |

При `rc != 0` в лог пишется `ffuf output tail` — последние строки вывода ffuf (например `stat ...: no such file or directory` при неверном словаре).

---

## Выходные файлы

| Файл | Описание |
|------|----------|
| `results/{name}.{method}.json` | JSON ffuf с результатами метода |
| `results/filter_audit.jsonl` | Журнал фильтров/рестартов/дропов (audit) |
| `blocked/{name}.{method}.json` | Причина блокировки, прогресс, ошибки |
| `results/method_summary.json` | Сводка по методам для каждой цели |
| `results/method_diffs.jsonl` | Пути, где ответы различаются между методами |
| `results/method_diffs.csv` | То же в CSV |

`{name}` — нормализованное имя цели (например `https_example_com`).

---

## Итоговый вывод

```
[PROGRESS] [##########----------] 23/46 (50%) active=1 ok=20 empty=2 blocked=1
https_example_com :: OK :: reason=- :: out=results/https_example_com.get.json
  - method=GET ok=True has_results=True reason=- out=results/https_example_com.get.json
  - method=POST ok=False has_results=False reason=monitor_ban out=-
METHOD SUMMARY: results/method_summary.json
SUMMARY total=46 completed=46 ok=30 empty=10 blocked=6
```

| Счётчик | Значение |
|---------|----------|
| `ok` | Есть результаты хотя бы по одному методу |
| `empty` | Завершено без результатов (все методы пусты) |
| `blocked` | Остановлено досрочно (бан/ошибки/stall/краш) |

---

## Типовые проблемы

- **Все цели падают с `reason=exec rc=1`.** Смотри `ffuf output tail` в логе. Частые причины: неверный путь `--wordlist`, битый `--header` (без `:`), неправильный `--ffuf-extra`.
- **Пусто, но `statuses=[]` после фильтра.** Значит весь сайт — однородный catch-all; после фильтрации совпадений нет. Это корректный результат.
- **LLM не вызывается.** Нет API-ключей в окружении/`.env` (в логе `llm: no api key in env`) либо `--llm-trigger 0`.
