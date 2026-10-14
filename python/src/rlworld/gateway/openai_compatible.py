"""OpenAI compatible chat transport: covers vLLM, SGLang and most hosted APIs (DESIGN.md 6.8).

Token level output follows the vLLM OpenAI server extensions: logprobs plus
return_token_ids, which add prompt_token_ids and per choice token_ids.
"""

import base64
from typing import Any, Protocol

import httpx

from rlworld.gateway.config import EndpointConfig
from rlworld.gateway.errors import UpstreamError
from rlworld.gateway.messages import ChatRequest, TokenTrace, Usage
from rlworld.trajectories.object_store import ObjectStore, get_blob

RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})
PASSTHROUGH_IMAGE_SCHEMES = ("http://", "https://", "data:")
BLOB_SCHEME = "blob:"


class ImageResolver(Protocol):
    """Turns a screenshot uri such as s3://frames/1.webp into a URL the endpoint can fetch."""

    def resolve(self, image_uri: str) -> str: ...


class PassthroughImageResolver:
    def resolve(self, image_uri: str) -> str:
        if not image_uri.startswith(PASSTHROUGH_IMAGE_SCHEMES):
            raise ValueError(f"no resolver for image uri {image_uri}; presign it or inline it as a data URL")
        return image_uri


class BlobImageResolver:
    """Frames the node agents stored as blobs travel inline as data URLs; other URIs pass through."""

    def __init__(self, object_store: ObjectStore, fallback: ImageResolver | None = None) -> None:
        self.object_store = object_store
        self.fallback = fallback or PassthroughImageResolver()

    def resolve(self, image_uri: str) -> str:
        if not image_uri.startswith(BLOB_SCHEME):
            return self.fallback.resolve(image_uri)
        frame = get_blob(self.object_store, image_uri.removeprefix(BLOB_SCHEME))
        return f"data:{image_media_type(frame)};base64,{base64.b64encode(frame).decode('ascii')}"


def image_media_type(frame: bytes) -> str:
    if frame.startswith(b"\x89PNG"):
        return "image/png"
    if frame.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/webp"


def convert_content(content: Any, image_resolver: ImageResolver) -> Any:
    if isinstance(content, str):
        return content
    converted_parts = []
    for part in content:
        if part.get("type") == "image":
            converted_parts.append({"type": "image_url", "image_url": {"url": image_resolver.resolve(part["uri"])}})
        else:
            converted_parts.append({"type": "text", "text": part.get("text", "")})
    return converted_parts


def build_payload(request: ChatRequest, image_resolver: ImageResolver) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [
            {**message, "content": convert_content(message.get("content", ""), image_resolver)}
            for message in request.messages
        ],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }
    if request.seed is not None:
        payload["seed"] = request.seed
    if request.tools:
        payload["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
    if request.response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": request.response_schema, "strict": True},
        }
    if request.require_token_ids:
        payload["logprobs"] = True
        payload["return_token_ids"] = True
    return payload


def parse_token_trace(payload: dict[str, Any], choice: dict[str, Any]) -> TokenTrace | None:
    completion_token_ids = choice.get("token_ids")
    prompt_token_ids = payload.get("prompt_token_ids") or choice.get("prompt_token_ids")
    if completion_token_ids is None or prompt_token_ids is None:
        return None
    logprob_entries = (choice.get("logprobs") or {}).get("content") or []
    return TokenTrace(
        prompt_token_ids=tuple(prompt_token_ids),
        completion_token_ids=tuple(completion_token_ids),
        completion_logprobs=tuple(entry["logprob"] for entry in logprob_entries),
    )


def parse_response(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    try:
        choice = payload["choices"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise UpstreamError("response has no choices", retryable=True, counts_as_endpoint_failure=True) from error
    message = choice.get("message") or {}
    usage_payload = payload.get("usage") or {}
    return {
        "text": message.get("content") or "",
        # OpenRouter and recent vLLM name the hidden reasoning "reasoning", older vLLM "reasoning_content".
        "reasoning": message.get("reasoning") or message.get("reasoning_content") or "",
        "tool_calls": tuple(
            {"name": tool_call["function"]["name"], "arguments": tool_call["function"].get("arguments", "{}")}
            for tool_call in message.get("tool_calls") or []
        ),
        "served_model": payload.get("model") or requested_model,
        "finish_reason": choice.get("finish_reason"),
        "usage": Usage(
            input_tokens=usage_payload.get("prompt_tokens", 0),
            output_tokens=usage_payload.get("completion_tokens", 0),
            cached_input_tokens=(usage_payload.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        ),
        "token_trace": parse_token_trace(payload, choice),
    }


def retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after_value = response.headers.get("retry-after")
    try:
        return float(retry_after_value) if retry_after_value else None
    except ValueError:
        return None


class OpenAICompatibleTransport:
    def __init__(self, http_client: httpx.Client, image_resolver: ImageResolver | None = None) -> None:
        self.http_client = http_client
        self.image_resolver = image_resolver or PassthroughImageResolver()

    def send(self, endpoint: EndpointConfig, api_key: str | None, request: ChatRequest) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = f"{endpoint.base_url.rstrip('/')}/chat/completions"
        try:
            response = self.http_client.post(
                url, json=build_payload(request, self.image_resolver), headers=headers, timeout=endpoint.timeout_seconds
            )
        except httpx.TimeoutException as error:
            raise UpstreamError(f"{endpoint.name} timed out", retryable=True,
                                counts_as_endpoint_failure=True) from error
        except httpx.TransportError as error:
            raise UpstreamError(
                f"{endpoint.name} connection failed: {error}", retryable=True, counts_as_endpoint_failure=True
            ) from error
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_STATUS_CODES
            raise UpstreamError(
                f"{endpoint.name} returned {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds(response),
                counts_as_endpoint_failure=response.status_code >= 500,
            )
        return parse_response(response.json(), request.model)
