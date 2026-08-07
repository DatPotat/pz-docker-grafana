# Project Zomboid b42 server + monitoring

Docker Compose stack: a Project Zomboid dedicated server with Prometheus, Loki,
Grafana and a small exporter that reports player count and events parsed from the
server log.

Target host is an Ubuntu server. Local development happens on Windows, where the
`node-exporter` service will not report real host metrics (see *Windows notes*).

## Services

| Service | Image | Purpose |
|---|---|---|
| `zomboid` | `steamcmd/steamcmd:ubuntu-24` | The game server |
| `rcon-exporter` | built locally | Player count via RCON, counters from the log |
| `node-exporter` | `prom/node-exporter` | Host CPU, RAM, disk, load |
| `prometheus` | `prom/prometheus` | Metric storage, 30 day retention |
| `loki` | `grafana/loki` | Log storage, 30 day retention |
| `alloy` | `grafana/alloy` | Tails the server log into Loki |
| `scheduler` | built locally | Daily save, backup and restart |
| `grafana` | `grafana/grafana` | Dashboard |

## Quick start

```bash
cp .env.example .env
# Fill in PZ_ADMIN_PASSWORD, PZ_RCON_PASSWORD, GF_SECURITY_ADMIN_PASSWORD.
# Empty passwords do NOT fall back to defaults: the server will refuse to start.

# Generate a self-signed certificate for Grafana (see TLS below)

docker compose up -d
```

First start downloads the server files (several GB) and then every Workshop mod
listed in the config, so it takes a while. Watch progress with `du -sh server/`.

## Configuration

Everything lives in `.env`:

| Variable | Notes |
|---|---|
| `PZ_SERVER_NAME` | Also the config file name: `data/Zomboid/Server/<name>.ini` |
| `PZ_ADMIN_PASSWORD` | Required. Empty means the server prompts on the console and never starts |
| `PZ_RCON_PASSWORD` | Empty disables RCON server-side, and `pz_players_online` stays at 0 |
| `PZ_PORT`, `PZ_UDP_PORT` | Game ports, published to the internet |
| `PZ_JVM_XMS`, `PZ_JVM_XMX` | Java heap. Defaults sized for a 24 GB host, ~150 mods, up to 15 players |
| `PZ_VALIDATE` | Re-verify server files against the Steam manifest on start. Off by default |
| `GF_SERVER_ROOT_URL` | Must match how you reach Grafana, scheme and port included |

The remaining variables are self-explanatory and documented inline in
`.env.example`: `PZ_SERVER_PASSWORD`, `PZ_SERVER_WELCOME_MESSAGE`,
`PZ_MAX_PLAYERS`, `PZ_RCON_PORT`, `PZ_LANGUAGE`, `PZ_PUBLIC`.

**The server config is generated once and then left alone.** On first start
`entrypoint.sh` writes a minimal `.ini` from `.env`; everything else takes the
game's own defaults. After that the file is yours: edit
`data/Zomboid/Server/<name>.ini` directly, or edit it on a local machine and copy
it over. Changing `.env` afterwards will not rewrite it.

### JVM heap sizing

`PZ_JVM_XMX=15g` assumes a 24 GB host. The heap is not the whole story: the JVM
adds roughly 3 GB of non-heap memory on top, the monitoring stack takes about
1 GB, and the OS needs its own room. On a smaller machine, lower both values.

There is no JMX exporter, so heap usage itself is invisible. The only signal that
the heap is too small is `pz_oom_errors_total` on the dashboard. If it ever ticks
above zero, raise `PZ_JVM_XMX`.

## Access

Only the two game ports face the internet, plus Grafana over HTTPS. RCON is bound
to loopback.

```bash
sudo ufw allow 16261/udp comment 'Project Zomboid game'
sudo ufw allow 16262/udp comment 'Project Zomboid direct'
sudo ufw allow 3000/tcp  comment 'Grafana'
sudo ufw reload
```

**ufw does not control Docker-published ports.** Docker writes its own rules into
the PREROUTING and FORWARD chains, while ufw filters INPUT, so a published port
is reachable regardless of what `ufw status` claims. What actually keeps a
service private is the `127.0.0.1:` prefix in the `ports:` section of
`docker-compose.yml`. Remove it and the service is on the internet, ufw or not.

Verify from another machine, not from the server:

```bash
nmap -sU -p 16261,16262 <server-ip>   # expected open
nmap -p 9090,9100,9115,27015 <server-ip>   # expected closed
```

### TLS for Grafana

Grafana serves HTTPS itself with a self-signed certificate. Generate it before
the first start:

