# Product Specification — WhatsApp Advertisement Assistant Bot

## 1. Overview

The **WhatsApp Advertisement Assistant Bot** is a conversational tool that allows multiple authorised operators (e.g., owner and staff) to create, manage, and publish digital advertisements for in-store TV screens — entirely through WhatsApp chat. All operators share the same permissions. No dedicated web dashboard or desktop application is required for **day-to-day ad creation and publishing**. An **Admin Console** exists for system configuration and operational oversight (operator allowlist management, CMS connection settings, audit log); it is not needed for routine ad operations.

**Core product behaviour:**

1. The bot holds a **free-form, chat-style dialog** with the operator to define the product to advertise. There is no rigid form or fixed question order.
2. To better identify the product, the bot may ask for a **product photo** and/or an **EAN barcode** (product code).
3. Once it has enough information — combining what the operator provided and what it finds on the web — the bot **suggests generating an advertisement image** and presents it for review.
4. If the operator is not satisfied, they can **regenerate** the ad: either using the previous image as a visual reference (keeping the general style while applying requested changes), or **starting from scratch** with a completely fresh generation.

---

## 2. Goals and Non-Goals

### Goals
- Allow an operator to compose ad content (text, images, prices) by chatting with the bot over WhatsApp in a natural, free-form style.
- Automatically enrich product details using publicly available web information when the operator provides limited input.
- Optionally accept a product photo and/or EAN barcode to better identify and describe the product.
- Generate publication-ready advertisement visuals suitable for TV display (1920 × 1080 px, 10% safe margins, landscape).
- Allow the operator to regenerate the ad — either using the previous image as a reference or from scratch — if the initial result is not satisfactory.
- Publish completed advertisements to the store's TV Content Management System (CMS).
- Remove (delete all) advertisements from the CMS when instructed.
- Support Hebrew-language conversations and Israeli business conventions by default.
- Remember each operator's language and currency preferences across sessions (identified by phone number).
- Support multiple operators per store with identical permissions.

### Non-Goals
- Multi-role permission workflows (e.g., manager approval required before staff can publish).
- Scheduling future publication times or managing campaigns across time windows.
- Editing or updating already-published advertisements (only append-new or delete-all operations are supported).
- Consumer-facing functionality; this bot serves only store operators.

---

## 3. Conversation Experience

### 3.1 Dialog Model

The bot conducts a **free-form, multi-turn chat dialog** to collect the information needed to create an advertisement. There is no fixed wizard sequence; the operator can provide details in any order, in any phrasing, and across multiple messages.

The bot proactively identifies the product by:
- Parsing the operator's natural-language description.
- Optionally requesting a **product photo** (an image of the item).
- Optionally requesting the **EAN barcode** (product code) for precise identification.
- Searching the web to fill in product details not explicitly provided by the operator (e.g., brand description, common packaging details).

Once the bot has sufficient information, it suggests generating an advertisement image and presents the result for approval.

### 3.2 Conversation Principles

| Principle | Description |
|-----------|-------------|
| **Free-form first** | The bot accepts any natural phrasing. The operator does not need to learn commands or follow a script. |
| **Clarify only missing essentials** | The bot asks follow-up questions only when the minimum required information is absent: at minimum, a **product name** and a **price**. It does not ask for optional details unless they are genuinely needed. |
| **Show, don't ask too much** | After gathering the essentials, the bot generates and shows an ad rather than asking more questions. Improvement is driven by the operator's reaction to the visual, not by a pre-generation questionnaire. |
| **Iterate with context** | The bot retains context between turns in an ad session. When regenerating, it applies only the changes the operator specifies (e.g., *"make the price bigger"*, *"use a blue background"*) without losing other collected details. |
| **Draft isolation** | In multi-operator mode, each operator works on their own draft. Drafts are not shared across operators. |
| **Memory** | The bot remembers the operator's preferred **language** and **currency** based on their WhatsApp number (phone number identity). These preferences are applied automatically in future sessions and can be overridden at any time. |

### 3.3 Required vs Optional Fields

