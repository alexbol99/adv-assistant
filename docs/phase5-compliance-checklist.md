# Phase 5 Compliance Checklist (Enrichment Sources)

Use this checklist before enabling enrichment in staging/production.

## Source Terms Review

- [ ] Open Food Facts terms reviewed (API usage, attribution, rate limits, dataset license obligations).
- [ ] Fallback EAN provider terms reviewed (commercial usage, retention, redistribution limits).
- [ ] Web-search provider terms reviewed (query limits, caching/storage rules, attribution requirements).

## Data Handling Review

- [ ] Confirm only normalized enrichment fields are persisted in `ad_draft`.
- [ ] Confirm raw provider payloads are not persisted in DB.
- [ ] Confirm enrichment audit logs do not contain raw provider payloads.
- [ ] Confirm retention policy for enrichment-derived fields aligns with product policy (30 days in draft lifecycle scope).

## Security and Operations

- [ ] API secrets stored in Secret Manager (not in repo, not in plaintext env files committed to git).
- [ ] Outbound request timeouts and retries are configured to avoid webhook/task blocking.
- [ ] Enrichment disable switch verified (`ENRICHMENT_ENABLED=false`).

## Sign-off

- Reviewer:
- Date:
- Environment approved:
- Notes:
