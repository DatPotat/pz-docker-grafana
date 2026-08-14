import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from mcrcon import MCRcon
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pz-exporter")

RCON_HOST = os.environ.get("RCON_HOST", "zomboid")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "5"))
LOG_FILE = os.environ.get("LOG_FILE", "/logs/server-console.txt")
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

log_last_activity.set(float("nan"))
last_autosave.set(float("nan"))

errors = Counter("pz_errors_total", "ERROR lines in the server log")
deaths = Counter("pz_player_deaths_total", "Player death announcements")
starts = Counter("pz_server_starts_total", "Server startup completions")
connections = Counter("pz_connection_attempts_total", "Client connection attempts")
autosaves = Counter("pz_autosave_total", "World autosaves")
oom_errors = Counter("pz_oom_errors_total", "Java OutOfMemoryError occurrences")

LOG_PATTERNS = [
    (re.compile(r"^ERROR:"), errors),
    (re.compile(r"player.*died|zombie.*killed.*player|was killed"), deaths),
    (re.compile(r"LuaNet.*Initialization.*DONE"), starts),
    (re.compile(r"Connected new client|initiating a connection"), connections),
    (re.compile(r"Saving finish"), autosaves),
    (re.compile(r"OutOfMemoryError"), oom_errors),
]


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


def tail(path):
    handle = None
    inode = None
    skip_existing = True

    while True:
        if handle is None:
            try:
                handle = open(path, "r", errors="replace")
            except OSError:
                time.sleep(2)
                continue
            if skip_existing:
                handle.seek(0, os.SEEK_END)
                skip_existing = False
            inode = os.fstat(handle.fileno()).st_ino

        line = handle.readline()
        if line:
            yield line
            continue

        try:
            stat = os.stat(path)
            rotated = stat.st_ino != inode or stat.st_size < handle.tell()
        except OSError:
            rotated = True

        if rotated:
            log.info("Server log rotated, reopening %s", path)
            handle.close()
            handle = None
            continue

        time.sleep(1)


def watch_log():
    for line in tail(LOG_FILE):
        log_last_activity.set(time.time())
        for pattern, counter in LOG_PATTERNS:
            if pattern.search(line):
                counter.inc()
                if counter is autosaves:
                    last_autosave.set(time.time())


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
    log.info("Exporter starting: RCON %s:%s, log %s", RCON_HOST, RCON_PORT, LOG_FILE)

    httpd = HTTPServer(("0.0.0.0", LISTEN_PORT), MetricsHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=watch_log, daemon=True).start()
    log.info("Metrics on :%s/metrics", LISTEN_PORT)

    if not RCON_PASSWORD:
        log.warning("RCON_PASSWORD is empty: RCON is disabled server-side, "
                    "pz_players_online stays at 0. Log metrics still work.")
        rcon_up.set(0)
        threading.Event().wait()

    while True:
        scrape_rcon()
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()