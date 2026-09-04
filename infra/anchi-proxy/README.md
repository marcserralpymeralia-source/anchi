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
curl --fail --user "$PROXY_AUTH_USERNAME:$PROXY_AUTH_PASSWORD" http://127.0.0.1:8787/health
ss -ltnp | grep ':8787'
```

For the temporary closed-demo setup, the listener may use `0.0.0.0:8787` so
Anchi can check it remotely. This is HTTP Basic Auth without TLS: use only a
temporary credential and only for the health check. Do not use it for real
credentials or production traffic. The future forwarding implementation must
add TLS/mTLS, tenant isolation, allowlists, timeouts, audit logging without
secrets, and a narrowly scoped destination adapter before activation.