```bash
mkdir -p grafana/certs
IP=<server-ip>
openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout grafana/certs/grafana.key \
  -out grafana/certs/grafana.crt \
  -subj "/CN=${IP}" \
  -addext "subjectAltName=IP:${IP},DNS:localhost,IP:127.0.0.1"

sudo chown -R 472:0 grafana/certs
sudo chmod 640 grafana/certs/grafana.key
sudo chmod 644 grafana/certs/grafana.crt
```

The IP must appear in the SAN field or browsers reject the certificate outright.
The `472:0` ownership is not optional: that is the uid the Grafana container runs
as, and it cannot read the key otherwise.

A self-signed certificate encrypts the session, so the admin password no longer
crosses the network in clear text. It does not authenticate the server, and
browsers warn on first visit. Import `grafana.crt` into your machine's trust
store to silence the warning.

`GF_SECURITY_ADMIN_PASSWORD` only applies when Grafana initialises its database
the very first time. Changing it later does nothing; change the password in the
UI, or remove the `grafana-data` volume and start over.

## Layout

```
data/Zomboid/     server data: configs, saves, logs, database
server/           the SteamCMD installation and downloaded Workshop mods
backups/          external backup archives
grafana/certs/    TLS certificate and key
```

Both are bind mounts, so everything is directly inspectable and editable from the
host. Neither directory belongs in git.

## File permissions

The containers run as root, so everything under `data/`, `server/` and
`backups/` is created owned by root and is not writable by your user. That
blocks the normal workflow of editing `data/Zomboid/Server/<name>.ini` directly,
looking through the downloaded mods under `server/steamapps/`, or copying a
backup archive somewhere else.

Grant yourself access once:

```bash
sudo apt install -y acl
make fix-perms
```

The target takes ownership of both trees and then sets two ACLs on each: a
regular one for the files that exist now, and a default one that new files
inherit as they are created.

```bash
sudo chown -R $(id -un):$(id -gn) data/Zomboid server backups
sudo setfacl -R -m  u:$(id -un):rwX data/Zomboid server backups   # existing
sudo setfacl -R -d -m u:$(id -un):rwX data/Zomboid server backups   # future
```

Verify:

```bash
touch data/Zomboid/Server/perm-test && rm data/Zomboid/Server/perm-test
getfacl data/Zomboid/Server | grep default
```

### Why this is not permanent

Inheritance covers most new files but not all of them. A named-user ACL entry is
always capped by the file's mask, and the mask is derived from the mode the
creating program asked for:

| Created with | Resulting mask | Writable by you |
|---|---|---|
| `0666` request — Java, SteamCMD, most C programs | `rw-` | yes |
| `0777` directory request — Java | `rwx` | yes |
| explicit `0600` | `---` | no |
| explicit `0755` directory | `r-x` | no |

The server and SteamCMD fall in the first two rows, so in practice this holds.
When a file turns up that you still cannot edit, run `make fix-perms` again.

One side effect worth knowing: while a default ACL is set on a directory, the
process `umask` is ignored for files created inside it. Permissions come from the
ACL instead.

## Metrics

The exporter serves `/metrics` on port 9115, not published outside the compose
network.

| Metric | Source |
|---|---|
| `pz_players_online` | RCON |
| `pz_rcon_up` | RCON reachable |
| `pz_log_last_activity_timestamp` | Last line seen in the server log |
| `pz_last_autosave_timestamp` | Last world save seen in the log |
| `pz_errors_total`, `pz_player_deaths_total` | Log |
| `pz_server_starts_total`, `pz_connection_attempts_total` | Log |
| `pz_autosave_total`, `pz_oom_errors_total` | Log |
| `pz_backup_last_success_timestamp`, `pz_backup_size_bytes` | Scheduler |
| `pz_backup_duration_seconds`, `pz_backup_failures_total` | Scheduler |
| `pz_restart_last_timestamp` | Scheduler |

The log patterns match PZ b42 console wording and may break on game updates. If a
counter stops moving after a patch, that is the first place to look.

Dashboards are provisioned from `grafana/provisioning/dashboards/`. Saving from
the browser is rejected on purpose: edit the JSON in this repo instead. Grafana
polls that directory every 30 seconds. **Do not lower `updateIntervalSeconds` to
10 or below** — at that point Grafana switches to filesystem watch events, which
do not reliably cross a Docker bind mount, and dashboard changes stop arriving.

## Mods

Mods are listed in `data/Zomboid/Server/<name>.ini` (`Mods=` and
`WorkshopItems=`), and the server downloads them itself on start.

Steam Workshop always serves the newest version of a mod, so pinning is
impossible. When an author publishes an update, players get it immediately while
the server keeps running the old files until it restarts, and joining fails with
a version mismatch. The fix is to restart quickly rather than to pin:

1. A server mod detects the Workshop update, warns players, and quits the server.
2. The process exits, so Docker restarts the container.
3. `entrypoint.sh` runs again and the server picks up the new mod files.

