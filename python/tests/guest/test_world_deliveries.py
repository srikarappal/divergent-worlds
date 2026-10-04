import json

import pytest
from support.guest_fakes import build_agent

from rlworld.guest import GuestFailure
from rlworld.worlds import direct_channel, event_record, generate_mid_market_company
from rlworld.worlds.model import ChatMessage

SIZE = {"employees": 5, "customers": 2, "emails_per_person": 1, "chat_messages": 2}


@pytest.fixture
def world():
    return generate_mid_market_company(9, SIZE)


def colleague(world):
    return next(person for person in world.people if person.id != world.agent_persona_id)


def set_up(agent, world, apps=("mail", "chat", "files")):
    agent.handle("inject", {"world": world.model_dump(mode="json"), "apps": list(apps)})


def post(agent, world, channel, message_id, text):
    channel_directory = agent.world_root / "chat" / channel
    channel_directory.mkdir(parents=True, exist_ok=True)
    (channel_directory / f"{message_id}.json").write_text(json.dumps(
        {"sender_id": world.agent_persona_id, "text": text, "sent_at": "2026-03-14T09:05:00+00:00"}))


def reply_record(world, channel="finance"):
    return ChatMessage(id="chat_reply_0001", channel=channel, sender_id=colleague(world).id, text="Here you go.",
                       sent_at=world.base_time).model_dump(mode="json")


def changes(agent):
    return agent.handle("observe", {"kinds": ["file_diff"]})["file_changes"]


def test_deliveries_land_without_counting_as_the_agents_changes(tmp_path, world):
    agent = build_agent(tmp_path, world_inside_home=True)
    set_up(agent, world)
    (agent.world_root / "files" / "notes.txt").write_text("the agent's own work")
    agent.handle("inject", {"deliveries": [{"app": "mail", "record": event_record(world.events[0], world)},
                                           {"app": "chat", "record": reply_record(world)}]})
    assert changes(agent) == [{"path": "world/files/notes.txt", "change": "created"}]
    state = agent.handle("extract", {"apps": ["mail", "chat"]})
    assert any(message["subject"] == "Where are we on reconciliation?" for message in state["mail"]["inbox"])
    assert state["chat"]["finance"][-1]["text"] == "Here you go."


@pytest.mark.parametrize("delivery, message", [
    ({"app": "calendar", "record": {}}, "calendar is not an app of this world"),
    ({"app": "files", "record": {"path": "a.txt"}}, "files takes no records mid task"),
    ({"app": "chat", "record": {"id": "x"}}, "cannot deliver to chat"),
    ({"app": "chat"}, "cannot deliver to chat"),
])
def test_deliveries_need_one_of_the_worlds_apps_and_a_valid_record(tmp_path, world, delivery, message):
    agent = build_agent(tmp_path)
    set_up(agent, world)
    with pytest.raises(GuestFailure) as failure:
        agent.handle("inject", {"deliveries": [delivery]})
    assert failure.value.code == "bad_request" and message in str(failure.value)


def test_nothing_is_delivered_before_the_world_is_set_up(tmp_path, world):
    agent = build_agent(tmp_path)
    with pytest.raises(GuestFailure, match="mail is not an app of this world"):
        agent.handle("inject", {"deliveries": [{"app": "mail", "record": event_record(world.events[0], world)}]})


def test_the_agents_new_chat_posts_are_reported_once(tmp_path, world):
    agent = build_agent(tmp_path)
    set_up(agent, world)
    assert "chat_posts" not in agent.handle("observe", {"kinds": ["screenshot"]})
    channel = direct_channel(colleague(world).id)
    post(agent, world, channel, "post_1", "Which invoices were disputed?")
    agent.handle("inject", {"deliveries": [{"app": "chat", "record": reply_record(world, channel)}]})
    observation = agent.handle("observe", {"kinds": []})
    assert observation["chat_posts"] == [{"id": "post_1", "channel": channel, "text": "Which invoices were disputed?",
                                          "sent_at": "2026-03-14T09:05:00+00:00"}]
    assert "chat_posts" not in agent.handle("observe", {"kinds": []})
    post(agent, world, "finance", "post_2", "@team status?")
    assert [item["id"] for item in agent.handle("observe", {"kinds": []})["chat_posts"]] == ["post_2"]


def test_worlds_without_chat_report_no_posts(tmp_path, world):
    agent = build_agent(tmp_path)
    set_up(agent, world, apps=("mail",))
    post(agent, world, "finance", "post_1", "hello")
    assert "chat_posts" not in agent.handle("observe", {"kinds": []})


def test_setting_up_a_world_again_marks_its_posts_as_seen(tmp_path, world):
    agent = build_agent(tmp_path)
    set_up(agent, world)
    post(agent, world, "finance", "post_1", "first")
    agent.handle("observe", {"kinds": []})
    set_up(agent, world)
    assert "chat_posts" not in agent.handle("observe", {"kinds": []})
