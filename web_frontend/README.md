# Web front end

A browser UI for the scanner, with the features of `main_cli.py`. One FastAPI process imports `printer_backend` and returns HTML pages; [htmx](https://htmx.org) swaps parts of a page without a build step. There is no separate JSON API.

## Running it

```
python3 -m venv .venv
.venv/bin/pip install -r web_frontend/requirements.txt
.venv/bin/python -m web_frontend            # from the repository root
```

Run it **from the repository root**: `./data` (saved printers and settings) and `./data/scans` (the temporary scan folder) are relative to where it starts. It listens on `0.0.0.0:8000`. `scanimage` (`sane-utils`) and `img2pdf` have to be installed, as for the CLI.

| Environment variable | Default | Meaning |
|---|---|---|
| `WEB_HOST` | `0.0.0.0` | Address to listen on |
| `WEB_PORT` | `8000` | Port |
| `WEB_ALLOWED_HOSTS` | `*` | Comma separated host names the server answers to, e.g. `scanner.lan,192.168.0.20`. Set it so that other sites can't reach the server through your name (DNS rebinding) |

It always runs **one worker**: the saved printers and the running scan live in the memory of the process, so a second process wouldn't know about the scan.

### As a service

`/etc/systemd/system/scanner-web.service`, with the paths and the user changed to yours:

```ini
[Unit]
Description=Document scanner web UI
After=network-online.target
Wants=network-online.target

[Service]
User=scanner
WorkingDirectory=/opt/OCDocumentScanner
ExecStart=/opt/OCDocumentScanner/.venv/bin/python -m web_frontend
Environment=WEB_ALLOWED_HOSTS=scanner.lan,192.168.0.20
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now scanner-web`. The user needs to write to `data/`, `scans/` and the save folder, and be allowed to use the scanner (a USB scanner is usually in the `scanner` group).

### With Docker

On a Linux host with `avahi-daemon` running:

```
echo "DATA_LOCATION=/srv/data/printer_app" > .env     # where it keeps its data, see below
./docker-init.sh                                       # once: creates the folders and gives them to the app's user
docker compose up -d --build
```

`docker-init.sh` creates `data/`, `temp_scans/` and `scans/` in `DATA_LOCATION` and makes `APP_UID:APP_GID` their owner, using `sudo` for what your user isn't allowed to do. Docker would create missing folders itself, but owned by root, and the app doesn't run as root. It reads the same `.env` as Compose, is safe to run again, and fixes the owner of folders that already exist (say, ones Docker created).

Open `http://192.168.0.250/` (port 80). The Caddy proxy (`Caddyfile`) listens on that one address only and forwards to the app, which listens on `127.0.0.1:8000`. To use another address, change it in `Caddyfile`. It starts again after a reboot. `docker compose logs -f` shows the log and `docker compose down` stops it. The image has `scanimage`, `sane-airscan` and `img2pdf`, so the host only needs Docker and Avahi.

Settings go in a `.env` file next to `docker-compose.yaml`. All of them are optional:

| Variable | Default | Meaning |
|---|---|---|
| `WEB_HOST` | `127.0.0.1` | Address the app listens on. Only the proxy on this machine needs to reach it |
| `WEB_PORT` | `8000` | Port the app listens on. If you change it, change `Caddyfile` to match |
| `WEB_ALLOWED_HOSTS` | `*` | As in the table above |
| `DATA_LOCATION` | none, required | The host folder that holds `data/` (saved printers and settings, and the temporary scan folder), `temp_scans/` and `scans/` (the finished scans) |
| `APP_UID`, `APP_GID` | `1000` | The user that runs the app. It has to be able to write to the three folders in `DATA_LOCATION`: `id -u` and `id -g` show yours |

**The save location is always inside `/app/storage`**, which is `$DATA_LOCATION/scans` on the host. The field on the scan page leaves that part out: what you enter is added to it, so `Invoices/2026` saves in `$DATA_LOCATION/scans/Invoices/2026`, and an empty field saves in the folder itself. Anything that leads outside it (an absolute path, `~`, `..`) is refused, because the rest of the container is read-only and files saved there would be lost with it. This is the `SAVE_ROOT` setting in `docker-compose.yaml`; without it (outside Docker) the field holds the whole path, as before.

Both containers use the host's network (`network_mode: host`) because finding scanners relies on multicast, which doesn't reach Docker's own networks. That is also why there is no `ports:` section: the proxy listens on port 80 directly. To make the app reachable from other machines itself, set `WEB_HOST=0.0.0.0` and point the proxy at it, or limit port 80 with a firewall. The image has the same scanner support as a Debian 13 host with `sane-utils`, `sane-airscan` and `hplip`: SANE's standard backends, `airscan` and HP's `hpaio`. It finds network scanners through the host's Avahi daemon, so the folders `/run/avahi-daemon` and `/run/dbus` are mounted read-only. Scanners that need a vendor's own driver or plugin need extra steps, see below. For a USB scanner, see the lines at the end of `docker-compose.yaml`.

#### Scanners that need a vendor driver or plugin

The image only has Debian's packages. What a scanner's maker installs on the host by other means (a plugin, a driver from their website) isn't in the container, so the scanner can show up in the search or the dropdown and then fail when you add it or open its page, e.g. with "Error during device I/O". `airscan` (most network scanners from the last years) needs nothing extra.

**HP (`hpaio:` devices)** needs the proprietary plugin that `hp-plugin` downloads from HP, which Debian doesn't ship. Run `hp-plugin` on the host once, and the compose file mounts what it installed (`/usr/share/hplip/scan/plugins` and `/var/lib/hp/hplip.state`) read-only into the container. If you don't use HP's plugin, delete those two mounts: Compose stops with an error while the host files are missing. Update `hplip` on the host and rebuild the image (`docker compose up -d --build`) together, so that the plugin and the image's `hplip` are the same version.

**Other makers** (Brother, Epson and so on) may need the same kind of step. This is the approach that worked for HP; it has not been tried with any other maker.

1. Make sure it works on the host: `scanimage -d '<device name from scanimage -L>' -A` should print the options.
2. Find what the vendor's installer put on the host: `dpkg -L <package>` for a package, or look at what the installer says. Typically libraries for SANE (`/usr/lib/x86_64-linux-gnu/sane/`), a line in `/etc/sane.d/dll.d/` or `dll.conf`, and the driver's own data and settings (`/opt`, `/usr/share`, `/etc`, `/var/lib`).
3. Mount those in `docker-compose.yaml` under `scanner-web`, read-only and at the same path as on the host. Use the long form with `create_host_path: false`, as the HP mounts do, so that Compose stops with an error instead of creating an empty root-owned folder where a file belongs.
4. Check it in the container: `docker compose exec scanner-web scanimage -d '<device>' -A`.

If that isn't enough, `SANE_DEBUG_<BACKEND>=5` (e.g. `SANE_DEBUG_HPAIO=5`) in front of the `scanimage` command makes the backend say more. A trace of what it opens, with `strace -f -e trace=openat,connect`, shows which file or socket is missing. Install `strace` in a one-off container with `docker run --rm --network host -u 0 --entrypoint sh ocdocumentscanner`, because the app's container is read-only. Drivers that need system libraries which the image doesn't have, or a background service, can't be mounted: if Debian has the package, add it to the `apt-get install` line in the `Dockerfile` and rebuild. If the vendor only ships a `.deb`, installing it in the `Dockerfile` is the other way.

### Who can use it

There is **no login**: it is meant for a trusted network or VPN. Anyone who can reach it can scan, delete saved printers, and point the save location at any folder the service user can write to. So:

- run it as an unprivileged user, as above
- don't expose it to the internet. If you need to, put a reverse proxy with authentication in front of it
- requests from other websites are refused (a page in your browser can't make it scan), and `WEB_ALLOWED_HOSTS` limits the names it answers to

## What it does

| Page | Does |
|---|---|
| `/` | The dropdown of saved printers with their availability, showing the active one (the last one chosen, remembered on the server). A button to scan with it and one to search for a new printer |
| `/printers/discover` | Searches the network when it opens and lists the new printers. Tapping one adds it and makes it the active one |
| `/printers/{id}` | Switch the source, change the options and the save location (each saved as you go), start a scan, and delete the printer |
| `/scan` | The scan that is running, or the last one: progress, the questions of the scan ("place the next page", "turn the stack around"), cancel, a retry if saving failed, and downloading the saved files |

**Downloading** gives one file, or one zip if the scan made several. Where the browser has a "Save as" window (`showSaveFilePicker`: Chromium browsers, and only on HTTPS or `localhost`) it asks where to save. Elsewhere, such as in Firefox and Safari, the browser treats it as any other download, which still saves the file on that device: where depends on the browser's settings, and most can ask each time. "Download and delete from the server" deletes after the "Save as" window's file is written; without that window it can't see whether the file arrived, so it asks first. A download only works from the page of the latest scan, so a page left open can't take or delete the files of a newer one.

A scan runs on the server, so the page can be closed, or opened on another device, and it continues. A strip on the other pages shows that a scan is going on, or needs an answer. It asks the server every 3 seconds, so it disappears when the scan ends and appears when one starts on another device, without a reload. Only one scan runs at a time, because every scan uses and empties `./data/scans`. While it runs the devices are left alone: looking for printers and adding one are refused, and availability shows "In use".

Availability is checked when a page opens and when the dropdown is opened, not continuously, because each check talks to the printer.

The button at the top right of every page switches between light and dark colours. Like the other settings it is saved on the server, so it applies to every browser that uses the scanner; another browser that has a page open picks it up on its next page.

**The save location is saved when the field changes** (on leaving it, or on Enter), not on every keystroke: checking a folder creates it, so saving while typing would leave a folder for every half-typed path. `POST /scan` also sends the folder the page shows, because pressing Start right after typing sends both requests at once and the scan has to use what is on the screen.

## Structure

```
web_frontend/
  app.py            create_app: lifespan, cross-site and host checks, error handlers
  scan_job.py       ScanJob and ScanManager: the scan in the background (no FastAPI imports)
  dependencies.py   the repositories, as FastAPI dependencies
  device_ids.py     device names <-> ids that fit in a URL, and the short name to show for a device
  routes/           printers.py, scanning.py, settings.py
  templates/        Jinja2 pages; names starting with _ are the fragments htmx swaps in
  static/           app.css, app.js, and htmx and the Archivo font in vendor/
  STYLE.md          the style guide: colours, type, components
```

`routes` use `scan_job` and the repositories; `scan_job` only knows `printer_backend`. `create_app(repository, settings)` takes the repositories like `CLI_APP` does, so tests can give it temporary files.

**Style:** [STYLE.md](STYLE.md) explains it. To change the colours, change the values at the top of `static/app.css`, a block for each colour scheme.

**Errors:** the backend reports a refused value as `ValueError` and a failed command or folder as `RuntimeError`. One handler in `app.py` turns both into an inline message (status 422, which `static/app.js` lets htmx show), so a route only raises.

**Device ids:** device names such as `hpaio:/net/ENVY_6400_series?ip=192.168.0.178` can't go in a URL, so routes carry `device_to_id(device)`.

**A scan** is a `ScanJob` made by `POST /scan` after everything that can be refused was checked (name, save folder, options). It runs on a copy of the printer, so changing or deleting the printer during the scan doesn't affect it. The status fragment polls itself only while the job is active, so a locked phone or a reload catches up by itself. Questions have a number, so a late second tap can't answer the next question.

## Adding a feature

- **A page:** a router in `routes/`, included in `create_app`, and templates under `templates/<name>/`. Use the components in [STYLE.md](STYLE.md).
- **A new kind of save location (OpenCloud):** a `ScanStorage` subclass in the backend (see `printer_backend/README.md`), then let the save location field on the printer page (`templates/scan/_form.html`) choose it.
- **A step after the scan (OCR):** in `ScanJob._scan_and_save`, between the scan and `_save`.
- **Listing finished scans:** a new router that reads from the save location.
- **A JSON API:** a router that returns the same data the pages use; no change to the rest.

## Tests

```
.venv/bin/python -m unittest discover -s tests
```

`tests/test_web_scan_job.py` runs `ScanJob` on scripted events. `tests/test_web_routes.py` uses FastAPI's `TestClient` with temporary files, and replaces `Printer.scan` and the device commands, so no scanner is needed. They are skipped if FastAPI isn't installed.

## Not done

- Thumbnails of the pages, a history of scans, OCR, and OpenCloud storage.
- The pages were tried in Firefox only, including a touch screen emulation at 360px wide, and through the tests. The scans were run against a stand-in `scanimage`; the real printer is what the backend README's "What has been tested on real hardware" is about.
- Two people changing the same printer at once: the last change wins.
