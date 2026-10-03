"""Screen geometry: scale coordinates between env pixels and a model's screen space."""

import math
from dataclasses import dataclass

QWEN_PATCH_FACTOR = 28
QWEN_MIN_PIXELS = 56 * 56
QWEN_MAX_PIXELS = 16384 * 28 * 28


@dataclass(frozen=True)
class ScreenGeometry:
    env_width: int
    env_height: int
    model_width: int
    model_height: int

    def __post_init__(self) -> None:
        if min(self.env_width, self.env_height, self.model_width, self.model_height) <= 0:
            raise ValueError(f"screen sizes must be positive, got {self}")

    @property
    def is_identity(self) -> bool:
        return (self.env_width, self.env_height) == (self.model_width, self.model_height)

    def to_env(self, model_x: float, model_y: float) -> tuple[int, int]:
        """Model space to env pixels, clamped to the screen."""
        env_x = round(model_x * self.env_width / self.model_width)
        env_y = round(model_y * self.env_height / self.model_height)
        return clamp(env_x, self.env_width - 1), clamp(env_y, self.env_height - 1)

    def to_model(self, env_x: int, env_y: int) -> tuple[int, int]:
        """Env pixels to model space, clamped to the model screen."""
        model_x = round(env_x * self.model_width / self.env_width)
        model_y = round(env_y * self.model_height / self.env_height)
        return clamp(model_x, self.model_width - 1), clamp(model_y, self.model_height - 1)


def clamp(value: int, upper_bound: int) -> int:
    return max(0, min(value, upper_bound))


def fit_within(env_width: int, env_height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """Scale down to fit the box, keeping the aspect ratio; never scale up."""
    scale = min(1.0, max_width / env_width, max_height / env_height)
    return max(1, round(env_width * scale)), max(1, round(env_height * scale))


def smart_resize(
    env_width: int,
    env_height: int,
    factor: int = QWEN_PATCH_FACTOR,
    min_pixels: int = QWEN_MIN_PIXELS,
    max_pixels: int = QWEN_MAX_PIXELS,
) -> tuple[int, int]:
    """Qwen VL style resize: both sides become multiples of factor, total pixels stay in bounds."""
    resized_height = max(factor, round(env_height / factor) * factor)
    resized_width = max(factor, round(env_width / factor) * factor)
    if resized_height * resized_width > max_pixels:
        beta = math.sqrt(env_height * env_width / max_pixels)
        resized_height = max(factor, math.floor(env_height / beta / factor) * factor)
        resized_width = max(factor, math.floor(env_width / beta / factor) * factor)
    elif resized_height * resized_width < min_pixels:
        beta = math.sqrt(min_pixels / (env_height * env_width))
        resized_height = math.ceil(env_height * beta / factor) * factor
        resized_width = math.ceil(env_width * beta / factor) * factor
    return resized_width, resized_height
