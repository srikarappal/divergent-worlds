"""World diffs and side effect checks (DESIGN.md section 6.9).

Paths use "/" between segments, so file names keep their dots. Lists whose
items all carry a unique "id" are diffed by id, other lists by position. An
added or removed subtree is reported once, at its root.
"""

import fnmatch
from collections.abc import Sequence
from typing import Any

from rlworld.rewards.records import StateChange

PATH_SEPARATOR = "/"


def diff_states(initial_state: dict[str, Any], final_state: dict[str, Any]) -> list[StateChange]:
    changes: list[StateChange] = []
    collect_changes(initial_state, final_state, (), changes)
    return changes


def collect_changes(before: Any, after: Any, path_segments: tuple[str, ...], changes: list[StateChange]) -> None:
    if isinstance(before, list) and isinstance(after, list):
        before, after = list_as_mapping(before, after)
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            child_segments = (*path_segments, str(key))
            if key not in before:
                changes.append(StateChange(path=join_path(child_segments), kind="added", after=after[key]))
            elif key not in after:
                changes.append(StateChange(path=join_path(child_segments), kind="removed", before=before[key]))
            else:
                collect_changes(before[key], after[key], child_segments, changes)
        return
    if before != after:
        changes.append(StateChange(path=join_path(path_segments), kind="modified", before=before, after=after))


def list_as_mapping(before: list[Any], after: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if (before or after) and is_keyed_list(before) and is_keyed_list(after):
        return {str(item["id"]): item for item in before}, {str(item["id"]): item for item in after}
    return (
        {str(index): item for index, item in enumerate(before)},
        {str(index): item for index, item in enumerate(after)},
    )


def is_keyed_list(items: list[Any]) -> bool:
    if not all(isinstance(item, dict) and "id" in item for item in items):
        return False
    return len({str(item["id"]) for item in items}) == len(items)


def join_path(path_segments: Sequence[str]) -> str:
    return PATH_SEPARATOR.join(path_segments)


def path_matches(path: str, pattern: str) -> bool:
    """Glob match per segment; ** spans segments and a pattern covers everything below it."""
    return segments_match(tuple(path.split(PATH_SEPARATOR)), tuple(pattern.split(PATH_SEPARATOR)))


def segments_match(path_segments: tuple[str, ...], pattern_segments: tuple[str, ...]) -> bool:
    if not pattern_segments:
        return True
    head_pattern, remaining_patterns = pattern_segments[0], pattern_segments[1:]
    if head_pattern == "**":
        return any(
            segments_match(path_segments[start:], remaining_patterns) for start in range(len(path_segments) + 1)
        )
    if not path_segments:
        return False
    if not fnmatch.fnmatchcase(path_segments[0], head_pattern):
        return False
    return segments_match(path_segments[1:], remaining_patterns)


def side_effect_violations(
    changes: Sequence[StateChange],
    allowed_patterns: Sequence[str],
    volatile_patterns: Sequence[str] = (),
) -> list[StateChange]:
    """Changes outside what the task allows; volatile paths such as clocks are ignored."""
    permitted_patterns = (*allowed_patterns, *volatile_patterns)
    return [
        change
        for change in changes
        if not any(path_matches(change.path, pattern) for pattern in permitted_patterns)
    ]
