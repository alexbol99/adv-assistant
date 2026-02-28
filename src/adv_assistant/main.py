from fastapi import FastAPI

app = FastAPI(title="adv-assistant")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