| Field | Required? | Notes |
|-------|-----------|-------|
| Product name | **Required** | Must be confirmed before generating an ad. |
| Price | **Required** | Defaults to ILS (₪) unless the operator specifies another currency. |
| Product photo | Optional | Operator may send an image; the bot incorporates it into the generated ad. |
| EAN barcode | Optional | Helps the bot identify the product and enrich details from the web. |
| Promotional text | Optional | E.g., "20% off today only". The bot may suggest text if not provided. |

### 3.4 Example Happy Path

1. Operator: *"I want to advertise Tnuva cottage cheese"*
2. Bot: *"What is the price?"*
3. Operator: *"₪6.90"*
4. Bot: *"Great! I found some details about this product online. Generating your ad now…"* → sends preview image.
5. Operator: *"Looks good, publish it"*
6. Bot: *"Published! Your ad is now live on the TV screen."*

### 3.5 Regeneration Modes

After the bot presents a preview image, the operator can:

| Mode | Trigger phrase examples | Behaviour |
|------|------------------------|-----------|
| **Regenerate with reference** | *"Change the background colour to red"*, *"Make the price larger"* | The bot uses the previous image as a visual reference and applies only the requested changes. |
| **Regenerate from scratch** | *"Start over"*, *"Generate a completely new design"* | The bot discards the previous image and creates a fresh advertisement using all previously collected data. |

---

## 4. Supported Use Cases

### 4.1 Create a New Advertisement
The operator starts a free-form conversation describing the product. The bot gathers the product name and price (minimum), optionally asks for a photo or EAN code, enriches details from the web, and generates an ad visual.

### 4.2 Preview an Advertisement
The bot returns a preview image of the generated ad for operator approval before publishing.

### 4.3 Publish an Advertisement to TV
Once approved, the operator instructs the bot to publish. The bot appends the ad to the CMS playlist. There is no scheduling; the ad goes live immediately upon publishing.

### 4.4 Delete All Advertisements
The operator can instruct the bot to remove all currently active advertisements from the CMS. This is an all-or-nothing operation; individual ad removal is not supported. The bot requires explicit button-based confirmation before executing this action.

### 4.5 List Active Advertisements
The operator can request a summary list of advertisements currently published in the CMS.

### 4.6 Regenerate an Advertisement
After previewing an ad, the operator can request a regeneration — either refining the existing design or starting fresh (see §3.5).

### 4.7 Get Help
The operator can ask what actions the bot supports.

---

## 5. User Roles and Admin Console

### 5.1 Operator Role

| Role | Description |
|------|-------------|
| **Operator** | One of multiple authorised store users who can operate the bot via WhatsApp to create and publish ads. All operators have identical permissions. Identified by their registered WhatsApp phone number. |

### 5.2 Admin Console

An **admin console** is an essential part of the product. It enables configuration and oversight of the system without requiring direct code changes. The admin console provides:

- **Operator management**: register, update, or deactivate authorised WhatsApp phone numbers.
- **CMS connection settings**: configure the TV CMS endpoint used for publishing.
- **Default settings**: set the default language, currency, and regional preferences.
- **Active advertisement overview**: view the current list of ads published to the CMS.
- **Audit log**: view a history of bot actions (ad created, published, deleted) with timestamps.

The admin console is intended for the system administrator or store owner during setup and ongoing maintenance. It is not a day-to-day tool for ad creation.

---

## 6. Advertisement Visual Specifications

| Property | Value |
|----------|-------|
| **Output resolution** | 1920 × 1080 px (Full HD landscape) |
| **Safe margins** | 10% on all sides (192 px horizontal, 108 px vertical) |
| **Orientation** | Landscape |
| **Primary use** | In-store TV screens |

The operator provides content (text, prices, optional images). The bot handles layout and styling within the safe margin boundaries. Web-sourced product details may supplement operator-provided content in the layout.

---

## 7. Publishing Behaviour

| Behaviour | Details |
|-----------|---------|
| **Append-only** | New advertisements are always appended to the existing CMS playlist. |
| **No scheduling** | Ads go live immediately upon publish command; no future start/end dates. |
| **No individual update** | Published ads cannot be edited in place; the operator must delete all and re-publish if changes are needed. |
| **Delete-all** | The only removal operation removes all ads from the CMS at once. |
| **Permissions** | All authorised operators have identical permissions, including publish and delete-all. |

