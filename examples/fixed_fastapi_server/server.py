from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="Fixed EPL Prediction Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/predict")
def predict(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != 1:
        return {"error": "unsupported schema version"}
    return {
        "predicted_score_home": 2,
        "predicted_score_away": 1,
        "confidence": 0.9,
    }
