import argparse
import glob
import json
import os
import sys


def parse_status_filters(raw_values):
    if not raw_values:
        return []
    tokens = []
    for raw in raw_values.split(","):
        value = raw.strip()
        if not value:
            continue
        tokens.append(value)
    return tokens


def status_in_filters(status, tokens):
    if not tokens:
        return True
    for token in tokens:
        if token.endswith("xx") and len(token) == 3 and token[0].isdigit():
            base = int(token[0]) * 100
            if base <= status <= base + 99:
                return True
            continue
        if "-" in token:
            parts = token.split("-", 1)
            if parts[0].isdigit() and parts[1].isdigit():
                start = int(parts[0])
                end = int(parts[1])
                if start <= status <= end:
                    return True
                continue
        if token.isdigit() and int(token) == status:
            return True
    return False


def status_not_excluded(status, tokens):
    if not tokens:
        return True
    for token in tokens:
        if token.endswith("xx") and len(token) == 3 and token[0].isdigit():
            base = int(token[0]) * 100
            if base <= status <= base + 99:
                return False
            continue
        if "-" in token:
            parts = token.split("-", 1)
            if parts[0].isdigit() and parts[1].isdigit():
                start = int(parts[0])
                end = int(parts[1])
                if start <= status <= end:
                    return False
                continue
        if token.isdigit() and int(token) == status:
            return False
    return True


def within_range(value, min_value, max_value):
    if min_value is not None and value < min_value:
        return False
    if max_value is not None and value > max_value:
        return False
    return True


def iter_input_files(input_path):
    if os.path.isfile(input_path):
        return [input_path]
    pattern = os.path.join(input_path, "*.json")
    return sorted(glob.glob(pattern))


def load_results(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        print(f"Ошибка чтения {file_path}: {exc}", file=sys.stderr)
        return []
    if isinstance(data, dict):
        results = data.get("results", [])
        if isinstance(results, list):
            return results
    if isinstance(data, list):
        return data
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Парсер ffuf json. Выводит URL и статусы с фильтрами."
    )
    parser.add_argument(
        "--input",
        default=os.path.join(os.getcwd(), "json"),
        help="Каталог с json или конкретный файл",
    )
    parser.add_argument(
        "--status",
        default="",
        help="Статусы для включения: 200,301,2xx,200-399",
    )
    parser.add_argument(
        "--exclude-status",
        default="",
        help="Статусы для исключения: 404,5xx,400-499",
    )
    parser.add_argument("--min-length", type=int, default=None)
    parser.add_argument("--max-length", type=int, default=None)
    parser.add_argument("--min-lines", type=int, default=None)
    parser.add_argument("--max-lines", type=int, default=None)
    parser.add_argument("--min-words", type=int, default=None)
    parser.add_argument("--max-words", type=int, default=None)
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    include_status = parse_status_filters(args.status)
    exclude_status = parse_status_filters(args.exclude_status)

    files = iter_input_files(args.input)
    if not files:
        print("Файлы не найдены", file=sys.stderr)
        return 1

    if not args.no_header:
        print("file\tstatus\tlength\twords\tlines\turl")

    emitted = 0
    for file_path in files:
        results = load_results(file_path)
        for item in results:
            status = item.get("status")
            length = item.get("length")
            words = item.get("words")
            lines = item.get("lines")
            url = item.get("url")
            if status is None or url is None:
                continue
            if not status_in_filters(int(status), include_status):
                continue
            if not status_not_excluded(int(status), exclude_status):
                continue
            if length is None:
                length = 0
            if words is None:
                words = 0
            if lines is None:
                lines = 0
            if not within_range(int(length), args.min_length, args.max_length):
                continue
            if not within_range(int(lines), args.min_lines, args.max_lines):
                continue
            if not within_range(int(words), args.min_words, args.max_words):
                continue
            print(
                f"{os.path.basename(file_path)}\t{status}\t{length}\t{words}\t{lines}\t{url}"
            )
            emitted += 1
            if args.limit is not None and emitted >= args.limit:
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
