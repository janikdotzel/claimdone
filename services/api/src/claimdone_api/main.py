from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel


class HealthResponse(BaseModel):
    service: Literal["api"] = "api"
    status: Literal["ok"] = "ok"


app = FastAPI(title="ClaimDone API", version="0.0.0")


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    """Report whether the API process is ready to serve requests."""
    return HealthResponse()
