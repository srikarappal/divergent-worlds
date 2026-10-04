import json
from datetime import datetime, timedelta, timezone
from operator import attrgetter

import pytest
from pydantic import ValidationError

from rlworld.rewards import diff_states, side_effect_violations
from rlworld.worlds import (
    ChatIntegration,
    UnsupportedAppError,
    deliver_record,
    delivered_path,
    direct_channel,
    event_chat_message,
    event_record,
    extract_world_state,
    generate_mid_market_company,
    materialize_world,
    reset_world,
)
from rlworld.worlds.model import ChatMessage, World, WorldEvent

SIZE = {"employees": 6, "customers": 3, "emails_per_person": 2, "chat_messages": 3}


@pytest.fixture
def world():
    return generate_mid_market_company(5, SIZE)


def colleague(world):
    return next(person for person in world.people if person.id != world.agent_persona_id)


def post_file(target, channel, message_id, record):
    channel_directory = target / "chat" / channel
    channel_directory.mkdir(parents=True, exist_ok=True)
    (channel_directory / f"{message_id}.json").write_text(json.dumps(record))


def test_chat_round_trips_by_channel_in_time_order(world, tmp_path):
    materialize_world(world, ["chat"], tmp_path)
    channels = extract_world_state(["chat"], tmp_path)["chat"]
    assert set(channels) == {"finance"}
    expected = sorted(world.chat_messages, key=attrgetter("sent_at"))
    assert [message["id"] for message in channels["finance"]] == [message.id for message in expected]
    first = channels["finance"][0]
    assert set(first) == {"id", "sender_id", "text", "sent_at"}
    assert datetime.fromisoformat(first["sent_at"]) == expected[0].sent_at
    assert (tmp_path / "chat" / "owner.txt").read_text() == world.agent_persona_id
    reset_world(["chat"], tmp_path)
    assert extract_world_state(["chat"], tmp_path)["chat"] == {}


def test_only_the_agent_personas_messages_are_its_posts(world, tmp_path):
    materialize_world(world, ["chat"], tmp_path)
    chat = ChatIntegration()
    existing = {message.id for message in chat.owner_posts(tmp_path)}
    assert existing == {message.id for message in world.chat_messages if message.sender_id == world.agent_persona_id}
    post_file(tmp_path, direct_channel(colleague(world).id), "post_1",
              {"sender_id": world.agent_persona_id, "text": "Which invoices were disputed?",
               "sent_at": "2026-03-14T09:05:00+00:00"})
    post_file(tmp_path, "finance", "other_1", {"sender_id": colleague(world).id, "text": "Morning all",
                                                "sent_at": "2026-03-14T09:06:00+00:00"})
    posts = [message for message in chat.owner_posts(tmp_path) if message.id not in existing]
    assert [(post.id, post.channel, post.text) for post in posts] == [
        ("post_1", direct_channel(colleague(world).id), "Which invoices were disputed?")]
    assert ChatIntegration().owner_posts(tmp_path / "nowhere") == []


def test_the_folder_names_the_channel_and_malformed_files_are_no_messages(world, tmp_path):
    materialize_world(world, ["chat"], tmp_path)
    persona = world.agent_persona_id
    post_file(tmp_path, "finance", "renamed", {"id": "elsewhere", "channel": "sales", "sender_id": persona,
                                               "text": "hi", "sent_at": "2026-03-14T08:00:00"})
    post_file(tmp_path, "finance", "list", [1, 2])
    post_file(tmp_path, "finance", "extra", {"sender_id": persona, "text": "x", "sent_at": "2026-03-14T08:00:00",
                                             "reactions": 3})
    post_file(tmp_path, "finance", "no_text", {"sender_id": persona, "sent_at": "2026-03-14T08:00:00"})
    (tmp_path / "chat" / "finance" / "garbage.json").write_text("{not json")
    (tmp_path / "chat" / "bad channel!").mkdir()
    post_file(tmp_path, "bad channel!", "hidden", {"sender_id": persona, "text": "x",
                                                    "sent_at": "2026-03-14T08:00:00"})
    channels = extract_world_state(["chat"], tmp_path)["chat"]
    finance_ids = [message["id"] for message in channels["finance"]]
    assert "renamed" in finance_ids and not {"list", "extra", "no_text", "garbage", "elsewhere"} & set(finance_ids)
    assert set(channels) == {"finance"}
    renamed = next(message for message in channels["finance"] if message["id"] == "renamed")
    assert renamed["sent_at"] == "2026-03-14T08:00:00+00:00"


