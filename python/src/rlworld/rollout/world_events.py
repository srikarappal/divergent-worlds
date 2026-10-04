"""Scripted world events and colleagues who answer in chat, played during an episode (DESIGN.md section 6.4).

Before each turn, records land in the episode's apps: the scripted events
whose step has come (such as an email arriving) and the replies of colleagues
the agent wrote to in chat since the last turn. The agent reaches a colleague
in their direct channel or by mentioning @handle in a shared one. Colleagues
are played by the simulator stack that answers ask_user; what they know stays
on the host. Every delivered record names the state path it lands on, so the
reward pipeline never counts the world's own changes as the agent's side
effects.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from rlworld.actions.models import ChatPost, Observation
from rlworld.rollout.user_simulator import NpcSimulator
from rlworld.worlds.integrations import delivered_path, direct_channel
from rlworld.worlds.model import ChatMessage

CHAT_APP = "chat"
MAX_TRANSCRIPT_LINES = 20
REPLY_DELAY = timedelta(seconds=1)


class MissingSimulatorError(LookupError):
    """A colleague was written to but no simulator plays colleagues."""


@dataclass(frozen=True)
class ScheduledDelivery:
    """A record an app receives before the turn at step; label says where it came from in the step record."""

    step: int
    app: str
    record: dict[str, Any]
    label: str


@dataclass(frozen=True)
class NonPlayer:
    """A colleague who answers in chat: handle is the name others mention them by."""

    person_id: str
    handle: str
    persona: str
    knowledge: str


class WorldScript:
    """One episode's scripted events and colleagues; the runner shows it every observation it receives."""

    def __init__(self, events: Sequence[ScheduledDelivery], npcs: Sequence[NonPlayer],
                 simulator: NpcSimulator | None) -> None:
        self.events = tuple(events)
        self.npcs = tuple(npcs)
        self.simulator = simulator
        self.pending_posts: list[ChatPost] = []
        self.transcripts: dict[str, list[str]] = {}
        self.reply_numbers = itertools.count(1)
        self.delivered_paths: list[str] = []

    def observe(self, observation: Observation) -> None:
        self.pending_posts.extend(observation.chat_posts or ())

    def addressed(self, post: ChatPost) -> list[NonPlayer]:
        return [npc for npc in self.npcs
                if post.channel == direct_channel(npc.person_id) or f"@{npc.handle}" in post.text]

    def reply(self, npc: NonPlayer, post: ChatPost, transcript: Sequence[str], step: int) -> ScheduledDelivery:
        if self.simulator is None:
            raise MissingSimulatorError(f"{npc.person_id} was written to but no simulator plays colleagues")
        text = self.simulator.reply(npc.persona, npc.knowledge, transcript[-MAX_TRANSCRIPT_LINES:], post.text)
        message = ChatMessage(id=f"chat_reply_{next(self.reply_numbers):04d}", channel=post.channel,
                              sender_id=npc.person_id, text=text, sent_at=post.sent_at + REPLY_DELAY)
        return ScheduledDelivery(step, CHAT_APP, message.model_dump(mode="json"), f"chat reply from {npc.person_id}")

    def due(self, step: int) -> list[ScheduledDelivery]:
        """The events scripted for this step, then a reply from every colleague each new post reaches."""
        deliveries = [event for event in self.events if event.step == step]
        posts, self.pending_posts = self.pending_posts, []
        for post in posts:
            npcs = self.addressed(post)
            if not npcs:
                continue
            transcript = self.transcripts.setdefault(post.channel, [])
            replies = [self.reply(npc, post, tuple(transcript), step) for npc in npcs]
            transcript.append(f"agent: {post.text}")
            transcript.extend(f"{npc.handle}: {reply.record['text']}" for npc, reply in zip(npcs, replies))
            deliveries.extend(replies)
        return deliveries

    def record_delivered(self, deliveries: Sequence[ScheduledDelivery]) -> None:
        self.delivered_paths.extend(delivered_path(delivery.app, delivery.record) for delivery in deliveries)
