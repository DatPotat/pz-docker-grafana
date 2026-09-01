import logging
import os
import signal
import sys
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mcrcon import MCRcon
from prometheus_client import Counter, Gauge, start_http_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pz-scheduler")

RCON_HOST = os.environ.get("RCON_HOST", "zomboid")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
MAINTENANCE_TIME = os.environ.get("MAINTENANCE_TIME", "14:00")
MAINTENANCE_TZ = os.environ.get("MAINTENANCE_TZ", "Europe/Moscow")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "3"))
BACKUP_TIMEOUT = int(os.environ.get("BACKUP_TIMEOUT", "900"))
BACKUP_COMPRESS_LEVEL = int(os.environ.get("BACKUP_COMPRESS_LEVEL", "6"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9116"))
SAVE_SETTLE_SECONDS = int(os.environ.get("SAVE_SETTLE_SECONDS", "30"))
SHUTDOWN_SETTLE_SECONDS = int(os.environ.get("SHUTDOWN_SETTLE_SECONDS", "20"))

BACKUP_PATHS = ["Saves", "db", "Server"]

WARNING_OFFSETS = (600, 300, 60, 30, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)


def plural(n, forms):
    if n % 100 // 10 == 1:
        return forms[2]
    tail = n % 10
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def countdown_message(seconds):
    if seconds >= 60 and seconds % 60 == 0:
        value = seconds // 60
        unit = plural(value, ("минуту", "минуты", "минут"))
    else:
        value = seconds
        unit = plural(value, ("секунду", "секунды", "секунд"))
    return "Перезапуск сервера через %d %s" % (value, unit)

backup_last_success = Gauge(
    "pz_backup_last_success_timestamp", "Unix time of the last successful backup"
)
backup_size = Gauge("pz_backup_size_bytes", "Size of the last backup archive")
backup_duration = Gauge("pz_backup_duration_seconds", "How long the last backup took")
backup_failures = Counter("pz_backup_failures_total", "Failed backup attempts")
restart_last = Gauge(
    "pz_restart_last_timestamp", "Unix time of the last scheduled restart"
)

backup_last_success.set(float("nan"))
restart_last.set(float("nan"))


class BackupTimeout(Exception):
    pass


def rcon(command):
    with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
        return mcr.command(command)


def next_window(now):
    hour, minute = (int(x) for x in MAINTENANCE_TIME.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def sleep_until(when, tz):
    while True:
        remaining = (when - datetime.now(tz)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def _on_timeout(signum, frame):
    raise BackupTimeout("backup exceeded %ss" % BACKUP_TIMEOUT)


def make_backup():
    stamp = datetime.now(ZoneInfo(MAINTENANCE_TZ)).strftime("%Y-%m-%dT%H-%M")
    archive = BACKUP_DIR / ("pz-backup-%s.tar.gz" % stamp)
    partial = BACKUP_DIR / ("pz-backup-%s.tar.gz.partial" % stamp)
    started = time.time()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGALRM, _on_timeout)
    signal.alarm(BACKUP_TIMEOUT)
    try:
        with tarfile.open(partial, "w:gz", compresslevel=BACKUP_COMPRESS_LEVEL) as tar:
            for name in BACKUP_PATHS:
                source = DATA_DIR / name
                if source.exists():
                    tar.add(source, arcname=name)
                else:
                    log.warning("Skipping %s: not found under %s", name, DATA_DIR)
        partial.rename(archive)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        signal.alarm(0)

    elapsed = time.time() - started
    backup_size.set(archive.stat().st_size)
    backup_duration.set(elapsed)
    backup_last_success.set(time.time())
    log.info(
        "Backup written: %s (%.0f MB, %.0fs)",
        archive.name,
        archive.stat().st_size / 1e6,
        elapsed,
    )
    return archive


def prune_backups():
    cutoff = time.time() - BACKUP_KEEP_DAYS * 86400
    for old in BACKUP_DIR.glob("pz-backup-*"):
        if old.name.endswith(".partial") or old.stat().st_mtime < cutoff:
            old.unlink()
            log.info("Pruned: %s", old.name)


def run_window():
    try:
        log.info("Flushing the world")
        rcon("save")
        time.sleep(SAVE_SETTLE_SECONDS)
    except Exception as exc:
        log.error("save failed: %s", exc)

    try:
        log.info("Restarting the server")
        restart_last.set(time.time())
        rcon("quit")
    except Exception as exc:
        log.info("quit sent, connection closed (%s)", exc)

    time.sleep(SHUTDOWN_SETTLE_SECONDS)

    try:
        make_backup()
        prune_backups()
    except Exception as exc:
        backup_failures.inc()
        log.error("Backup failed: %s", exc)


def send_one(argv):
    verb, *rest = argv
    command = (
        '%s "%s"' % (verb, " ".join(rest))
        if verb == "servermsg" and rest
        else " ".join(argv)
    )
    print(rcon(command))


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "rcon":
        send_one(sys.argv[2:])
        return

    tz = ZoneInfo(MAINTENANCE_TZ)
    start_http_server(LISTEN_PORT)
    log.info(
        "Scheduler started: maintenance at %s %s, metrics on :%s",
        MAINTENANCE_TIME,
        MAINTENANCE_TZ,
        LISTEN_PORT,
    )

    if not RCON_PASSWORD:
        log.error("RCON_PASSWORD is empty: cannot save or restart. Idling.")
        while True:
            time.sleep(3600)

    while True:
        window = next_window(datetime.now(tz))
        log.info("Next maintenance window: %s", window.isoformat())

        for offset in WARNING_OFFSETS:
            sleep_until(window - timedelta(seconds=offset), tz)
            message = countdown_message(offset)
            try:
                rcon('servermsg "%s"' % message)
                log.info("Warned players: %s", message)
            except Exception as exc:
                log.warning("Could not warn players: %s", exc)

        sleep_until(window, tz)
        run_window()
        time.sleep(120)


if __name__ == "__main__":
    main()