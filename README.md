# OCDocumentScanner
Web interface for server-side document scanning and saving to Open Cloud

- `printer_backend/`: finds scanners, reads their options and runs scans through `scanimage` and `img2pdf`, and sends files to printers through CUPS ([docs](printer_backend/README.md))
- `web_frontend/`: the browser UI, for scanning and for printing uploaded files ([docs](web_frontend/README.md))
- `cups/`: the print server, a CUPS container that the app talks to through a socket. It listens on nothing else
- `main_cli.py`: the same features in a terminal

**Printing** (`/print`): choose a file on your computer and it is printed on the active printer. PDF, images, Word, Excel, PowerPoint, OpenDocument, text and RTF files are accepted, up to 50 MiB. The file must match its extension. Office and text files are converted to PDF with LibreOffice; the copies, sides (one or two, long or short edge) and colour (colour or black and white) are the ones the printer offers. The file is kept only until the printer has it.

To run the web UI with Docker: set `DATA_LOCATION`, `SCANNER_DOMAIN` and `DUCKDNS_TOKEN` in a `.env` file, then `./docker-init.sh && docker compose up -d --build` and open https://scanner-lazar.duckdns.org/ ([details](web_frontend/README.md#with-docker)).
