"""Shared strict-model configuration and scalar constraints."""

# Pydantic/FastAPI currently attaches field metadata incorrectly to PEP 695 aliases.
# ruff: noqa: UP040

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
)

CONTRACT_VERSION = "1.0.0"
ContractVersion: TypeAlias = Literal["1.0.0"]

Identifier: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        strip_whitespace=True,
    ),
]
NonEmptyText: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=4_000, strip_whitespace=True),
]
ShortText: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
]
Sha256Digest: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[a-f0-9]{64}$"),
]
Confidence: TypeAlias = Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
JsonScalar: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool | None
StrictBoolean: TypeAlias = StrictBool
StrictInteger: TypeAlias = StrictInt


def _require_false(value: object) -> object:
    if type(value) is not bool or value is not False:
        raise ValueError("value must be the boolean false")
    return value


def _require_true(value: object) -> object:
    if type(value) is not bool or value is not True:
        raise ValueError("value must be the boolean true")
    return value


def _require_three(value: object) -> object:
    if type(value) is not int or value != 3:
        raise ValueError("value must be the integer 3")
    return value


AlwaysFalse: TypeAlias = Annotated[Literal[False], BeforeValidator(_require_false)]
AlwaysTrue: TypeAlias = Annotated[Literal[True], BeforeValidator(_require_true)]
ExactlyThree: TypeAlias = Annotated[Literal[3], BeforeValidator(_require_three)]


def to_camel(value: str) -> str:
    """Convert a Python field name to the public camelCase wire name."""

    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class ContractModel(BaseModel):
    """Base for closed, immutable, camelCase JSON contract values."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        str_strip_whitespace=True,
        validate_by_alias=True,
        validate_by_name=False,
    )
