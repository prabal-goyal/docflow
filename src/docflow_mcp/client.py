"""A small async client for the DocFlow HTTP API.

The MCP server only ever talks to DocFlow through this client, never to the database, so every
agent action goes through the same login, role checks, rate limits and audit log as a person's.
"""

import logging
from typing import Any

import httpx2
from pydantic import SecretStr

logger = logging.getLogger(__name__)

# Longest API error detail passed on to the model; DocFlow's own messages are much shorter.
MAX_DETAIL_CHARS = 500


class DocFlowError(Exception):
    """A failure the agent can act on. The message is shown to the model, so it must not leak
    secrets and should say what to do next."""


class DocFlowClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: SecretStr,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._token: str | None = None
        self._http = httpx2.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send an authenticated request and return the decoded JSON body (None for 204)."""
        if self._token is None:
            self._token = await self._login()
        response = await self._send(method, path, **kwargs)
        if response.status_code == 401:
            # Access tokens expire. The API rejects the request before doing anything, so
            # logging in again and resending once is safe even for writes.
            self._token = await self._login()
            response = await self._send(method, path, **kwargs)
        if not response.is_success:
            raise DocFlowError(error_message(response))
        return None if response.status_code == 204 else response.json()

    async def _login(self) -> str:
        response = await self._post_raw(
            "/auth/login",
            data={"username": self._email, "password": self._password.get_secret_value()},
        )
        if response.status_code == 401:
            raise DocFlowError(
                "DocFlow rejected the MCP server's login. Ask the user to check "
                "DOCFLOW_MCP_EMAIL and DOCFLOW_MCP_PASSWORD."
            )
        if not response.is_success:
            raise DocFlowError(error_message(response))
        body = response.json()
        if body.get("must_change_password"):
            raise DocFlowError(
                "The DocFlow account this server uses must change its password before it can "
                "be used. Ask the user to change it in DocFlow."
            )
        return str(body["access_token"])

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._token}"}
        return await self._call(self._http.request(method, path, headers=headers, **kwargs))

    async def _post_raw(self, path: str, **kwargs: Any) -> httpx2.Response:
        return await self._call(self._http.post(path, **kwargs))

    async def _call(self, pending: Any) -> httpx2.Response:
        try:
            response: httpx2.Response = await pending
        except httpx2.TimeoutException:
            raise DocFlowError("DocFlow did not answer in time. Try again shortly.") from None
        except httpx2.RequestError as exc:
            logger.warning("Could not reach DocFlow: %s", exc)
            raise DocFlowError(
                f"Could not reach DocFlow at {self._http.base_url}. Is it running?"
            ) from None
        return response


def error_message(response: httpx2.Response) -> str:
    """Turn an API error into a short instruction for the model."""
    status = response.status_code
    if status == 403:
        return (
            "The DocFlow account this server uses is not allowed to do this. "
            "A person with the right role has to do it in DocFlow."
        )
    if status == 404:
        return "Not found, or not visible to the DocFlow account this server uses."
    if status == 429:
        wait = response.headers.get("Retry-After")
        return "DocFlow is rate limiting requests. " + (
            f"Wait {wait} seconds and try again." if wait else "Wait and try again."
        )
    if status in (400, 409, 413, 415, 422):
        # DocFlow's own 4xx messages are written for people and safe to show.
        return f"DocFlow refused the request: {detail(response)}"
    logger.error("DocFlow returned %s for %s", status, response.request.url)
    return f"DocFlow returned an unexpected error (HTTP {status}). Try again later."


def detail(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    value = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(value, list):
        # FastAPI validation errors: a list of {loc, msg} objects.
        value = "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
            for item in value
            if isinstance(item, dict)
        )
    return str(value)[:MAX_DETAIL_CHARS]
