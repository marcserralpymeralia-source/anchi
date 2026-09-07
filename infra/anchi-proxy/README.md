# Anchi proxy gateway (health-only demo)

This folder contains the first, intentionally inactive infrastructure slice
for the future database gateway. It is not a database proxy and it never
forwards requests. The process exposes only an authenticated `GET /health`
endpoint and returns `503` for every data endpoint.

## Remote installation

Copy this folder to `/root/anchi-proxy`, copy `config.env.example` to
`config.env`, replace `PROXY_AUTH_PASSWORD` with a random secret, and install
`anchi-proxy.service` as
`/etc/systemd/system/anchi-proxy.service`. Then run:

```sh
systemctl daemon-reload
systemctl enable --now anchi-proxy.service
. ./config.env
curl --fail --user "$PROXY_AUTH_USERNAME:$PROXY_AUTH_PASSWORD" http://127.0.0.1:8787/health
ss -ltnp | grep ':8787'
```

The recommended external setup is an HTTPS reverse proxy on port `443` with
the server's valid certificate, forwarding only `/health` to
`http://127.0.0.1:8787/health`. Keep port `8787` on loopback and pass the Basic
Auth header through to the gateway. In Anchi, configure the TLS hostname on
port `443`, with certificate verification enabled.

`apache-health-vhost.conf.example` contains a minimal Apache template for
that publication. Keep the actual certificate paths and hostname outside the
repository when installing it.

Do not expose port `8787` directly or use HTTP Basic Auth over the public
internet. The future forwarding implementation must add tenant isolation,
allowlists, timeouts, audit logging without secrets, and a narrowly scoped
destination adapter before activation.
