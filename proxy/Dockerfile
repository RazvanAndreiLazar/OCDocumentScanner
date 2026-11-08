# Caddy with the DuckDNS module, which gets the Let's Encrypt certificate through DuckDNS (see proxy/Caddyfile).
# The builder and the image have the same version, so the module is built for the Caddy that runs it.
FROM caddy:2.11.7-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/duckdns@v0.5.0

FROM caddy:2.11.7
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
