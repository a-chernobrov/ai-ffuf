import os
import re
import csv
import json
import time
import signal
import threading
import subprocess
from urllib.parse import urlparse

try:
    from openai import OpenAI
except Exception:
    OpenAI = None
try:
    import requests
except Exception:
    requests = None
try:
    from json_repair import repair_json
except Exception:
    repair_json = None

def _load_local_env():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f.read().splitlines():
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

def sanitize_name(target):
    p = urlparse(target)
    name = f"{p.scheme}_{p.netloc}"
    if p.path and p.path != "/":
        name += "_" + re.sub(r"[^a-zA-Z0-9._-]", "_", p.path.strip("/"))
    return name

class FFUFRunner:
    def __init__(self, base_url, wordlist, out_dir, blocked_dir, max_restarts, stall_seconds, llm_model, use_llm, llm_trigger, headers=None, report_func=None, max_error_rate=0.5, error_block_min=200, ban_403_rate=0.6, ban_429_rate=0.2, threads=None, mode="path", host_suffix=None, early_error_rate=0.3, early_error_window=30, ban_backoff_seconds=60, mid_error_rate=None, mid_error_window=None, late_error_rate=None, late_error_window=None, late_error_min_progress=None, rate=None, delay=None):
        self.base_url = base_url.rstrip("/")
        self.wordlist = wordlist
        self.out_dir = out_dir
        self.blocked_dir = blocked_dir
        self.max_restarts = max_restarts
        self.stall_seconds = stall_seconds
        self.llm_model = llm_model
        self.use_llm = use_llm
        self.llm_trigger = llm_trigger
        self.filters = {"fc": {403, 404}, "fl": set(), "fw": set(), "fs": set()}
        self.pre_flags = []
        self.status_counts = {}
        self.line_counts = {}
        self.word_counts = {}
        self.size_counts = {}
        self.size_counts_by_status = {}
        self.word_counts_by_status = {}
        self.line_counts_by_status = {}
        self.error_counts = {}
        self.results = []
        self.proc = None
        self.last_output_time = time.time()
        self._allowed_pre = set()
        try:
            self.ban_backoff_seconds = int(ban_backoff_seconds)
        except Exception:
            self.ban_backoff_seconds = 60
        self.headers = headers or []
        self._last_beat = 0
        self._watchdog_stop = threading.Event()
        self._job_name = None
        self._cur = 0
        self._tot = 0
        self._last_watch_beat = 0
        self.report_func = report_func
        self.max_error_rate = max_error_rate
        self.error_block_min = error_block_min
        self.ban_403_rate = ban_403_rate
        self.ban_429_rate = ban_429_rate
        self.mode = str(mode or "path").strip()
        self.host_suffix = host_suffix or ""
        self.early_error_rate = float(early_error_rate)
        self.early_error_window = int(early_error_window)
        try:
            self.mid_error_rate = float(mid_error_rate) if mid_error_rate is not None else None
        except Exception:
            self.mid_error_rate = None
        try:
            self.mid_error_window = int(mid_error_window) if mid_error_window is not None else None
        except Exception:
            self.mid_error_window = None
        try:
            self.late_error_rate = float(late_error_rate) if late_error_rate is not None else None
        except Exception:
            self.late_error_rate = None
        try:
            self.late_error_window = int(late_error_window) if late_error_window is not None else None
        except Exception:
            self.late_error_window = None
        try:
            self.late_error_min_progress = int(late_error_min_progress) if late_error_min_progress is not None else None
        except Exception:
            self.late_error_min_progress = None
        try:
            if threads is not None:
                it = int(threads)
                if it > 0:
                    self.pre_flags += ["-t", str(it)]
        except Exception:
            pass
        try:
            if rate is not None:
                ir = int(rate)
                if ir > 0:
                    self.pre_flags += ["-rate", str(ir)]
        except Exception:
            pass
        try:
            if delay is not None:
                dv = float(delay)
                if dv > 0:
                    self.pre_flags += ["-p", str(dv)]
        except Exception:
            pass

    def _log(self, name, msg):
        t = time.strftime("%H:%M:%S")
        print(f"[{t}] {name} :: {msg}")
        try:
            if self.report_func:
                self.report_func(name, msg)
        except Exception:
            pass

    def _stop_proc(self):
        try:
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    try:
                        os.kill(self.proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
        finally:
            try:
                self._watchdog_stop.set()
            except Exception:
                pass
    def _watchdog(self, name):
        while not self._watchdog_stop.is_set():
            time.sleep(1)
            try:
                if self.proc and self.proc.poll() is None:
                    if time.time() - self.last_output_time > self.stall_seconds:
                        self._log(name, "watchdog: stall kill")
                        try:
                            self.proc.terminate()
                        except Exception:
                            try:
                                os.kill(self.proc.pid, signal.SIGKILL)
                            except Exception:
                                pass
                        break
                    now = time.time()
                    if now - self._last_watch_beat >= 3:
                        self._last_watch_beat = now
                        try:
                            cur = self._cur; tot = self._tot
                            last = int(now - self.last_output_time)
                            err = self.error_counts.get("ffuf_errors", 0)
                            tops = sorted(self.status_counts.items(), key=lambda x: -x[1])[:3]
                            self._log(name, f"heartbeat: progress {cur}/{tot} last_out={last}s errors={err} top_status={tops}")
                        except Exception:
                            pass
            except Exception:
                pass

    def _csv_has_results(self, csv_path):
        try:
            if not (csv_path and os.path.exists(csv_path)):
                return False
            with open(csv_path, "r", encoding="utf-8") as f:
                try:
                    rdr = csv.DictReader(f)
                    for _ in rdr:
                        return True
                    return False
                except Exception:
                    f.seek(0)
                    lines = [ln for ln in f.read().splitlines() if ln.strip()]
                    return len(lines) > 1
        except Exception:
            return False

    def _merge_filters(self, rec):
        for k in ("fc", "fl", "fw", "fs"):
            vals = rec.get(k) or []
            for v in vals:
                self.filters[k].add(v)

    def _sanitize_filters(self, rec):
        cleaned = {"fc": {403, 404}, "fl": set(), "fw": set(), "fs": set()}
        for k in ("fc", "fl", "fw", "fs"):
            vals = rec.get(k) or []
            for v in vals:
                try:
                    iv = int(v)
                except Exception:
                    continue
                if k == "fc":
                    # Ignore any attempt to add additional status codes; keep defaults only
                    continue
                else:
                    if iv >= 0:
                        cleaned[k].add(iv)
        try:
            pairs = sorted(self.wl_pair_counts.items(), key=lambda x: -x[1])
            if pairs:
                wv, lv = pairs[0][0]
                cleaned["fw"].add(int(wv))
                cleaned["fl"].add(int(lv))
            return cleaned
        except Exception:
            return cleaned

    def _sanitize_llm_flags(self, tokens):
        out = []
        i = 0
        block = {"-u", "-w", "-of", "-o", "-debug-log", "-mc", "-fc", "-fl", "-fw", "-fs"}
        expects_value = {"-rate", "-p", "-t", "-timeout", "-H", "-x", "-e"}
        while i < len(tokens):
            tok = str(tokens[i])
            if tok in block:
                i += 1
                if i < len(tokens) and tok in expects_value:
                    i += 1
                continue
            if tok in self._allowed_pre:
                out.append(tok)
                if i + 1 < len(tokens):
                    val = str(tokens[i+1])
                    if not val.startswith("-"):
                        out.append(val)
                        i += 2
                        continue
                i += 1
                continue
            if tok.startswith("-"):
                i += 1
                continue
            i += 1
        return out

    def _should_apply_antirate(self):
        try:
            tot = sum(self.status_counts.values())
            err = self.error_counts.get("ffuf_errors", 0)
            if self._cur >= self.error_block_min and err / max(self._cur, 1) >= self.max_error_rate:
                return True
            if tot >= 30:
                c429 = self.status_counts.get(429, 0)
                c403 = self.status_counts.get(403, 0)
                if c429 / max(tot, 1) >= self.ban_429_rate or c403 / max(tot, 1) >= self.ban_403_rate:
                    return True
            return False
        except Exception:
            return False

    def _llm_chat(self, content):
        _load_local_env()
        llm_configs = [
            {
                "base_url": os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
                "api_key": os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"),
                "model": self.llm_model
            },
            {
                "base_url": os.environ.get("VOIDAI_BASE_URL"),
                "api_key": os.environ.get("VOIDAI_API_KEY"),
                "model": os.environ.get("VOIDAI_MODEL_NAME", self.llm_model)
            }
        ]
        if not self.use_llm:
            return None
        for config in llm_configs:
            if not config["api_key"]:
                continue
            try:
                if OpenAI is not None:
                    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
                    r = client.chat.completions.create(
                        model=config["model"],
                        messages=[{"role": "user", "content": content}]
                    )
                    self._log(self._job_name or "llm", f"LLM success: {config['base_url'][:30]}...")
                    return r.choices[0].message.content
                if requests is not None:
                    h = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}
                    d = {"model": config["model"], "messages": [{"role": "user", "content": content}]}
                    u = config["base_url"].rstrip("/") + "/chat/completions"
                    r = requests.post(u, headers=h, json=d, timeout=30)
                    r.raise_for_status()
                    j = r.json()
                    self._log(self._job_name or "llm", f"LLM success (requests): {config['base_url'][:30]}...")
                    return j.get("choices", [{}])[0].get("message", {}).get("content")
            except Exception as e:
                self._log(self._job_name or "llm", f"LLM fail {config['base_url'][:30]}...: {str(e)[:100]}")
                continue
        return None

    def _llm_preflight_plan(self):
        payload = {"target": self.base_url, "filters": {k: sorted(list(v)) for k,v in self.filters.items()}, "config": {"max_error_rate": self.max_error_rate, "error_block_min": self.error_block_min, "ban_403_rate": self.ban_403_rate, "ban_429_rate": self.ban_429_rate}}
        content = (
            "Return JSON ONLY with keys: filters (object with fc, fl, fw, fs arrays) and rationale (string). "
            "Do NOT include any status codes in fc other than 403 and 404; prefer leaving fc unchanged. "
            "Focus on fl/fw/fs for deduplication. Include anti-rate keys (rate, backoff, threads, delay) ONLY when telemetry shows rate-limit (high 403/429 ratio) or high error rate."\
            "\n\n" + json.dumps(payload)
        )
        try:
            text = self._llm_chat(content)
            if not text:
                return {}, None, None
            try:
                data = json.loads(text.strip())
            except Exception:
                if repair_json is not None:
                    data = json.loads(repair_json(text))
                else:
                    return {}, None, text
            rec_filters = {k: set(data.get("filters", {}).get(k, [])) for k in ("fc","fl","fw","fs")}
            rationale = data.get("rationale") or data.get("reason") or data.get("explanation")
            try:
                rate = data.get("rate")
                threads = data.get("threads")
                delay = data.get("delay")
                backoff = data.get("backoff")
                apply_ar = self._should_apply_antirate()
                if apply_ar and isinstance(rate, int) and rate > 0:
                    if "-rate" in self.pre_flags:
                        idx = self.pre_flags.index("-rate")
                        if idx + 1 < len(self.pre_flags):
                            self.pre_flags[idx+1] = str(rate)
                    else:
                        self.pre_flags += ["-rate", str(rate)]
                    self._log(self._job_name or "llm", f"llm anti-rate set -rate {rate}")
                if apply_ar and isinstance(threads, int) and threads > 0:
                    if "-t" in self.pre_flags:
                        idx = self.pre_flags.index("-t")
                        if idx + 1 < len(self.pre_flags):
                            self.pre_flags[idx+1] = str(threads)
                    else:
                        self.pre_flags += ["-t", str(threads)]
                    self._log(self._job_name or "llm", f"llm anti-rate set -t {threads}")
                if apply_ar and (isinstance(delay, (int, float)) and float(delay) > 0):
                    dval = str(delay)
                    if "-p" in self.pre_flags:
                        idx = self.pre_flags.index("-p")
                        if idx + 1 < len(self.pre_flags):
                            self.pre_flags[idx+1] = dval
                    else:
                        self.pre_flags += ["-p", dval]
                    self._log(self._job_name or "llm", f"llm anti-rate set -p {dval}")
                if apply_ar and isinstance(backoff, int) and backoff > 0:
                    self.ban_backoff_seconds = max(self.ban_backoff_seconds, backoff)
                    self._log(self._job_name or "llm", f"llm anti-rate set backoff {self.ban_backoff_seconds}s")
            except Exception:
                pass
            return rec_filters, rationale, text
        except Exception:
            return {}, None, None

    def _recommend_filters_llm(self):
        payload = {
            "status_counts": self.status_counts,
            "line_counts": self.line_counts,
            "word_counts": self.word_counts,
            "size_counts": self.size_counts,
            "errors": self.error_counts,
            "progress": {"cur": self._cur, "tot": self._tot},
            "config": {
                "max_error_rate": self.max_error_rate,
                "error_block_min": self.error_block_min,
                "ban_403_rate": self.ban_403_rate,
                "ban_429_rate": self.ban_429_rate,
            }
        }
        content = (
            "Suggest ffuf filters and anti-rate strategies. Return JSON ONLY with keys: fc, fl, fw, fs (arrays) and rationale (string). "
            "Do NOT include any status codes in fc other than 403 and 404; prefer leaving fc unchanged. Focus on fl/fw/fs. "
            "Optionally include 'rate' and 'backoff' ONLY when telemetry indicates rate-limit or high error rate."\
            "\n\n" + json.dumps(payload)
        )
        try:
            text = self._llm_chat(content)
            if not text:
                return {}, None, None
            try:
                data = json.loads(text.strip())
            except Exception:
                if repair_json is not None:
                    data = json.loads(repair_json(text))
                else:
                    return {}, None, text
            rec = {k: set(data.get(k, [])) for k in ("fc","fl","fw","fs")}
            rationale = data.get("rationale") or data.get("reason") or data.get("explanation")
            rate = data.get("rate")
            backoff = data.get("backoff")
            threads = data.get("threads")
            delay = data.get("delay")
            apply_ar = self._should_apply_antirate()
            if apply_ar and isinstance(rate, int) and rate > 0:
                try:
                    if "-rate" in self.pre_flags:
                        idx = self.pre_flags.index("-rate")
                        if idx + 1 < len(self.pre_flags):
                            self.pre_flags[idx+1] = str(rate)
                    else:
                        self.pre_flags += ["-rate", str(rate)]
                    self._log(self._job_name or "llm", f"llm anti-rate set -rate {rate}")
                except Exception:
                    pass
            if apply_ar and isinstance(backoff, int) and backoff > 0:
                try:
                    self.ban_backoff_seconds = max(self.ban_backoff_seconds, backoff)
                    self._log(self._job_name or "llm", f"llm anti-rate set backoff {self.ban_backoff_seconds}s")
                except Exception:
                    pass
            if apply_ar and isinstance(threads, int) and threads > 0:
                try:
                    if "-t" in self.pre_flags:
                        idx = self.pre_flags.index("-t")
                        if idx + 1 < len(self.pre_flags):
                            self.pre_flags[idx+1] = str(threads)
                    else:
                        self.pre_flags += ["-t", str(threads)]
                    self._log(self._job_name or "llm", f"llm anti-rate set -t {threads}")
                except Exception:
                    pass
            if apply_ar and (isinstance(delay, (int, float)) and float(delay) > 0):
                try:
                    dval = str(delay)
                    if "-p" in self.pre_flags:
                        idx = self.pre_flags.index("-p")
                        if idx + 1 < len(self.pre_flags):
                            self.pre_flags[idx+1] = dval
                    else:
                        self.pre_flags += ["-p", dval]
                    self._log(self._job_name or "llm", f"llm anti-rate set -p {dval}")
                except Exception:
                    pass
            return rec, rationale, text
        except Exception:
            return {}, None, None

    def _build_cmd(self, out_csv_path):
        debug_log_path = out_csv_path[:-4] + ".ffuf.log"
        if self.mode == "vhost":
            cmd = [
                "ffuf","-u", f"{self.base_url}","-w", self.wordlist,
                "-mc","all","-v","-noninteractive","-of","json","-o", out_csv_path,
                "-debug-log", debug_log_path,
            ]
        else:
            cmd = [
                "ffuf","-u", f"{self.base_url}/FUZZ","-w", self.wordlist,
                "-mc","all","-v","-noninteractive","-of","json","-o", out_csv_path,
                "-debug-log", debug_log_path,
            ]
        if self.pre_flags:
            cmd += self.pre_flags
        if self.mode == "vhost":
            host = f"Host: FUZZ{self.host_suffix}"
            cmd += ["-H", host]
        for h in self.headers:
            cmd += ["-H", h]
        for k, vals in self.filters.items():
            if vals:
                cmd += [f"-{k}", ",".join(str(v) for v in sorted(vals))]
        return cmd

    def _validate_llm_filters(self, rec):
        clean = self._sanitize_filters(rec)
        try:
            clean["fc"] = {403, 404} if (clean.get("fc") and any(x not in {403,404} for x in clean.get("fc", set()))) else clean.get("fc", {403,404})
        except Exception:
            pass
        return clean

    def _has_real_filter_change(self, clean):
        try:
            for k in ("fc","fl","fw","fs"):
                vals = clean.get(k) or set()
                for v in vals:
                    if v not in self.filters[k]:
                        return True
            return False
        except Exception:
            return True

    def _parse_progress_line(self, line):
        m = re.search(r"Status:\s*(\d+).*Size:\s*(\d+).*Words:\s*(\d+).*Lines:\s*(\d+)", line)
        if not m:
            em = re.search(r"Errors:\s*(\d+)", line)
            if em:
                try:
                    ecount = int(em.group(1))
                    self.error_counts["ffuf_errors"] = ecount
                except Exception:
                    pass
            pm = re.search(r"Progress:\s*\[(\d+)/(\d+)\]", line)
            if pm:
                try:
                    cur = int(pm.group(1)); tot = int(pm.group(2))
                    self._cur = cur; self._tot = tot
                    now = time.time()
                    if now - self._last_beat >= 10:
                        self._last_beat = now
                        tops = sorted(self.status_counts.items(), key=lambda x: -x[1])[:3]
                        self._log(self._job_name or "heartbeat", f"heartbeat: progress {cur}/{tot} last_out={int(now-self.last_output_time)}s errors={self.error_counts.get('ffuf_errors',0)} top_status={tops}")
                except Exception:
                    pass
            return
        s = int(m.group(1)); sz = int(m.group(2)); w = int(m.group(3)); l = int(m.group(4))
        self.status_counts[s] = self.status_counts.get(s,0)+1
        self.size_counts[sz] = self.size_counts.get(sz,0)+1
        self.word_counts[w] = self.word_counts.get(w,0)+1
        self.line_counts[l] = self.line_counts.get(l,0)+1
        dsz = self.size_counts_by_status.setdefault(s, {})
        dsz[sz] = dsz.get(sz, 0) + 1
        dw = self.word_counts_by_status.setdefault(s, {})
        dw[w] = dw.get(w, 0) + 1
        dl = self.line_counts_by_status.setdefault(s, {})
        dl[l] = dl.get(l, 0) + 1
        self.last_output_time = time.time()

    def run(self, name):
        self._job_name = name
        res_dir = self.out_dir if self.mode != "vhost" else os.path.join(self.out_dir, "vhost")
        os.makedirs(res_dir, exist_ok=True)
        os.makedirs(self.blocked_dir, exist_ok=True)
        out_csv_path = os.path.join(res_dir, f"{name}.json")
        restarts = 0
        stall_restarts = 0
        error_block_triggered = False
        ban_restarts = 0
        if self.use_llm:
            self._log(name, "llm: preflight plan")
            rf, pr, raw_plan = self._llm_preflight_plan()
            if any(rf.get(k) for k in ("fc","fl","fw","fs")):
                cleanf = self._validate_llm_filters(rf)
                if self._has_real_filter_change(cleanf):
                    self._merge_filters(cleanf)
                    self._log(name, f"llm plan filters { {k: sorted(list(v)) for k,v in cleanf.items()} }")
                else:
                    self._log(name, "llm plan noop")
            if pr:
                self._log(name, f"llm plan rationale: {pr}")
            elif raw_plan:
                try:
                    snip = raw_plan.strip().replace("\n"," ")
                    self._log(name, f"llm plan raw: {snip[:300]}")
                except Exception:
                    pass
        while True:
            did_restart = False
            cmd = self._build_cmd(out_csv_path)
            self._log(name, "start ffuf")
            try:
                disp = []
                prev = None
                for tok in cmd:
                    if prev in {"-H", "-d"} and (" " in tok or ":" in tok):
                        disp.append(f'"{tok}"')
                    else:
                        disp.append(tok)
                    prev = tok if tok.startswith("-") else prev
                self._log(name, "cmd: " + " ".join(disp))
            except Exception:
                pass
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            self._watchdog_stop.clear()
            wd = threading.Thread(target=self._watchdog, args=(name,), daemon=True)
            wd.start()
            start_time = time.time()
            self.last_output_time = start_time
            total = 0
            late_block_triggered = False
            late_check_total = 0
            late_check_err = 0
            try:
                for line in self.proc.stdout:
                    if not line:
                        continue
                    self.last_output_time = time.time()
                    try:
                        self._parse_progress_line(line)
                    except Exception:
                        pass
                    try:
                        err = self.error_counts.get("ffuf_errors", 0)
                        curp = self._cur
                        if curp >= self.error_block_min and err / max(curp, 1) >= self.max_error_rate:
                            self._log(name, f"too many errors: {err}/{curp} -> block")
                            error_block_triggered = True
                            try:
                                self.proc.terminate()
                            except Exception:
                                pass
                            break
                    except Exception:
                        pass
                    if "Status:" in line:
                        total += 1
                        err = self.error_counts.get("ffuf_errors", 0)
                        if total >= self.early_error_window and err / max(total, 1) >= self.early_error_rate:
                            try:
                                if "-rate" in self.pre_flags:
                                    idx = self.pre_flags.index("-rate")
                                    if idx + 1 < len(self.pre_flags):
                                        try:
                                            new_rate = max(5, int(self.pre_flags[idx+1]) // 2)
                                        except Exception:
                                            new_rate = 10
                                        self.pre_flags[idx+1] = str(new_rate)
                                else:
                                    self.pre_flags += ["-rate", "10"]
                                self._log(name, f"react errors: {err}/{total} -> set -rate {self.pre_flags[self.pre_flags.index('-rate')+1]}")
                            except Exception:
                                pass
                            self._stop_proc()
                            restarts += 1
                            did_restart = True
                            break
                        if (self.late_error_rate is not None) and (self.late_error_window is not None) and (self.late_error_min_progress is not None):
                            try:
                                if total - late_check_total >= self.late_error_window:
                                    curp = self._cur if self._cur else total
                                    delta_total = total - late_check_total
                                    delta_err = err - late_check_err
                                    if curp >= self.late_error_min_progress and delta_err / max(delta_total, 1) >= self.late_error_rate:
                                        self._log(name, f"late errors: +{delta_err}/+{delta_total} at progress {curp} -> block")
                                        late_block_triggered = True
                                        try:
                                            self.proc.terminate()
                                        except Exception:
                                            pass
                                        break
                                    late_check_total = total
                                    late_check_err = err
                            except Exception:
                                pass
                        if (self.mid_error_rate is not None) and (self.mid_error_window is not None):
                            curp = self._cur if self._cur else total
                            if curp >= self.mid_error_window and err / max(curp, 1) >= self.mid_error_rate:
                                try:
                                    pre_before = list(self.pre_flags)
                                    backoff_before = self.ban_backoff_seconds
                                    rec, rr, raw_rec = self._recommend_filters_llm()
                                    cleanr = self._validate_llm_filters(rec)
                                    real_change = any(cleanr.get(k) for k in ("fc","fl","fw","fs")) and self._has_real_filter_change(cleanr)
                                    anti_changed = (pre_before != self.pre_flags) or (backoff_before != self.ban_backoff_seconds)
                                    if real_change:
                                        self._merge_filters(cleanr)
                                        self._log(name, f"restart {restarts+1}: llm propose filters { {k: sorted(list(v)) for k,v in cleanr.items()} }")
                                        if rr:
                                            self._log(name, f"llm filters rationale: {rr}")
                                        elif raw_rec:
                                            try:
                                                snip = raw_rec.strip().replace("\n"," ")
                                                self._log(name, f"llm filters raw: {snip[:300]}")
                                            except Exception:
                                                pass
                                    else:
                                        self._log(name, "llm propose noop")
                                    if real_change or anti_changed:
                                        self._log(name, "restart: mid_error threshold hit; applying filters and rescheduling")
                                        self._stop_proc()
                                        restarts += 1
                                        did_restart = True
                                        break
                                except Exception:
                                    pass
                        tot = sum(self.status_counts.values())
                        if tot >= 50:
                            c429 = self.status_counts.get(429, 0)
                            c403 = self.status_counts.get(403, 0)
                            if c429 / tot >= self.ban_429_rate or c403 / tot >= self.ban_403_rate:
                                self._log(name, f"ban suspected: 429={c429},403={c403}, total={tot} -> backoff {self.ban_backoff_seconds}s")
                                try:
                                    if "-t" in self.pre_flags:
                                        idx = self.pre_flags.index("-t")
                                        if idx + 1 < len(self.pre_flags):
                                            try:
                                                new_t = max(5, int(self.pre_flags[idx+1]) // 2)
                                            except Exception:
                                                new_t = 10
                                            self.pre_flags[idx+1] = str(new_t)
                                    else:
                                        self.pre_flags += ["-t", "10"]
                                    if "-p" not in self.pre_flags:
                                        self.pre_flags += ["-p", "1"]
                                    self._log(name, f"auto anti-rate: set -t {self.pre_flags[self.pre_flags.index('-t')+1]} -p {self.pre_flags[self.pre_flags.index('-p')+1]}")
                                except Exception:
                                    pass
                                if self.use_llm:
                                    try:
                                        rec, rr, raw_rec = self._recommend_filters_llm()
                                        cleanr = self._validate_llm_filters(rec)
                                        if any(cleanr.get(k) for k in ("fc","fl","fw","fs")):
                                            if self._has_real_filter_change(cleanr):
                                                self._merge_filters(cleanr)
                                                self._log(name, f"llm propose filters { {k: sorted(list(v)) for k,v in cleanr.items()} }")
                                            else:
                                                self._log(name, "llm propose noop")
                                            if rr:
                                                self._log(name, f"llm filters rationale: {rr}")
                                    except Exception:
                                        pass
                                self._stop_proc()
                                time.sleep(self.ban_backoff_seconds)
                                ban_restarts += 1
                                restarts += 1
                                did_restart = True
                                break
                    if (time.time() - start_time > self.stall_seconds) and (time.time() - self.last_output_time > self.stall_seconds):
                        self.error_counts["stall"] = self.error_counts.get("stall",0)+1
                        self._log(name, "stall detected")
                        self._stop_proc()
                        stall_restarts += 1
                        did_restart = True
                        break
                    if self.use_llm and self.llm_trigger and total >= self.llm_trigger and restarts < self.max_restarts:
                        pre_before = list(self.pre_flags)
                        backoff_before = self.ban_backoff_seconds
                        rec, rr, raw_rec = self._recommend_filters_llm()
                        cleanr = self._validate_llm_filters(rec)
                        real_change = any(cleanr.get(k) for k in ("fc","fl","fw","fs")) and self._has_real_filter_change(cleanr)
                        anti_changed = (pre_before != self.pre_flags) or (backoff_before != self.ban_backoff_seconds)
                        if real_change:
                            self._merge_filters(cleanr)
                            self._log(name, f"restart {restarts+1}: llm propose filters { {k: sorted(list(v)) for k,v in cleanr.items()} }")
                            if rr:
                                self._log(name, f"llm filters rationale: {rr}")
                            elif raw_rec:
                                try:
                                    snip = raw_rec.strip().replace("\n"," ")
                                    self._log(name, f"llm filters raw: {snip[:300]}")
                                except Exception:
                                    pass
                        else:
                            self._log(name, "llm propose noop")
                        if real_change or anti_changed:
                            self._log(name, "restart: applying filters and rescheduling")
                            self._stop_proc()
                            restarts += 1
                            did_restart = True
                            break
                try:
                    rc = self.proc.wait(timeout=5)
                except Exception:
                    try:
                        os.kill(self.proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
                    rc = 1
                self._watchdog_stop.set()
                try:
                    csv_sz = os.path.getsize(out_csv_path) if os.path.exists(out_csv_path) else 0
                except Exception:
                    csv_sz = 0
                if self._tot:
                    try:
                        self._cur = self._tot
                        self._log(name, f"heartbeat: progress {self._cur}/{self._tot} last_out={int(time.time()-self.last_output_time)}s errors={self.error_counts.get('ffuf_errors',0)} top_status={sorted(self.status_counts.items(), key=lambda x: -x[1])[:3]}")
                    except Exception:
                        pass
                if late_block_triggered:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "late_errors", "csv": out_csv_path, "errors": self.error_counts.get("ffuf_errors",0), "progress": [self._cur, self._tot]}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=late_errors")
                    return False, {"blocked": True, "reason": "late_errors", "csv": out_csv_path}
                if error_block_triggered:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "errors", "csv": out_csv_path, "errors": self.error_counts.get("ffuf_errors",0), "progress": [self._cur, self._tot]}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=errors")
                    return False, {"blocked": True, "reason": "errors", "csv": out_csv_path}
                if ban_restarts >= self.max_restarts:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "ban", "csv": out_csv_path, "errors": self.error_counts.get("ffuf_errors",0), "progress": [self._cur, self._tot]}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=ban")
                    return False, {"blocked": True, "reason": "ban", "csv": out_csv_path}
                if did_restart and restarts >= self.max_restarts:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "restarts", "csv": out_csv_path}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=restarts")
                    return False, {"blocked": True, "reason": "restarts", "csv": out_csv_path}
                if did_restart and stall_restarts >= self.max_restarts:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "stall", "csv": out_csv_path}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=stall")
                    return False, {"blocked": True, "reason": "stall", "csv": out_csv_path}
                if did_restart:
                    continue
                if rc == 0:
                    if restarts == 0 and stall_restarts == 0:
                        self._log(name, f"finished ok csv={out_csv_path} bytes={csv_sz} filters={ {k: sorted(list(v)) for k,v in self.filters.items()} } statuses={self.status_counts}")
                    else:
                        self._log(name, f"finished ok after restarts csv={out_csv_path} bytes={csv_sz} filters={ {k: sorted(list(v)) for k,v in self.filters.items()} } statuses={self.status_counts}")
                    break
                has_csv = csv_sz > 0
                has_rows = self._csv_has_results(out_csv_path)
                totproc = sum(self.status_counts.values())
                completed = bool(self._tot) and (self._cur >= self._tot)
                if (has_rows or (has_csv and (completed or totproc > 0))):
                    self._log(name, f"finished ok after restarts csv={out_csv_path} bytes={csv_sz} rc={rc} filters={ {k: sorted(list(v)) for k,v in self.filters.items()} } statuses={self.status_counts}")
                    break
                if stall_restarts >= self.max_restarts:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "stall", "csv": out_csv_path}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=stall")
                    return False, {"blocked": True, "reason": "stall", "csv": out_csv_path}
                if not did_restart:
                    try:
                        with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                            json.dump({"blocked": True, "reason": "exec", "csv": out_csv_path, "rc": rc}, f)
                    except Exception:
                        pass
                    self._log(name, "finished blocked reason=exec")
                    return False, {"blocked": True, "reason": "exec", "csv": out_csv_path}
            except Exception as e:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                self._watchdog_stop.set()
                with open(os.path.join(self.blocked_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                    json.dump({"error": str(e)}, f)
                self._log(name, f"finished error {str(e)}")
                return False, {"blocked": True}
        return True, {"csv": out_csv_path, "filters": {k: sorted(list(v)) for k,v in self.filters.items()}, "status_counts": self.status_counts}

def run_one(target, wordlists: list[str], out_dir, blocked_dir, max_restarts, stall_seconds, llm_model, use_llm, llm_trigger, headers=None, report_func=None, max_error_rate=0.5, error_block_min=200, ban_403_rate=0.6, ban_429_rate=0.2, threads=None, mode="path", host_suffix=None, early_error_rate=0.3, early_error_window=30, ban_backoff_seconds=60, mid_error_rate=None, mid_error_window=None, late_error_rate=None, late_error_window=None, late_error_min_progress=None, rate=None, delay=None):
    name = sanitize_name(target)
    for wl_idx, wl in enumerate(wordlists):
        runner = FFUFRunner(
            target,
            wl,
            out_dir,
            blocked_dir,
            max_restarts,
            stall_seconds,
            llm_model,
            use_llm,
            llm_trigger,
            headers=headers,
            report_func=report_func,
            max_error_rate=max_error_rate,
            error_block_min=error_block_min,
            ban_403_rate=ban_403_rate,
            ban_429_rate=ban_429_rate,
            threads=threads,
            mode=mode,
            host_suffix=host_suffix,
            early_error_rate=early_error_rate,
            early_error_window=early_error_window,
            ban_backoff_seconds=ban_backoff_seconds,
            mid_error_rate=mid_error_rate,
            mid_error_window=mid_error_window,
            late_error_rate=late_error_rate,
            late_error_window=late_error_window,
            late_error_min_progress=late_error_min_progress,
            rate=rate,
            delay=delay,
        )
        runner._log(name, f"Trying wordlist {wl_idx+1}/{len(wordlists)}: {os.path.basename(wl)}")
        ok, info = runner.run(name)
        csv_path = info.get('csv') if info else ''
        has_results = csv_path and os.path.exists(csv_path) and runner._csv_has_results(csv_path)
        runner._log(name, f"Wordlist {wl_idx+1}: ok={ok}, has_results={has_results}, csv_size={os.path.getsize(csv_path) if csv_path and os.path.exists(csv_path) else 0 if csv_path else 'no_path'}")
        if has_results:
            runner._log(name, f"Success with wordlist {os.path.basename(wl)}")
            return name, (True, info)
    if wordlists:
        runner._log(name, "No results from any wordlist - blocked")
    return name, (False, {'blocked': True, 'reason': 'no_results_all_wordlists'})
