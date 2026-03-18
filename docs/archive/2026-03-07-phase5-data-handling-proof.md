# Phase 5 Data Handling Proof (March 7, 2026)

This note captures evidence for **Phase 5 Compliance Checklist** data-handling items.

## Checklist Coverage

### 1) Only normalized enrichment fields are persisted in `ad_draft`
- Model stores normalized columns only (`enriched_brand`, `enriched_category`, `enriched_description`, `enriched_image_url`, `enrichment_source`, `enrichment_unavailable_notified_at`).
- Reference: `src/adv_assistant/db/models.py`

### 2) Raw provider payloads are not persisted in DB
- There is no `enrichment_raw_payload` field on `AdDraft`.
- Regression guard exists in test: `test_no_raw_enrichment_payload_field_is_defined`.
- Reference: `tests/test_phase5_enrichment.py`

### 3) Enrichment audit logs do not include raw payloads
- `enrichment_applied` audit metadata records only `wamid`, `ean`, `source`, `updated_fields`.
- Regression guard exists in test: `assert "raw_payload" not in audit_event.metadata_json`.
- References:
  - `src/adv_assistant/pipeline.py`
  - `tests/test_phase5_enrichment.py`

### 4) Retention policy alignment for enrichment-derived fields (30 days)
- `RetentionPolicy.draft_days` default is set to `30` days.
- Regression guard added in test: `test_retention_policy_defaults_align_with_product_retention`.
- References:
  - `src/adv_assistant/db/retention.py`
  - `tests/test_repositories.py`

## Verification Commands

```bash
uv run pytest -q tests/test_phase5_enrichment.py
uv run pytest -q tests/test_repositories.py -k retention
uv run ruff check src tests
```

