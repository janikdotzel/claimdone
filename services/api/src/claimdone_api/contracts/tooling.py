"""Closed, server-resolved tool invocation contracts."""

from typing import Annotated

from pydantic import Field

from .base import ContractModel, ContractVersion, Identifier, StrictInteger
from .enums import AllowedTool


class ToolInvocationArguments(ContractModel):
    """V1 arguments are intentionally empty and resolved by trusted server state."""


class ToolInvocation(ContractModel):
    """One bounded invocation with no model-controlled case ID, URL, or field value."""

    contract_version: ContractVersion
    invocation_id: Identifier
    sequence: Annotated[StrictInteger, Field(ge=1, le=40)]
    tool: AllowedTool
    arguments: ToolInvocationArguments
