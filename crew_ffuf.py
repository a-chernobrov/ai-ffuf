import os
import csv
import json
import time
import queue
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor

from ffuf_agent import run_one


def load_local_env():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, ".env")
        if not os.path.exists(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and v and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--targets", required=True)
    p.add_argument("--wordlists", nargs="+", required=True, help="Список словарей")
    p.add_argument("--wordlist", nargs="+", dest="wordlists", help="Алиас для --wordlists")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--output-dir", default="results")
    p.add_argument("--blocked-dir", default="blocked")
    p.add_argument("--max-restarts", type=int, default=2)
    p.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL_NAME", "x-ai/grok-4.1-fast"))
    p.add_argument("--llm-trigger", type=int, default=50, help="Через сколько status-строк дергать LLM")
    p.add_argument("--header", action="append", default=[], help="Доп. заголовки: 'Name: value'")
    p.add_argument("--methods", nargs="+", default=["GET", "POST"], help="Порядок HTTP-методов, например: GET POST")
    p.add_argument("--post-data", default=None, help="Тело для POST (опционально), например 'a=1&b=FUZZ'")
    p.add_argument("--mode", choices=["path", "vhost", "deep-scan"], default="path")
    p.add_argument("--host-suffix", default=".domain")
    p.add_argument("--deep-source-dir", "--deep-source", dest="deep_source_dir", default="/opt/my-tools/ai-ffuf/results")
    p.add_argument("--deep-depth", type=int, default=1)
    p.add_argument("--deep-paths-file", default=None, help="Файл со стартовыми каталогами для deep-scan (по одному каталогу в строке)")
    p.add_argument("--deep-paths-merge", action="store_true", help="Объединить --deep-paths-file с авто-выбором из seed 301/302")
    p.add_argument("-t", "--threads", type=int, default=None)
    p.add_argument("--rate", type=int, default=None)
    p.add_argument("--p", type=float, default=None)
    p.add_argument("--monitor-period-seconds", type=int, default=10)
    p.add_argument("--monitor-stall-seconds", type=int, default=60)
    p.add_argument("--monitor-error-window-seconds", type=int, default=120)
    p.add_argument("--monitor-error-growth", type=int, default=20)
    p.add_argument("--ban-403-rate", type=float, default=0.6)
    p.add_argument("--ban-429-rate", type=float, default=0.2)
    p.add_argument("--ffuf-extra", action="append", default=[], help="Доп. флаги ffuf одной строкой")
    p.add_argument("--ffuf-allow", action="append", default=[], help="Добавить флаг в whitelist --ffuf-extra")
    p.add_argument("--ffuf-allow-reset", action="store_true", help="Очистить дефолтный whitelist --ffuf-extra")
    return p.parse_args()


def build_method_reports(results, output_dir):
    summary = []
    diffs = []
    for r in results:
        name = r.get("name")
        info = r.get("info") or {}
        scans = info.get("scans") or []
        method_entries = {}
        method_status_counts = {}
        for s in scans:
            method = (s.get("method") or "").upper()
            p = s.get("json")
            if not method or not p or (not os.path.exists(p)):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    j = json.load(f)
            except Exception:
                continue
            arr = j.get("results") or []
            idx = {}
            sc = {}
            for it in arr:
                fuzz = str((it.get("input") or {}).get("FUZZ", ""))
                if not fuzz:
                    continue
                st = it.get("status")
                ln = it.get("lines")
                wd = it.get("words")
                sz = it.get("length")
                rl = it.get("redirectlocation")
                idx[fuzz] = {"status": st, "lines": ln, "words": wd, "size": sz, "redirect": rl, "url": it.get("url")}
                sc[st] = sc.get(st, 0) + 1
            method_entries[method] = idx
            method_status_counts[method] = sc
        methods = sorted(method_entries.keys())
        summary.append({"name": name, "methods": methods, "status_counts": method_status_counts})
        if len(methods) < 2:
            continue
        all_keys = set()
        for m in methods:
            all_keys.update(method_entries[m].keys())
        for k in sorted(all_keys):
            by_method = {m: method_entries[m].get(k) for m in methods}
            sig = set()
            for m in methods:
                v = by_method[m]
                if v is None:
                    sig.add((m, None))
                else:
                    sig.add((m, (v.get("status"), v.get("size"), v.get("words"), v.get("lines"), v.get("redirect"))))
            if len(sig) > len(methods):
                diffs.append({"name": name, "fuzz": k, "by_method": by_method})
    summary_path = os.path.join(output_dir, "method_summary.json")
    diffs_jsonl_path = os.path.join(output_dir, "method_diffs.jsonl")
    diffs_csv_path = os.path.join(output_dir, "method_diffs.csv")
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    try:
        with open(diffs_jsonl_path, "w", encoding="utf-8") as f:
            for d in diffs:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        with open(diffs_csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "fuzz", "method", "status", "size", "words", "lines", "redirect", "url"])
            for d in diffs:
                name = d.get("name")
                fuzz = d.get("fuzz")
                bym = d.get("by_method") or {}
                for method, v in sorted(bym.items()):
                    if not v:
                        w.writerow([name, fuzz, method, "", "", "", "", "", ""])
                    else:
                        w.writerow([name, fuzz, method, v.get("status"), v.get("size"), v.get("words"), v.get("lines"), v.get("redirect") or "", v.get("url") or ""])
    except Exception:
        pass
    return summary_path, diffs_jsonl_path, diffs_csv_path, len(diffs)


