from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, Protocol

from app.db.models import EmailSettings
from app.settings.integrations import backfill_imap_emails, read_latest_imap_emails, test_imap_connection
from sqlalchemy.orm import Session


@dataclass(slots=True)
class MailProviderResult:
    ok: bool
    message: str = ""
    data: dict[str, Any] | None = None


class MailProvider(Protocol):
    key: str

    def authorize(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def refresh(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def list_folders(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def initial_sync(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def sync_changes(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def get_message(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def get_attachment(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def mark_as_read(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def archive_message(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def test_connection(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def create_subscription(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def renew_subscription(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...

    def delete_subscription(self, *args: Any, **kwargs: Any) -> MailProviderResult: ...


class BaseMailProvider(ABC):
    key: str = ""

    def _unsupported(self, message: str = "Proveedor no implementado todavía.") -> MailProviderResult:
        return MailProviderResult(ok=False, message=message)

    def authorize(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def refresh(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def list_folders(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def initial_sync(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def sync_changes(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def get_message(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def get_attachment(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def mark_as_read(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def archive_message(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def create_subscription(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def renew_subscription(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()

    def delete_subscription(self, *args: Any, **kwargs: Any) -> MailProviderResult:
        return self._unsupported()


class GmailProvider(BaseMailProvider):
    key = "gmail"


class MicrosoftGraphProvider(BaseMailProvider):
    key = "microsoft365"


class ImapProvider(BaseMailProvider):
    key = "imap"

    def test_connection(self, settings: EmailSettings, db: Session | None = None) -> MailProviderResult:
        result = test_imap_connection(settings)
        return MailProviderResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), data=result)

    def initial_sync(self, db: Session, settings: EmailSettings, *, auto_process: bool = False, unread_only: bool = False, limit: int | None = None) -> MailProviderResult:
        result = read_latest_imap_emails(db, settings, settings.company_id, auto_process=auto_process, unread_only=unread_only, limit=limit)
        return MailProviderResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), data=result)

    def sync_changes(self, db: Session, settings: EmailSettings, *, from_date: str | None = None, to_date: str | None = None, limit: int = 100) -> MailProviderResult:
        result = backfill_imap_emails(db, settings, settings.company_id, from_date=from_date, to_date=to_date, limit=limit)
        return MailProviderResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), data=result)


PROVIDER_REGISTRY: dict[str, BaseMailProvider] = {
    "imap": ImapProvider(),
    "gmail": GmailProvider(),
    "microsoft365": MicrosoftGraphProvider(),
}


def get_mail_provider(provider_key: str | None) -> BaseMailProvider:
    return PROVIDER_REGISTRY.get((provider_key or "").strip().lower(), PROVIDER_REGISTRY["imap"])