`restart: unless-stopped` **is** the auto-restart script those mods ask for. Do
not replace `start-server.sh` with the wrapper scripts some mod authors ship:
they wrap the server in a `while true` loop, which means the container never
exits, SteamCMD never runs again, and `docker compose stop` risks killing the
server before the world is saved.

Watch the `Restarts` series on the dashboard. A rising restart rate means an
update broke the server and the container is looping.

## Logs

`server-console.txt` is tailed by Alloy and stored in Loki for 30 days, and shows
up in the *Server log* panel at the bottom of the dashboard. Use the panel query
to filter, for example `{job="zomboid"} |~ "ERROR|WARN"`.

Alloy reads that one file and nothing else. Collecting the other containers' logs
would mean handing it the Docker socket, which is full control over the daemon:
too much authority for a component wired to a Grafana instance that is published
on the internet. Use `docker compose logs` for those.

Alloy starts tailing from the end of the file, so history from before it started
is not ingested. It keeps read offsets in the `alloy-data` volume and survives
the log rotation PZ performs on startup.

Promtail, which this replaced, reached end of life in March 2026.

## Backups and the maintenance window

Two independent layers.

**The game's own backups.** PZ zips the world into `data/Zomboid/backups/` on
its own schedule, configured in `<name>.ini`:

```
BackupsPeriod=720          # every 12 hours; 0 disables it
BackupsCount=6             # 3 days at that interval
BackupsOnStart=true        # default
BackupsOnVersionChange=true # default; fires when Steam pulls a new build
```

These cover the world only, they land on the same disk as the world, and
`make wipe-world` deletes them along with it.

**The external backup**, run by the `scheduler` service once a day at
`MAINTENANCE_TIME` in `MAINTENANCE_TZ`:

```
warn players at 10, 5 and 1 minutes
save            -> flush the world
archive         -> backups/pz-backup-<date>.tar.gz
prune           -> delete archives older than BACKUP_KEEP_DAYS
quit            -> process exits, Docker restarts the container
```

The archive holds `Saves/`, `db/` **and `Server/`** — the last one is the
`.ini` with the mod list and `SandboxVars.lua`, which the game's own backups do
not include and which is the most painful part to rebuild by hand.

The daily restart is not only about backups: a 24-hour cycle clears accumulated
memory, and it is also when PZ picks up updated Workshop mods.

Two things worth knowing:

- The archive is taken from a running server. `save` flushes the world first,
  but nothing guarantees a file is not written to during the next 30 seconds.
  PZ's own backups work the same way. Taking it after `quit` is not an option:
  Docker restarts the container immediately.
- A failed backup does not cancel the restart, and a failed `save` does not
  cancel the backup. Each step is independent so one broken piece cannot stall
  the whole window.

Restoring is manual and deliberately not automated:

```bash
docker compose stop zomboid
tar -xzf backups/pz-backup-<date>.tar.gz -C data/Zomboid
docker compose start zomboid
```

The `scheduler` container mounts the world **read-only**. It archives and it
talks RCON; it cannot write into the game data, and it has no access to the
Docker socket.

## Operations

```bash
docker compose logs -f zomboid          # live console
tail -f data/Zomboid/server-console.txt # same thing, from the host

docker compose up -d                    # apply config changes (recreates containers)
docker compose restart zomboid          # plain restart, ignores compose changes
docker compose down                     # stop, 120s grace period for the world save
```

`docker compose restart` does not apply changes to volumes, ports or environment.
Use `up -d` for those.

Back up the config, which is the part that cannot be recreated:

```bash
cp -r data/Zomboid/Server ~/pz-config-backup-$(date +%F)
```

Full reset:

```bash
docker compose down -v
sudo rm -rf data server
```

Drop `-v` to keep the metric history. Remove only `server/` to re-download the
game and mods while keeping the world; remove only `data/` for the opposite.

## Windows notes

Development happens on Windows, deployment on Ubuntu. Two things differ:

- `node-exporter` uses `pid: host` and mounts `/`, which are Linux constructs. On
  Docker Desktop the service either fails or reports the WSL2 VM, not Windows.
  The rest of the stack works.
- `.gitattributes` forces LF on shell scripts. Without it, git can hand
  `entrypoint.sh` CRLF line endings on clone and the container dies with
  `\r: command not found`.

## Known limits

- Network metrics are not collected. `node_network_*` would report the exporter's
  own container interface; real host figures need `network_mode: host`, which
  breaks DNS for Prometheus.
- No JVM heap, GC, FPS or tick metrics. PZ b42 exposes none of these without a
  Lua mod or a JMX agent.
- No alerting. Problems are visible on the dashboard but nothing notifies you.
- Backups stay on the same machine. A disk failure takes the world and every
  archive with it; copying `backups/` off the server is not automated.
