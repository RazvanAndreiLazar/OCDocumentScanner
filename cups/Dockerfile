# The print server of the app: CUPS with cups-filters, which turn a file into what the printer takes (see
# printer_backend/printing.py). It runs in its own container, so the app never gets the rights cupsd needs,
# and it talks to the app only through the socket in the cups-socket volume.
FROM debian:trixie-slim

# cups: the scheduler, with CUPS' own filters and mime.convs. Those include rastertopwg, which makes the
# image/urf and image/pwg-raster that driverless printers take: without it the ENVY's queue (it takes URF,
# not PDF) refuses every PDF with 'Unsupported document-format'. cups-filters: pdftopdf and the
# Ghostscript/Poppler rasterisers. cups-client: lp, lpstat, lpadmin, here for the health check.
# cups-browsed (only recommended) is left out on purpose: CUPS makes no queues of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cups cups-client cups-filters ghostscript libnss-mdns avahi-utils \
    && rm -rf /var/lib/apt/lists/*

# so that the printers' .local names resolve through Avahi (the mounted daemon socket)
RUN sed -i 's/^hosts:.*/hosts: files mdns4_minimal [NOTFOUND=return] dns/' /etc/nsswitch.conf

COPY cupsd.conf /etc/cups/cupsd.conf

CMD ["cupsd", "-f"]
