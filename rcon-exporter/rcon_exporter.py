import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcrcon import MCRcon
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pz-exporter")

RCON_HOST = os.environ.get("RCON_HOST", "zomboid")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "5"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/logs/Logs"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9115"))

players_online = Gauge("pz_players_online", "Players currently connected (via RCON)")
rcon_up = Gauge("pz_rcon_up", "RCON reachable (1=yes, 0=no)")
log_last_activity = Gauge(
    "pz_log_last_activity_timestamp",
    "Unix time when the exporter last saw a new line in the server log",
)
last_autosave = Gauge(
    "pz_last_autosave_timestamp", "Unix time of the last world save seen in the log"
)

errors = Counter("pz_errors_total", "ERROR lines", ["phase"])
warnings = Counter("pz_warnings_total", "WARN lines", ["phase"])
starts = Counter("pz_server_starts_total", "Server startup completions")
deaths = Counter("pz_player_deaths_total", "Player deaths")
autosaves = Counter("pz_autosave_total", "World saves")
oom_errors = Counter("pz_oom_errors_total", "Java OutOfMemoryError occurrences")

for _phase in ("startup", "runtime"):
    errors.labels(phase=_phase)
    warnings.labels(phase=_phase)

log_last_activity.set(float("nan"))
last_autosave.set(float("nan"))

RE_SERVER_STARTED = re.compile(r"\*\*\* SERVER STARTED")
RE_ERROR = re.compile(r"^\[[^\]]*\]\s+ERROR:")
RE_WARN = re.compile(r"^\[[^\]]*\]\s+WARN\s*:")
RE_AUTOSAVE = re.compile(r"Saving finish")
RE_OOM = re.compile(r"OutOfMemoryError")
RE_DIED = re.compile(r"\[Died\]")

_phase_lock = threading.Lock()
_phase = "runtime"


def set_phase(value):
    global _phase
    with _phase_lock:
        if _phase != value:
            log.info("Phase: %s", value)
        _phase = value


def current_phase():
    with _phase_lock:
        return _phase


def newest_match(pattern):
    files = list(LOG_DIR.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def tail(pattern, on_new_file=None):
    handle = None
    current = None
    skip_existing = True

    while True:
        newest = newest_match(pattern)
        if newest is None:
            time.sleep(2)
            continue

        if handle is None or newest != current:
            switched = current is not None and newest != current
            if handle is not None:
                for line in handle:
                    yield line
                handle.close()
                log.info("Switching to %s", newest.name)
            handle = open(newest, "r", errors="replace")
            if skip_existing:
                handle.seek(0, os.SEEK_END)
                skip_existing = False
            current = newest
            if switched and on_new_file is not None:
                on_new_file()

        while True:
            line = handle.readline()
            if not line:
                break
            yield line

        try:
            if newest.stat().st_size < handle.tell():
                log.info("%s was truncated, reopening", newest.name)
                handle.close()
                handle = None
                continue
        except OSError:
            handle.close()
            handle = None
            continue

        time.sleep(1)


def watch_debug_log():
    for line in tail("*_DebugLog-server.txt", on_new_file=lambda: set_phase("startup")):
        log_last_activity.set(time.time())
        if RE_SERVER_STARTED.search(line):
            starts.inc()
            set_phase("runtime")
            continue
        if RE_ERROR.match(line):
            errors.labels(phase=current_phase()).inc()
        elif RE_WARN.match(line):
            warnings.labels(phase=current_phase()).inc()
        if RE_AUTOSAVE.search(line):
            autosaves.inc()
            last_autosave.set(time.time())
        if RE_OOM.search(line):
            oom_errors.inc()


def watch_perk_log():
    for line in tail("*_PerkLog.txt"):
        if RE_DIED.search(line):
            deaths.inc()


_rcon_was_up = None


def scrape_rcon():
    global _rcon_was_up
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            resp = mcr.command("players")
            match = re.search(r"\((\d+)\)", resp)
            if match:
                players_online.set(int(match.group(1)))
            rcon_up.set(1)
            if _rcon_was_up is not True:
                log.info("RCON connected")
                _rcon_was_up = True
    except Exception as exc:
        rcon_up.set(0)
        if _rcon_was_up is not False:
            log.warning("RCON unavailable: %s", exc)
            _rcon_was_up = False


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        payload = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


def main():
    log.info("Exporter starting: RCON %s:%s, logs %s", RCON_HOST, RCON_PORT, LOG_DIR)

    httpd = HTTPServer(("0.0.0.0", LISTEN_PORT), MetricsHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    for worker in (watch_debug_log, watch_perk_log):
        threading.Thread(target=worker, daemon=True).start()
    log.info("Metrics on :%s/metrics", LISTEN_PORT)

    if not RCON_PASSWORD:
        log.warning("RCON_PASSWORD is empty: pz_players_online stays at 0.")
        rcon_up.set(0)
        threading.Event().wait()

    while True:
        scrape_rcon()
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()