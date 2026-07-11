#!/usr/bin/env python3
import os
import json
import glob
import argparse


def main():
    parser = argparse.ArgumentParser(description="Получить домены с находками из JSON файлов")
    parser.add_argument("dir", nargs="?", default="results", help="Каталог с JSON файлами")
    parser.add_argument("--check", action="store_true", help="Показать все домены в каталоге")
    args = parser.parse_args()

    dir_path = os.path.abspath(args.dir)
    all_files = glob.glob(os.path.join(dir_path, "*.json"))
    all_domains = set()
    hits = []
    for f in all_files:
        try:
            with open(f, "r", encoding="utf-8") as ff:
                data = json.load(ff)
            name = os.path.basename(f).rsplit(".get.json", 1)[0] if f.endswith(".get.json") else os.path.basename(f).rsplit(".json", 1)[0]
            all_domains.add(name)
            res = data.get("results", [])
            if len(res) > 0:
                hits.append(name)
        except:
            pass

    if args.check:
        print(f"Всего доменов ({len(all_domains)}):")
        for h in sorted(all_domains):
            print(h)
    else:
        print(f"Домены с находками ({len(hits)}):")
        for h in sorted(set(hits)):
            print(h)


if __name__ == "__main__":
    main()