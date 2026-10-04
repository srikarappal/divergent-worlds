"""What a task changes in its world, for restores that start from a world snapshot (DESIGN.md sections 3 and 6.2).

When a world loads slowly or differently each time, envs restore from a world
snapshot that already holds it and task setup injects only the task's own
state: the mail, chat, documents and events it adds or changes. The records
those refer to stay whole so the delta is a coherent world. A task that removes
anything, or that is set in another world, cannot start from that snapshot.
"""

from typing import Any

from rlworld.worlds.model import Document, World

WORLD_FIELDS = ("generator", "seed", "base_time", "organization_name", "domain", "agent_persona_id")
MATERIALIZED_COLLECTIONS = ("emails", "chat_messages", "documents", "events")
REFERENCE_COLLECTIONS = ("people", "customers", "invoices", "deals")


def item_key(item: Any) -> str:
    return item.path if isinstance(item, Document) else item.id


def world_delta(base: World, task_world: World) -> World | None:
    """The task world with only the materialized items it adds or changes, or None when it cannot be a delta."""
    if any(getattr(base, field) != getattr(task_world, field) for field in WORLD_FIELDS):
        return None
    changes: dict[str, tuple[Any, ...]] = {}
    for collection in (*MATERIALIZED_COLLECTIONS, *REFERENCE_COLLECTIONS):
        base_items = {item_key(item): item for item in getattr(base, collection)}
        task_items = {item_key(item): item for item in getattr(task_world, collection)}
        if not base_items.keys() <= task_items.keys():
            return None
        if collection in MATERIALIZED_COLLECTIONS:
            changes[collection] = tuple(item for key, item in task_items.items() if base_items.get(key) != item)
    return task_world.model_copy(update=changes)
