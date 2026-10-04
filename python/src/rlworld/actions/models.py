"""Canonical actions and observations (DESIGN.md section 6.3).

Coordinates are integer pixels in the env's declared screen space; adapters
translate to and from each model's own coordinate convention.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from rlworld.actions.accessibility import PrunedNode
from rlworld.actions.keys import MODIFIER_ORDER, normalize_key_combo

MouseButton = Literal["left", "right", "middle"]
# DOM observations carry each element's id in this attribute, the BrowserGym bid convention; browser actions use it.
ELEMENT_ATTRIBUTE = "data-rlworld-id"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Point(FrozenModel):
    x: NonNegativeInt
    y: NonNegativeInt


class ActionModel(FrozenModel):
    modality: ClassVar[str]
    ends_episode: ClassVar[bool] = False


class MoveAction(ActionModel):
    modality: ClassVar[str] = "computer"
    name: Literal["move"] = "move"
    x: NonNegativeInt
    y: NonNegativeInt


class ClickAction(ActionModel):
    modality: ClassVar[str] = "computer"
    name: Literal["click"] = "click"
    x: NonNegativeInt
    y: NonNegativeInt
    button: MouseButton = "left"
    count: int = Field(default=1, ge=1, le=3)
    modifiers: tuple[str, ...] = ()

    @field_validator("modifiers", mode="before")
    @classmethod
    def normalize_modifiers(cls, raw_modifiers: Any) -> tuple[str, ...]:
        if not raw_modifiers:
            return ()
        modifiers = normalize_key_combo(raw_modifiers)
        non_modifiers = [key for key in modifiers if key not in MODIFIER_ORDER]
        if non_modifiers:
            raise ValueError(f"click modifiers must be modifier keys, got {non_modifiers}")
        return modifiers


class DragAction(ActionModel):
    modality: ClassVar[str] = "computer"
    name: Literal["drag"] = "drag"
    path: tuple[Point, ...] = Field(min_length=2)
    button: MouseButton = "left"


class ScrollAction(ActionModel):
    """Wheel notches; positive delta_y scrolls down, positive delta_x scrolls right."""

    modality: ClassVar[str] = "computer"
    name: Literal["scroll"] = "scroll"
    x: NonNegativeInt
    y: NonNegativeInt
    delta_x: int = 0
    delta_y: int = 0

    @model_validator(mode="after")
    def check_delta(self) -> "ScrollAction":
        if self.delta_x == 0 and self.delta_y == 0:
            raise ValueError("scroll needs a non zero delta_x or delta_y")
        return self


class TypeAction(ActionModel):
    modality: ClassVar[str] = "computer"
    name: Literal["type"] = "type"
    text: str = Field(min_length=1)


class KeyAction(ActionModel):
    modality: ClassVar[str] = "computer"
    name: Literal["key"] = "key"
    keys: tuple[str, ...]

    @field_validator("keys", mode="before")
    @classmethod
    def normalize_keys(cls, raw_keys: Any) -> tuple[str, ...]:
        return normalize_key_combo(raw_keys)


class WaitAction(ActionModel):
    """A zero duration wait is a plain observe step."""

    modality: ClassVar[str] = "computer"
    name: Literal["wait"] = "wait"
    duration_ms: int = Field(default=1000, ge=0, le=60_000)


class GotoAction(ActionModel):
    modality: ClassVar[str] = "browser"
    name: Literal["goto"] = "goto"
    url: str = Field(min_length=1)


class ElementClickAction(ActionModel):
    modality: ClassVar[str] = "browser"
    name: Literal["click_element"] = "click_element"
    element_id: str = Field(min_length=1)


class FillAction(ActionModel):
    modality: ClassVar[str] = "browser"
    name: Literal["fill"] = "fill"
    element_id: str = Field(min_length=1)
    text: str


class SelectOptionAction(ActionModel):
    modality: ClassVar[str] = "browser"
    name: Literal["select_option"] = "select_option"
    element_id: str = Field(min_length=1)
    option: str


class BackAction(ActionModel):
    modality: ClassVar[str] = "browser"
    name: Literal["back"] = "back"


class TabAction(ActionModel):
    modality: ClassVar[str] = "browser"
    name: Literal["tab"] = "tab"
    operation: Literal["new", "close", "switch"]
    index: NonNegativeInt | None = None
    url: str | None = None

    @model_validator(mode="after")
    def check_operation(self) -> "TabAction":
        if self.operation == "switch" and self.index is None:
            raise ValueError("switching tabs needs an index")
        if self.url is not None and self.operation != "new":
            raise ValueError("only new tabs take a url")
        return self


class ExecAction(ActionModel):
    modality: ClassVar[str] = "terminal"
    name: Literal["exec"] = "exec"
    command: str = Field(min_length=1)
    timeout_s: PositiveInt = Field(default=60, le=3_600)


class NativeCallAction(ActionModel):
    modality: ClassVar[str] = "native"
    name: Literal["native_call"] = "native_call"
    app: str = Field(min_length=1)
    method: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AskUserAction(ActionModel):
    modality: ClassVar[str] = "control"
    name: Literal["ask_user"] = "ask_user"
    question: str = ""


class AnswerAction(ActionModel):
    modality: ClassVar[str] = "control"
    ends_episode: ClassVar[bool] = True
    name: Literal["answer"] = "answer"
    text: str


class DoneAction(ActionModel):
    modality: ClassVar[str] = "control"
    ends_episode: ClassVar[bool] = True
    name: Literal["done"] = "done"


class GiveUpAction(ActionModel):
    modality: ClassVar[str] = "control"
    ends_episode: ClassVar[bool] = True
    name: Literal["give_up"] = "give_up"
    reason: str = ""


Action = Annotated[
    MoveAction
    | ClickAction
    | DragAction
    | ScrollAction
    | TypeAction
    | KeyAction
    | WaitAction
    | GotoAction
    | ElementClickAction
    | FillAction
    | SelectOptionAction
    | BackAction
    | TabAction
    | ExecAction
    | NativeCallAction
    | AskUserAction
    | AnswerAction
    | DoneAction
    | GiveUpAction,
    Field(discriminator="name"),
]
ACTION_TYPE_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)
ACTION_CLASSES: tuple[type[ActionModel], ...] = (
    MoveAction,
    ClickAction,
    DragAction,
    ScrollAction,
    TypeAction,
    KeyAction,
    WaitAction,
    GotoAction,
    ElementClickAction,
    FillAction,
    SelectOptionAction,
    BackAction,
    TabAction,
    ExecAction,
    NativeCallAction,
    AskUserAction,
    AnswerAction,
    DoneAction,
    GiveUpAction,
)
MODALITIES = ("computer", "browser", "terminal", "native", "control")


class ActionNotAllowedError(ValueError):
    """The task does not allow this action's modality."""


