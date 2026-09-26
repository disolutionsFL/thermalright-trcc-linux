#!/usr/bin/env python3
"""Drive the cooler LCD's live parts and publish the rig's status over HTTP.

Each tick (default 2s) it samples CPU/GPU temperature + load and each GPU's ComfyUI
queue, then:

* colours the temperature numbers by band (green/yellow/orange/red), and
* writes one white job line per GPU -- label or model, elapsed, +queued,
* serves the same state read-only over HTTP (optional "api" config), plus the
  exact frame on the LCD, for a dashboard to poll: GET /status, /screen.png, /health.

Drives the trcc daemon (see packaging/systemd/README.md, "Headless machines")
through ONE long-lived `trcc shell` session: one-shot `trcc` invocations cost ~0.5s
each, while lines written to an open shell are near-instant. Only sends a change
when a value changes, plus a full resync every `resync_seconds` -- so if trccd
restarts and reloads the theme, the screen is repainted within a minute without
any coupling between the two services.

Stdlib only; runs on the system python3. Config and install: contrib/headless-lcd/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def log(msg: str) -> None:
    print(f"lcd_watcher: {msg}", file=sys.stderr, flush=True)


# ---- sensors -----------------------------------------------------------------

CPU_HWMON_DRIVERS = ("coretemp", "k10temp", "zenpower")
CPU_LABELS = ("Package id 0", "Tctl", "Tdie")


def find_cpu_sensor() -> Path | None:
    """The package temperature input of the first CPU hwmon driver found."""
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            name = (hw / "name").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name not in CPU_HWMON_DRIVERS:
            continue
        inputs = sorted(hw.glob("temp*_input"))
        for inp in inputs:
            label = inp.with_name(inp.name.replace("_input", "_label"))
            if label.exists() and label.read_text(encoding="utf-8").strip() in CPU_LABELS:
                return inp
        if inputs:  # no recognised label: temp1 is the package on these drivers
            return inputs[0]
    return None


def read_cpu_temp(sensor: Path | None) -> float | None:
    if sensor is None:
        return None
    try:
        return int(sensor.read_text(encoding="utf-8")) / 1000
    except (OSError, ValueError):
        return None


class CpuUsage:
    """Whole-CPU busy % from /proc/stat deltas between calls."""

    def __init__(self) -> None:
        self.prev: tuple[int, int] | None = None

    def read(self) -> float | None:
        try:
            fields = [int(x) for x in Path("/proc/stat").read_text(encoding="utf-8").split("\n", 1)[0].split()[1:]]
        except (OSError, ValueError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)   # idle + iowait
        total = sum(fields)
        prev, self.prev = self.prev, (idle, total)
        if prev is None or total == prev[1]:
            return None
        return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def read_gpus() -> dict[int, dict]:
    """nvidia-smi index -> {name, temp, usage}. Empty if no NVIDIA driver/GPU."""
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        log(f"nvidia-smi failed: {e}")
        return {}
    gpus: dict[int, dict] = {}
    for line in out.strip().splitlines():
        try:
            idx, name, temp, usage = (p.strip() for p in line.split(","))
            gpus[int(idx)] = {"name": name, "temp": float(temp), "usage": float(usage)}
        except ValueError:
            continue
    return gpus


# ---- ComfyUI jobs ------------------------------------------------------------

#: Loader inputs that name the model a job is running, in preference order.
MODEL_INPUTS = ("unet_name", "ckpt_name", "model_name", "gguf_name")
NBSP = " "


def job_label(prompt: dict, extra: dict) -> str:
    """What to call a running job: an explicit label if the submitter sent one
    (extra_data "label" -- e.g. from an orchestrating service), else the model
    its loader node names, else "job"."""
    if extra.get("label"):
        return str(extra["label"])
    for node in (prompt or {}).values():
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        for key in MODEL_INPUTS:
            name = inputs.get(key)
            if isinstance(name, str) and name:
                stem = Path(name).name
                for ext in (".safetensors", ".gguf", ".ckpt", ".pt", ".pth"):
                    stem = stem.removesuffix(ext)
                return stem
    return "job"


def read_queue(host: str, port: int) -> tuple[tuple[str, str] | None, int] | None:
    """(running (prompt_id, label) or None, pending count); None if unreachable."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/queue", timeout=3) as r:
            q = json.load(r)
    except (OSError, ValueError):
        return None
    running = None
    if q.get("queue_running"):
        _num, prompt_id, prompt, extra, *_ = q["queue_running"][0]
        running = (prompt_id, job_label(prompt, extra or {}))
    return running, len(q.get("queue_pending", []))


