# T5: Request type classification loop

## Summary

- Added request-type classification state to conversation sessions and draft defaults so ad requests can stay unresolved until the operator clarifies whether the request is for a single product, multiple products, or a general store ad.
- Updated the inbound pipeline to detect request type from direct keywords first, fall back to ad-field extraction for single-product signals, and persist classification audit events plus pending question state when the request remains ambiguous.
- Added reprompt handling so image uploads and button clicks do not break the classification loop, and added explicit acknowledgement replies for resolved multi-product and store-general classifications.

## Tests Run

- Attempted: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_repositories.py tests/test_conversation_flow_v1_t5.py`
- Attempted: `timeout 45s sh -c 'UV_CACHE_DIR=/tmp/uv-cache uv run pytest -vv tests/test_conversation_flow_v1_t5.py::test_ambiguous_create_ad_enters_classification_loop'`
- Attempted: `timeout 45s sh -c 'UV_CACHE_DIR=/tmp/uv-cache uv run pytest -vv tests/test_repositories.py::test_pending_question_updates'`
- Result: all three commands collected tests but timed out in this sandbox before completion.

## Key Behavior Changes

- Ambiguous `create ad` requests now prompt the operator to choose one ad type instead of immediately continuing with an unresolved draft.
- Classification follow-up replies reuse the active draft, remember the original ad intent, and clear the pending classification state once resolved.
- Non-text replies received while classification is pending are reprompted and do not ingest media into the draft.
