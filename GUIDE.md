# Руководство по запуску и параметрам

Это руководство описывает запуск фазинга через `crew_ffuf.py`, работу агента `ffuf_agent.py`, назначение всех параметров и рекомендуемые значения. Документ актуален для режимов `path` и `vhost` и учитывает динамические рестарты, анти‑rate, фильтры, телеметрию и UI.

## Установка и окружение

- `.env`
  - `OPENAI_BASE_URL` — базовый URL провайдера совместимого с OpenAI API.
  - `OPENAI_API_KEY` — ключ доступа.
  - `OPENAI_MODEL_NAME` — модель по умолчанию.
- Чтение `.env` выполняется при старте (`crew_ffuf.py:35`).
- Модель берётся из `OPENAI_MODEL_NAME`, но может быть передана через `--llm-model` (`crew_ffuf.py:60`).

## Запуск

Пример базового запуска в режиме `path`:

```bash
python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlist /opt/wordlist-for-fuzz/content_fuzz/common.txt \
  --workers 3 \
  --output-dir results
```

Пример запуска с LLM и порогами ошибок:

```bash
python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlist /opt/wordlist-for-fuzz/content_fuzz/common.txt \
  --workers 3 \
  --mode vhost \
  --host-suffix .domain \
  --llm-model gpt-4o-mini \
  --llm-trigger 50 \
  --early-error-rate 0.3 \
  --early-error-window 30 \
  --mid-error-rate 0.25 \
  --mid-error-window 1000
```

## Параметры CLI и их смысл

- `--targets` (обязательный) — путь к файлу со списком целей (по одной в строке). `crew_ffuf.py:53`
- `--wordlist` (обязательный) — путь к словарю. `crew_ffuf.py:54`
- `--workers` — число параллельных воркеров. По умолчанию `2`. `crew_ffuf.py:55`
- `--output-dir` — куда писать результаты CSV и лог `agent_events.jsonl`. По умолчанию `results`. `crew_ffuf.py:56,78`
- `--blocked-dir` — куда писать JSON‑файлы блокировок (stall/ban/errors/exec). По умолчанию `blocked`. `crew_ffuf.py:57`
- `--max-restarts` — максимум рестартов на задачу (включая stall/ban/ранние ошибки/LLM триггеры). По умолчанию `2`. `crew_ffuf.py:58`
- `--stall-seconds` — порог «молчания» процесса, после которого воркер считается зависшим и перезапускается. По умолчанию `30`. `crew_ffuf.py:59`, обработка в `ffuf_agent.py:130–157`
- `--llm-model` — модель LLM. По умолчанию `OPENAI_MODEL_NAME` или `x-ai/grok-4.1-fast`. `crew_ffuf.py:60`
- `--llm-trigger` — порог прогресса для первого LLM‑рестарта по телеметрии. По умолчанию `50`. `crew_ffuf.py:61`, используется в `ffuf_agent.py:638–662`
- `--header` — дополнительные HTTP заголовки для ffuf, можно указывать несколько. `crew_ffuf.py:62`
- `--max-error-rate` — максимальная доля ошибок от текущего прогресса (`errors/cur`), при превышении блокируем задачу «errors». По умолчанию `0.5`. `crew_ffuf.py:63`, логика `ffuf_agent.py:235–248` и `ffuf_agent.py:555–564`
- `--error-block-min` — минимальный прогресс для учёта `max_error_rate`. По умолчанию `200`. `crew_ffuf.py:64`
- `--ban-403-rate` — доля `403` от статусов, при которой подозревается бан и включается анти‑rate/бэкофф. По умолчанию `0.6`. `crew_ffuf.py:65`, `ffuf_agent.py:590–607`
- `--ban-429-rate` — доля `429` от статусов для бан‑логики. По умолчанию `0.2`. `crew_ffuf.py:66`, `ffuf_agent.py:590–607`
- `--threads`/`-t` — передаётся в ffuf (`-t`). Если не указано, можно адаптировать через анти‑rate. `crew_ffuf.py:67`, `ffuf_agent.py:83–100, 393–396`
- `--mode` — `path` или `vhost`. В `vhost` используется заголовок `Host: FUZZ{suffix}` и вывод в `results/vhost`. `crew_ffuf.py:68`, `ffuf_agent.py:413–437, 496`
- `--host-suffix` — суффикс для VHOST‑фазинга (например, `.domain`). `crew_ffuf.py:69`, `ffuf_agent.py:429–431`
- `--early-error-rate` — «ранний» порог ошибок от прогресса для быстрого снижения `-rate` и рестарта. По умолчанию `0.3`. `crew_ffuf.py:70`, `ffuf_agent.py:568–588`
- `--early-error-window` — минимальный прогресс для раннего порога. По умолчанию `30`. `crew_ffuf.py:71`
- `--ban-backoff-seconds` — пауза при подозрении на бан. По умолчанию `60`. `crew_ffuf.py:72`, `ffuf_agent.py:624–629`
- `--mid-error-rate` — «средний» порог ошибок от текущего прогресса (`errors/cur`), при достижении вызывается LLM и выполняется рестарт с применением фильтров/анти‑rate. По умолчанию `None` (выключено). `crew_ffuf.py:72–73`, `ffuf_agent.py:600–629`
- `--mid-error-window` — минимальный прогресс для оценки среднего порога. По умолчанию `None`. `crew_ffuf.py:72–73`