class JobTracker:
    """Per-GPU job state + line text. Elapsed counts from when a job was first seen
    running (ComfyUI's create_time is submission time, which includes queueing)."""

    def __init__(self, max_chars: int, idle_text: str, offline_text: str,
                 pad: bool = False) -> None:
        self.max_chars = max_chars
        #: Left-justify: trcc only CENTRES text, so pad every line to max_chars
        #: with no-break spaces (Qt trims ordinary trailing spaces when centring).
        #: Only exact with a MONOSPACE font on the element.
        self.pad = pad
        self.idle_text = idle_text
        self.offline_text = offline_text
        self.started: dict[str, float] = {}   # prompt_id -> monotonic first-seen

    def job(self, state: tuple[tuple[str, str] | None, int] | None) -> dict:
        """Structured job state for the API."""
        if state is None:
            return {"state": "offline"}
        running, pending = state
        if running is None:
            return {"state": "idle", "pending": pending}
        prompt_id, label = running
        t0 = self.started.setdefault(prompt_id, time.monotonic())
        return {"state": "running", "prompt_id": prompt_id, "label": label,
                "elapsed_s": int(time.monotonic() - t0), "pending": pending}

    def line(self, job: dict) -> str:
        """The LCD line for a job() dict (unpadded)."""
        if job["state"] == "offline":
            return self.offline_text
        queued = f" · +{job['pending']}" if job.get("pending") else ""
        if job["state"] == "idle":
            return self.idle_text + queued
        secs = job["elapsed_s"]
        suffix = f" · {secs // 60}:{secs % 60:02d}" + queued
        label, room = job["label"], self.max_chars - len(suffix)
        if len(label) > room:
            label = label[: max(room - 1, 1)] + "…"
        return label + suffix

    def padded(self, line: str) -> str:
        return line.ljust(self.max_chars, NBSP) if self.pad else line

    def forget_finished(self, live_ids: set[str]) -> None:
        for pid in list(self.started):
            if pid not in live_ids:
                del self.started[pid]


def shell_quote(text: str) -> str:
    """Quote a value for one `trcc shell` line (it splits like a shell)."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


# ---- bands -------------------------------------------------------------------

def pick_band(temp: float, bands: list[list], current: int | None, hysteresis: float) -> int:
    """Index of the band for `temp`. Bands are [[min_temp, colour], ...] ascending.

    Rising uses the plain thresholds; falling only drops out of the current band
    once `temp` is `hysteresis` below its threshold, so a reading sitting on a
    boundary does not flicker between two colours.
    """
    target = 0
    for i, (threshold, _colour) in enumerate(bands):
        if temp >= threshold:
            target = i
    if current is not None and target < current:
        if temp >= bands[current][0] - hysteresis:
            return current
    return target


# ---- trcc shell --------------------------------------------------------------

def daemon_socket() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "trcc.sock"


class TrccShell:
    """One persistent `trcc shell` routed to the daemon; respawned if it dies."""

    def __init__(self, trcc: str) -> None:
        self.trcc = trcc
        self.proc: subprocess.Popen[str] | None = None

    @staticmethod
    def _wait_for_daemon_socket(timeout: float = 120) -> None:
        """Block until trccd's socket exists.

        A trcc client that finds no daemon AUTO-SPAWNS one. If that happens while
        trccd is still starting, the spawned daemon takes the socket and trccd keeps
        the USB panel, so every command lands on a daemon with no screen attached.
        """
        sock = daemon_socket()
        deadline = time.monotonic() + timeout
        while not sock.is_socket():
            if time.monotonic() > deadline:
                raise RuntimeError(f"no trcc daemon socket at {sock}")
            time.sleep(1)

    def _spawn(self) -> None:
        self._wait_for_daemon_socket()
        env = dict(os.environ, TRCC_DAEMON="1")
        self.proc = subprocess.Popen(
            [self.trcc, "shell"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, env=env,
        )
        log(f"trcc shell started (pid {self.proc.pid})")

    def send(self, line: str) -> bool:
        """Write one command line; True if written. Respawns a dead shell once."""
        for _attempt in (1, 2):
            if self.proc is None or self.proc.poll() is not None:
                self._spawn()
            assert self.proc is not None and self.proc.stdin is not None
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError) as e:
                log(f"shell write failed ({e}); respawning")
                self.proc = None
        return False


# ---- HTTP API ----------------------------------------------------------------

class Status:
    """Latest sampled state, shared between the loop and the HTTP threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict = {}

    def set(self, data: dict) -> None:
        with self.lock:
            self.data = data

    def get(self) -> dict:
        with self.lock:
            return self.data


def serve_api(cfg: dict, status: Status, device: str) -> None:
    """Read-only HTTP: /status (JSON), /screen.png (LCD frame), /health."""
    bind = cfg.get("bind", "0.0.0.0")
    port = int(cfg.get("port", 8795))
    preview_url = (f"http://127.0.0.1:{int(cfg.get('trcc_api_port', 8787))}"
                   f"/devices/{device}/display/preview")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # stdlib handler name
            path = self.path.split("?", 1)[0]
            if path == "/status":
                self._send(200, json.dumps(status.get()).encode(), "application/json")
            elif path == "/screen.png":
                try:
                    with urllib.request.urlopen(preview_url, timeout=5) as r:
                        self._send(200, r.read(), "image/png")
                except (OSError, ValueError) as e:
                    msg = json.dumps({"error": f"LCD preview unavailable: {e}"}).encode()
                    self._send(503, msg, "application/json")
            elif path == "/health":
                age = time.time() - status.get().get("updated", 0)
                ok = age < 30
                body = json.dumps({"ok": ok, "status_age_s": round(age, 1)}).encode()
                self._send(200 if ok else 503, body, "application/json")
            else:
                self._send(404, b'{"error": "try /status, /screen.png or /health"}',
                           "application/json")

        def log_message(self, fmt: str, *args) -> None:  # quiet: dashboards poll often
            pass

    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"status API on http://{bind}:{port} (/status, /screen.png, /health)")


