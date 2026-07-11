import os
import re
import json
import time
import shlex
import signal
import threading
import subprocess
from datetime import datetime
from collections import deque
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

_FILTER_AUDIT_LOCK = threading.Lock()


def sanitize_name(target):
    p = urlparse(target)
    name = f"{p.scheme}_{p.netloc}"
    if p.path and p.path != "/":
        name += "_" + re.sub(r"[^a-zA-Z0-9._-]", "_", p.path.strip("/"))
    return name


class FFUFRunner:
    _audit_lock = threading.Lock()

    def __init__(
        self,
        base_url,
        wordlist,
        out_dir,
        blocked_dir,
        max_restarts,
        llm_model,
        llm_trigger,
        headers=None,
        report_func=None,
        ban_403_rate=0.6,
        ban_429_rate=0.2,
        threads=None,
        mode="path",
        host_suffix=None,
        rate=None,
        delay=None,
        monitor_period_seconds=10,
        monitor_stall_seconds=60,
        monitor_error_window_seconds=120,
        monitor_error_growth=20,
        ffuf_extra=None,
        ffuf_allow=None,
        ffuf_allow_reset=False,
        method="GET",
        post_data=None,
        ffuf_wordlists=None,
        url_template=None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.wordlist = wordlist
        self.out_dir = out_dir
        self.blocked_dir = blocked_dir
        self.max_restarts = int(max_restarts)
        self.llm_model = llm_model
        self.llm_trigger = int(llm_trigger)
        self.report_func = report_func
        self.ban_403_rate = float(ban_403_rate)
        self.ban_429_rate = float(ban_429_rate)
        self.mode = str(mode or "path")
        self.host_suffix = host_suffix or ""
        self.headers = headers or []
        if not any(h.lower().startswith("user-agent:") for h in self.headers):
            self.headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
        self.method = str(method or "GET").upper()
        self.post_data = post_data
        self.ffuf_wordlists = ffuf_wordlists
        self.url_template = url_template
        initial_fc = {400} if self.mode == "vhost" else {404}
        self.filters = {"fc": initial_fc, "fl": set(), "fw": set(), "fs": set()}
        self.pre_flags = ["-ac"]
        self.proc = None
        self.last_output_time = time.time()
        self.status_counts = {}
        self.error_counts = {"ffuf_errors": 0}
        self.pattern_counts_by_status = {}
        self._cur = 0
        self._tot = 0
        self._job_name = None
        self._drop_reason = None
        self._restart_requested = False
        self._watchdog_stop = threading.Event()
        self._err_samples = deque()
        self._last_llm_total = 0
        self.monitor_period_seconds = max(1, int(monitor_period_seconds))
        self.monitor_stall_seconds = max(1, int(monitor_stall_seconds))
        self.monitor_error_window_seconds = max(10, int(monitor_error_window_seconds))
        self.monitor_error_growth = max(1, int(monitor_error_growth))
        self.noise_min_total = 10
        self.noise_min_hits = 40
        self.noise_ratio = 0.92
        self.noise_stability_required = 2
        self.noise_window_size = 300
        self.noise_window_min_total = 30
        self._noise_last_candidate = None
        self._noise_stable_ticks = 0
        self._recent_statuses = deque(maxlen=self.noise_window_size)
        self._recent_sigs = deque(maxlen=self.noise_window_size)
        self._audit_path = None
        self._attempt_no = 0
        self._allowed_pre = {
            "-ac",
            "-acc",
            "-ach",
            "-ack",
            "-acs",
            "-D",
            "-e",
            "-ic",
            "-ignore-body",
            "-json",
            "-maxtime",
            "-maxtime-job",
            "-mt",
            "-r",
            "-raw",
            "-recursion",
            "-recursion-depth",
            "-recursion-strategy",
            "-s",
            "-sa",
            "-se",
            "-sf",
            "-silent",
            "-split-by-host",
            "-timeout",
            "-v",
            "-x",
        }
        self._blocked_extra = {"-u", "-w", "-of", "-o", "-debug-log", "-mc", "-fc", "-fl", "-fw", "-fs", "-rate", "-p", "-t", "-H", "-X", "-d"}
        self._flags_need_value = {"-acc", "-ack", "-acs", "-D", "-e", "-maxtime", "-maxtime-job", "-mt", "-recursion-depth", "-recursion-strategy", "-timeout", "-x"}

        if threads is not None:
            try:
                t = int(threads)
                if t > 0:
                    self.pre_flags += ["-t", str(t)]
            except Exception:
                pass
        if rate is not None:
            try:
                r = int(rate)
                if r > 0:
                    self.pre_flags += ["-rate", str(r)]
            except Exception:
                pass
        if delay is not None:
            try:
                p = float(delay)
                if p > 0:
                    self.pre_flags += ["-p", str(p)]
            except Exception:
                pass
        if ffuf_allow_reset:
            self._allowed_pre = set()
        for fl in (ffuf_allow or []):
            fs = str(fl).strip()
            if not fs:
                continue
            if not fs.startswith("-"):
                fs = "-" + fs
            self._allowed_pre.add(fs)
        for raw in (ffuf_extra or []):
            self.pre_flags += self._sanitize_extra_flags(raw)

    def _log(self, name, msg):
        try:
            if self.report_func:
                self.report_func(name, msg)
                return
        except Exception:
            pass
        print(f"[{time.strftime('%H:%M:%S')}] {name} :: {msg}")

    def _sanitize_extra_flags(self, raw):
        out = []
        try:
            tokens = shlex.split(str(raw))
        except Exception:
            return out
        i = 0
        while i < len(tokens):
            tok = str(tokens[i])
            if tok in self._blocked_extra:
                i += 2 if (tok in self._flags_need_value and i + 1 < len(tokens)) else 1
                continue
            if tok in self._allowed_pre:
                out.append(tok)
                if tok in self._flags_need_value and i + 1 < len(tokens):
                    val = str(tokens[i + 1])
                    if not val.startswith("-"):
                        out.append(val)
                        i += 2
                        continue
                i += 1
                continue
            i += 1
        return out

    def _audit_event(self, name, source, action, data=None):
        if not self._audit_path:
            return
        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "name": name,
            "target": self.base_url,
            "method": self.method,
            "attempt": self._attempt_no,
            "source": source,
            "action": action,
            "filters": {k: sorted(list(v)) for k, v in self.filters.items()},
        }
        if isinstance(data, dict) and data:
            rec["data"] = data
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        try:
            with self._audit_lock:
                with open(self._audit_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

    def _llm_chat(self, content):
        self._log(self._job_name or "llm", f"llm: trying model={self.llm_model}")
        llm_configs = [
            {
                "base_url": os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
                "api_key": os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"),
                "model": self.llm_model,
            },
            {
                "base_url": os.environ.get("VOIDAI_BASE_URL"),
                "api_key": os.environ.get("VOIDAI_API_KEY"),
                "model": os.environ.get("VOIDAI_MODEL_NAME", self.llm_model),
            },
        ]
        had_key = False
        for c in llm_configs:
            if not c["api_key"]:
                continue
            had_key = True
            try:
                if OpenAI is not None:
                    client = OpenAI(api_key=c["api_key"], base_url=c["base_url"])
                    r = client.chat.completions.create(model=c["model"], messages=[{"role": "user", "content": content}])
                    return r.choices[0].message.content
                if requests is not None:
                    h = {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}
                    d = {"model": c["model"], "messages": [{"role": "user", "content": content}]}
                    u = c["base_url"].rstrip("/") + "/chat/completions"
                    rr = requests.post(u, headers=h, json=d, timeout=30)
                    rr.raise_for_status()
                    j = rr.json()
                    return j.get("choices", [{}])[0].get("message", {}).get("content")
            except Exception as e:
                self._log(self._job_name or "llm", f"llm: provider failed {str(e)[:120]}")
                continue
        if not had_key:
            self._log(self._job_name or "llm", "llm: no api key in env")
        return None

    def _build_llm_prompt(self, phase):
        payload = {
            "phase": phase,
            "target": self.base_url,
            "progress": {"cur": self._cur, "tot": self._tot},
            "status_counts": self.status_counts,
            "errors": self.error_counts,
            "filters": {k: sorted(list(v)) for k, v in self.filters.items()},
            "monitor": {
                "stall_seconds": self.monitor_stall_seconds,
                "error_window_seconds": self.monitor_error_window_seconds,
                "error_growth": self.monitor_error_growth,
                "ban_403_rate": self.ban_403_rate,
                "ban_429_rate": self.ban_429_rate,
            },
        }
        body_less = self.method in ("HEAD", "OPTIONS")
        filter_rule = (
            "Hard rules: fc may contain only 403 and 404. Prefer fc unchanged. Do NOT set fl/fw/fs (method has no response body, all values are 0). "
            if body_less else
            "Hard rules: fc may contain only 403 and 404. Prefer fc unchanged. Focus on fl/fw/fs for cleaning output. "
        )
        return (
            "Return JSON ONLY with keys: fc, fl, fw, fs (arrays of integers), rate (int|null), threads (int|null), delay (number|null), backoff (int|null), drop (bool), drop_reason (string), rationale (string). "
            + filter_rule +
            "Set drop=true only on clear signs of ban/stall/error explosion."
            "\n\n" + json.dumps(payload)
        )

    def _llm_recommend(self, phase):
        text = self._llm_chat(self._build_llm_prompt(phase))
        if not text:
            return None
        try:
            data = json.loads(text.strip())
        except Exception:
            if repair_json is None:
                return None
            try:
                data = json.loads(repair_json(text))
            except Exception:
                return None
        return data

    def _sanitize_filters(self, data):
        out = {"fc": {403, 404}, "fl": set(), "fw": set(), "fs": set()}
        for k in ("fl", "fw", "fs"):
            for v in (data.get(k) or []):
                try:
                    iv = int(v)
                    if iv >= 0:
                        out[k].add(iv)
                except Exception:
                    pass
        return out

    def _apply_llm_action(self, data, name, phase="runtime"):
        if not data:
            return
        try:
            if bool(data.get("drop")):
                self._drop_reason = str(data.get("drop_reason") or "llm_drop")
                self._log(name, f"llm: drop requested reason={self._drop_reason}")
                return
        except Exception:
            pass
        changed = False
        clean = self._sanitize_filters(data)
        picked = None
        for key in ("fs", "fw", "fl"):
            cur = self.filters[key]
            add = [v for v in clean[key] if v not in cur]
            if add:
                cur.add(add[0])
                picked = key
                changed = True
                break
        if picked:
            chosen = sorted(list(self.filters[picked]))[-1]
            self._log(name, f"llm: apply single-filter dimension={picked} value={chosen}")
            self._audit_event(
                name,
                source=f"llm:{phase}",
                action="filter_add",
                data={"dimension": picked, "value": chosen},
            )
        try:
            r = data.get("rate")
            if isinstance(r, int) and r > 0:
                self._set_pre_flag("-rate", str(r))
                changed = True
                self._audit_event(name, source=f"llm:{phase}", action="tune_rate", data={"value": int(r)})
            t = data.get("threads")
            if isinstance(t, int) and t > 0:
                self._set_pre_flag("-t", str(t))
                changed = True
                self._audit_event(name, source=f"llm:{phase}", action="tune_threads", data={"value": int(t)})
            d = data.get("delay")
            if isinstance(d, (int, float)) and float(d) > 0:
                self._set_pre_flag("-p", str(d))
                changed = True
                self._audit_event(name, source=f"llm:{phase}", action="tune_delay", data={"value": float(d)})
        except Exception:
            pass
        if changed:
            self._restart_requested = True
            self._log(name, f"llm: restart requested filters={ {k: sorted(list(v)) for k,v in self.filters.items()} }")

    def _set_pre_flag(self, key, val):
        if key in self.pre_flags:
            idx = self.pre_flags.index(key)
            if idx + 1 < len(self.pre_flags):
                self.pre_flags[idx + 1] = val
                return
        self.pre_flags += [key, val]

    def _record_error_sample(self):
        now = time.time()
        self._err_samples.append((now, int(self.error_counts.get("ffuf_errors", 0))))
        keep = now - max(self.monitor_error_window_seconds * 2, 60)
        while self._err_samples and self._err_samples[0][0] < keep:
            self._err_samples.popleft()

    def _check_health(self, name):
        now = time.time()
        if now - self.last_output_time >= self.monitor_stall_seconds:
            self._drop_reason = "monitor_stall"
            self._log(name, f"health: drop monitor_stall idle={int(now - self.last_output_time)}s")
            return
        self._record_error_sample()
        win_from = now - self.monitor_error_window_seconds
        while self._err_samples and self._err_samples[0][0] < win_from:
            self._err_samples.popleft()
        if len(self._err_samples) >= 2:
            delta = self._err_samples[-1][1] - self._err_samples[0][1]
            if delta >= self.monitor_error_growth:
                self._drop_reason = "monitor_error_growth"
                self._log(name, f"health: drop monitor_error_growth delta_errors=+{delta}")
                return
        total = sum(self.status_counts.values())
        if total >= 30:
            c403 = self.status_counts.get(403, 0)
            c429 = self.status_counts.get(429, 0)
            if (c403 / total) >= self.ban_403_rate or (c429 / total) >= self.ban_429_rate:
                self._drop_reason = "monitor_ban"
                self._log(name, f"health: drop monitor_ban 403={c403} 429={c429} total={total}")

    def _stop_proc(self):
        try:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except Exception:
                    try:
                        os.kill(self.proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
        except Exception:
            pass

    def _watchdog(self, name):
        while not self._watchdog_stop.is_set():
            time.sleep(self.monitor_period_seconds)
            try:
                if not self.proc or self.proc.poll() is not None:
                    continue
                statuses = sorted(self.status_counts.items(), key=lambda x: (-x[1], x[0]))
                self._log(
                    name,
                    f"heartbeat: progress {self._cur}/{self._tot} last_out={int(time.time()-self.last_output_time)}s errors={self.error_counts.get('ffuf_errors', 0)} statuses={statuses}",
                )
                self._check_health(name)
                if self._drop_reason:
                    self._stop_proc()
                    break
            except Exception:
                pass

    def _build_cmd(self, out_path):
        if self.ffuf_wordlists:
            cmd = ["ffuf", "-u", self.url_template or f"{self.base_url}/FUZZ", "-v", "-noninteractive", "-of", "json", "-o", out_path]
            for w in self.ffuf_wordlists:
                if isinstance(w, (list, tuple)) and len(w) == 2 and str(w[1]).strip():
                    cmd += ["-w", f"{w[0]}:{w[1]}"]
                else:
                    cmd += ["-w", str(w[0] if isinstance(w, (list, tuple)) else w)]
        elif self.mode == "vhost":
            cmd = ["ffuf", "-u", self.base_url, "-w", self.wordlist, "-v", "-noninteractive", "-of", "json", "-o", out_path]
            if self.host_suffix:
                cmd += ["-H", f"Host: FUZZ{self.host_suffix}"]
            else:
                cmd += ["-H", "Host: FUZZ"]
        else:
            cmd = ["ffuf", "-u", f"{self.base_url}/FUZZ", "-w", self.wordlist, "-v", "-noninteractive", "-of", "json", "-o", out_path]
        for h in self.headers:
            cmd += ["-H", h]
        if self.method != "GET":
            cmd += ["-X", self.method]
            if self.method == "POST" and self.post_data:
                cmd += ["-d", str(self.post_data)]
        cmd += self.pre_flags
        for k in ("fc", "fl", "fw", "fs"):
            vals = sorted(list(self.filters[k]))
            if vals:
                cmd += [f"-{k}", ",".join(str(v) for v in vals)]
        return cmd

    def _parse_line(self, line):
        em = re.search(r"Errors:\s*(\d+)", line)
        if em:
            try:
                self.error_counts["ffuf_errors"] = int(em.group(1))
            except Exception:
                pass
        pm = re.search(r"Progress:\s*\[(\d+)/(\d+)\]", line)
        if pm:
            try:
                self._cur = int(pm.group(1))
                self._tot = int(pm.group(2))
            except Exception:
                pass
        dm = re.search(r"Status:\s*(\d+).*Size:\s*(\d+).*Words:\s*(\d+).*Lines:\s*(\d+)", line)
        if dm:
            try:
                st = int(dm.group(1)); sz = int(dm.group(2)); wd = int(dm.group(3)); ln = int(dm.group(4))
                self.status_counts[st] = self.status_counts.get(st, 0) + 1
                mp = self.pattern_counts_by_status.setdefault(st, {})
                key = (wd, ln, sz)
                mp[key] = mp.get(key, 0) + 1
                self._recent_statuses.append(st)
                self._recent_sigs.append(key)
                return
            except Exception:
                pass
        sm = re.search(r"Status:\s*(\d+)", line)
        if sm:
            try:
                st = int(sm.group(1))
                self.status_counts[st] = self.status_counts.get(st, 0) + 1
                self._recent_statuses.append(st)
                self._recent_sigs.append(None)
            except Exception:
                pass

    def _pick_status_candidate(self, status_counts, total, strict=False):
        min_hits = self.noise_min_hits if strict else min(self.noise_min_hits, max(3, int(total * 0.85)))
        for st, cnt in status_counts.items():
            if st in self.filters["fc"]:
                continue
            ratio = cnt / max(total, 1)
            if ratio < self.noise_ratio and cnt < min_hits:
                continue
            return ("fc", st, cnt, ratio)
        return None

    def _pick_body_candidate(self, sig_counts, total, strict=False):
        fs_counts = {}
        fw_counts = {}
        fl_counts = {}
        for (wd, ln, sz), cnt in sig_counts.items():
            fs_counts[sz] = fs_counts.get(sz, 0) + cnt
            fw_counts[wd] = fw_counts.get(wd, 0) + cnt
            fl_counts[ln] = fl_counts.get(ln, 0) + cnt
        tops = []
        if fs_counts:
            v, c = max(fs_counts.items(), key=lambda x: x[1]); tops.append((c, "fs", v))
        if fw_counts:
            v, c = max(fw_counts.items(), key=lambda x: x[1]); tops.append((c, "fw", v))
        if fl_counts:
            v, c = max(fl_counts.items(), key=lambda x: x[1]); tops.append((c, "fl", v))
        if not tops:
            return None
        tops.sort(key=lambda x: (-x[0], {"fs": 0, "fw": 1, "fl": 2}.get(x[1], 9)))
        min_hits = self.noise_min_hits if strict else min(self.noise_min_hits, max(3, int(total * 0.85)))
        for cnt, dim, val in tops:
            if val in self.filters[dim]:
                continue
            if cnt < min_hits:
                continue
            ratio = cnt / max(total, 1)
            if ratio < self.noise_ratio:
                continue
            return (dim, val, cnt, ratio)
        return None

    def _maybe_apply_noise_filter(self, name, final_pass=False):
        required_ticks = 1 if final_pass else self.noise_stability_required
        if self.method in ("HEAD", "OPTIONS"):
            candidate = None
            total_recent = len(self._recent_statuses)
            if total_recent >= self.noise_window_min_total:
                recent_counts = {}
                for st in self._recent_statuses:
                    recent_counts[st] = recent_counts.get(st, 0) + 1
                candidate = self._pick_status_candidate(recent_counts, total_recent, strict=False)
            if not candidate:
                total = sum(self.status_counts.values())
                if total < self.noise_min_total:
                    return
                candidate = self._pick_status_candidate(self.status_counts, total, strict=True)
                if not candidate:
                    return
            dim, val, cnt, ratio = candidate
            cand = (dim, val)
            if cand == self._noise_last_candidate:
                self._noise_stable_ticks += 1
            else:
                self._noise_last_candidate = cand
                self._noise_stable_ticks = 1
            if self._noise_stable_ticks < required_ticks:
                return
            self.filters[dim].add(val)
            self._restart_requested = True
            self._log(name, f"auto: noise filter applied {dim}={val} hit={cnt} ratio={ratio:.2f}")
            self._audit_event(
                name,
                source="auto_noise",
                action="filter_add",
                data={"dimension": dim, "value": val, "hit": cnt, "ratio": round(ratio, 4), "final_pass": bool(final_pass)},
            )
            return
        candidate = None
        candidate_total = None
        total_recent = len(self._recent_sigs)
        if total_recent >= self.noise_window_min_total:
            recent_sig_counts = {}
            for sig in self._recent_sigs:
                if sig is None:
                    continue
                recent_sig_counts[sig] = recent_sig_counts.get(sig, 0) + 1
            if recent_sig_counts:
                candidate = self._pick_body_candidate(recent_sig_counts, total_recent, strict=False)
                if candidate:
                    candidate_total = total_recent
        if not candidate:
            total = sum(self.status_counts.values())
            if total < self.noise_min_total:
                return
            sig_counts = {}
            for mp in self.pattern_counts_by_status.values():
                for sig, cnt in mp.items():
                    sig_counts[sig] = sig_counts.get(sig, 0) + cnt
            if not sig_counts:
                return
            candidate = self._pick_body_candidate(sig_counts, total, strict=True)
            if candidate:
                candidate_total = total
        if not candidate:
            return
        dim, val, cnt, ratio = candidate
        cand = (dim, val)
        if cand == self._noise_last_candidate:
            self._noise_stable_ticks += 1
        else:
            self._noise_last_candidate = cand
            self._noise_stable_ticks = 1
        if self._noise_stable_ticks < required_ticks:
            return
        self.filters[dim].add(val)
        self._restart_requested = True
        self._log(name, f"auto: noise filter applied {dim}={val} hit={cnt}/{candidate_total} ratio={ratio:.2f} stable_ticks={self._noise_stable_ticks}")
        self._audit_event(
            name,
            source="auto_noise",
            action="filter_add",
            data={"dimension": dim, "value": val, "hit": cnt, "total": candidate_total, "ratio": round(ratio, 4), "final_pass": bool(final_pass)},
        )

    def _output_has_results(self, out_json_path):
        if not os.path.exists(out_json_path):
            return False
        try:
            with open(out_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            res = data.get("results") if isinstance(data, dict) else None
            return bool(res and isinstance(res, list) and len(res) > 0)
        except Exception:
            return False

    def run(self, name, out_path=None):
        self._job_name = name
        self._drop_reason = None
        self._restart_requested = False
        self._last_llm_total = 0
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.blocked_dir, exist_ok=True)
        if not out_path:
            out_path = os.path.join(self.out_dir, f"{name}.json")
        out_parent = os.path.dirname(out_path)
        if out_parent:
            os.makedirs(out_parent, exist_ok=True)
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        self._audit_path = os.path.join(self.out_dir, "filter_audit.jsonl")
        self._audit_event(name, source="runner", action="run_start", data={"out_json": out_path})

        pre = self._llm_recommend("preflight")
        self._apply_llm_action(pre, name, phase="preflight")
        if self._drop_reason:
            return False, {"blocked": True, "reason": self._drop_reason, "json": out_path, "audit_log": self._audit_path}

        restarts = 0
        while restarts <= self.max_restarts:
            self._attempt_no = restarts + 1
            self._restart_requested = False
            self._noise_last_candidate = None
            self._noise_stable_ticks = 0
            self.status_counts = {}
            self.pattern_counts_by_status = {}
            self._recent_statuses.clear()
            self._recent_sigs.clear()
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                pass
            cmd = self._build_cmd(out_path)
            self._log(name, "start ffuf")
            self._log(name, "cmd: " + " ".join([f'"{x}"' if (" " in str(x) or ":" in str(x)) else str(x) for x in cmd]))

            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            self.last_output_time = time.time()
            self._watchdog_stop.clear()
            wd = threading.Thread(target=self._watchdog, args=(name,), daemon=True)
            wd.start()

            try:
                for line in self.proc.stdout:
                    if not line:
                        continue
                    self.last_output_time = time.time()
                    self._parse_line(line)
                    total = sum(self.status_counts.values())
                    self._maybe_apply_noise_filter(name)
                    if self._restart_requested:
                        self._stop_proc()
                        break
                    if self.llm_trigger > 0 and total - self._last_llm_total >= self.llm_trigger:
                        self._last_llm_total = total
                        rec = self._llm_recommend("runtime")
                        self._apply_llm_action(rec, name, phase="runtime")
                        if self._drop_reason:
                            self._stop_proc()
                            break
                        if self._restart_requested:
                            self._stop_proc()
                            break
            except Exception:
                pass

            try:
                rc = self.proc.wait(timeout=5)
            except Exception:
                rc = 1
            self._watchdog_stop.set()

            if self._drop_reason:
                reason = self._drop_reason
                try:
                    blocked_name = re.sub(r"[^a-zA-Z0-9._-]", "_", str(name))
                    with open(os.path.join(self.blocked_dir, f"{blocked_name}.json"), "w", encoding="utf-8") as f:
                        json.dump({"blocked": True, "reason": reason, "json": out_path, "progress": [self._cur, self._tot], "errors": self.error_counts.get("ffuf_errors", 0)}, f)
                except Exception:
                    pass
                self._log(name, f"finished blocked reason={reason}")
                return False, {"blocked": True, "reason": reason, "json": out_path, "audit_log": self._audit_path}

            if self._restart_requested:
                restarts += 1
                self._log(name, f"restart: llm_update #{restarts}")
                self._audit_event(name, source="runner", action="restart", data={"reason": "llm_or_noise_runtime", "restart_no": restarts})
                continue
            self._maybe_apply_noise_filter(name, final_pass=True)
            if self._restart_requested:
                restarts += 1
                self._log(name, f"restart: final_noise_update #{restarts}")
                self._audit_event(name, source="runner", action="restart", data={"reason": "final_noise_update", "restart_no": restarts})
                continue

            if rc == 0:
                has_results = self._output_has_results(out_path)
                if has_results:
                    self._log(name, f"finished ok json={out_path}")
                else:
                    self._log(name, f"finished ok empty json={out_path}")
                return True, {"json": out_path, "filters": {k: sorted(list(v)) for k, v in self.filters.items()}, "status_counts": self.status_counts, "empty": (not has_results), "audit_log": self._audit_path}

            if self._output_has_results(out_path):
                self._log(name, f"finished ok with non-zero rc json={out_path}")
                return True, {"json": out_path, "filters": {k: sorted(list(v)) for k, v in self.filters.items()}, "status_counts": self.status_counts, "empty": False, "audit_log": self._audit_path}

            self._log(name, f"finished blocked reason=exec rc={rc}")
            return False, {"blocked": True, "reason": "exec", "json": out_path, "rc": rc, "audit_log": self._audit_path}

        self._log(name, "finished blocked reason=restarts")
        return False, {"blocked": True, "reason": "restarts", "json": out_path, "audit_log": self._audit_path}


def _load_json_results(json_path):
    if (not json_path) or (not os.path.exists(json_path)):
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    arr = data.get("results")
    if not isinstance(arr, list):
        return []
    return arr


def _normalize_path(raw):
    s = "/" + str(raw or "").strip()
    s = re.sub(r"/+", "/", s)
    return s


def _append_filter_audit(out_dir, record):
    if not out_dir:
        return None
    path = os.path.join(out_dir, "filter_audit.jsonl")
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        os.makedirs(out_dir, exist_ok=True)
        with _FILTER_AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return path
    except Exception:
        return path


def _redirect_to_dir_path(raw_path):
    p = _normalize_path(raw_path)
    if p == "/":
        return None
    if p.endswith("/"):
        d = p.rstrip("/")
    else:
        d = p.rsplit("/", 1)[0]
    if (not d) or d == "/":
        return None
    return d.lstrip("/")


def _extract_deep_paths_from_json(json_path, target):
    paths, _ = _analyze_deep_seed_json(json_path, target)
    return paths


def _load_deep_paths_dict(paths_file):
    out = []
    if not paths_file:
        return out
    try:
        with open(paths_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = str(line).strip()
                if not s or s.startswith("#"):
                    continue
                out.append(s.strip("/"))
    except Exception:
        return []
    seen = set()
    cleaned = []
    for p in out:
        if not p:
            continue
        norm = re.sub(r"/+", "/", p).strip("/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(norm)
    return cleaned


def _analyze_deep_seed_json(json_path, target):
    p_target = urlparse(str(target))
    target_host = (p_target.netloc or "").lower()
    out = set()
    stats = {
        "results_total": 0,
        "redirects_301_302": 0,
        "empty_location": 0,
        "cross_domain": 0,
        "no_valid_dir": 0,
        "accepted": 0,
    }
    for it in _load_json_results(json_path):
        stats["results_total"] += 1
        try:
            st = int(it.get("status"))
        except Exception:
            continue
        if st not in (301, 302):
            continue
        stats["redirects_301_302"] += 1
        loc = str(it.get("redirectlocation") or "").strip()
        if not loc:
            stats["empty_location"] += 1
            continue
        p_loc = urlparse(loc)
        if (p_loc.scheme or p_loc.netloc):
            loc_scheme = (p_loc.scheme or "").lower()
            if (loc_scheme not in ("http", "https")) or (p_loc.netloc or "").lower() != target_host:
                stats["cross_domain"] += 1
                continue
            loc_path = _normalize_path(p_loc.path)
        else:
            loc_path = _normalize_path(p_loc.path or loc)
        dir_part = _redirect_to_dir_path(loc_path)
        if not dir_part:
            stats["no_valid_dir"] += 1
            continue
        out.add(dir_part)
    stats["accepted"] = len(out)
    return sorted(out), stats


def _find_seed_json_path(seed_dir, name, method):
    if not seed_dir:
        return None
    base = os.path.join(seed_dir, f"{name}.{str(method).lower()}.json")
    if os.path.exists(base):
        return base
    return None


def _has_non_empty_results(json_path):
    if (not json_path) or (not os.path.exists(json_path)):
        return False
    try:
        if os.path.getsize(json_path) <= 2200:
            arr = _load_json_results(json_path)
            return bool(arr)
    except Exception:
        pass
    return bool(_load_json_results(json_path))


def _run_one_deep_scan(
    target,
    wordlists,
    out_dir,
    blocked_dir,
    max_restarts=2,
    llm_model="x-ai/grok-4.1-fast",
    llm_trigger=50,
    headers=None,
    methods=None,
    post_data=None,
    report_func=None,
    ban_403_rate=0.6,
    ban_429_rate=0.2,
    threads=None,
    rate=None,
    delay=None,
    monitor_period_seconds=10,
    monitor_stall_seconds=60,
    monitor_error_window_seconds=120,
    monitor_error_growth=20,
    ffuf_extra=None,
    ffuf_allow=None,
    ffuf_allow_reset=False,
    deep_source_dir=None,
    deep_depth=1,
    deep_paths_file=None,
    deep_paths_merge=False,
):
    name = sanitize_name(target)
    meths = [str(m).upper() for m in (methods or ["GET"]) if str(m).strip()]
    scans = []
    any_ok = False
    site_dir = None
    depth_limit = max(1, int(deep_depth or 1))
    source_dir = deep_source_dir or out_dir
    manual_seed_paths = _load_deep_paths_dict(deep_paths_file)
    for method in meths:
        seed_json = _find_seed_json_path(source_dir, name, method)
        current_paths = []
        seed_stats = None
        if seed_json and _has_non_empty_results(seed_json):
            current_paths, seed_stats = _analyze_deep_seed_json(seed_json, target)
        if manual_seed_paths:
            if deep_paths_merge:
                current_paths = sorted(set(current_paths + manual_seed_paths))
            else:
                current_paths = list(manual_seed_paths)
        if not current_paths:
            reason = "seed_empty_or_missing"
            if manual_seed_paths:
                reason = "manual_paths_empty_or_invalid"
            scans.append({"method": method, "step": 0, "ok": False, "has_results": False, "reason": reason, "json": seed_json})
            continue
        if site_dir is None:
            site_dir = os.path.join(out_dir, name)
            os.makedirs(site_dir, exist_ok=True)
        _append_filter_audit(
            out_dir,
            {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "name": f"{name}.{method.lower()}",
                "target": target,
                "method": method,
                "attempt": 0,
                "source": "deep_scan_seed",
                "action": "selected_dirs",
                "filters": {"fc": [400, 403, 404], "fl": [], "fw": [], "fs": []},
                "data": {
                    "seed_json": seed_json,
                    "manual_paths_file": deep_paths_file,
                    "manual_paths_count": len(manual_seed_paths),
                    "manual_paths_merge": bool(deep_paths_merge),
                    "selected_count": len(current_paths),
                    "selected_dirs": current_paths,
                    "seed_stats": seed_stats,
                },
            },
        )
        if report_func:
            try:
                report_func(
                    f"{name}.{method.lower()}.seed",
                    f"deep-scan selected_dirs={len(current_paths)} from={os.path.basename(seed_json) if seed_json else '-'} manual={len(manual_seed_paths)} merge={bool(deep_paths_merge)} dirs={current_paths[:20]}",
                )
            except Exception:
                pass
        for step in range(1, depth_limit + 1):
            if not current_paths:
                break
            paths_dict = os.path.join(site_dir, f"{method.lower()}_step{step}_paths.txt")
            with open(paths_dict, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(set(current_paths))) + "\n")
            step_json = os.path.join(site_dir, f"{method.lower()}_step{step}.json")
            step_has_results = False
            step_ok = False
            step_reason = ""
            for idx, wl in enumerate(wordlists or []):
                run_name = f"{name}.{method.lower()}.step{step}"
                runner = FFUFRunner(
                    base_url=target,
                    wordlist=wl,
                    out_dir=out_dir,
                    blocked_dir=blocked_dir,
                    max_restarts=max_restarts,
                    llm_model=llm_model,
                    llm_trigger=llm_trigger,
                    headers=headers,
                    report_func=report_func,
                    ban_403_rate=ban_403_rate,
                    ban_429_rate=ban_429_rate,
                    threads=threads,
                    mode="path",
                    host_suffix="",
                    rate=rate,
                    delay=delay,
                    monitor_period_seconds=monitor_period_seconds,
                    monitor_stall_seconds=monitor_stall_seconds,
                    monitor_error_window_seconds=monitor_error_window_seconds,
                    monitor_error_growth=monitor_error_growth,
                    ffuf_extra=ffuf_extra,
                    ffuf_allow=ffuf_allow,
                    ffuf_allow_reset=ffuf_allow_reset,
                    method=method,
                    post_data=post_data,
                    ffuf_wordlists=[(paths_dict, "PATH"), (wl, "FUZZ")],
                    url_template=f"{target.rstrip('/')}/PATH/FUZZ",
                )
                wl_lines = None
                combos = None
                est_total_seconds = None
                try:
                    wl_lines = sum(1 for _ in open(wl, "r", encoding="utf-8", errors="ignore") if _.strip())
                    combos = len(current_paths) * wl_lines
                    eta_min = round(combos / max(1, int(rate or 50)) / 60, 1) if rate else "?"
                    if rate:
                        est_total_seconds = max(1, int(combos / max(1, int(rate))))
                    runner._log(run_name, f"deep-scan step={step} trying wordlist {idx+1}/{len(wordlists)}: {os.path.basename(wl)} paths={len(current_paths)} words={wl_lines} combos={combos} eta~{eta_min}min")
                except Exception:
                    runner._log(run_name, f"deep-scan step={step} trying wordlist {idx+1}/{len(wordlists)}: {os.path.basename(wl)}")
                monitor_stop = threading.Event()
                start_ts = time.time()
                def _deep_tick():
                    while not monitor_stop.wait(5):
                        elapsed = int(time.time() - start_ts)
                        if est_total_seconds:
                            pct = min(99, int((elapsed * 100) / est_total_seconds))
                            runner._log(run_name, f"deep-scan step={step} running elapsed={elapsed}s est={est_total_seconds}s approx={pct}%")
                        elif combos and wl_lines:
                            runner._log(run_name, f"deep-scan step={step} running elapsed={elapsed}s combos={combos}")
                        else:
                            runner._log(run_name, f"deep-scan step={step} running elapsed={elapsed}s")
                tick_thread = threading.Thread(target=_deep_tick, daemon=True)
                tick_thread.start()
                ok, info = runner.run(run_name, out_path=step_json)
                monitor_stop.set()
                try:
                    tick_thread.join(timeout=1)
                except Exception:
                    pass
                info = info or {}
                step_has_results = bool(info.get("json") and runner._output_has_results(info.get("json")))
                step_ok = bool(ok)
                step_reason = str(info.get("reason") or "")
                scans.append(
                    {
                        "method": method,
                        "step": step,
                        "wordlist": os.path.basename(wl),
                        "ok": step_ok,
                        "has_results": step_has_results,
                        "reason": step_reason,
                        "json": info.get("json") or step_json,
                        "audit_log": info.get("audit_log"),
                        "seed_paths_dict": paths_dict,
                    }
                )
                if step_has_results:
                    any_ok = True
                    break
                if step_reason in {"monitor_stall", "monitor_error_growth", "monitor_ban", "llm_drop"}:
                    break
            if not step_has_results:
                break
            current_paths = _extract_deep_paths_from_json(step_json, target)
    if any_ok:
        return name, (True, {"scans": scans})
    return name, (False, {"blocked": False, "reason": "no_results_all_wordlists", "scans": scans})


def run_one(
    target,
    wordlists,
    out_dir,
    blocked_dir,
    max_restarts=2,
    llm_model="x-ai/grok-4.1-fast",
    llm_trigger=50,
    headers=None,
    methods=None,
    post_data=None,
    report_func=None,
    ban_403_rate=0.6,
    ban_429_rate=0.2,
    threads=None,
    mode="path",
    host_suffix=".domain",
    rate=None,
    delay=None,
    monitor_period_seconds=10,
    monitor_stall_seconds=60,
    monitor_error_window_seconds=120,
    monitor_error_growth=20,
    ffuf_extra=None,
    ffuf_allow=None,
    ffuf_allow_reset=False,
    deep_source_dir=None,
    deep_depth=1,
    deep_paths_file=None,
    deep_paths_merge=False,
):
    if str(mode or "").strip().lower() == "deep-scan":
        return _run_one_deep_scan(
            target=target,
            wordlists=wordlists,
            out_dir=out_dir,
            blocked_dir=blocked_dir,
            max_restarts=max_restarts,
            llm_model=llm_model,
            llm_trigger=llm_trigger,
            headers=headers,
            methods=methods,
            post_data=post_data,
            report_func=report_func,
            ban_403_rate=ban_403_rate,
            ban_429_rate=ban_429_rate,
            threads=threads,
            rate=rate,
            delay=delay,
            monitor_period_seconds=monitor_period_seconds,
            monitor_stall_seconds=monitor_stall_seconds,
            monitor_error_window_seconds=monitor_error_window_seconds,
            monitor_error_growth=monitor_error_growth,
            ffuf_extra=ffuf_extra,
            ffuf_allow=ffuf_allow,
            ffuf_allow_reset=ffuf_allow_reset,
            deep_source_dir=deep_source_dir,
            deep_depth=deep_depth,
            deep_paths_file=deep_paths_file,
            deep_paths_merge=deep_paths_merge,
        )
    name = sanitize_name(target)
    meths = [str(m).upper() for m in (methods or ["GET"]) if str(m).strip()]
    scans = []
    any_ok = False
    for method in meths:
        for idx, wl in enumerate(wordlists or []):
            run_name = f"{name}.{method.lower()}"
            runner = FFUFRunner(
            base_url=target,
            wordlist=wl,
            out_dir=out_dir,
            blocked_dir=blocked_dir,
            max_restarts=max_restarts,
            llm_model=llm_model,
            llm_trigger=llm_trigger,
            headers=headers,
            report_func=report_func,
            ban_403_rate=ban_403_rate,
            ban_429_rate=ban_429_rate,
            threads=threads,
            mode=mode,
            host_suffix=host_suffix,
            rate=rate,
            delay=delay,
            monitor_period_seconds=monitor_period_seconds,
            monitor_stall_seconds=monitor_stall_seconds,
            monitor_error_window_seconds=monitor_error_window_seconds,
            monitor_error_growth=monitor_error_growth,
            ffuf_extra=ffuf_extra,
            ffuf_allow=ffuf_allow,
            ffuf_allow_reset=ffuf_allow_reset,
            method=method,
            post_data=post_data,
        )
            runner._log(run_name, f"trying wordlist {idx+1}/{len(wordlists)}: {os.path.basename(wl)}")
            ok, info = runner.run(run_name)
            info = info or {}
            has_results = bool(info.get("json") and runner._output_has_results(info.get("json")))
            scans.append({"method": method, "wordlist": os.path.basename(wl), "ok": bool(ok), "has_results": has_results, "reason": info.get("reason"), "json": info.get("json"), "audit_log": info.get("audit_log")})
            if has_results:
                any_ok = True
                break
            reason = info.get("reason", "")
            if reason in {"monitor_stall", "monitor_error_growth", "monitor_ban", "llm_drop"}:
                break
    if any_ok:
        return name, (True, {"scans": scans})
    return name, (False, {"blocked": False, "reason": "no_results_all_wordlists", "scans": scans})
