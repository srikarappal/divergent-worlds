"""Canonical actions, observations, key names, geometry and per model adapters (DESIGN.md section 6.3)."""

from rlworld.actions.geometry import ScreenGeometry, fit_within, smart_resize
from rlworld.actions.keys import KeyNameError, normalize_key_combo
from rlworld.actions.models import (
    MODALITIES,
    Action,
    ActionModel,
    ActionNotAllowedError,
    Observation,
    action_payload,
    ensure_actions_allowed,
    parse_action,
)

__all__ = [
    "MODALITIES",
    "Action",
    "ActionModel",
    "ActionNotAllowedError",
    "KeyNameError",
    "Observation",
    "ScreenGeometry",
    "action_payload",
    "ensure_actions_allowed",
    "fit_within",
    "normalize_key_combo",
    "parse_action",
    "smart_resize",
]
