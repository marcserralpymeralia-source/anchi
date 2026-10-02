"""One-shot Render Cron client for ANCHI's protected jobs endpoint."""

from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def invoke(*, endpoint_url: str | None = None, secret: str | None = None, allowed_host: str | None = None, timeout_seconds: float | None = None, opener=None) -> int:
    endpoint = (endpoint_url if endpoint_url is not None else os.getenv("PILOT_CRON_ENDPOINT_URL", "")).strip()
    token = (secret if secret is not None else os.getenv("CRON_SECRET", "")).strip()
    if not endpoint or not token:
        print("cron invocation configuration missing", file=sys.stderr)
        return 2
    if len(token) > 8192 or "\r" in token or "\n" in token:
        print("cron invocation credential is invalid", file=sys.stderr)
        return 2
    try:
        parsed = urlsplit(endpoint)
        timeout = float(timeout_seconds if timeout_seconds is not None else os.getenv("PILOT_CRON_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        print("cron invocation configuration invalid", file=sys.stderr)
        return 2
    expected_host = (allowed_host if allowed_host is not None else os.getenv("PILOT_CRON_ALLOWED_HOST", "")).strip().lower().rstrip(".")
    endpoint_host = (parsed.hostname or "").strip().lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not endpoint_host
        or not endpoint_host.endswith(".onrender.com")
        or not expected_host
        or endpoint_host != expected_host
        or parsed.port not in {None, 443}
        or parsed.path.rstrip("/") != "/cron/jobs"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        print("cron endpoint must be an HTTPS /cron/jobs URL", file=sys.stderr)
        return 2
    if not 1 <= timeout <= 120:
        print("cron timeout must be between 1 and 120 seconds", file=sys.stderr)
        return 2
    request = Request(endpoint, method="POST", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    open_request = opener or build_opener(_NoRedirect()).open
    try:
        with open_request(request, timeout=timeout) as response:
            status = int(response.status)
            if not 200 <= status < 300:
                print(f"cron endpoint returned HTTP {status}", file=sys.stderr)
                return 1
        print(f"cron invocation completed HTTP {status}")
        return 0
    except HTTPError as exc:
        print(f"cron endpoint returned HTTP {exc.code}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError):
        print("cron endpoint request failed", file=sys.stderr)
        return 1


def main() -> int:
    return invoke()


if __name__ == "__main__":
    raise SystemExit(main())
