#!/usr/bin/env python3
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.db.database import SessionLocal, init_db  # noqa: E402
from app.demo_seed import reset_demo_company  # noqa: E402


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = reset_demo_company(db)
        print(
            "Reset demo completado: "
            f"company_id={result['company_id']} "
            f"customers={result['customers']} "
            f"products={result['products']} "
            f"orders={result['orders']} "
            f"imports={result['imports']}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
