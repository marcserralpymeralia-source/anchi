# GEMAVI Agent Architecture

GEMAVI Agent is evolving from an email-first app into a modular, multi-tenant order-agent platform.

## Product principle

The UI must stay simple for `User` and `Admin`, while the backend carries the complexity needed to scale across tenants, channels, formats, outputs, scoring, RAG, and learning.

Rule of the platform:

`entrada recibida -> normalización -> clasificación -> extracción -> matching -> scoring -> revisión/exportación -> aprendizaje`

## Current implementation status

Already in place:

- Multi-tenant data model based on `company_id`.
- Email as the first working channel.
- Shared `inbound_messages` layer.
- Common attachment storage for original content.
- Common pipeline services in `app/agent/platform.py`.
- Configurable scoring and export foundations.
- Branding and tenant-level settings.

In progress:

- Real channel abstraction for WhatsApp, voice, social, web, portal, and API.
- Deeper dashboard optimization using SQL-side aggregation.
- Review, correction, and learning workflows tied to approvals.
- RAG retrieval and learned aliases as controlled support, not source of truth.

## Layering

### Frontend

- Agent workbench for operations.
- Admin settings for operational configuration.
- Superadmin views for technical control.

### Backend

- `app/agent/platform.py` for the shared order pipeline.
- `app/settings/integrations.py` for channel ingestion and connectors.
- `app/dashboard/service.py` for queue and dashboard composition.
- `app/db/models.py` for normalized platform data.

### Data

- `companies`
- `input_channels`
- `channel_settings`
- `inbound_messages`
- `message_attachments`
- `normalized_inputs`
- `customers`
- `customer_aliases`
- `customer_contacts`
- `products`
- `product_aliases`
- `orders`
- `order_lines`
- `order_reviews`
- `manual_corrections`
- `learned_aliases`
- `rag_documents`
- `rag_cases`
- `scoring_settings`
- `scoring_results`
- `export_jobs`
- `alerts`
- `agent_logs`

## Channel contract

Every channel should support:

- fetch new messages
- normalize message
- get attachments
- send response
- mark as processed
- check status
- test connection

## Service contract

The core services are split so that each responsibility is isolated:

- ingestion
- normalization
- extraction
- matching
- scoring
- review
- learning
- export
- alerts

## Scaling rule

Never hardcode a tenant-specific channel, prompt, credential, scoring rule, or export format into the core flow.

The app should behave like a single order agent from the user perspective, even when internally it is a platform of services and modules.

