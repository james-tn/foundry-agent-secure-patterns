import os
import secrets
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel


app = FastAPI(
    title="Internal Partner API",
    description="Private POC API that simulates internal envelope-status operations.",
    version="1.0.0",
)

api_key_header = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)

ENVELOPES = {
    "env-1001": {
        "envelope_id": "env-1001",
        "status": "completed",
        "document_name": "Mutual NDA",
        "recipient": "alex@example.internal",
        "updated_at": "2026-08-04T18:30:00Z",
    },
    "env-1002": {
        "envelope_id": "env-1002",
        "status": "sent",
        "document_name": "Master Services Agreement",
        "recipient": "sam@example.internal",
        "updated_at": "2026-08-04T19:15:00Z",
    },
}


class EnvelopeStatus(BaseModel):
    envelope_id: str
    status: str
    document_name: str
    recipient: str
    updated_at: str


def require_api_key(provided_key: str | None = Depends(api_key_header)) -> None:
    expected_key = os.environ.get("INTERNAL_API_KEY")
    if not expected_key:
        raise HTTPException(status_code=500, detail="API authentication is not configured")
    if not provided_key or not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/healthz", tags=["Operations"])
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "visibility": "private-vnet-only",
        "time": datetime.now(UTC).isoformat(),
    }


@app.get(
    "/internal/envelopes/{envelope_id}",
    response_model=EnvelopeStatus,
    tags=["Envelopes"],
    dependencies=[Depends(require_api_key)],
    operation_id="getEnvelopeStatus",
    summary="Get an internal envelope's status",
)
def get_envelope_status(envelope_id: str) -> EnvelopeStatus:
    envelope = ENVELOPES.get(envelope_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail="Envelope not found")
    return EnvelopeStatus(**envelope)
