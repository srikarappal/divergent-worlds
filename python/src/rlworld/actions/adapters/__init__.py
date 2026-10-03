"""Per model action adapters and their registry."""

from typing import Any

from rlworld.actions.adapters.base import (
    ActionParseError,
    ActionRenderError,
    AdapterCapabilityError,
    ModelAdapter,
)
from rlworld.actions.adapters.computer_tool import ComputerToolAdapter
from rlworld.actions.adapters.text_actions import TextActionAdapter
from rlworld.actions.adapters.typed_actions import TypedActionAdapter

ADAPTER_CLASSES: dict[str, type] = {
    ComputerToolAdapter.format_name: ComputerToolAdapter,
    TypedActionAdapter.format_name: TypedActionAdapter,
    TextActionAdapter.format_name: TextActionAdapter,
}


def get_adapter(format_name: str, **options: Any) -> ModelAdapter:
    if format_name not in ADAPTER_CLASSES:
        known_formats = ", ".join(sorted(ADAPTER_CLASSES))
        raise KeyError(f"unknown action format {format_name!r}, known formats: {known_formats}")
    return ADAPTER_CLASSES[format_name](**options)


def supported_modalities(format_name: str) -> frozenset[str]:
    return ADAPTER_CLASSES[format_name].supported_modalities


__all__ = [
    "ADAPTER_CLASSES",
    "ActionParseError",
    "ActionRenderError",
    "AdapterCapabilityError",
    "ComputerToolAdapter",
    "ModelAdapter",
    "TextActionAdapter",
    "TypedActionAdapter",
    "get_adapter",
    "supported_modalities",
]
