import json
import threading

import httpx
import pytest

from rlworld.guest.chat_app import ChatError, ChatServer, chat_state, post_message
from rlworld.worlds import extract_world_state, generate_mid_market_company, materialize_world

SIZE = {"employees": 6, "customers": 4, "emails_per_person": 1}


@pytest.fixture
def world_root(tmp_path):
    world = generate_mid_market_company(5, SIZE)
    materialize_world(world, ["chat"], tmp_path)
    return tmp_path


def test_the_state_lists_channels_then_a_direct_conversation_with_every_colleague(world_root):
    state = chat_state(world_root)
    names = [channel["name"] for channel in state["channels"]]
    first_direct = next(index for index, name in enumerate(names) if name.startswith("dm_"))
    assert all(not name.startswith("dm_") for name in names[:first_direct])
    assert all(name.startswith("dm_") for name in names[first_direct:])
    assert f"dm_{state['owner']}" not in names
    seeded = next(channel for channel in state["channels"] if channel["messages"])
    assert seeded["title"].startswith("#") and seeded["messages"][0]["sender"]


def test_posts_land_in_the_chat_folder_as_the_agent_persona(world_root):
    state = chat_state(world_root)
    colleague_channel = next(channel["name"] for channel in state["channels"] if channel["name"].startswith("dm_"))
    message = post_message(world_root, colleague_channel, "  Can you send the December invoices?  ")
    assert (message.sender_id, message.text) == (state["owner"], "Can you send the December invoices?")
    extracted = extract_world_state(["chat"], world_root)["chat"][colleague_channel]
    assert [entry["text"] for entry in extracted] == ["Can you send the December invoices?"]
    with pytest.raises(ChatError, match="no channel"):
        post_message(world_root, "random_channel", "hello")
    with pytest.raises(ChatError, match="characters"):
        post_message(world_root, colleague_channel, "   ")


def test_the_page_and_api_serve_over_http(world_root):
    server = ChatServer(("127.0.0.1", 0), world_root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = httpx.get(f"{base_url}/")
        assert page.status_code == 200 and "<title>Chat</title>" in page.text
        channel = httpx.get(f"{base_url}/api/state").json()["channels"][0]["name"]
        created = httpx.post(f"{base_url}/api/messages", content=json.dumps({"channel": channel, "text": "On it"}))
        assert created.status_code == 201 and created.json()["channel"] == channel
        refused = httpx.post(f"{base_url}/api/messages", content=json.dumps({"channel": "nope", "text": "x"}))
        assert refused.status_code == 400
        assert httpx.get(f"{base_url}/missing").status_code == 404
    finally:
        server.shutdown()