def main():
    load_local_env()
    args = parse_args()
    if args.mode != "deep-scan":
        if (
            int(args.deep_depth) != 1
            or str(args.deep_source_dir) != "/opt/my-tools/ai-ffuf/results"
            or args.deep_paths_file
            or bool(args.deep_paths_merge)
        ):
            print("[WARN] deep-scan flags are ignored because --mode is not 'deep-scan'. Use: --mode deep-scan")
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(args.blocked_dir, exist_ok=True)

    targets_arg = str(args.targets).strip()
    targets_path = os.path.expanduser(targets_arg)
    if targets_arg.startswith("http://") or targets_arg.startswith("https://"):
        raw_targets = [targets_arg]
    else:
        with open(targets_path, "r", encoding="utf-8") as f:
            raw_targets = [x.strip() for x in f.read().splitlines() if x.strip()]
    seen = set()
    targets = []
    skipped_duplicates = 0
    for t in raw_targets:
        norm = t.rstrip("/")
        if norm in seen:
            skipped_duplicates += 1
            continue
        seen.add(norm)
        targets.append(norm)
    if skipped_duplicates:
        print(f"[INFO] Skipped duplicate targets: {skipped_duplicates}")
    wordlists = [os.path.expanduser(x.strip()) for x in args.wordlists if x and x.strip()]

    lock = threading.Lock()
    stats = {"total": len(targets), "completed": 0, "ok": 0, "empty": 0, "blocked": 0, "active": 0}
    evt_q = queue.Queue()
    progress_state = {"last": 0.0}

    def print_progress(force=False):
        now = time.time()
        if (not force) and (now - progress_state["last"] < 5):
            return
        progress_state["last"] = now
        with lock:
            total = stats["total"]
            completed = stats["completed"]
            ok = stats["ok"]
            empty = stats["empty"]
            blocked = stats["blocked"]
            active = stats["active"]
        pct = int((completed * 100 / total) if total else 100)
        filled = int(pct / 5)
        bar = "#" * filled + "-" * (20 - filled)
        print(f"[PROGRESS] [{bar}] {completed}/{total} ({pct}%) active={active} ok={ok} empty={empty} blocked={blocked}")

    def reporter(name, msg):
        evt_q.put((name, msg))

    def log_event(name, msg):
        print(f"[{time.strftime('%H:%M:%S')}] {name} :: {msg}")

    done_evt = threading.Event()

    def event_loop():
        while not done_evt.is_set() or not evt_q.empty():
            try:
                n, m = evt_q.get(timeout=0.2)
                log_event(n, m)
            except Exception:
                pass
            print_progress()

    t = threading.Thread(target=event_loop, daemon=True)
    t.start()
    print_progress(force=True)

    targets_q = queue.Queue()
    results = []
    for x in targets:
        targets_q.put(x)

    def worker():
        while True:
            try:
                target = targets_q.get(timeout=0.5)
            except Exception:
                if targets_q.empty():
                    break
                continue
            try:
                with lock:
                    stats["active"] += 1
                name, (ok, info) = run_one(
                    target=target,
                    wordlists=wordlists,
                    out_dir=args.output_dir,
                    blocked_dir=args.blocked_dir,
                    max_restarts=args.max_restarts,
                    llm_model=args.llm_model,
                    llm_trigger=args.llm_trigger,
                    headers=[h.strip() for h in args.header],
                    methods=[m.strip().upper() for m in args.methods if m.strip()],
                    post_data=args.post_data,
                    report_func=reporter,
                    ban_403_rate=args.ban_403_rate,
                    ban_429_rate=args.ban_429_rate,
                    threads=args.threads,
                    mode=args.mode,
                    host_suffix=args.host_suffix,
                    rate=args.rate,
                    delay=args.p,
                    monitor_period_seconds=args.monitor_period_seconds,
                    monitor_stall_seconds=args.monitor_stall_seconds,
                    monitor_error_window_seconds=args.monitor_error_window_seconds,
                    monitor_error_growth=args.monitor_error_growth,
                    ffuf_extra=args.ffuf_extra,
                    ffuf_allow=args.ffuf_allow,
                    ffuf_allow_reset=args.ffuf_allow_reset,
                    deep_source_dir=args.deep_source_dir,
                    deep_depth=args.deep_depth,
                    deep_paths_file=(os.path.expanduser(args.deep_paths_file.strip()) if args.deep_paths_file and args.deep_paths_file.strip() else None),
                    deep_paths_merge=args.deep_paths_merge,
                )
                with lock:
                    stats["active"] -= 1
                    stats["completed"] += 1
                    if ok:
                        stats["ok"] += 1
                    elif (info or {}).get("blocked"):
                        stats["blocked"] += 1
                    else:
                        stats["empty"] += 1
                    results.append({"name": name, "ok": ok, "info": info or {}})
                print_progress(force=True)
            except Exception as e:
                with lock:
                    stats["active"] -= 1
                    stats["completed"] += 1
                    stats["blocked"] += 1
                    results.append({"name": target, "ok": False, "info": {"error": str(e)}})
                print_progress(force=True)
            finally:
                try:
                    targets_q.task_done()
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
        for _ in range(max(1, int(args.workers))):
            ex.submit(worker)
        targets_q.join()

    done_evt.set()
    t.join(timeout=2)
    print_progress(force=True)

    for r in results:
        name = r.get("name")
        ok = r.get("ok")
        info = r.get("info", {})
        reason = info.get("reason", "")
        csv_path = info.get("json", info.get("csv", ""))
        print(f"{name} :: {'OK' if ok else 'FAILED'} :: reason={reason or '-'} :: out={csv_path or '-'}")
        scans = info.get("scans") or []
        for s in scans:
            print(f"  - method={s.get('method')} ok={s.get('ok')} has_results={s.get('has_results')} reason={s.get('reason') or '-'} out={s.get('json') or '-'}")
            if s.get("audit_log"):
                print(f"    audit_log={s.get('audit_log')}")
            if s.get("reason_detail"):
                print(f"    detail={json.dumps(s.get('reason_detail'), ensure_ascii=False)}")
    summary_path, diffs_jsonl_path, diffs_csv_path, diffs_count = build_method_reports(results, args.output_dir)
    print(f"METHOD SUMMARY: {summary_path}")
    print(f"METHOD DIFFS JSONL: {diffs_jsonl_path}")
    print(f"METHOD DIFFS CSV: {diffs_csv_path}")
    print(f"METHOD DIFFS COUNT: {diffs_count}")
    print(
        f"SUMMARY total={stats['total']} completed={stats['completed']} ok={stats['ok']} empty={stats['empty']} blocked={stats['blocked']}"
    )


if __name__ == "__main__":
    main()
