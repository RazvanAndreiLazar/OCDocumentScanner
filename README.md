# OCDocumentScanner
Web interface for server-side document scanning and saving to Open Cloud

- `printer_backend/`: finds scanners, reads their options and runs scans through `scanimage` and `img2pdf` ([docs](printer_backend/README.md))
- `web_frontend/`: the browser UI ([docs](web_frontend/README.md))
- `main_cli.py`: the same features in a terminal
