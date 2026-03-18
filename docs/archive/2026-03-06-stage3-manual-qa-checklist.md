# Stage 3 Manual QA Checklist (Staging)

Date: 2026-03-07  
Branch: `codex/mvp-stage4-release-readiness`  
Owner: David Bool

## Environment

- [x] Staging backend deployed with latest migrations (`uv run alembic upgrade head`)
- [x] At least two test operators prepared:
  - [x] Operator A: connected to CMS mapping
  - [x] Operator B: not connected (missing mapping)
- [x] WhatsApp test device/session ready

## Scenario 1: Connected Operator Publish Success

Steps:
1. Send create-ad message with product name only.
2. Wait for preview response.
3. Verify publish CTA buttons are visible.
4. Press publish.

Expected:
- [x] Preview generated successfully
- [x] Publish CTA shown immediately after preview
- [x] Publish action succeeds
- [x] Published ad routed to operator's configured campaign/playlist

Evidence:
- [x] Screenshot/chat log attached
- [x] Relevant audit events recorded

## Scenario 2: Unconnected Operator Publish Block

Steps:
1. From operator without CMS mapping, generate a preview.
2. Press publish.

Expected:
- [x] Publish is blocked
- [x] Exact message returned: `אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור`
- [x] No CMS side effect occurs

Evidence:
- [x] Screenshot/chat log attached
- [x] Blocked publish audit event recorded

## Scenario 3: Logo Upload Flow

Steps:
1. Send intent to set/update logo.
2. Upload image.
3. Start a new ad and generate preview.

Expected:
- [x] Uploaded image saved as operator logo
- [x] Logo appears in generation context (not as product photo)
- [x] Flow completes without routing errors

Evidence:
- [x] Screenshot/chat log attached
- [x] Stored logo URL verified in admin or DB

## Scenario 4: Product Photo Upload Flow

Steps:
1. Start ad creation flow.
2. Upload product image (non-logo path).
3. Generate preview.

Expected:
- [x] Uploaded image saved as draft product photo
- [x] Product photo used for current draft only
- [x] No overwrite of operator logo memory

Evidence:
- [x] Screenshot/chat log attached
- [x] Draft fields verified in admin or DB

## Scenario 5: New Ad Isolation (No Field Leakage)

Steps:
1. Complete/generate an ad draft with product details.
2. Start a brand-new ad.
3. Provide only new product name and generate preview.

Expected:
- [x] New draft does not inherit previous draft product fields
- [x] System memory fields (store type / guidance / logo / language) still persist

Evidence:
- [x] Screenshot/chat log attached
- [x] Draft comparison verified

## Optional Cross-Checks (Recommended)

- [ ] Hebrew operator flow still shows publish CTA after successful previews
- [ ] English operator flow still shows publish CTA after successful previews
- [ ] Regenerate declined path still shows publish CTA

## Sign-Off

- QA Result: [x] PASS  [ ] FAIL
- Notes / defects:
  - Scenario 2 validated as publish-blocking (preview generation remains allowed by design).
  - Scenario 5 validated via DB draft comparison + operator system-memory persistence.
- Verified by: David Bool
- Date: 2026-03-07
