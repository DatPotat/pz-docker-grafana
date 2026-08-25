#!/bin/bash

set -euo pipefail

PZ_SERVER_DIR="${PZ_SERVER_DIR:-/opt/pzserver}"
PZ_DATA_DIR="${PZ_DATA_DIR:-/root/Zomboid}"
STEAM_APP_ID="${STEAM_APP_ID:-380870}"

SERVER_NAME="${PZ_SERVER_NAME:-servertest}"
INI_FILE="${PZ_DATA_DIR}/Server/${SERVER_NAME}.ini"
START_SCRIPT="${PZ_SERVER_DIR}/start-server.sh"
JVM_CONFIG="${PZ_SERVER_DIR}/ProjectZomboid64.json"

PZ_JVM_XMS="${PZ_JVM_XMS:-10g}"
PZ_JVM_XMX="${PZ_JVM_XMX:-15g}"
PZ_VALIDATE="${PZ_VALIDATE:-false}"

echo "=========================================="
echo " Project Zomboid Dedicated Server"
echo " Server name: ${SERVER_NAME}"
echo " Max players: ${PZ_MAX_PLAYERS:-32}"
echo " JVM heap:    -Xms${PZ_JVM_XMS} -Xmx${PZ_JVM_XMX}"
echo " Validate:    ${PZ_VALIDATE}"
echo "=========================================="

# --- 1. Install or update the server via SteamCMD -------------------------
MANIFEST="${PZ_SERVER_DIR}/steamapps/appmanifest_${STEAM_APP_ID}.acf"
if [ -f "${MANIFEST}" ] && ! grep -q '"UpdateResult"[[:space:]]*"0"' "${MANIFEST}"; then
    echo "Stale SteamCMD manifest, removing it before the update"
    rm -f "${MANIFEST}"
fi

echo "[1/3] Updating server files (app ${STEAM_APP_ID})..."
if [ "${PZ_VALIDATE}" = "true" ]; then
    steamcmd +force_install_dir "${PZ_SERVER_DIR}" \
        +login anonymous \
        +app_update "${STEAM_APP_ID}" validate \
        +quit
else
    steamcmd +force_install_dir "${PZ_SERVER_DIR}" \
        +login anonymous \
        +app_update "${STEAM_APP_ID}" \
        +quit
fi

# --- 2. First-run config --------------------------------------------------
mkdir -p "${PZ_DATA_DIR}/Server" "${PZ_DATA_DIR}/Saves/Multiplayer"

if [ -f "${INI_FILE}" ]; then
    echo "[2/3] Config exists, leaving it alone: ${INI_FILE}"
else
    echo "[2/3] First run, generating config: ${INI_FILE}"
    cat > "${INI_FILE}" <<EOF
PublicName=${PZ_SERVER_NAME:-servertest}
Public=${PZ_PUBLIC:-false}
ServerWelcomeMessage=${PZ_SERVER_WELCOME_MESSAGE:-Welcome to Project Zomboid!}
DefaultPort=${PZ_PORT:-16261}
UDPPort=${PZ_UDP_PORT:-16262}
MaxPlayers=${PZ_MAX_PLAYERS:-32}
Password=${PZ_SERVER_PASSWORD:-}
RCONPort=${PZ_RCON_PORT:-27015}
RCONPassword=${PZ_RCON_PASSWORD:-}
# Broadcasts a chat message on player death. Required by pz_player_deaths_total.
AnnounceDeath=true
# Periodic world flush in real minutes. 0 (the game default) disables it and
# risks losing progress on a crash; it also makes pz_autosave_total dead.
SaveWorldEveryMinutes=10
# The game's own backups. Period is in minutes, 0 disables them. Count applies
# per backup type, and one archive of a grown world runs close to a gigabyte.
BackupsPeriod=180
BackupsCount=4
EOF
fi

# --- 3. JVM heap ----------------------------------------------------------
if [ ! -f "${START_SCRIPT}" ]; then
    echo "ERROR: ${START_SCRIPT} not found. SteamCMD download failed?" >&2
    exit 1
fi
if [ ! -f "${JVM_CONFIG}" ]; then
    echo "ERROR: ${JVM_CONFIG} not found. SteamCMD download failed?" >&2
    exit 1
fi

sed -i -E "s/-Xms[0-9]+[kKmMgG]/-Xms${PZ_JVM_XMS}/g; s/-Xmx[0-9]+[kKmMgG]/-Xmx${PZ_JVM_XMX}/g" "${JVM_CONFIG}"

if ! grep -q -- "-Xms" "${JVM_CONFIG}"; then
    sed -i -E "s/\"-Xmx${PZ_JVM_XMX}\"/\"-Xms${PZ_JVM_XMS}\", \"-Xmx${PZ_JVM_XMX}\"/" "${JVM_CONFIG}"
fi

# "-XX:ZUncommitDelay" - будет работать если отличаются -Xms и -Xmx
for flag in "-XX:+AlwaysPreTouch" "-XX:ZUncommitDelay=300"; do
    grep -q -- "${flag}" "${JVM_CONFIG}" || \
        sed -i -E "s/\"-Xmx${PZ_JVM_XMX}\"/\"-Xmx${PZ_JVM_XMX}\", \"${flag}\"/" "${JVM_CONFIG}"
done

xms_count=$(grep -c -- "-Xms${PZ_JVM_XMS}" "${JVM_CONFIG}" || true)
xmx_count=$(grep -c -- "-Xmx${PZ_JVM_XMX}" "${JVM_CONFIG}" || true)
if [ "${xms_count}" -lt 1 ] || [ "${xmx_count}" -lt 1 ]; then
    echo "ERROR: could not set the JVM heap in ${JVM_CONFIG}." >&2
    echo "       The upstream file format changed. Current vmArgs:" >&2
    grep -n -- "-X" "${JVM_CONFIG}" >&2 || echo "       (no JVM arguments found at all)" >&2
    exit 1
fi
echo "[3/3] JVM heap set: -Xms${PZ_JVM_XMS} -Xmx${PZ_JVM_XMX}"

export JAVA_TOOL_OPTIONS="-Duser.language=${PZ_LANGUAGE:-EN} -Djava.awt.headless=true"

echo "Starting server..."
echo "=========================================="

cd "${PZ_SERVER_DIR}"
exec bash "${START_SCRIPT}" \
    -servername "${SERVER_NAME}" \
    -adminpassword "${PZ_ADMIN_PASSWORD}"