# ---- main loop ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--once", action="store_true", help="one pass, print, exit")
    args = ap.parse_args()

    cfg = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
    device = cfg["device"]
    interval = float(cfg.get("interval", 2))
    hysteresis = float(cfg.get("hysteresis", 2))
    resync = float(cfg.get("resync_seconds", 60))
    cpu_cfg = cfg.get("cpu")
    gpu_cfg = cfg.get("gpu")
    jobs_cfg = cfg.get("jobs")
    trcc = str(Path(cfg.get("trcc", "~/.local/bin/trcc")).expanduser())

    cpu_sensor = find_cpu_sensor()
    cpu_usage = CpuUsage()
    cpu_usage.read()
    log(f"device {device}; cpu sensor {cpu_sensor}; interval {interval}s")
    if cpu_cfg and cpu_sensor is None:
        log("no CPU hwmon sensor found; CPU element will not be coloured")

    jobs_host = jobs_cfg.get("host", "127.0.0.1") if jobs_cfg else ""
    tracker = JobTracker(
        int(jobs_cfg.get("max_chars", 34)), jobs_cfg.get("idle_text", "idle"),
        jobs_cfg.get("offline_text", "comfyui down"), bool(jobs_cfg.get("left_justify", False)),
    ) if jobs_cfg else None

    status = Status()
    if cfg.get("api") and not args.once:
        serve_api(cfg["api"], status, device)

    shell = TrccShell(trcc)
    bands_now: dict[str, int] = {}   # element id -> band index currently applied
    lines_now: dict[str, str] = {}   # element id -> job line currently shown
    last_resync = 0.0
    host = socket.gethostname()

    while True:
        full = time.monotonic() - last_resync >= resync
        cpu_temp = read_cpu_temp(cpu_sensor)
        gpus = read_gpus()

        # -- temperature colours
        readings: dict[str, tuple[float, list[list]]] = {}
        if cpu_cfg and cpu_temp is not None:
            readings[cpu_cfg["element"]] = (cpu_temp, cpu_cfg["bands"])
        if gpu_cfg:
            for idx, g in gpus.items():
                readings[gpu_cfg["element"].format(n=idx)] = (g["temp"], gpu_cfg["bands"])
        colours: dict[str, str] = {}
        for element, (temp, bands) in readings.items():
            band = pick_band(temp, bands, bands_now.get(element), hysteresis)
            colours[element] = bands[band][1]
            if args.once:
                print(f"{element}: {temp:.0f}C -> band {band} {bands[band][1]}")
            elif full or bands_now.get(element) != band:
                if shell.send(f"display overlay-update {device} {element} --color {bands[band][1]}"):
                    if bands_now.get(element) != band:
                        log(f"{element} {temp:.0f}C -> {bands[band][1]}")
                    bands_now[element] = band

        # -- job lines
        jobs: dict[int, dict] = {}
        if jobs_cfg and tracker:
            live: set[str] = set()
            for gpu, port in jobs_cfg["ports"].items():
                job = tracker.job(read_queue(jobs_host, int(port)))
                job["comfyui_port"] = int(port)
                jobs[int(gpu)] = job
                if job.get("prompt_id"):
                    live.add(job["prompt_id"])
                element = jobs_cfg["element"].format(n=gpu)
                line = tracker.line(job)
                if args.once:
                    print(f"{element}: {line!r}")
                elif full or lines_now.get(element) != line:
                    text = shell_quote(tracker.padded(line))
                    if shell.send(f"display overlay-update {device} {element} --text {text}"):
                        if lines_now.get(element) != line and (
                                job["state"] != "running" or element not in lines_now
                                or lines_now[element].split(" · ")[0] != line.split(" · ")[0]):
                            log(f"{element}: {line}")   # state/label changes, not every elapsed tick
                        lines_now[element] = line
            tracker.forget_finished(live)

        # -- API state
        gpu_cfg_el = gpu_cfg["element"] if gpu_cfg else None
        status.set({
            "host": host,
            "device": device,
            "updated": time.time(),
            "interval_s": interval,
            "cpu": {
                "temp_c": cpu_temp,
                "usage_pct": cpu_usage.read(),
                "colour": colours.get(cpu_cfg["element"]) if cpu_cfg else None,
            },
            "gpus": [
                {"index": idx, **g,
                 "colour": colours.get(gpu_cfg_el.format(n=idx)) if gpu_cfg_el else None,
                 "job": jobs.get(idx),
                 "job_line": tracker.line(jobs[idx]) if tracker and idx in jobs else None}
                for idx, g in sorted(gpus.items())
            ],
        })

        if full:
            last_resync = time.monotonic()
        if args.once:
            print(json.dumps(status.get(), indent=2))
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
