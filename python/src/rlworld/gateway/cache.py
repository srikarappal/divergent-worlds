"""Response caches for reproducible evals; RL rollouts never read them."""

import json
import threading
from pathlib import Path
from typing import Protocol

from rlworld.gateway.messages import ChatResponse


class ResponseCache(Protocol):
    def get(self, request_key: str) -> ChatResponse | None: ...

    def put(self, request_key: str, response: ChatResponse) -> None: ...


class InMemoryResponseCache:
    def __init__(self) -> None:
        self.responses: dict[str, ChatResponse] = {}
        self.lock = threading.Lock()

    def get(self, request_key: str) -> ChatResponse | None:
        with self.lock:
            return self.responses.get(request_key)

    def put(self, request_key: str, response: ChatResponse) -> None:
        with self.lock:
            self.responses[request_key] = response


class FileResponseCache:
    """One JSON file per request digest, so a cached eval replays across processes and machines."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, request_key: str) -> Path:
        return self.directory / f"{request_key.removeprefix('sha256:')}.json"

    def get(self, request_key: str) -> ChatResponse | None:
        cache_path = self.path_for(request_key)
        if not cache_path.exists():
            return None
        return ChatResponse.model_validate(json.loads(cache_path.read_text()))

    def put(self, request_key: str, response: ChatResponse) -> None:
        cache_path = self.path_for(request_key)
        temporary_path = cache_path.with_suffix(".tmp")
        temporary_path.write_text(response.model_dump_json())
        temporary_path.replace(cache_path)
