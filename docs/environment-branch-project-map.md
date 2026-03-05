# Branch, Environment, and GCP Project Mapping

```mermaid
flowchart LR
    subgraph Git["Git Branches"]
        F["feature/* or codex/*"]
        PR["Pull Request"]
        M["main"]
        T["tag v* (release marker)"]
    end

    subgraph Local["Local Environment"]
        DEV["Developer machine\n(.env + docker-compose + ngrok)"]
    end

    subgraph STG["GCP Project: adv-assistant-staging"]
        STG_RUN_WEB["Cloud Run: webhook-staging"]
        STG_RUN_WORK["Cloud Run: worker-staging"]
        STG_SQL["Cloud SQL: staging"]
        STG_TASKS["Cloud Tasks: staging queue"]
        STG_GCS["GCS bucket: staging media"]
    end

    subgraph PROD["GCP Project: adv-assistant-prod"]
        PRD_RUN_WEB["Cloud Run: webhook-prod"]
        PRD_RUN_WORK["Cloud Run: worker-prod"]
        PRD_SQL["Cloud SQL: prod"]
        PRD_TASKS["Cloud Tasks: prod queue"]
        PRD_GCS["GCS bucket: prod media"]
    end

    F --> PR
    PR --> M
    M -->|Auto CI/CD deploy| STG_RUN_WEB
    M -->|Auto CI/CD deploy| STG_RUN_WORK
    M -->|Build image once| T
    T -->|Manual approval promotion\n(same image digest)| PRD_RUN_WEB
    T -->|Manual approval promotion\n(same image digest)| PRD_RUN_WORK

    F -. develop/test .-> DEV
    STG_RUN_WEB --> STG_TASKS --> STG_RUN_WORK
    PRD_RUN_WEB --> PRD_TASKS --> PRD_RUN_WORK
```

## Mapping Rules
- `feature/*`, `codex/*`: local development and PR validation only.
- `main`: staging deployment target.
- `v*` tag: production promotion trigger.
- Staging and production are isolated by project, credentials, and external integrations.
