# Technology Decisions — WhatsApp Advertisement Assistant Bot

This document records the concrete technology decisions and implementation approach for the WhatsApp Advertisement Assistant Bot, complementing the [Product Specification](product-spec.md) and [Architecture & Technical Specification](architecture-and-technical-spec.md).

---

## 1. Technology Stack

| Layer | Choice |
|-------|--------|
| **Language** | Python (3.12+) |
| **Web framework** | FastAPI (async-native, fits Cloud Run) |
| **LLM client** | OpenAI Python SDK (GPT-4o or equivalent) |
| **WhatsApp provider** | Meta Cloud API (webhook receiver + Send API) |
| **Ad generation** | Nano Banana (see §5) |
| **Database** | PostgreSQL via Cloud SQL (see §3) |
| **ORM / migrations** | SQLAlchemy 2 (async) + Alembic |
| **Task queue** | Google Cloud Tasks (see §2) |
| **Media storage** | Google Cloud Storage (see §4) |
| **Secrets** | Google Secret Manager |
| **Dependency management** | `uv` / `pyproject.toml` |
| **Testing** | pytest + pytest-asyncio |
| **Linting / formatting** | ruff |

---

## 2. Deployment — GCP Cloud Run + Cloud Tasks

### 2.1 Cloud Run

The bot application server is deployed as a **stateless, containerised service on Google Cloud Run**.

- **Async processing**: the Webhook Handler returns HTTP 200 to Meta Cloud API immediately upon receipt, then enqueues a task to Cloud Tasks for actual message processing. This prevents Meta from retrying due to slow responses and keeps the webhook endpoint fast.
- The Cloud Run service processes both inbound webhook deliveries (from Meta) and task handler invocations (from Cloud Tasks) within the same container.
- Minimum instances: 1 (avoid cold-start delays for a real-time chat bot); scale-to-zero acceptable for non-production environments.
- Region: `me-west1` (Tel Aviv) as primary; fallback to `europe-west1` (Belgium) if unavailable.

### 2.2 Cloud Tasks

- Each incoming WhatsApp message is serialised and enqueued as a Cloud Tasks HTTP task targeting the bot's internal `/tasks/process-message` endpoint.
- This decouples webhook acknowledgement from message processing and provides built-in retry semantics with configurable back-off.
- Ad generation jobs (which may be long-running) are also enqueued as separate tasks so they do not block conversation response latency.
- Queue configuration: max retries = 5; min back-off = 5 s; max back-off = 300 s.

---

## 3. Database — PostgreSQL (Cloud SQL)

A **Cloud SQL PostgreSQL** instance is provisioned from day one, even if some entities start as simple key-value stores.

**Rationale:**
- Conversation sessions and operator preferences need durable persistence across Cloud Run instance restarts.
- Ad drafts, published ad records, and audit events require relational integrity.
- Starting with Postgres avoids a later painful migration from an in-memory or Redis-only approach.

**Key tables** (map to conceptual data model in arch spec §5):

| Table | Notes |
|-------|-------|
| `operator` | Registered WhatsApp phone numbers; language/currency preferences |
| `conversation_session` | Per-operator session state; serialised as JSONB |
| `ad_draft` | Advertisement in progress or completed |
| `published_ad` | Immutable record of CMS publish events |
| `system_config` | Singleton configuration row (CMS URL, defaults) |
| `audit_event` | Append-only log of admin and bot actions |

**Connection:** Cloud Run connects to Cloud SQL via the **Cloud SQL Auth Proxy** (Unix socket, no public IP required). Async SQLAlchemy with `asyncpg` driver.

---

## 4. Media Storage — Google Cloud Storage

### 4.1 URL Strategy: Public Objects with Unguessable Names

Rendered ad images and operator-uploaded product photos are stored in a **GCS bucket** with the following approach:

- Each object is given a **cryptographically random, unguessable name** (UUID v4 prefix + original filename/extension), e.g.:  
  `ads/7f3a1c2d-4b5e-4f6a-9d0b-123456789abc.png`
- Objects are made **publicly readable** (uniform bucket-level ACL: `allUsers` `objectViewer`).
- The resulting public URL (`https://storage.googleapis.com/<bucket>/<object>`) is used directly in:
  - WhatsApp image messages (preview delivery).
  - CMS publish payloads (`image_url` field).

**Why not signed URLs?**  
Signed URLs have a finite expiry. The CMS may cache or re-fetch the URL at any time; an expired signed URL would break the CMS display. Public URLs with unguessable names avoid this issue.

> **Note — CMS URL behaviour:** Whether the CMS stores the URL for later re-fetch or embeds the image at publish time is currently undetermined (TBD). The public-object approach handles both cases; this assumption should be validated once the CMS integration is specified.

### 4.2 Lifecycle TTL Cleanup

- A **GCS Object Lifecycle rule** is configured on the bucket to automatically delete objects older than **N days** (default: 90 days; configurable).
- This prevents unbounded storage growth and ensures images for expired campaigns are removed without manual intervention.
- The lifecycle rule is applied at the bucket level and requires no application-level cleanup code.

### 4.3 Bucket Configuration

| Setting | Value |
|---------|-------|
| Location | `me-west1` (co-located with Cloud Run) |
| Storage class | Standard |
| Public access | Uniform: `allUsers objectViewer` |
| Object naming | `{category}/{uuid4}.{ext}` |
| Lifecycle rule | Delete after 90 days |
| CORS | Allow `GET` from `*` (read-only; uploads are server-side only) |

---

## 5. Ad Generation — Nano Banana

### 5.1 Integration Model

**Nano Banana** is the chosen ad image generation service. Its API uses an **asynchronous job model**:

