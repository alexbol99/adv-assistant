# Product Specification — WhatsApp Advertisement Assistant Bot

## 1. Overview

The **WhatsApp Advertisement Assistant Bot** is a conversational tool that allows a single operator (owner/operator role) to create, manage, and publish digital advertisements for in-store TV screens — entirely through WhatsApp chat. No dedicated web dashboard or desktop application is required for day-to-day operations.

---

## 2. Goals and Non-Goals

### Goals
- Allow an operator to compose ad content (text, images, prices) by chatting with the bot over WhatsApp.
- Generate publication-ready advertisement visuals suitable for TV display.
- Publish completed advertisements to the store's TV Content Management System (CMS).
- Remove (delete all) advertisements from the CMS when instructed.
- Support Hebrew-language conversations and Israeli business conventions by default.

### Non-Goals
- Multi-user or multi-role workflows (e.g., separate manager and cashier accounts).
- Multi-language operator UI (the operator interface is Hebrew by default; English override is supported per session).
- Scheduling future publication times or managing campaigns across time windows.
- Editing or updating already-published advertisements (only append-new or delete-all operations are supported).
- Consumer-facing functionality; this bot serves only the store owner/operator.

---

## 3. User Roles

| Role | Description |
|------|-------------|
| **Owner / Operator** | Single user who owns and operates the store. Interacts with the bot via WhatsApp to create and publish ads. |

There is no other role. No admin console, no multi-staff access.

---

## 4. Supported Use Cases

### 4.1 Create a New Advertisement
The operator sends a message describing the product, price, and any promotional text. The bot confirms the details with the operator before generating the ad visual.

### 4.2 Preview an Advertisement
The bot returns a preview image of the generated ad for operator approval before publishing.

### 4.3 Publish an Advertisement to TV
Once approved, the operator instructs the bot to publish. The bot appends the ad to the CMS playlist. There is no scheduling; the ad goes live immediately upon publishing.

### 4.4 Delete All Advertisements
The operator can instruct the bot to remove all currently active advertisements from the CMS. This is an all-or-nothing operation; individual ad removal is not supported.

### 4.5 List Active Advertisements
The operator can request a summary list of advertisements currently published in the CMS.

### 4.6 Get Help
The operator can ask what commands or actions the bot supports.

---

## 5. Conversation Experience

- The primary language of the bot is **Hebrew**. The operator may request English responses for a session.
- The bot uses a friendly, conversational tone appropriate for a small-business owner.
- All monetary values default to **Israeli New Shekel (₪ / ILS)** unless the operator specifies a different currency.
- The bot proactively asks for missing information (e.g., product name, price, promotional text) rather than failing silently.
- The bot confirms destructive actions (e.g., "Delete all ads") with an explicit confirmation prompt before proceeding.

---

## 6. Advertisement Visual Specifications

| Property | Value |
|----------|-------|
| **Output resolution** | 1920 × 1080 px (Full HD landscape) |
| **Safe margins** | 10% on all sides (192 px horizontal, 108 px vertical) |
| **Orientation** | Landscape |
| **Primary use** | In-store TV screens |

The operator provides content (text, prices, optional images). The bot handles layout and styling within the safe margin boundaries.

---

## 7. Publishing Behaviour

| Behaviour | Details |
|-----------|---------|
| **Append-only** | New advertisements are always appended to the existing CMS playlist. |
| **No scheduling** | Ads go live immediately upon publish command; no future start/end dates. |
| **No individual update** | Published ads cannot be edited in place; the operator must delete all and re-publish if changes are needed. |
| **Delete-all** | The only removal operation removes all ads from the CMS at once. |

---

## 8. Regional and Localisation Defaults

| Setting | Default |
|---------|---------|
| Region | Israel |
| Currency | ILS (₪) |
| Language | Hebrew |
| Date format | DD/MM/YYYY |
| Number format | Israeli (e.g., 1,234.56) |

The operator may override currency and language per session.

---

## 9. Safety and Operator Protections

- The bot will not perform any destructive action (delete all ads) without an explicit confirmation step.
- The bot will not accept instructions that appear to alter its own behaviour, impersonate other systems, or bypass its operating rules (see Architecture & Technical Spec for implementation details).
- The bot will not store or transmit payment card numbers or other sensitive personal data.

---

## 10. Out of Scope

The following are explicitly outside the scope of this product:

- Consumer-facing chatbot or customer interaction.
- E-commerce or online ordering integration.
- Analytics, reporting, or ad performance tracking.
- Multi-store or franchise management.
- Integration with any ERP, POS, or inventory system.
- Automated ad generation from a product catalog.
