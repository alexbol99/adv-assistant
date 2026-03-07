# Stage 3 Manual QA Checklist (Staging)

Date: 2026-03-06  
Branch: `codex/mvp-stage3-system-memory`  
Owner: ____________________

## Environment

- [ ] Staging backend deployed with latest migrations (`uv run alembic upgrade head`)
- [ ] At least two test operators prepared:
  - [ ] Operator A: connected to CMS mapping
  - [ ] Operator B: not connected (missing mapping)
- [ ] WhatsApp test device/session ready

## Scenario 1: Connected Operator Publish Success

Steps:
1. Send create-ad message with product name only.
2. Wait for preview response.
3. Verify publish CTA buttons are visible.
4. Press publish.

Expected:
- [ ] Preview generated successfully
- [ ] Publish CTA shown immediately after preview
- [ ] Publish action succeeds
- [ ] Published ad routed to operator's configured campaign/playlist

Evidence:
- [ ] Screenshot/chat log attached
- [ ] Relevant audit events recorded

## Scenario 2: Unconnected Operator Publish Block

Steps:
1. From operator without CMS mapping, generate a preview.
2. Press publish.

Expected:
- [ ] Publish is blocked
- [ ] Exact message returned: `אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור`
- [ ] No CMS side effect occurs

Evidence:
- [ ] Screenshot/chat log attached
- [ ] Blocked publish audit event recorded

## Scenario 3: Logo Upload Flow

Steps:
1. Send intent to set/update logo.
2. Upload image.
3. Start a new ad and generate preview.

Expected:
- [ ] Uploaded image saved as operator logo
- [ ] Logo appears in generation context (not as product photo)
- [ ] Flow completes without routing errors

Evidence:
- [ ] Screenshot/chat log attached
- [ ] Stored logo URL verified in admin or DB

## Scenario 4: Product Photo Upload Flow

Steps:
1. Start ad creation flow.
2. Upload product image (non-logo path).
3. Generate preview.

Expected:
- [ ] Uploaded image saved as draft product photo
- [ ] Product photo used for current draft only
- [ ] No overwrite of operator logo memory

Evidence:
- [ ] Screenshot/chat log attached
- [ ] Draft fields verified in admin or DB

## Scenario 5: New Ad Isolation (No Field Leakage)

Steps:
1. Complete/generate an ad draft with product details.
2. Start a brand-new ad.
3. Provide only new product name and generate preview.

Expected:
- [ ] New draft does not inherit previous draft product fields
- [ ] System memory fields (store type / guidance / logo / language) still persist

Evidence:
- [ ] Screenshot/chat log attached
- [ ] Draft comparison verified

## Optional Cross-Checks (Recommended)

- [ ] Hebrew operator flow still shows publish CTA after successful previews
- [ ] English operator flow still shows publish CTA after successful previews
- [ ] Regenerate declined path still shows publish CTA

## Sign-Off

- QA Result: [ ] PASS  [ ] FAIL
- Notes / defects:
  - __________________________________________
  - __________________________________________
- Verified by: ____________________
- Date: ____________________
