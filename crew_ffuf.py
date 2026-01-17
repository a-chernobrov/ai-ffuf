import os
import sys
import json
import argparse
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM
try:
    from crewai.tools import tool
except Exception:
    tool = None
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
import time
import queue
import threading
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Ensure local modules are importable
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ffuf_agent import run_one

try:
    from crewai.tools import BaseTool
except Exception:
    try:
        from crewai_tools import BaseTool
    except Exception:
        BaseTool = None

def main():
    # Load .env if present
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, ".env")
        if os.path.exists(env_path):
            for line in open(env_path, "r", encoding="utf-8").read().splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" in s:
                    k, v = s.split("=", 1)
                    k = k.strip(); v = v.strip().strip("'\"")
                    if k and v and k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("--targets", required=True)
    p.add_argument("--wordlist", required=True)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--output-dir", default="results")
    p.add_argument("--blocked-dir", default="blocked")
    p.add_argument("--max-restarts", type=int, default=2)
    p.add_argument("--stall-seconds", type=int, default=30)
    p.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL_NAME", "x-ai/grok-4.1-fast"))
    p.add_argument("--llm-trigger", type=int, default=50)
    p.add_argument("--header", action="append", help="Доп заголовок запроса, можно несколько: 'Name: value'", default=[])
    p.add_argument("--max-error-rate", type=float, default=0.5)
    p.add_argument("--error-block-min", type=int, default=200)
    p.add_argument("--ban-403-rate", type=float, default=0.6)
    p.add_argument("--ban-429-rate", type=float, default=0.2)
    p.add_argument("-t", "--threads", type=int, default=None)
    p.add_argument("--mode", choices=["path", "vhost"], default="path")
    p.add_argument("--host-suffix", default=".domain")
    p.add_argument("--early-error-rate", type=float, default=0.3)
    p.add_argument("--early-error-window", type=int, default=30)
    p.add_argument("--ban-backoff-seconds", type=int, default=60)
    p.add_argument("--mid-error-rate", type=float, default=None, help="Порог ошибки/прогресса для реактивного рестарта (напр. 0.25)")
    p.add_argument("--mid-error-window", type=int, default=None, help="Окно минимального прогресса для оценки (напр. 200)")
    p.add_argument("--late-error-rate", type=float, default=None, help="Порог доли ошибок в позднем окне для дропа задачи (напр. 0.02)")
    p.add_argument("--late-error-window", type=int, default=None, help="Размер окна (кол-во status-строк) для late-error-rate (напр. 2000)")
    p.add_argument("--late-error-min-progress", type=int, default=None, help="Минимальный прогресс, после которого включается late-error (напр. 80000)")
    args = p.parse_args()
    try:
        os.makedirs(args.output_dir, exist_ok=True)
    except Exception:
        pass
    agent_log_path = os.path.join(args.output_dir, "agent_events.jsonl")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    api_key = os.environ.get("OPENAI_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))
    llm = None
    try:
        if api_key:
            llm = LLM(model=f"openrouter/{args.llm_model}", base_url=base_url, api_key=api_key)
    except Exception:
        llm = None

    runner = Agent(
        role="Runner",
        goal="Запустить ffuf по плану и применить фильтры/рестарты",
        backstory="Исполнитель запускает джобы ffuf и агрегирует телеметрию",
        llm=llm if llm is not None else None,
    )
    targets_list = [x.strip() for x in open(args.targets, "r", encoding="utf-8").read().splitlines() if x.strip()]

    console = Console()
    job_stats = {"total": len(targets_list), "completed": 0, "ok": 0, "failed": 0}
    job_state = {}
    evt_q = queue.Queue()
    done_evt = threading.Event()

    def build_active_table():
        tbl = Table(title="Active Jobs")
        tbl.add_column("Name")
        tbl.add_column("Status")
        tbl.add_column("Reason")
        tbl.add_column("Progress")
        tbl.add_column("LastOut(s)")
        tbl.add_column("Errors")
        tbl.add_column("LLM Filters")
        tbl.add_column("Anti-rate")
        items = []
        for n, s in job_state.items():
            st = s.get("status", "")
            pr = 2 if st == "running" else (1 if st in {"restarting", "backoff", "stall"} else 0)
            upd = s.get("updated", s.get("started", 0) or 0)
            items.append((pr, upd, n, s))
        items.sort(key=lambda x: (-x[0], -x[1]))
        lim = min(len(items), (10 if str(args.mode).strip() == "vhost" else 20))
        for _, _, n, s in items[:lim]:
            prog = s.get("progress", "")
            lf = s.get("llm_filters", {})
            ef = s.get("final_filters", {})
            cf = s.get("current_filters", {})
            disp = []
            try:
                for k in ("fc","fl","fw","fs"):
                    vals = lf.get(k) or []
                    if k == "fc":
                        vals = [v for v in vals if v not in (403, 404)]
                    if vals:
                        disp.append(f"{k}:{','.join(str(v) for v in sorted(vals))}")
            except Exception:
                pass
            if not disp and ef:
                try:
                    tmp = []
                    for k in ("fc","fl","fw","fs"):
                        vals = ef.get(k) or []
                        if k == "fc":
                            vals = [v for v in vals if v not in (403, 404)]
                        if vals:
                            tmp.append(f"{k}:{','.join(str(v) for v in sorted(vals))}")
                    disp = tmp
                except Exception:
                    pass
            if not disp and cf:
                try:
                    tmp = []
                    for k in ("fc","fl","fw","fs"):
                        vals = cf.get(k) or []
                        if k == "fc":
                            vals = [v for v in vals if v not in (403, 404)]
                        if vals:
                            tmp.append(f"{k}:{','.join(str(v) for v in sorted(vals))}")
                    disp = tmp
                except Exception:
                    pass
            if not disp and s.get("reason") == "llm_filters":
                disp = ["defaults-only"]
            ar = s.get("anti_rate", {})
            ar_disp = []
            try:
                if "t" in ar:
                    ar_disp.append(f"-t {ar['t']}")
                if "p" in ar:
                    ar_disp.append(f"-p {ar['p']}")
                if "rate" in ar:
                    ar_disp.append(f"-rate {ar['rate']}")
                if "backoff" in ar:
                    ar_disp.append(f"backoff {ar['backoff']}s")
            except Exception:
                pass
            status = s.get("status", "")
            style = None
            if status == "backoff":
                style = "yellow"
            elif status == "stall":
                style = "red"
            elif status == "restarting":
                style = "cyan"
            tbl.add_row(n, status, s.get("reason", ""), prog, str(s.get("last_out", 0)), str(s.get("errors", 0)), " ".join(disp), " ".join(ar_disp), style=style)
        return tbl

    def reporter(name, msg):
        evt_q.put((name, msg))

    def process_event(name, msg, live):
        color = (
            "cyan" if "start ffuf" in msg else
            "yellow" if "llm" in msg else
            "red" if ("stall" in msg or "error" in msg or "blocked" in msg) else
            "green" if "finished ok" in msg else
            "white"
        )
        console.print(Panel(f"{msg}", title=name, border_style=color))
        st = job_state.get(name, {})
        def _parse_filters(text):
            try:
                pos = text.find("filters")
                if pos == -1:
                    pos = 0
                brace = text.find("{", pos)
                if brace == -1:
                    return None
                depth = 0
                end = None
                for idx in range(brace, len(text)):
                    ch = text[idx]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = idx
                            break
                if end is None:
                    return None
                s = text[brace:end+1].replace("'", '"')
                data = json.loads(s)
                out = {}
                for k in ("fc","fl","fw","fs"):
                    vals = data.get(k) or []
                    clean = []
                    for v in vals:
                        try:
                            clean.append(int(v))
                        except Exception:
                            pass
                    out[k] = clean
                return out
            except Exception:
                return None
        if "heartbeat:" in msg:
            try:
                last_out = int(msg.split("last_out=")[-1].split("s")[0])
            except Exception:
                last_out = st.get("last_out", 0)
            try:
                prog_part = msg.split("progress ")[-1].split(" ")[0]
                cur = None; tot = None
                if "/" in prog_part:
                    try:
                        cur_s, tot_s = prog_part.split("/")
                        cur = int(cur_s); tot = int(tot_s)
                    except Exception:
                        cur = None; tot = None
            except Exception:
                prog_part = st.get("progress", "")
            try:
                errors = int(msg.split("errors=")[-1].split(" ")[0])
            except Exception:
                errors = st.get("errors", 0)
            new_status = "running"
            if cur is not None and tot is not None and tot > 0 and cur >= tot:
                new_status = "ok"
            st.update({"status": new_status, "last_out": last_out, "progress": prog_part, "errors": errors, "updated": time.time()})
        elif "start ffuf" in msg:
            st.update({"status": "running", "last_out": 0, "progress": "0/?", "errors": 0, "started": time.time(), "updated": time.time()})
        elif "watchdog: stall kill" in msg:
            st.update({"status": "stall", "updated": time.time(), "last_out": st.get("last_out", 0)})
        elif ("restart:" in msg) or (msg.startswith("restart ")) or (" restart " in msg):
            r = st.get("reason", "")
            if "llm propose noop" in msg or "llm plan noop" in msg:
                r = "llm_noop"
            elif "llm" in msg:
                r = "llm_filters"
            elif "mid_error" in msg:
                r = "mid_error"
            elif "react errors:" in msg:
                r = "early_error"
            elif "auto anti-rate:" in msg:
                r = "ban_backoff"
            st.update({"status": "restarting", "reason": r, "updated": time.time()})
        elif "ban suspected" in msg:
            st.update({"status": "backoff", "reason": "ban_backoff", "updated": time.time()})
        elif "stall detected" in msg or "watchdog: stall kill" in msg:
            st.update({"status": "stall", "reason": "stall", "updated": time.time()})
        elif "llm plan filters" in msg or "llm propose filters" in msg:
            parsed = _parse_filters(msg)
            if parsed:
                try:
                    vis = False
                    for k in ("fl","fw","fs"):
                        if parsed.get(k):
                            vis = True
                            break
                    if not vis:
                        fcv = [v for v in (parsed.get("fc") or []) if v not in (403,404)]
                        vis = len(fcv) > 0
                    st.update({"llm_filters": parsed, "reason": ("llm_filters" if vis else "llm_noop"), "updated": time.time()})
                except Exception:
                    st.update({"llm_filters": parsed, "reason": "llm_filters", "updated": time.time()})
        elif "llm anti-rate set" in msg or "auto anti-rate:" in msg or "react errors:" in msg or "restart: applying filters" in msg:
            try:
                ar = st.get("anti_rate", {})
                if "-t " in msg:
                    try:
                        v = msg.split("-t ")[-1].split(" ")[0]
                        ar["t"] = int(v)
                    except Exception:
                        pass
                if "-p " in msg:
                    try:
                        v = msg.split("-p ")[-1].split(" ")[0]
                        ar["p"] = float(v)
                    except Exception:
                        pass
                if "-rate " in msg:
                    try:
                        v = msg.split("-rate ")[-1].split(" ")[0]
                        ar["rate"] = int(v)
                    except Exception:
                        pass
                if "backoff" in msg:
                    try:
                        v = msg.split("backoff ")[-1].split("s")[0]
                        ar["backoff"] = int(v)
                    except Exception:
                        pass
                r = st.get("reason", "")
                if "react errors:" in msg:
                    r = "early_error"
                elif "auto anti-rate:" in msg:
                    r = "ban_backoff"
                elif "llm anti-rate" in msg or "restart: applying filters" in msg:
                    r = "llm_filters"
                st.update({"anti_rate": ar, "reason": r, "updated": time.time()})
            except Exception:
                pass
        elif "cmd: ffuf" in msg:
            try:
                s = msg.split("cmd: ", 1)[-1]
                toks = s.strip().split()
                curf = {}
                i = 0
                while i < len(toks):
                    t = toks[i]
                    if t in {"-fc","-fl","-fw","-fs"} and i+1 < len(toks):
                        key = t[1:]
                        vals = []
                        for part in toks[i+1].split(","):
                            try:
                                vals.append(int(part))
                            except Exception:
                                pass
                        curf[key] = vals
                        i += 2
                        continue
                    i += 1
                upd = {"updated": time.time(), "status": "running"}
                if curf:
                    upd["current_filters"] = curf
                st.update(upd)
            except Exception:
                pass
        elif "finished ok" in msg:
            job_stats["completed"] += 1
            job_stats["ok"] += 1
            try:
                # parse final filters from the log line if present
                parsed = _parse_filters(msg)
                if parsed:
                    st.update({"final_filters": parsed})
            except Exception:
                pass
            st.update({"status": "ok", "updated": time.time(), "reason": st.get("reason")})
        elif "finished blocked" in msg or "finished error" in msg:
            job_stats["completed"] += 1
            job_stats["failed"] += 1
            st.update({"status": "failed", "updated": time.time()})
        job_state[name] = st
        try:
            payload = {
                "ts": time.time(),
                "name": name,
                "msg": msg,
                "status": st.get("status"),
                "reason": st.get("reason"),
                "progress": st.get("progress"),
                "last_out": st.get("last_out"),
                "errors": st.get("errors"),
                "llm_filters": st.get("llm_filters"),
                "anti_rate": st.get("anti_rate"),
                "final_filters": st.get("final_filters"),
                "current_filters": st.get("current_filters"),
            }
            with open(agent_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass
        active_tbl = build_active_table()
        live.update(active_tbl)
        table = Table(title="Job Summary")
        table.add_column("Total", justify="right")
        table.add_column("Completed", justify="right")
        table.add_column("OK", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Running", justify="right")
        table.add_column("Restarting", justify="right")
        table.add_column("Backoff", justify="right")
        table.add_column("Stall", justify="right")
        rs = sum(1 for v in job_state.values() if v.get("status") == "running")
        rr = sum(1 for v in job_state.values() if v.get("status") == "restarting")
        rb = sum(1 for v in job_state.values() if v.get("status") == "backoff")
        rsl = sum(1 for v in job_state.values() if v.get("status") == "stall")
        table.add_row(str(job_stats["total"]), str(job_stats["completed"]), str(job_stats["ok"]), str(job_stats["failed"]), str(rs), str(rr), str(rb), str(rsl))
        console.print(table)

    class FFUFArgs(BaseModel):
        target: str

    tools = []
    if tool is not None:
        @tool("Запускает ffuf для указанной цели")
        def FFUFRunTool(target: str) -> str:
            """Запускает ffuf для указанной цели и возвращает JSON-результат"""
            name, (ok, info) = run_one(
                target,
                args.wordlist,
                args.output_dir,
                args.blocked_dir,
                args.max_restarts,
                args.stall_seconds,
                args.llm_model,
                True,
                args.llm_trigger,
                headers=[h.strip() for h in args.header],
                report_func=reporter,
                max_error_rate=args.max_error_rate,
                error_block_min=args.error_block_min,
                ban_403_rate=args.ban_403_rate,
                ban_429_rate=args.ban_429_rate,
                threads=args.threads,
                mode=args.mode,
                host_suffix=args.host_suffix,
                early_error_rate=args.early_error_rate,
                early_error_window=args.early_error_window,
                ban_backoff_seconds=args.ban_backoff_seconds,
                mid_error_rate=args.mid_error_rate,
                mid_error_window=args.mid_error_window,
                late_error_rate=args.late_error_rate,
                late_error_window=args.late_error_window,
                late_error_min_progress=args.late_error_min_progress,
            )
            return json.dumps({"name": name, "ok": ok, "info": info})
        tools = [FFUFRunTool]

    active_tbl = build_active_table()
    with Live(active_tbl, console=console, refresh_per_second=8) as live:
        def ui_loop():
            while not done_evt.is_set() or not evt_q.empty():
                try:
                    name, msg = evt_q.get(timeout=0.2)
                    process_event(name, msg, live)
                except Exception:
                    pass
                time.sleep(0.05)

        ui_thread = threading.Thread(target=ui_loop, daemon=True)
        ui_thread.start()

        if args.workers and args.workers > 1:
            results = []
            targets_q = queue.Queue()
            for t in targets_list:
                targets_q.put(t)
            def worker_loop():
                while True:
                    try:
                        t = targets_q.get(timeout=0.5)
                    except Exception:
                        if targets_q.empty():
                            break
                        continue
                    try:
                        name, (ok, info) = run_one(
                            t,
                            args.wordlist,
                            args.output_dir,
                            args.blocked_dir,
                            args.max_restarts,
                            args.stall_seconds,
                            args.llm_model,
                            bool(llm),
                            args.llm_trigger,
                            [h.strip() for h in args.header],
                            reporter,
                            args.max_error_rate,
                            args.error_block_min,
                            args.ban_403_rate,
                            args.ban_429_rate,
                            args.threads,
                            args.mode,
                            args.host_suffix,
                            args.early_error_rate,
                            args.early_error_window,
                            args.ban_backoff_seconds,
                            args.mid_error_rate,
                            args.mid_error_window,
                            args.late_error_rate,
                            args.late_error_window,
                            args.late_error_min_progress,
                        )
                        results.append({"name": name, "ok": ok, "info": info})
                    except Exception:
                        pass
                    finally:
                        try:
                            targets_q.task_done()
                        except Exception:
                            pass
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                for _ in range(args.workers):
                    ex.submit(worker_loop)
                try:
                    targets_q.join()
                except Exception:
                    pass
            done_evt.set()
            ui_thread.join(timeout=2)
            lines = []
            for r in results:
                name = r.get("name")
                ok = r.get("ok")
                info = r.get("info") or {}
                csvp = info.get("csv") or ""
                filt = info.get("filters") or {}
                sc = info.get("status_counts") or {}
                filt_strs = []
                for k in ("fc","fl","fw","fs"):
                    vals = filt.get(k) or []
                    if k == "fc":
                        vals = [v for v in vals if v not in (403,404)]
                    if vals:
                        filt_strs.append(f"{k}:{','.join(str(v) for v in sorted(vals))}")
                status_strs = [f"{k}:{v}" for k, v in sorted(sc.items())]
                line = f"{name} :: {'OK' if ok else 'FAILED'} :: csv={csvp} :: filters={' '.join(filt_strs) if filt_strs else '-'} :: statuses={' '.join(status_strs) if status_strs else '-'}"
                lines.append(line)
            console.print(Panel("\n".join(lines), title="Results", border_style="green"))
            table = Table(title="Job Summary Final")
            table.add_column("Total", justify="right")
            table.add_column("Completed", justify="right")
            table.add_column("OK", justify="right")
            table.add_column("Failed", justify="right")
            table.add_row(str(job_stats["total"]), str(job_stats["completed"]), str(job_stats["ok"]), str(job_stats["failed"]))
            console.print(table)
        else:
            tasks = []
            for t in targets_list:
                tasks.append(Task(
                    description=f"Выполни фазинг: вызови FFUFRunTool(target='{t}') и верни JSON-результат",
                    expected_output="JSON результат",
                    agent=runner,
                    tools=tools,
                ))
            crew = Crew(agents=[runner], tasks=tasks, process=Process.sequential)
            result = crew.kickoff()
            done_evt.set()
            ui_thread.join(timeout=2)
            console.print(Panel(str(result), title="Crew Result", border_style="green"))
            table = Table(title="Job Summary Final")
            table.add_column("Total", justify="right")
            table.add_column("Completed", justify="right")
            table.add_column("OK", justify="right")
            table.add_column("Failed", justify="right")
            table.add_row(str(job_stats["total"]), str(job_stats["completed"]), str(job_stats["ok"]), str(job_stats["failed"]))
            console.print(table)

if __name__ == "__main__":
    main()
