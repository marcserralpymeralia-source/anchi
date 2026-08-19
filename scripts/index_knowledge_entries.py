#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

os.environ.setdefault("APP_ENV", "development")

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.knowledge.service import index_knowledge_entries  # noqa: E402
from app.master.database import MasterSessionLocal  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.tenancy.database import get_tenant_engine, tenant_db_session  # noqa: E402
from app.tenancy.migrations import upgrade_tenant_schema  # noqa: E402


def _resolve_tenant(master_db, *, company_id: int | None, company_slug: str | None):  # noqa: ANN001
    query = master_db.query(MasterTenantDatabase).join(MasterCompany)
    if company_id is not None:
        query = query.filter(MasterTenantDatabase.company_id == company_id)
    elif company_slug:
        query = query.filter(MasterCompany.slug == company_slug)
    else:
        raise ValueError("Indica --company-id o --company-slug.")
    tenant = query.order_by(MasterCompany.name).first()
    if not tenant or not tenant.database_url:
        raise ValueError("No se encontro una base tenant activa para esa compania.")
    return tenant


def main() -> int:
    parser = argparse.ArgumentParser(description="Indexa embeddings de conocimiento empresarial para un tenant.")
    parser.add_argument("--company-id", type=int, default=None)
    parser.add_argument("--company-slug", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    master_db = MasterSessionLocal()
    try:
        tenant = _resolve_tenant(master_db, company_id=args.company_id, company_slug=args.company_slug)
    finally:
        master_db.close()

    upgrade_tenant_schema(get_tenant_engine(tenant.database_url), company_id=tenant.company_id)
    Session = tenant_db_session(tenant.database_url)
    db = Session()
    try:
        stats = index_knowledge_entries(db, company_id=tenant.company_id, model=args.model, batch_size=args.batch_size)
        print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

