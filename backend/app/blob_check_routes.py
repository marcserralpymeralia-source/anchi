from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import current_tenant_user
from app.blob_check import check_blob

router = APIRouter()


@router.get("/internal/blob-check")
def blob_check(_: object = Depends(current_tenant_user)):
    return check_blob()
