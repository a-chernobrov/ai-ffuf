# Руководство по запуску и параметрам

## Установка и окружение

`.env` в корне проекта — настройка LLM chain (основной + fallback):

```
# Основной провайдер
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL_NAME=x-ai/grok-4.1-fast

# Fallback провайдер (опционально)
VOIDAI_BASE_URL=https://api.voidai.app/v1
VOIDAI_API_KEY=sk-...
VOIDAI_MODEL_NAME=magistral-medium-latest
```

При ошибке основного (timeout/401/429) — автопереключение на fallback. В логах: `LLM success/fail <url>`.

---

## Запуск

Базовый (path):

```bash
python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlists /opt/wordlist-for-fuzz/content_fuzz/common.txt \
  --workers 3
```

С несколькими словарями (fallback по порядку):

```bash
python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlists common.txt fuzz-Bo0oM.txt \
  --workers 5 \
  --methods GET POST \
  --max-restarts 3
```

Vhost-режим:

```bash
python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlists subdomains.txt \
  --workers 3 \
  --mode vhost \
  --host-suffix .example.com
```

Deep-scan (полная команда):

```bash
python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlists /opt/wordlist-for-fuzz/content_fuzz/common.txt /opt/wordlist-for-fuzz/content_fuzz/fuzz-Bo0oM.txt \
  --workers 3 \
  --methods GET POST \
  --mode deep-scan \
  --deep-source-dir /opt/my-tools/ai-ffuf/results \
  --deep-depth 3 \
  --header "X-Pentest: GHACK" \
  --max-restarts 3 \
  --rate 50 \
  --monitor-period-seconds 10 \
  --monitor-stall-seconds 60 \
  --monitor-error-window-seconds 120 \
  --monitor-error-growth 20 \
  --ban-403-rate 0.6 \
  --ban-429-rate 0.2
```

---

## Параметры CLI

### Основные

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--targets` | **обязательный** | Файл со списком целей (по одной URL в строке) |
| `--wordlists` / `--wordlist` | **обязательный** | Список словарей через пробел. Если после первого словаря нет результатов — пробуется следующий (fallback) |
| `--workers` | `2` | Число параллельных воркеров |
| `--output-dir` | `results` | Директория для JSON-результатов ffuf и method_summary |
| `--blocked-dir` | `blocked` | Директория для JSON-файлов заблокированных задач |
| `--max-restarts` | `2` | Максимум рестартов на одну задачу (метод + словарь) |

### HTTP

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--methods` | `GET POST` | HTTP-методы для фазинга, через пробел. Каждый метод — отдельный скан |
| `--post-data` | — | Тело POST-запроса, например `a=1&b=FUZZ` |
| `--header` | — | Дополнительный заголовок `Name: value`. Можно указывать несколько раз |
| `--mode` | `path` | Режим фазинга: `path` — путь в URL, `vhost` — заголовок Host, `deep-scan` — каскадный фаззинг по редирект-поинтам из seed JSON |
| `--host-suffix` | `.domain` | Суффикс для vhost-режима: `Host: FUZZ.domain` |

### Deep-scan

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--deep-source-dir` | `/opt/my-tools/ai-ffuf/results` | Каталог с seed JSON, из которых берутся стартовые 301/302 поинты для deep-scan |
| `--deep-depth` | `1` | Глубина каскада: сколько шагов (`step1`, `step2`, ...) выполнять |

### Скорость ffuf

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `-t` / `--threads` | — | Число потоков ffuf (`-t`) |
| `--rate` | — | Максимальная скорость запросов в секунду (`-rate`) |
| `--p` | — | Задержка между запросами в секундах (`-p`), например `0.1` |

### LLM

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--llm-model` | `OPENAI_MODEL_NAME` или `x-ai/grok-4.1-fast` | Модель LLM |
| `--llm-trigger` | `50` | Каждые N обработанных ответов ffuf — вызов LLM для анализа и рекомендации фильтров |

### Мониторинг (watchdog)

