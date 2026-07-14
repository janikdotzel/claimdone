"""Narrow OpenAI client ports and the only production client factory."""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict, cast

from .config import ProviderConfig, ProviderMode

OPENAI_API_BASE_URL = "https://api.openai.com/v1"


class InputTextPart(TypedDict):
    type: Literal["input_text"]
    text: str


class InputImagePart(TypedDict):
    type: Literal["input_image"]
    image_url: str
    detail: Literal["high"]


type ResponseInputPart = InputTextPart | InputImagePart


class ResponseInputMessage(TypedDict):
    role: Literal["user"]
    content: list[ResponseInputPart]


class ResponseJSONSchemaFormat(TypedDict):
    type: Literal["json_schema"]
    name: str
    schema: dict[str, object]
    strict: bool


class ResponseTextConfig(TypedDict):
    format: ResponseJSONSchemaFormat


class ResponsesAPI(Protocol):
    def create(
        self,
        *,
        model: str,
        instructions: str,
        input: list[ResponseInputMessage],
        text: ResponseTextConfig,
        max_output_tokens: int,
        store: bool,
        timeout: float,
    ) -> object: ...


class TranscriptionsAPI(Protocol):
    def create(
        self,
        *,
        model: str,
        file: tuple[str, bytes, str],
        response_format: Literal["text"],
        timeout: float,
    ) -> object: ...


class AudioAPI(Protocol):
    @property
    def transcriptions(self) -> TranscriptionsAPI: ...


class OpenAIClientPort(Protocol):
    @property
    def responses(self) -> ResponsesAPI: ...

    @property
    def audio(self) -> AudioAPI: ...


def create_openai_client(
    *,
    api_key: str,
    config: ProviderConfig,
    organization: str,
    project: str,
) -> OpenAIClientPort:
    """Build a bounded client from explicit credentials and a pinned API origin."""

    if config.mode is not ProviderMode.LIVE:
        raise ValueError("The OpenAI client factory requires live provider mode")
    if type(api_key) is not str or not api_key.strip():
        raise ValueError("An explicitly injected OpenAI API key is required")
    if type(organization) is not str or not organization.strip():
        raise ValueError("An explicitly injected organization is required")
    if type(project) is not str or not project.strip():
        raise ValueError("An explicitly injected project is required")

    # Import lazily so deterministic tests need only their injected fake client.
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=OPENAI_API_BASE_URL,
        organization=organization,
        project=project,
        max_retries=config.sdk_max_retries,
        timeout=max(
            config.extraction_timeout_seconds,
            config.transcription_timeout_seconds,
        ),
    )
    return cast(OpenAIClientPort, client)
