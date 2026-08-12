#!/usr/bin/env python3
"""Daily maintenance window for the Project Zomboid server.

At the configured local time: warn players over RCON, flush the world, archive
it outside data/, then quit. The process exiting is what restarts the server --
Docker's restart policy brings the container back up, which is also when PZ
picks up updated Workshop mods.

The container mounts the world read-only. It never writes into the game data.
"""

import logging
import os
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
MAINTENANCE_TIME = os.environ.get("MAINTENANCE_TIME", "13:00")
MAINTENANCE_TZ = os.environ.get("MAINTENANCE_TZ", "Europe/Moscow")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "3"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9116"))
SAVE_SETTLE_SECONDS = int(os.environ.get("SAVE_SETTLE_SECONDS", "30"))

# Saves and db are the world; Server holds the .ini and SandboxVars.lua, which
# PZ's own backups do not cover and which are the most painful to rebuild.
BACKUP_PATHS = ["Saves", "db", "Server"]

# Seconds before the window, and what players see. Confirmed working in-game.
WARNINGS = [
    (600, "Перезапуск сервера через 10 минут"),
    (300, "Перезапуск сервера через 5 минут"),
    (60, "Перезапуск сервера через 1 минуту"),
]

backup_last_success = Gauge(
    "pz_backup_last_success_timestamp", "Unix time of the last successful backup"
)
backup_size = Gauge("pz_backup_size_bytes", "Size of the last backup archive")
backup_duration = Gauge(
    "pz_backup_duration_seconds", "How long the last backup took"
)
backup_failures = Counter("pz_backup_failures_total", "Failed backup attempts")
restart_last = Gauge(
    "pz_restart_last_timestamp", "Unix time of the last scheduled restart"
)

# NaN, not 0: a "seconds since" panel would otherwise read as decades until the
# first run.
backup_last_success.set(float("nan"))
restart_last.set(float("nan"))


def rcon(command):
    """Send one RCON command. Returns the reply, or None on failure."""
    with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
        return mcr.command(command)


def next_window(now):
    """The next occurrence of MAINTENANCE_TIME in MAINTENANCE_TZ, after now."""
    hour, minute = (int(x) for x in MAINTENANCE_TIME.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def sleep_until(when, tz):
    """Sleep in short steps so a clock change or DST shift is picked up."""
    while True:
        remaining = (when - datetime.now(tz)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def make_backup():
    """Archive the world and config into BACKUP_DIR. Returns the path."""
    stamp = datetime.now(ZoneInfo(MAINTENANCE_TZ)).strftime("%Y-%m-%dT%H-%M")
    archive = BACKUP_DIR / f"pz-backup-{stamp}.tar.gz"
    started = time.time()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for name in BACKUP_PATHS:
            source = DATA_DIR / name
            if source.exists():
                tar.add(source, arcname=name)
            else:
                log.warning("Skipping %s: not found under %s", name, DATA_DIR)

    backup_size.set(archive.stat().st_size)
    backup_duration.set(time.time() - started)
    backup_last_success.set(time.time())
    log.info(
        "Backup written: %s (%.1f MB, %.0fs)",
        archive.name,
        archive.stat().st_size / 1e6,
        time.time() - started,
    )
    return archive


def prune_backups():
    """Delete archives older than BACKUP_KEEP_DAYS."""
    cutoff = time.time() - BACKUP_KEEP_DAYS * 86400
    for old in BACKUP_DIR.glob("pz-backup-*.tar.gz"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            log.info("Pruned old backup: %s", old.name)


def run_window():
    """Save, back up, restart. A failed backup must not skip the restart."""
    try:
        log.info("Flushing the world")
        rcon("save")
        time.sleep(SAVE_SETTLE_SECONDS)
    except Exception as exc:
        log.error("save failed, backing up anyway: %s", exc)

    try:
        make_backup()
        prune_backups()
    except Exception as exc:
        backup_failures.inc()
        log.error("Backup failed: %s", exc)

    try:
        log.info("Restarting the server")
        restart_last.set(time.time())
        rcon("quit")
    except Exception as exc:
        # The server drops the connection as it shuts down, so an error here is
        # expected as often as not.
        log.info("quit sent, connection closed (%s)", exc)


def send_one(argv):
    """One-shot mode: `scheduler.py rcon <command>`. Lets the Makefile reuse the
    RCON credentials this container already has instead of a second copy."""
    verb, *rest = argv
    # servermsg expects the text quoted, and the shell strips the quotes before
    # they reach us. Re-add them so this path matches the scheduled warnings.
    command = f'{verb} "{" ".join(rest)}"' if verb == "servermsg" and rest else " ".join(argv)
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

        for offset, message in WARNINGS:
            sleep_until(window - timedelta(seconds=offset), tz)
            try:
                rcon(f'servermsg "{message}"')
                log.info("Warned players: %s", message)
            except Exception as exc:
                log.warning("Could not warn players: %s", exc)

        sleep_until(window, tz)
        run_window()

        # The container is on its way down; wait past the window so a fast
        # restart cannot trigger a second run for the same day.
        time.sleep(120)


if __name__ == "__main__":
    main()