Watchdog-поток проверяет состояние ffuf каждые `--monitor-period-seconds`.

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--monitor-period-seconds` | `10` | Интервал тика watchdog в секундах |
| `--monitor-stall-seconds` | `60` | Если нет вывода от ffuf дольше N секунд → drop с причиной `monitor_stall` |
| `--monitor-error-window-seconds` | `120` | Скользящее окно для подсчёта роста ошибок |
| `--monitor-error-growth` | `20` | Если ошибки выросли на N за окно → drop с причиной `monitor_error_growth` |
| `--ban-403-rate` | `0.6` | Если доля 403 от всех ответов ≥ N → drop с причиной `monitor_ban` |
| `--ban-429-rate` | `0.2` | Если доля 429 от всех ответов ≥ N → drop с причиной `monitor_ban` |

### Дополнительные флаги ffuf

| Флаг | По умолчанию | Описание |
|------|-------------|----------|
| `--ffuf-extra` | — | Передать дополнительные флаги в ffuf. Фильтруется по whitelist. Можно указывать несколько раз |
| `--ffuf-allow` | — | Добавить флаг в whitelist `--ffuf-extra`. Можно указывать несколько раз |
| `--ffuf-allow-reset` | `false` | Очистить дефолтный whitelist перед применением `--ffuf-allow` |

**Whitelist `--ffuf-extra` (разрешены):**
```
-ac -acc -ach -ack -acs -D -e -ic -ignore-body -json -maxtime -maxtime-job
-mt -r -raw -recursion -recursion-depth -recursion-strategy -s -sa -se -sf
-silent -split-by-host -timeout -v -x
```

**Заблокированы в `--ffuf-extra` (управляются агентом):**
```
-u -w -of -o -debug-log -mc -fc -fl -fw -fs -rate -p -t -H -X -d
```

---

## Поведение

### Wordlists fallback

Для каждого метода словари перебираются по порядку. Если ffuf завершился без результатов — пробуется следующий словарь. Если задача заблокирована (`monitor_stall`, `monitor_error_growth`, `monitor_ban`, `llm_drop`) — следующий словарь не пробуется.

### Deep-scan логика

`deep-scan` работает по методам так же, как обычный `path`-режим, но стартует не с общего словаря путей, а с найденных редиректов из seed JSON.

1. Для target + method ищется seed: `{deep_source_dir}/{sanitize_name}.{method}.json`.
2. Seed пропускается, если файл отсутствует или пустой.
3. Берутся только ответы `301/302`, где `redirectlocation`:
   - указывает на тот же `scheme://host`;
   - остаётся в том же поинте (например `/docs` -> `/docs/` или `/docs/...`).
   Редиректы на другие поинты/домены отбрасываются.
4. Для сайта создаётся каталог `results/{sanitize_name}`.
5. Для каждого шага формируется словарь путей `results/{sanitize_name}/{method}_stepN_paths.txt`.
6. Запускается ffuf с двумя словарями:
   - `-w {method}_stepN_paths.txt:PATH`
   - `-w <обычный словарь>:FUZZ`
   URL-шаблон: `target/PATH/FUZZ`.
7. Результат шага пишется в `results/{sanitize_name}/{method}_stepN.json`.
8. Из `stepN.json` снова извлекаются валидные `301/302` по тем же правилам, и цикл повторяется до `--deep-depth`.

### Фильтры шума (auto-noise)

Агент автоматически определяет шумовые паттерны и добавляет фильтры, после чего перезапускает ffuf.

- **GET/POST**: анализируются паттерны по размеру (`fs`), словам (`fw`), строкам (`fl`). Если паттерн доминирует в ≥92% ответов при ≥40 совпадениях — добавляется фильтр.
- **HEAD/OPTIONS**: тело ответа отсутствует, поэтому анализируются статус-коды (`fc`). Если статус доминирует в ≥92% ответов при ≥40 совпадениях — добавляется в `fc`.
- Начальные `fc`: `{400, 403, 404}`.
- Стабилизация: паттерн должен быть кандидатом 2 тика подряд перед применением.

### LLM-анализ

Каждые `--llm-trigger` ответов ffuf вызывается LLM. LLM может:
- Рекомендовать фильтры `fl`/`fw`/`fs` → применяются и ffuf перезапускается.
- Вернуть `drop=true` → задача блокируется с причиной `llm_drop`.

### Рестарты

Рестарт происходит при:
- Применении noise-фильтра (авто).
- Рекомендации LLM с изменением фильтров.
- Превышении `--max-restarts` → блокировка с причиной `restarts`.

---

## Счётчики прогресса

```
[PROGRESS] [=====>    ] 5/20 (25%) active=3 ok=2 empty=1 blocked=0
```

| Счётчик | Значение |
|---------|----------|
| `ok` | Найдены результаты хотя бы одним методом/словарём |
| `empty` | Завершено без результатов (все методы и словари пусты) |
| `blocked` | Остановлено досрочно из-за бана/ошибок/stall/краша |

---

## Причины блокировки

| Причина | Описание |
|---------|----------|
| `monitor_stall` | Нет вывода от ffuf дольше `--monitor-stall-seconds` |
| `monitor_error_growth` | Ошибки выросли на `--monitor-error-growth` за окно `--monitor-error-window-seconds` |
| `monitor_ban` | Доля 403 или 429 превысила порог |
| `llm_drop` | LLM вернул `drop=true` |
| `exec` | ffuf завершился с ненулевым кодом без результатов |
| `restarts` | Исчерпан лимит `--max-restarts` |

---

## Выходные файлы

| Файл | Описание |
|------|----------|
| `results/{name}.{method}.json` | JSON-вывод ffuf с результатами |
| `results/{name}/{method}_stepN_paths.txt` | Словарь путей для шага deep-scan |
| `results/{name}/{method}_stepN.json` | JSON шага deep-scan (`step1`, `step2`, ...) |
| `blocked/{name}.json` | JSON с причиной блокировки, прогрессом и ошибками |
| `results/method_summary.json` | Сводка по методам для каждой цели |
| `results/method_diffs.jsonl` | Пути, где ответы отличаются между методами |
| `results/method_diffs.csv` | То же в CSV |

---

## Итоговый вывод

```
http_example_com.get :: OK :: reason=- :: out=results/http_example_com.get.json
  - method=GET ok=True has_results=True reason=- out=results/http_example_com.get.json
  - method=POST ok=False has_results=False reason=monitor_ban out=-
SUMMARY total=10 completed=10 ok=7 empty=2 blocked=1
```
