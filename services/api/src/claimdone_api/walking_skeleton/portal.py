"""Narrow server-side port for sandbox Portal A."""

from typing import Protocol
from urllib.parse import quote

import httpx

from .errors import PortalUnavailableError
from .models import PortalDraftFields, PortalSessionView, RenderedPortalValues


class PortalPort(Protocol):
    def fill_to_review(
        self,
        case_id: str,
        fields: PortalDraftFields,
    ) -> tuple[str, RenderedPortalValues]:
        """Reset, fill, stop at review, and freshly read rendered values."""

    def cleanup_case(self, case_id: str) -> None:
        """Idempotently remove one owned sandbox portal session."""


class HttpPortalPort:
    """Strict adapter for the local Next.js sandbox API; never submits."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        *,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
        )

    def fill_to_review(
        self,
        case_id: str,
        fields: PortalDraftFields,
    ) -> tuple[str, RenderedPortalValues]:
        encoded_case_id = quote(case_id, safe="")
        case_path = f"/api/sandbox/cases/{encoded_case_id}"
        try:
            reset = self._request(
                "POST",
                "/api/dev/reset",
                json={"caseId": case_id, "fixture": "empty", "variant": "A"},
            )
            reset_view = PortalSessionView.model_validate_json(reset.content)
            if reset_view.state != "draft":
                raise PortalUnavailableError("Portal reset did not produce a draft")
            saved = self._request(
                "PUT",
                f"{case_path}/draft?variant=A",
                json={
                    "expectedVersion": reset_view.version,
                    "fields": fields.model_dump(mode="json", by_alias=True),
                },
            )
            saved_view = PortalSessionView.model_validate_json(saved.content)
            if saved_view.state != "draft":
                raise PortalUnavailableError("Portal draft save left the draft state")
            reviewed = self._request(
                "POST",
                f"{case_path}/review?variant=A",
                json={"expectedVersion": saved_view.version},
            )
            reviewed_view = PortalSessionView.model_validate_json(reviewed.content)
            if reviewed_view.state != "review":
                raise PortalUnavailableError("Portal did not stop at review")
            rendered = self._request(
                "GET",
                f"{case_path}/rendered-values?variant=A",
            )
            rendered_values = RenderedPortalValues.model_validate_json(rendered.content)
            if rendered_values.case_id != case_id:
                raise PortalUnavailableError("Portal returned values for another case")
        except (httpx.HTTPError, ValueError) as error:
            raise PortalUnavailableError("Sandbox portal request failed") from error
        review_url = f"{self._base_url}/sandbox/A/cases/{encoded_case_id}"
        return review_url, rendered_values

    def cleanup_case(self, case_id: str) -> None:
        encoded_case_id = quote(case_id, safe="")
        try:
            response = self._client.request(
                "DELETE",
                f"/api/sandbox/cases/{encoded_case_id}?variant=A",
            )
        except httpx.HTTPError as error:
            raise PortalUnavailableError("Sandbox portal cleanup failed") from error
        if response.status_code == 404:
            return
        if not 200 <= response.status_code < 300:
            raise PortalUnavailableError(
                f"Sandbox portal cleanup returned HTTP {response.status_code}"
            )

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: object | None = None,
    ) -> httpx.Response:
        response = self._client.request(method, url, json=json)
        if not 200 <= response.status_code < 300:
            raise PortalUnavailableError(f"Sandbox portal returned HTTP {response.status_code}")
        return response