def test_deliveries_land_in_mail_and_chat_and_name_their_state_paths(world, tmp_path):
    materialize_world(world, ["mail", "chat", "files"], tmp_path)
    initial_state = extract_world_state(["mail", "chat", "files"], tmp_path)
    email = event_record(world.events[0], world)
    reply = ChatMessage(id="chat_reply_0001", channel="finance", sender_id=colleague(world).id, text="Done.",
                        sent_at=world.base_time).model_dump(mode="json")
    deliver_record("mail", email, tmp_path)
    deliver_record("chat", reply, tmp_path)
    final_state = extract_world_state(["mail", "chat", "files"], tmp_path)
    assert any(message["id"] == email["id"] for message in final_state["mail"]["inbox"])
    assert final_state["chat"]["finance"][-1]["text"] == "Done."
    paths = [delivered_path("mail", email), delivered_path("chat", reply)]
    assert paths == [f"mail/inbox/{email['id']}", "chat/finance/chat_reply_0001"]
    changes = diff_states(initial_state, final_state)
    assert sorted(change.path for change in changes) == sorted(paths)
    assert side_effect_violations(changes, [], paths) == []


@pytest.mark.parametrize("app, record, error", [
    ("files", {"path": "a.txt", "kind": "text", "content": "x"}, UnsupportedAppError),
    ("crm", {}, UnsupportedAppError),
    ("chat", {"id": "x", "channel": "../escape", "sender_id": "person_001", "text": "x",
              "sent_at": "2026-03-14T09:00:00+00:00"}, ValidationError),
    ("mail", {"id": "x"}, ValidationError),
])
def test_deliveries_need_an_app_that_takes_them_and_a_valid_record(tmp_path, app, record, error):
    with pytest.raises(error):
        deliver_record(app, record, tmp_path)


def test_scripted_chat_events_become_messages_at_the_worlds_time(world):
    sender = colleague(world).id
    event = WorldEvent(id="event_ping", trigger_step=2, app="chat", kind="chat_reply",
                       payload={"channel": "finance", "sender_id": sender, "text": "Any update?"})
    assert event_chat_message(event, world) == ChatMessage(id="chat_event_ping", channel="finance", sender_id=sender,
                                                           text="Any update?", sent_at=world.base_time)
    assert datetime.fromisoformat(event_record(event, world)["sent_at"]) == world.base_time
    assert event_record(world.events[0], world)["subject"] == "Where are we on reconciliation?"


def world_with_events(world, *events):
    return World.model_validate({**world.model_dump(mode="json"),
                                 "events": [event.model_dump(mode="json") for event in events]})


def test_scripted_events_must_name_the_worlds_people_and_their_app(world):
    sender = colleague(world).id
    valid = WorldEvent(id="event_ok", trigger_step=1, app="chat", kind="chat_reply",
                       payload={"channel": "finance", "sender_id": sender, "text": "hi"})
    assert world_with_events(world, valid).events == (valid,)
    stranger = valid.model_copy(update={"id": "event_stranger", "payload": {**valid.payload, "sender_id": "person_x"}})
    with pytest.raises(ValidationError, match="event event_stranger has unknown sender person_x"):
        world_with_events(world, stranger)
    misplaced = valid.model_copy(update={"id": "event_misplaced", "app": "mail"})
    with pytest.raises(ValidationError, match="lands in chat"):
        world_with_events(world, misplaced)
    outsider = WorldEvent(id="event_outsider", trigger_step=1, app="mail", kind="email_arrives",
                          payload={"sender": "spy@elsewhere.example", "recipients": [world.agent_persona.email],
                                   "subject": "s", "body": "b"})
    with pytest.raises(ValidationError, match="spy@elsewhere.example"):
        world_with_events(world, outsider)


def test_channels_are_plain_names():
    assert direct_channel("person_002") == "dm_person_002"
    moment = datetime(2026, 3, 14, tzinfo=timezone.utc)
    for channel in ("finance", "dm_person_002", "sales-team"):
        assert ChatMessage(id="m", channel=channel, sender_id="p", text="t", sent_at=moment).channel == channel
    for channel in ("", "../etc", "a/b", ".hidden", "with space"):
        with pytest.raises(ValidationError):
            ChatMessage(id="m", channel=channel, sender_id="p", text="t", sent_at=moment)


def test_naive_and_aware_times_sort_together(world, tmp_path):
    materialize_world(world, ["chat"], tmp_path)
    late = (world.base_time + timedelta(days=1)).replace(tzinfo=None).isoformat()
    post_file(tmp_path, "finance", "zz_late", {"sender_id": world.agent_persona_id, "text": "late", "sent_at": late})
    assert extract_world_state(["chat"], tmp_path)["chat"]["finance"][-1]["id"] == "zz_late"
