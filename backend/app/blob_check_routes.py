from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth.dependencies import current_tenant_user

router = APIRouter()


@router.get("/internal/blob-check")
def blob_check(_: object = Depends(current_tenant_user)):
    client = None
    path = "anchi-tests/runtime-check.txt"

    try:
        from vercel.blob import BlobClient

        client = BlobClient()

        result = client.put(
            path,
            b"ok",
            access="private",
            content_type="text/plain",
            overwrite=True,
        )

        return {
            "ok": True,
            "storage": "vercel_blob",
            "private": True,
            "result_type": type(result).__name__,
        }

    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )

    finally:
        if client is not None:
            try:
                client.delete(path)
            except Exception:
                pass

            try:
                client.close()
            except Exception:
                pass
