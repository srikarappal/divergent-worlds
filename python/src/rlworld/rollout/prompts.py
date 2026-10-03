"""Cache friendly policy prompts (DESIGN.md section 6.8).

Only the tail of the prompt changes from turn to turn. The system prompt, the
task instruction and one entry per past turn (the observation's short text and
the model's own reply) never change once written. The heavy parts of the latest
observation (accessibility tree, DOM and the screenshot, which sits last) are
dropped from it when the next observation arrives, so prefix caching holds for
the whole episode. The sliding_window history keeps the last few screenshots
instead, which rewrites the middle of the prompt every turn; preflight warns
about it.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from rlworld.actions.accessibility import PrunedNode
from rlworld.actions.geometry import ScreenGeometry
from rlworld.actions.models import Observation

SLIDING_WINDOW_SCREENSHOTS = 3
TEXT_ACTION_SPACE = "text_action_space"
SYSTEM_PROMPT = (
    "You are a computer use agent working on a {os_family} desktop. Complete the task you are given by acting on "
    "the screen. Each turn shows the current state of the screen; choose your next action. Finish when the task "
    "is complete and ask the user only for information you cannot find yourself."
)
TEXT_ACTION_SECTION = (
    "\n\n## Output format\nThought: your reasoning\nAction: your action\n\n"
    "Coordinates refer to a {width} by {height} grid over the screenshot.\n\n## Action space\n{actions}"
)


@dataclass(frozen=True)
class PastTurn:
    observation: Observation
    reply: str


def quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def accessibility_lines(nodes: Sequence[PrunedNode], geometry: ScreenGeometry) -> list[str]:
    """One line per visible node; positions are node centers in the model's coordinate space."""
    lines = []
    for node in nodes:
        line = f"{'  ' * node.depth}[{node.node_id}] {node.role}"
        if node.name:
            line += f" {quoted(node.name)}"
        if node.value:
            line += f" value={quoted(node.value)}"
        if node.bounds is not None:
            center_x, center_y = geometry.to_model(node.bounds.x + node.bounds.width // 2,
                                                   node.bounds.y + node.bounds.height // 2)
            line += f" at ({center_x},{center_y})"
        if node.states:
            line += f" [{', '.join(node.states)}]"
        lines.append(line)
    return lines


def short_text(observation: Observation) -> list[str]:
    """What stays in the history: results the screen alone may not show."""
    sections = [f"Step {observation.step_index}."]
    if observation.user_reply is not None:
        sections.append(f"User reply: {observation.user_reply}")
    if observation.terminal is not None:
        terminal = observation.terminal
        sections.append(f"Terminal exit code {terminal.exit_code}\nstdout:\n{terminal.stdout}\n"
                        f"stderr:\n{terminal.stderr}")
    if observation.native_result is not None:
        sections.append(f"Native result: {json.dumps(observation.native_result, sort_keys=True, default=str)}")
    if observation.file_changes:
        changes = ", ".join(f"{change.change} {change.path}" for change in observation.file_changes)
        sections.append(f"File changes: {changes}")
    return sections


class PromptBuilder:
    def __init__(
        self,
        os_family: str,
        instruction: str,
        geometry: ScreenGeometry,
        tool_definitions: Sequence[dict[str, Any]],
        history: Literal["append_only", "sliding_window"] = "append_only",
    ) -> None:
        self.instruction = instruction
        self.geometry = geometry
        self.history = history
        self.tools = tuple(definition for definition in tool_definitions if definition.get("type") != TEXT_ACTION_SPACE)
        system_prompt = SYSTEM_PROMPT.format(os_family=os_family)
        for definition in tool_definitions:
            if definition.get("type") == TEXT_ACTION_SPACE:
                system_prompt += TEXT_ACTION_SECTION.format(width=definition["screen_width"],
                                                            height=definition["screen_height"],
                                                            actions="\n".join(definition["actions"]))
        self.system_prompt = system_prompt

    def observation_content(
        self, observation: Observation, is_first: bool, is_latest: bool, with_screenshot: bool
    ) -> list[dict[str, Any]]:
        sections = [f"Task: {self.instruction}"] if is_first else []
        sections.extend(short_text(observation))
        if is_latest and observation.accessibility is not None:
            sections.append("Accessibility tree:\n" + "\n".join(accessibility_lines(observation.accessibility,
                                                                                   self.geometry)))
        if is_latest and observation.dom is not None:
            sections.append(f"DOM:\n{observation.dom}")
        content: list[dict[str, Any]] = [{"type": "text", "text": "\n\n".join(sections)}]
        if with_screenshot and observation.screenshot is not None:
            content.append({"type": "image", "uri": observation.screenshot.uri})
        return content

    def messages(self, past_turns: Sequence[PastTurn], observation: Observation) -> tuple[dict[str, Any], ...]:
        first_windowed_turn = len(past_turns)
        if self.history == "sliding_window":
            first_windowed_turn = max(0, len(past_turns) - (SLIDING_WINDOW_SCREENSHOTS - 1))
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for turn_index, past_turn in enumerate(past_turns):
            messages.append({"role": "user", "content": self.observation_content(
                past_turn.observation, turn_index == 0, False, turn_index >= first_windowed_turn)})
            messages.append({"role": "assistant", "content": past_turn.reply})
        messages.append({"role": "user", "content": self.observation_content(observation, not past_turns, True, True)})
        return tuple(messages)
