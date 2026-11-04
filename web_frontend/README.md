# Web front end

A browser UI for the scanner, with the features of `main_cli.py`. One FastAPI process imports `printer_backend` and returns HTML pages; [htmx](https://htmx.org) swaps parts of a page without a build step. There is no separate JSON API.

## Running it

```
python3 -m venv .venv
.venv/bin/pip install -r web_frontend/requirements.txt
.venv/bin/python -m web_frontend            # from the repository root
```

Run it **from the repository root**: `./data` (saved printers and settings) and `./scans` (the temporary scan folder) are relative to where it starts. It listens on `0.0.0.0:8000`. `scanimage` (`sane-utils`) and `img2pdf` have to be installed, as for the CLI.

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
| `/scan` | The scan that is running, or the last one: progress, the questions of the scan ("place the next page", "turn the stack around"), cancel, and a retry if saving failed |

A scan runs on the server, so the page can be closed, or opened on another device, and it continues. A strip on the other pages shows that a scan is going on, or needs an answer. It asks the server every 3 seconds, so it disappears when the scan ends and appears when one starts on another device, without a reload. Only one scan runs at a time, because every scan uses and empties `./scans`. While it runs the devices are left alone: looking for printers and adding one are refused, and availability shows "In use".

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
- **Downloading or listing finished scans:** a new router that reads from the save location.
- **A JSON API:** a router that returns the same data the pages use; no change to the rest.

## Tests

```
.venv/bin/python -m unittest discover -s tests
```

`tests/test_web_scan_job.py` runs `ScanJob` on scripted events. `tests/test_web_routes.py` uses FastAPI's `TestClient` with temporary files, and replaces `Printer.scan` and the device commands, so no scanner is needed. They are skipped if FastAPI isn't installed.

## Not done

- Downloading the finished files from the browser, thumbnails of the pages, a history of scans, OCR, and OpenCloud storage.
- The pages were tried in Firefox only, including a touch screen emulation at 360px wide, and through the tests. The scans were run against a stand-in `scanimage`; the real printer is what the backend README's "What has been tested on real hardware" is about.
- Two people changing the same printer at once: the last change wins.
