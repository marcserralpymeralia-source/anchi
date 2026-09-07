#!/usr/bin/env python3
"""Inactive network gateway placeholder for Anchi.

This process is intentionally not a database proxy.  It exposes a local
health endpoint and rejects every data request until an authenticated,
tenant-aware forwarding implementation is added.
"""

from __future__ import annotations

import json
import base64
import hmac
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LOGGER = logging.getLogger("anchi-proxy")
SERVICE_NAME = "anchi-proxy"


def _is_enabled() -> bool:
    return os.environ.get("PROXY_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _configured_credentials() -> tuple[str, str]:
    return (
        os.environ.get("PROXY_AUTH_USERNAME", "").strip(),
        os.environ.get("PROXY_AUTH_PASSWORD", ""),
    )


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "anchi-proxy/0.1"

    def _send_json(self, status: HTTPStatus, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            if not self._authorized():
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "service": SERVICE_NAME,
                    "status": "ready" if not _is_enabled() else "blocked",
                    "traffic_enabled": False,
                    "message": "Gateway preparado; el forwarding está desactivado.",
                },
            )
            return
        self._reject_data_request()

    def _authorized(self) -> bool:
        username, password = _configured_credentials()
        if not username or not password:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"service": SERVICE_NAME, "status": "misconfigured", "message": "El gateway no tiene credenciales de salud configuradas."},
            )
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return self._send_unauthorized()
        try:
            decoded = base64.b64decode(header[6:].encode("ascii"), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return self._send_unauthorized()
        expected = f"{username}:{password}"
        if not hmac.compare_digest(decoded, expected):
            return self._send_unauthorized()
        return True

    def _send_unauthorized(self) -> bool:
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"service": SERVICE_NAME, "status": "unauthorized", "message": "Credenciales no válidas."},
            {"WWW-Authenticate": 'Basic realm="Anchi proxy health"'},
        )
        return False

    def do_POST(self) -> None:  # noqa: N802
        self._reject_data_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_data_request()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_data_request()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_data_request()

    def _reject_data_request(self) -> None:
        LOGGER.info("rejected method=%s path=%s", self.command, self.path.split("?", 1)[0])
        self._send_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "service": SERVICE_NAME,
                "status": "disabled",
                "message": "El gateway está preparado, pero no acepta tráfico de datos todavía.",
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        # Never copy a query string into logs: a future adapter may receive
        # credentials or access tokens in a request URL by mistake.
        LOGGER.info("http method=%s path=%s", self.command, self.path.split("?", 1)[0])


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("LISTEN_PORT", "8787"))
    except ValueError as exc:
        raise SystemExit("LISTEN_PORT debe ser un número") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("LISTEN_PORT debe estar entre 1 y 65535")
    if _is_enabled():
        LOGGER.warning("PROXY_ENABLED está activado, pero esta versión sigue bloqueando todo el tráfico")
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    LOGGER.info("listening host=%s port=%s traffic_enabled=false", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
