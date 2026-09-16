from __future__ import annotations

import json
import logging
from http.cookies import SimpleCookie
from typing import Any

from starlette.datastructures import MutableHeaders

from app.auth.sessions import SESSION_KEY, get_session
from app.master.database import MasterSessionLocal

logger = logging.getLogger(__name__)


class ServerSideSessionMiddleware:
    """Keep browser session state in the master database.

    The browser receives only the random session identifier.  The signed
    Starlette envelope is intentionally not used in production-like
    environments because it exposed tenant context and made revocation
    dependent on application-side checks.
    """

    def __init__(
        self,
        app,
        *,
        session_cookie: str = "session",
        max_age: int | None = 14 * 24 * 60 * 60,
        path: str = "/",
        domain: str | None = None,
        https_only: bool = False,
        same_site: str = "lax",
    ) -> None:
        self.app = app
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.path = path
        self.domain = domain
        self.https_only = https_only
        self.same_site = same_site

    @staticmethod
    def _cookie_value(scope: dict[str, Any], name: str) -> str | None:
        headers = dict(scope.get("headers") or [])
        raw_cookie = headers.get(b"cookie", b"").decode("latin-1")
        if not raw_cookie:
            return None
        cookies = SimpleCookie()
        cookies.load(raw_cookie)
        morsel = cookies.get(name)
        return morsel.value if morsel is not None else None

    def _load_session(self, raw_token: str | None) -> dict[str, Any]:
        if not raw_token:
            return {}
        db = MasterSessionLocal()
        try:
            row = get_session(db, raw_token)
            if row is None or row.revoked_at is not None:
                return {}
            payload = json.loads(row.session_data_json or "{}")
            if not isinstance(payload, dict):
                return {}
            payload[SESSION_KEY] = raw_token
            return payload
        except Exception:  # noqa: BLE001
            # Authentication dependencies will reject the request if the
            # master database is unavailable. Public pages should still be
            # renderable while the session is treated as anonymous.
            logger.exception("No se pudo cargar la sesión server-side")
            return {}
        finally:
            db.close()

    def _save_session(self, raw_token: str, payload: dict[str, Any]) -> bool:
        db = MasterSessionLocal()
        try:
            row = get_session(db, raw_token)
            if row is None or row.revoked_at is not None:
                return False
            row.session_data_json = json.dumps(
                {key: value for key, value in payload.items() if key != SESSION_KEY},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            db.commit()
            return True
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("No se pudo guardar la sesión server-side")
            return False
        finally:
            db.close()

    def _set_cookie_header(self, raw_token: str) -> str:
        cookie = SimpleCookie()
        cookie[self.session_cookie] = raw_token
        morsel = cookie[self.session_cookie]
        morsel["path"] = self.path
        morsel["httponly"] = True
        morsel["samesite"] = self.same_site
        if self.max_age is not None:
            morsel["max-age"] = str(self.max_age)
        if self.domain:
            morsel["domain"] = self.domain
        if self.https_only:
            morsel["secure"] = True
        return morsel.OutputString()

    def _delete_cookie_header(self) -> str:
        cookie = SimpleCookie()
        cookie[self.session_cookie] = ""
        morsel = cookie[self.session_cookie]
        morsel["path"] = self.path
        morsel["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
        morsel["max-age"] = "0"
        morsel["httponly"] = True
        morsel["samesite"] = self.same_site
        if self.domain:
            morsel["domain"] = self.domain
        if self.https_only:
            morsel["secure"] = True
        return morsel.OutputString()

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_token = self._cookie_value(scope, self.session_cookie)
        scope["session"] = self._load_session(raw_token)
        scope["server_session_cookie"] = raw_token

        async def send_wrapper(message):  # noqa: ANN001
            if message.get("type") == "http.response.start":
                payload = scope.get("session") or {}
                current_token = payload.get(SESSION_KEY)
                headers = MutableHeaders(scope=message)
                if isinstance(current_token, str) and current_token:
                    if self._save_session(current_token, payload):
                        headers.append("set-cookie", self._set_cookie_header(current_token))
                    elif raw_token:
                        headers.append("set-cookie", self._delete_cookie_header())
                elif raw_token:
                    headers.append("set-cookie", self._delete_cookie_header())
            await send(message)

        await self.app(scope, receive, send_wrapper)
