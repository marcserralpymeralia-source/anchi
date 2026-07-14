from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/mail", tags=["mail"])


@router.get("")
def mail_inbox(
    request: Request,
):
    query = request.url.query
    suffix = f"?{query}" if query else ""
    return RedirectResponse(f"/channels{suffix}", status_code=307)
