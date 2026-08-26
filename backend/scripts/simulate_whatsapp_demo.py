"""Run a local-only WhatsApp coexistence simulation.

This script creates synthetic webhook-shaped events and sends them through the
same parser and persistence layer used by Meta webhooks. It never calls Meta
and refuses to run in production or on Vercel.
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from typing import Any

from app.core.config import get_settings
from app.master.database import MasterSessionLocal
from app.tenancy.database import tenant_db_session
from app.whatsapp.service import (
    enqueue_whatsapp_processing,
    parse_payload_events,
    persist_event,
    resolve_company_from_slug,
    whatsapp_config,
)


def build_demo_payload(*, business_account_id: str, phone_number_id: str, run_id: str) -> dict[str, Any]:
    """Build a realistic, deterministic-enough coexistence webhook payload."""
    customer_phone = "+34600000000"
    business_phone = "+34610000000"
    timestamp = str(int(time.time()))

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": business_account_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "phone_number_id": phone_number_id,
                                "display_phone_number": business_phone,
                            },
                            "contacts": [{"profile": {"name": "Cliente Demo"}, "wa_id": customer_phone}],
                            "messages": [
                                {
                                    "id": f"demo-inbound-{run_id}",
                                    "from": customer_phone,
                                    "timestamp": timestamp,
                                    "type": "text",
                                    "text": {"body": "Hola Anchi, necesitamos 12 unidades del producto P-100."},
                                }
                            ],
                            "statuses": [
                                {
                                    "id": f"demo-outbound-{run_id}",
                                    "recipient_id": customer_phone,
                                    "status": "delivered",
                                    "timestamp": timestamp,
                                }
                            ],
                        },
                    },
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "phone_number_id": phone_number_id,
                                "display_phone_number": business_phone,
                            },
                            "contacts": [{"wa_id": customer_phone}],
                            "message_echoes": [
                                {
                                    "id": f"demo-echo-{run_id}",
                                    "from": business_phone,
                                    "to": customer_phone,
                                    "timestamp": timestamp,
                                    "type": "text",
                                    "text": {"body": "Gracias, hemos recibido tu pedido."},
                                }
                            ],
                        },
                    },
                    {
                        "field": "history",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "phone_number_id": phone_number_id,
                                "display_phone_number": business_phone,
                            },
                            "history": [
                                {
                                    "metadata": {"phase": 0, "chunk_order": 1, "progress": 100},
                                    "threads": [
                                        {
                                            "id": customer_phone,
                                            "messages": [
                                                {
                                                    "id": f"demo-history-in-{run_id}",
                                                    "from": customer_phone,
                                                    "timestamp": timestamp,
                                                    "type": "text",
                                                    "text": {"body": "¿Podéis confirmar disponibilidad?"},
                                                    "history_context": {"status": "READ"},
                                                },
                                                {
                                                    "id": f"demo-history-out-{run_id}",
                                                    "from": business_phone,
                                                    "to": customer_phone,
                                                    "timestamp": timestamp,
                                                    "type": "text",
                                                    "text": {"body": "Sí, lo revisamos ahora."},
                                                    "history_context": {"status": "DELIVERED"},
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                    {
                        "field": "smb_app_state_sync",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "state_sync": [
                                {
                                    "type": "contact",
                                    "action": "add",
                                    "contact": {"full_name": "Cliente Demo", "phone_number": customer_phone},
                                    "metadata": {"timestamp": timestamp},
                                }
                            ],
                        },
                    },
                    {
                        "field": "account_update",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "event": "ACCOUNT_RECONNECTED",
                        },
                    },
                ],
            }
        ],
    }


def _ensure_local_runtime() -> None:
    settings = get_settings()
    if settings.environment not in {"development", "demo", "test"}:
        raise RuntimeError("El simulador solo puede ejecutarse con APP_ENV=development, demo o test.")
    if os.getenv("VERCEL") == "1" or os.getenv("VERCEL_ENV"):
        raise RuntimeError("El simulador está bloqueado en Vercel.")


def run_simulation(*, company_slug: str, run_id: str, enqueue: bool) -> dict[str, Any]:
    _ensure_local_runtime()
    master_db = MasterSessionLocal()
    try:
        company, tenant = resolve_company_from_slug(master_db, company_slug)
        if not company or not tenant:
            raise RuntimeError(f"No se encontró el tenant local {company_slug!r}.")

        tenant_db = tenant_db_session(tenant.database_url)()
        try:
            config = whatsapp_config(tenant_db, company.id)
            payload = build_demo_payload(
                business_account_id=config.business_account_id or "demo-waba",
                phone_number_id=config.phone_number_id or "demo-phone",
                run_id=run_id,
            )
            events = parse_payload_events(payload)
            stored_ids: list[int] = []
            queued_ids: list[int] = []
            for event in events:
                message = persist_event(tenant_db, company.id, event)
                if message and message.id is not None:
                    stored_ids.append(message.id)
                    if enqueue and event.get("kind") == "message":
                        job = enqueue_whatsapp_processing(tenant_db, company.id, message.id)
                        if getattr(job, "id", None) is not None:
                            queued_ids.append(job.id)
            tenant_db.commit()
            return {
                "company_slug": company_slug,
                "run_id": run_id,
                "events": Counter(str(event.get("kind") or "unknown") for event in events),
                "stored_ids": stored_ids,
                "queued_job_ids": queued_ids,
            }
        finally:
            tenant_db.close()
    finally:
        master_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Simula eventos realistas de WhatsApp en una demo local de Anchi.")
    parser.add_argument("--company-slug", default="anchi-demo", help="Tenant local que recibirá la simulación.")
    parser.add_argument("--run-id", default=str(int(time.time())), help="Identificador para evitar duplicados entre ejecuciones.")
    parser.add_argument("--enqueue", action="store_true", help="Encola el mensaje entrante para el worker local.")
    args = parser.parse_args()

    result = run_simulation(company_slug=args.company_slug, run_id=args.run_id, enqueue=args.enqueue)
    print(f"Simulación WhatsApp completada para {result['company_slug']} (run_id={result['run_id']}).")
    print(f"Eventos: {dict(result['events'])}")
    print(f"Mensajes persistidos: {len(result['stored_ids'])}")
    print(f"Jobs encolados: {len(result['queued_job_ids'])}")


if __name__ == "__main__":
    main()
