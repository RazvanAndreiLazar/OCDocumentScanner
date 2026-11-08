# OCDocumentScanner
Web interface for server-side document scanning and saving to Open Cloud

- `printer_backend/`: finds scanners, reads their options and runs scans through `scanimage` and `img2pdf` ([docs](printer_backend/README.md))
- `web_frontend/`: the browser UI ([docs](web_frontend/README.md))
- `main_cli.py`: the same features in a terminal

To run the web UI with Docker: set `DATA_LOCATION`, `SCANNER_DOMAIN` and `DUCKDNS_TOKEN` in a `.env` file, then `./docker-init.sh && docker compose up -d --build` and open https://scanner-lazar.duckdns.org/ ([details](web_frontend/README.md#with-docker)).
