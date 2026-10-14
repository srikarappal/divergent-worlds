"""Provider neutral chat requests and responses.

Messages carry content parts of type text or image (with a uri); transports
map them to each API. Training requests ask for exact token ids so the trainer
never retokenizes a transcript.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from rlworld.specs.digest import content_digest

ESTIMATED_TOKENS_PER_IMAGE = 1_500
ESTIMATED_CHARACTERS_PER_TOKEN = 4


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    model: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1)
    seed: int | None = None
    response_schema: dict[str, Any] | None = None
    require_token_ids: bool = False
    cache_mode: Literal["off", "read_write", "read_only"] = "off"
    sample_index: NonNegativeInt = 0
    run_id: str = ""
    purpose: Literal["policy", "judge", "probe", "other"] = "policy"


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cached_input_tokens: NonNegativeInt = 0


class TokenTrace(BaseModel):
    """Exact tokens for one turn; only completion tokens are trained on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_token_ids: tuple[int, ...]
    completion_token_ids: tuple[int, ...]
    completion_logprobs: tuple[float, ...]


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    reasoning: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    served_model: str
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    token_trace: TokenTrace | None = None
    latency_seconds: float = 0.0
    cost_usd: float | None = None
    attempts: int = 1
    cached: bool = False


def message_parts(request: ChatRequest) -> list[dict[str, Any]]:
    parts = []
    for message in request.messages:
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append({"type": "text", "text": content})
        else:
            parts.extend(content)
    return parts


def contains_images(request: ChatRequest) -> bool:
    return any(part.get("type") == "image" for part in message_parts(request))


def estimate_request_tokens(request: ChatRequest) -> int:
    """Rough upper bound for rate limiting before the call; reconciled with real usage after."""
    text_characters = sum(len(part.get("text", "")) for part in message_parts(request) if part.get("type") == "text")
    image_count = sum(1 for part in message_parts(request) if part.get("type") == "image")
    return (text_characters // ESTIMATED_CHARACTERS_PER_TOKEN + image_count * ESTIMATED_TOKENS_PER_IMAGE
            + request.max_tokens)


def request_digest(endpoint_name: str, request: ChatRequest) -> str:
    """Cache identity: everything that shapes the answer, nothing about who asked."""
    return content_digest(
        {
            "endpoint": endpoint_name,
            "model": request.model,
            "messages": list(request.messages),
            "tools": list(request.tools),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "seed": request.seed,
            "response_schema": request.response_schema,
            "sample_index": request.sample_index,
        }
    )
