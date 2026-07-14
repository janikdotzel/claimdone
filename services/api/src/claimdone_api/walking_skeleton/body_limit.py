"""Direct ASGI request-body limits for Content-Length and streamed bodies."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        global_limit: int,
        intake_limit: int,
    ) -> None:
        if global_limit < 1 or intake_limit < global_limit:
            raise ValueError("Body limits must be positive and intake >= global")
        self._app = app
        self._global_limit = global_limit
        self._intake_limit = intake_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit = self._limit_for(scope)
        try:
            content_length = _content_length(scope)
        except ValueError:
            await _send_error(
                send,
                status=400,
                code="CONTENT_LENGTH_INVALID",
                message="Content-Length must be one non-negative decimal integer.",
            )
            return
        if content_length is not None and content_length > limit:
            await _send_413(send)
            return

        received = 0
        buffered: list[Message] = []
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > limit:
                await _send_413(send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay_receive, send)

    def _limit_for(self, scope: Scope) -> int:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        if method == "POST" and path.startswith("/api/cases/") and path.endswith("/intake"):
            return self._intake_limit
        return self._global_limit


def _content_length(scope: Scope) -> int | None:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"]
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("duplicate Content-Length")
    value = values[0]
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("invalid Content-Length")
    return int(value)


async def _send_413(send: Send) -> None:
    await _send_error(
        send,
        status=413,
        code="REQUEST_BODY_TOO_LARGE",
        message="The request body exceeds the configured limit.",
    )


async def _send_error(send: Send, *, status: int, code: str, message: str) -> None:
    body = (
        '{"error":{"code":"'
        + code
        + '","message":"'
        + message
        + '","reasonCodes":[],"fieldErrors":[],"gateDecision":null,'
        + '"currentVersion":null}}'
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
