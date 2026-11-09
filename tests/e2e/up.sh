#!/bin/sh
# Mock deployment for the print checks: the app, the CUPS sidecar and a simulated HP ENVY,
# on a private Docker network. The simulator is never advertised on the LAN, and the app is
# published on 127.0.0.1 only. Open http://127.0.0.1:8100/print once it is up.
# Driver checks (they upload and print to the simulator): docker exec ocds-e2e-app python -I /e2e/driver.py
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PORT=${E2E_PORT:-8100}
RUN="$HERE/run"

docker build -q -t ocds-e2e-cups "$ROOT/cups" >/dev/null
docker build -q -t ocds-e2e-sim "$HERE/simulator" >/dev/null
docker build -q -t ocds-e2e-app "$ROOT" >/dev/null

docker rm -f ocds-e2e-app ocds-e2e-sim ocds-e2e-cups >/dev/null 2>&1 || true
docker volume rm ocds-e2e-sock >/dev/null 2>&1 || true
docker volume create ocds-e2e-sock >/dev/null
docker network rm ocds-e2e-net >/dev/null 2>&1 || true
docker network create ocds-e2e-net >/dev/null

rm -rf "$RUN"
mkdir -p "$RUN/data" "$RUN/storage" "$RUN/spool"
printf '[{"device": "escl:http://127.0.0.1:631", "source": "", "options": {}}]\n' > "$RUN/data/printers.json"
printf '{"active_printer": "escl:http://127.0.0.1:631", "theme": "dark"}\n' > "$RUN/data/settings.json"

docker run -d --name ocds-e2e-cups --network ocds-e2e-net -p "127.0.0.1:$PORT:8000" --init \
  --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL \
  --security-opt no-new-privileges:true --tmpfs /var/spool/cups --tmpfs /var/cache/cups --tmpfs /tmp \
  -v ocds-e2e-sock:/run/cups -e CUPS_SERVER=/run/cups/cups.sock ocds-e2e-cups >/dev/null

docker run -d --name ocds-e2e-sim --network container:ocds-e2e-cups -v "$RUN/spool:/spool" ocds-e2e-sim \
  sh -c 'mkdir -p /run/dbus; dbus-daemon --system --fork; avahi-daemon -D --no-drop-root --no-rlimits; sleep 1; exec ippeveprinter -2 -f image/pwg-raster,image/urf,image/jpeg -k -d /spool -s 10 -p 631 SimENVY' >/dev/null
sleep 4

docker run -d --name ocds-e2e-app --network container:ocds-e2e-cups --init --read-only --tmpfs /tmp:size=512m \
  --cap-drop ALL --security-opt no-new-privileges:true --user 1000:1000 \
  -e CUPS_SERVER=/run/cups/cups.sock -e WEB_HOST=0.0.0.0 -e WEB_PORT=8000 -e SAVE_ROOT=/app/storage \
  -v "$RUN/data:/app/data" -v "$RUN/storage:/app/storage" -v ocds-e2e-sock:/run/cups -v "$HERE:/e2e:ro" \
  ocds-e2e-app >/dev/null
sleep 5

echo "Mock app: http://127.0.0.1:$PORT/print"
