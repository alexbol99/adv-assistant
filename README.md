# adv-assistant

**WhatsApp Advertisement Assistant Bot** — a conversational bot that lets a store owner/operator create, preview, and publish digital advertisements to in-store TV screens entirely through WhatsApp.

## Documentation

| Document | Description |
|----------|-------------|
| [Product Specification](docs/product-spec.md) | Goals, user roles, use cases, conversation experience, ad visual specs, publishing behaviour, and regional defaults. |
| [Architecture & Technical Specification](docs/architecture-and-technical-spec.md) | System components, data flow diagrams, conceptual data model, intents/commands, CMS integration interface, reliability, and prompt-injection guardrails. |
| [Technology Decisions](docs/technology-decisions.md) | Concrete technology decisions: Python stack, GCP Cloud Run + Cloud Tasks deployment, Cloud SQL PostgreSQL, GCS media storage, Nano Banana ad generation, and product enrichment approach for Israeli grocery. |
| [Workplan](docs/workplan.md) | Step-by-step implementation phases (0–11) for building the application. |

## Quick Summary

- **WhatsApp provider**: Meta Cloud API
- **Operator**: single user (store owner/operator)
- **Output**: 1920 × 1080 px landscape, 10% safe margins
- **CMS publishing**: append-only; delete-all is the only removal operation; no scheduling
- **Region / currency**: Israel / ILS (₪) by default