---

## 8. Regional and Localisation Defaults

| Setting | Default |
|---------|---------|
| Region | Israel |
| Currency | ILS (₪) |
| Language | Hebrew |
| Date format | DD/MM/YYYY |
| Number format | Israeli (e.g., 1,234.56) |

The operator may override currency and language per session. Overrides are remembered across sessions for the same phone number (see §3.2, Memory principle).

---

## 9. Security, Privacy, and Compliance

### 9.1 Operator Identity and Access
- The bot executes advertisement-management actions only for messages from registered operators' WhatsApp numbers.
- Phone number is the sole identity mechanism; no passwords or tokens are required for day-to-day use.
- The admin console controls which phone numbers are authorised to operate the bot.
- No self-enrollment is allowed via chat. Operator onboarding/offboarding is an admin action only.
- Authorisation source of truth is the `operator` table (`active=true` means authorised).
- Messages from unauthorised numbers receive a generic rejection message once per number per **60-minute window**; repeated attempts in that window are silently ignored.

#### 9.1.1 Admin Console Access (required)
- **Authentication**: strong authentication is required for Admin Console access. At launch, username + password with a secure credential store is acceptable; OIDC/SSO integration remains preferred for production maturity.
- **Authorisation**: all administrative actions (allowlist changes, CMS configuration updates, audit log access) must require verified admin-level credentials.
- **Session timeout**: idle admin sessions must be invalidated after a configurable timeout (recommended: 30 minutes).
- **CSRF protection**: all state-changing requests from the browser-based Admin Console must include CSRF tokens or use `SameSite` cookie attributes.
- **Audit logging**: all admin actions (configuration changes, operator onboarding/deactivation, credential updates) must be recorded in the audit log with actor, action, and timestamp.

### 9.2 Prompt Injection Risk
Because the operator's natural-language messages are processed by an AI language model, a malicious actor who gains access to the operator's WhatsApp account — or who sends spoofed or forged messages — may attempt **prompt injection**: crafting a message designed to override the bot's instructions, exfiltrate data, or trigger unauthorised actions such as deleting all ads or publishing malicious content.

Additionally, product names and promotional text entered by the operator may inadvertently or deliberately contain text intended to manipulate the AI model (indirect injection).

**Required protections (product-level):**
- The bot must resist instructions embedded in user messages that attempt to change its role, reveal its configuration, or bypass its operating rules.
- Product names, promotional text, and other operator-supplied content must be treated as data, not as instructions to the AI.
- Destructive actions (delete-all, publish) must require an explicit confirmation step that cannot be bypassed by a crafted message.
- The bot must not expose its internal configuration or system state to the operator or any third party.

### 9.3 Data Handling and Privacy
- The bot does not store or transmit payment card numbers, identity documents, or other sensitive personal data.
- Conversation history retention: **30 days**.
- Product photos uploaded by operators are used solely for ad generation and are not shared with third parties beyond what is necessary for that purpose; media retention is **90 days**.
- EAN/enrichment data retention: normalized fields in drafts are retained **30 days**; raw provider responses are not persisted.
- Audit log retention: **13 months**.

### 9.4 Compliance Considerations
- The product must comply with applicable data protection regulations for the region of operation (Israel: PPPA; EU users if applicable: GDPR).
- No consumer personal data is collected. Operator data (phone number, preferences) must be handled in accordance with applicable law.
- Ad content is operator-generated; the operator is responsible for ensuring the accuracy of prices and compliance with local advertising regulations.
- A short legal/compliance check of enrichment sources and usage terms is required before production go-live.

---

## 10. Out of Scope

The following are explicitly outside the scope of this product:

- Consumer-facing chatbot or customer interaction.
- E-commerce or online ordering integration.
- Analytics, reporting, or ad performance tracking.
- Multi-store or franchise management.
- Integration with any ERP, POS, or inventory system.
- Automated ad generation from a product catalog (without operator involvement).
- Scheduling or time-based ad management.
- Individual ad update or removal (only full delete-all is supported).