def action_name(action_class: type[ActionModel]) -> str:
    return action_class.model_fields["name"].default


def parse_action(payload: dict[str, Any]) -> Action:
    return ACTION_TYPE_ADAPTER.validate_python(payload)


def action_payload(action: ActionModel) -> dict[str, Any]:
    return action.model_dump(mode="json")


def action_classes_for(modalities: Iterable[str]) -> list[type[ActionModel]]:
    wanted_modalities = set(modalities)
    return [action_class for action_class in ACTION_CLASSES if action_class.modality in wanted_modalities]


def ensure_actions_allowed(actions: Sequence[ActionModel], allowed_modalities: Iterable[str]) -> None:
    allowed = set(allowed_modalities)
    rejected_names = [action.name for action in actions if action.modality not in allowed]
    if rejected_names:
        raise ActionNotAllowedError(
            f"actions {rejected_names} are outside the allowed modalities {sorted(allowed)}"
        )


class ScreenshotRef(FrozenModel):
    uri: str
    width: PositiveInt
    height: PositiveInt
    image_format: Literal["webp", "png", "jpeg"] = "webp"
    sha256: str


class TerminalOutput(FrozenModel):
    stdout: str = ""
    stderr: str = ""
    exit_code: int


class FileChange(FrozenModel):
    path: str
    change: Literal["created", "modified", "deleted"]


class ChatPost(FrozenModel):
    """A chat message the agent posted since the last observation, for colleagues who answer in chat."""

    id: str
    channel: str
    text: str
    sent_at: datetime


class Observation(FrozenModel):
    step_index: NonNegativeInt
    screenshot: ScreenshotRef | None = None
    accessibility: tuple[PrunedNode, ...] | None = None
    dom: str | None = None
    terminal: TerminalOutput | None = None
    file_changes: tuple[FileChange, ...] | None = None
    user_reply: str | None = None
    native_result: Any = None
    chat_posts: tuple[ChatPost, ...] | None = None