1. The bot POSTs a generation request and receives a **job ID** immediately.
2. The bot polls the job status endpoint using the job ID until the job is `completed` or `failed`.
3. On completion, the rendered image URL (or binary) is retrieved and uploaded to GCS.

**Callback support:** Nano Banana also supports a **webhook callback** (the service POSTs to a bot-provided URL when the job completes). The implementation should support both polling and callback modes; the callback mode is preferred in production to reduce polling overhead and latency.

### 5.2 Generation Modes

Both generation modes (defined in the arch spec) map to Nano Banana API parameters:

| Mode | Nano Banana approach |
|------|---------------------|
| Fresh generation | Submit structured ad data (product name, price, promo text, optional photo URL, enriched details) as generation inputs; no reference image. |
| Reference-based regeneration | Submit the same inputs **plus** the previous preview image URL as a visual reference; include the operator's change instructions in the prompt. |

### 5.3 Open Questions about Nano Banana API

The following questions are unresolved pending API documentation review and/or vendor confirmation. Decisions should be recorded here once answered.

1. **Authentication**: What authentication scheme does the Nano Banana API use? (API key in header? OAuth? Other?)
2. **Request schema**: What is the exact request body structure for a generation job? What fields are mandatory vs optional?
3. **Reference image input**: How is the reference image supplied for regeneration — as a URL, a base64-encoded blob, or a multipart upload?
4. **Polling endpoint**: What is the polling endpoint path and response schema for job status? What are the possible status values?
5. **Callback registration**: How is the callback URL registered — per-job in the request body, or globally per API key?
6. **Rate limits and quotas**: What are the rate limits (requests per minute/hour)? Are there concurrency limits on active jobs?
7. **Error handling**: What HTTP status codes and error payloads does the API return for invalid inputs, quota exhaustion, and generation failures? Is retry safe (idempotent job submission)?

---

## 6. Product Domain and Enrichment

### 6.1 Domain Focus

The primary product domain is **food and grocery**, targeting Israeli retail (supermarkets, convenience stores, small grocers). Enrichment logic and data sources are optimised for this domain:

- Product names and promotional text are predominantly in **Hebrew**.
- Prices are in **ILS (₪)** by default.
- Product identifiers are standard **EAN-13** barcodes used in the Israeli grocery market.

### 6.2 Barcode Decoding from Product Photos

When the operator sends a product photo, the bot attempts to extract an EAN barcode from the image using the following two-stage approach:

| Stage | Method | Notes |
|-------|--------|-------|
| **Primary** | Deterministic barcode decoder (e.g., **ZXing** via `zxing-cpp` Python bindings, or **zbar** via `pyzbar`) | Fast, free, no API call required; handles standard 1D (EAN-13, EAN-8, UPC-A) and 2D codes. |
| **Fallback** | Vision-LLM (e.g., GPT-4o with vision) | Used only if the deterministic decoder fails to detect a barcode. The LLM is prompted to identify and return the barcode digits from the image. This incurs additional latency and cost; use sparingly. |

The decoded EAN (if found) is stored in the `AdDraft.ean` field and passed to the product lookup stage.

### 6.3 EAN Product Lookup

Once an EAN barcode is available (either supplied directly by the operator or decoded from a photo), the bot enriches product data using the following provider chain:

| Priority | Provider | Notes |
|----------|----------|-------|
| **1 — Primary** | **Open Food Facts** (`https://world.openfoodfacts.org/api/v2/product/{ean}`) | Free, open database; strong coverage of packaged food/grocery globally including Israeli products. Returns product name, brand, ingredients, nutritional info, packaging, and images. |
| **2 — Fallback** | **EAN-Search.org** (`https://www.ean-search.org/perl/ean-search.pl`) | Commercial EAN database with broad coverage; free tier available. Used when Open Food Facts returns no result. Alternatively, **Barcodelookup.com** or **UPCitemdb.com** may be substituted. |
| **3 — Web search enrichment** | **Search API** (e.g., Google Custom Search API, Serper.dev, or Brave Search API) targeting Hebrew-language retailer pages | Used in addition to (or when) structured databases return sparse data. Queries include the product name and/or EAN; results from Israeli retailer sites (Shufersal, Rami Levy, Victory, etc.) are prioritised. Extracted text is passed to the LLM for structured field extraction. |

**Fallback behaviour:** If no provider returns usable data, the bot proceeds with only the operator-provided information and notifies the operator that enrichment was unavailable.

### 6.4 Enrichment Output

Regardless of the source, enrichment produces a structured object merged into `AdDraft`:

```python
class EnrichedProduct:
    product_name: str | None        # Confirmed or corrected name
    brand: str | None
    category: str | None
    description: str | None         # Short marketing description
    image_url: str | None           # Reference product image (if available)
    source: str                     # "open_food_facts" | "ean_search" | "web" | "none"
```

All enriched data is treated as **supplementary** — the operator's explicitly provided values always take precedence.

---

## 7. Summary of Key Decisions

| Decision | Choice |
|----------|--------|
| Language | Python 3.12+ |
| Deployment | GCP Cloud Run (containerised, stateless) |
| Async message processing | Google Cloud Tasks |
| Database | Cloud SQL PostgreSQL (from day one) |
| Media storage | GCS — public objects, unguessable names, lifecycle TTL |
| CMS image URL strategy | Public GCS object URLs (signed URLs avoided; CMS behaviour TBD) |
| Ad generation service | Nano Banana (async job + polling; callback supported) |
| Product domain | Food/grocery, Israel-focused |
| Barcode decoding | ZXing/zbar (primary) → vision-LLM (fallback) |
| EAN lookup | Open Food Facts (primary) → EAN-Search.org (fallback) → web search (enrichment) |
| Web search for enrichment | Search API targeting Hebrew retailer pages |
