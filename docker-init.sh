#!/usr/bin/env bash
# Run once before the first "docker compose up": creates the folders that docker-compose.yaml
# mounts and gives them to the user the app runs as. Docker would otherwise create missing
# folders owned by root, and the app (which doesn't run as root) couldn't write to them.
#
# It reads DATA_LOCATION, APP_UID and APP_GID the way Compose does: from the environment, or else
# from the .env file next to this script (APP_UID and APP_GID default to 1000). Safe to run again.
# It uses sudo for what the current user isn't allowed to do, e.g. creating folders in /srv or
# giving files to another user.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

fail() { echo "docker-init.sh: $*" >&2; exit 1; }
# Runs a command as the current user, and with sudo if that is refused
run() {
    "$@" 2>/dev/null && return 0
    echo "  needs more rights, using sudo: $*"
    sudo "$@"
}
FOLDERS=(data scans)     # data/: printers, settings, temporary scans; scans/: the finished ones


if [ ! -f .env ]; then
    [[ -t 0 ]] || fail "no .env yet: copy .env.example to .env and put your values in it"
    cp .env.example .env
    read -r -p "Created .env from .env.example. Edit it with your values, then press Enter to continue. "
fi

# Like Compose: a value already in the environment wins over the one in .env
environment=$(export -p)
source ./.env
eval "$environment"

APP_UID=${APP_UID:-1000}
APP_GID=${APP_GID:-1000}

[[ -n ${DATA_LOCATION:-} ]] || fail "DATA_LOCATION is not set: put it in .env, e.g. DATA_LOCATION=/srv/data/printer_app"
[[ $DATA_LOCATION == /* ]] || fail "DATA_LOCATION has to be an absolute path, not '$DATA_LOCATION'"
[[ $APP_UID =~ ^[0-9]+$ && $APP_GID =~ ^[0-9]+$ ]] || fail "APP_UID and APP_GID have to be numbers (id -u, id -g), not '$APP_UID' and '$APP_GID'"


echo "Folders in $DATA_LOCATION for the user $APP_UID:$APP_GID"
for folder in "${FOLDERS[@]}"; do
    path=$DATA_LOCATION/$folder
    run mkdir -p "$path"
    # anything inside that belongs to someone else, e.g. made by Docker as root, counts too
    if [[ -n $(find "$path" \( ! -uid "$APP_UID" -o ! -gid "$APP_GID" \) -print -quit 2>/dev/null) ]]; then
        run chown -R "$APP_UID:$APP_GID" "$path"
        echo "  $path: now owned by $APP_UID:$APP_GID"
    else
        echo "  $path: ok"
    fi
done

echo "Done. Start it with: docker compose up -d --build"
