"""Closed public request models for the canonical INT-002 mutations."""

from typing import Annotated, Literal

from pydantic import Field

from .models import ApiModel

_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


class Int002RunRequest(ApiModel):
    """Start or recover the one V1 portal run bound to a READY case version."""

    contract_version: Literal["4.0.0"]
    expected_version: Annotated[int, Field(ge=1, le=_SQLITE_MAX_INTEGER)]
