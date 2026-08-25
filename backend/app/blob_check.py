from __future__ import annotations

from vercel.blob import BlobClient


def check_blob() -> dict:
    client = BlobClient()
    path = "anchi-tests/runtime-check.txt"

    try:
        result = client.put(
            path,
            b"ok",
            access="private",
            content_type="text/plain",
            overwrite=True,
        )

        return {
            "ok": True,
            "result": str(result),
        }
    finally:
        try:
            client.delete(path)
        except Exception:
            pass
        client.close()
