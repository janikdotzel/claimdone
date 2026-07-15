"""Shared strict-model configuration and scalar constraints."""

# Pydantic/FastAPI currently attaches field metadata incorrectly to PEP 695 aliases.
# ruff: noqa: UP040

import re
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    AwareDatetime,
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

CONTRACT_VERSION = "4.0.0"
ContractVersion: TypeAlias = Literal["4.0.0"]

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
Identifier: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN.pattern,
        strip_whitespace=True,
    ),
]


def _require_exact_attachment_identifier(value: object) -> object:
    if type(value) is not str or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError("attachment identifier must already use the exact wire format")
    return value


def _require_unique_attachment_identifiers(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(value)) != len(value):
        raise ValueError("attachment identifiers must be unique")
    return value


ExactAttachmentIdentifier: TypeAlias = Annotated[
    Identifier,
    BeforeValidator(_require_exact_attachment_identifier),
]
ExactlyThreeAttachmentIdentifiers: TypeAlias = Annotated[
    tuple[ExactAttachmentIdentifier, ...],
    Field(min_length=3, max_length=3, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_require_unique_attachment_identifiers),
]
UpToThreeAttachmentIdentifiers: TypeAlias = Annotated[
    tuple[ExactAttachmentIdentifier, ...],
    Field(max_length=3, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_require_unique_attachment_identifiers),
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
GitCommitSha: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[a-f0-9]{40}$"),
]
Confidence: TypeAlias = Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
JsonScalar: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool | None
StrictBoolean: TypeAlias = StrictBool
StrictInteger: TypeAlias = StrictInt

_DATE_WIRE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_WIRE_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$")
_AWARE_DATETIME_WIRE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


def _require_wire_date(value: object) -> object:
    if type(value) is date:
        return value
    if type(value) is not str or _DATE_WIRE_PATTERN.fullmatch(value) is None:
        raise ValueError("date input must be an ISO date string or an exact date object")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("date input must be a valid ISO date string") from error
    return value


def _require_wire_time(value: object) -> object:
    if type(value) is time:
        return value
    if type(value) is not str or _TIME_WIRE_PATTERN.fullmatch(value) is None:
        raise ValueError("time input must be an ISO time string or an exact time object")
    try:
        time.fromisoformat(value)
    except ValueError as error:
        raise ValueError("time input must be a valid ISO time string") from error
    return value


def _require_wire_aware_datetime(value: object) -> object:
    if type(value) is datetime:
        return value
    if type(value) is not str or _AWARE_DATETIME_WIRE_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "datetime input must be an ISO timezone-aware string or an exact datetime object"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("datetime input must be a valid ISO datetime string") from error
    if parsed.utcoffset() is None:
        raise ValueError("datetime input must include a timezone offset")
    return value


WireDate: TypeAlias = Annotated[date, BeforeValidator(_require_wire_date)]
WireTime: TypeAlias = Annotated[time, BeforeValidator(_require_wire_time)]
WireAwareDatetime: TypeAlias = Annotated[
    AwareDatetime, BeforeValidator(_require_wire_aware_datetime)
]


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


def _require_exact_integer(allowed: frozenset[int], label: str) -> Callable[[object], object]:
    def validator(value: object) -> object:
        if type(value) is not int or value not in allowed:
            raise ValueError(f"value must be {label}")
        return value

    return validator


AlwaysFalse: TypeAlias = Annotated[Literal[False], BeforeValidator(_require_false)]
AlwaysTrue: TypeAlias = Annotated[Literal[True], BeforeValidator(_require_true)]
ExactlyThree: TypeAlias = Annotated[Literal[3], BeforeValidator(_require_three)]
ExactlyOne: TypeAlias = Annotated[
    Literal[1], BeforeValidator(_require_exact_integer(frozenset({1}), "the integer 1"))
]
ZeroOrOne: TypeAlias = Annotated[
    Literal[0, 1],
    BeforeValidator(_require_exact_integer(frozenset({0, 1}), "the integer 0 or 1")),
]
ExactlyEight: TypeAlias = Annotated[
    Literal[8], BeforeValidator(_require_exact_integer(frozenset({8}), "the integer 8"))
]
OneToThree: TypeAlias = Annotated[
    Literal[1, 2, 3],
    BeforeValidator(_require_exact_integer(frozenset({1, 2, 3}), "one of the integers 1, 2, or 3")),
]
OneOrTwo: TypeAlias = Annotated[
    Literal[1, 2],
    BeforeValidator(_require_exact_integer(frozenset({1, 2}), "the integer 1 or 2")),
]


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