## Поведение, рестарты и анти‑rate

- Рестарты происходят при:
  - Ранний всплеск ошибок: уменьшаем `-rate` (в 2 раза, минимум `5`) и перезапускаем (`ffuf_agent.py:568–588`).
  - Подозрение на бан по `403/429`: корректируем `-t`, `-p`, можем вызвать LLM рекомендации, делаем паузу `backoff` и перезапуск (`ffuf_agent.py:590–629`).
  - Stall: «молчание» дольше `--stall-seconds` → перезапуск (`ffuf_agent.py:130–157, 630–636`).
  - LLM‑триггер по прогрессу: рекомендуем фильтры (`fl/fw/fs`) и при реальном изменении — перезапуск (`ffuf_agent.py:638–662`).
  - Средний порог ошибок (`mid-error-rate`/`mid-error-window`): запрашиваем фильтры/анти‑rate у LLM и перезапускаем (`ffuf_agent.py:600–629`).
- Анти‑rate может изменять параметры запуска:
  - `-rate` — скорость запросов.
  - `-t` — количество потоков.
  - `-p` — задержка между запросами.
  - `backoff` — пауза перед перезапуском.

## Фильтры

- Базовые `fc` по умолчанию — только `{403,404}` и не расширяются моделью: любые попытки добавить другие статус‑коды игнорируются (`ffuf_agent.py:182–196, 439–445`).
- Рекомендуется фокусироваться на `fl`, `fw`, `fs` для устранения шума/дубликатов.
- Применение фильтров логируется, итоговые фильтры видны в сообщениях `finished ok` и в UI (`crew_ffuf.py:356–366`).

## UI: Active Jobs

- Колонки: `Name`, `Status`, `Reason`, `Progress`, `LastOut(s)`, `Errors`, `LLM Filters`, `Anti-rate` (`crew_ffuf.py:103–184`).
- Цвета статусов: `backoff` — жёлтый, `stall` — красный, `restarting` — циан (`crew_ffuf.py:175–183`).
- В режиме `vhost` таблица ограничена максимум 10 строками, в `path` — 20 (`crew_ffuf.py:119`).
- Сводка внизу показывает итоги: `Total/Completed/OK/Failed` (`crew_ffuf.py:392–406`).

## Логи

- Детальный агентный лог: `agent_events.jsonl` в `--output-dir`.
- Лог содержит каждое событие с полями статуса, причины, прогресса и текущих/финальных фильтров (`crew_ffuf.py:368–389`).
- FFUF пишет debug‑лог в `*.ffuf.log` рядом с CSV (`ffuf_agent.py:413–420`).

## Результаты

- Итоговые результаты задач печатаются построчно (читаемо), формат строки:
  - `name :: OK|FAILED :: csv=... :: filters=... :: statuses=...` (`crew_ffuf.py:510–518` заменено на построчный вывод).
- CSV‑файлы сохраняются в `--output-dir`, для `vhost` — в подпапке `vhost` (`ffuf_agent.py:496`).

## Рекомендации по настройке

- Для шумных хостов:
  - Установите `--llm-trigger 50–100` для ранней оптимизации фильтров.
  - `--mid-error-rate 0.2–0.3` и `--mid-error-window 500–2000` чтобы реагировать на накопление ошибок.
  - Снизьте `--ban-backoff-seconds` до `20–30`, если WAF быстро «отпускает».
- Для жёстких WAF:
  - Увеличьте `--max-restarts` до `5–8`.
  - Следите за `Anti-rate` колонкой, корректируйте `-t`/`-p`/`-rate` через пороги.

## Примечания

- Вся телеметрия (прогресс/ошибки/статусы) учитывается из stdout ffuf (`ffuf_agent.py:458–479`).
- Рестарт всегда гарантированно запускает новый ffuf (без зависаний) (`ffuf_agent.py:715–716`).
- В UI причина рестарта отражается: `llm_filters`, `early_error`, `ban_backoff`, `mid_error` (`crew_ffuf.py:263–274`).


## Запуск

python /opt/my-tools/ai-agents-pentest/crew_ffuf.py --targets domain.txt --workers 10 --wordlist /opt/wordlist-for-fuzz/content_fuzz/content_discovery_nullenc0de.txt --llm-trigger 100 --mid-error-rate 0.05 --mid-error-window 2000 --ban-backoff-seconds 500 --header 'X-Pentest: GHACK'


python3 crew_ffuf.py \
  --targets targets.txt \
  --wordlist /path/to/100k.txt \
  --workers 3 \
  --output-dir results \
  --blocked-dir blocked \
  --late-error-min-progress 80000 \
  --late-error-window 2000 \
  --late-error-rate 0.02