import json

import pytest
from support.rollout_fakes import INSTRUCTION, ScriptedGateway, build_policy, rollout_document

from rlworld.actions.adapters import ActionParseError, get_adapter
from rlworld.actions.geometry import ScreenGeometry
from rlworld.actions.models import Observation
from rlworld.gateway.messages import ChatResponse
from rlworld.rollout import PastTurn, PromptBuilder, adapter_output, assistant_reply

GEOMETRY = ScreenGeometry(1280, 800, 1000, 1000)


def observation(step_index, **fields):
    return Observation.model_validate({
        "step_index": step_index,
        "screenshot": {"uri": f"s3://frames/{step_index}.webp", "width": 1280, "height": 800,
                       "sha256": f"{step_index}"},
        "accessibility": [{"node_id": "cell_b2", "role": "cell", "name": "B2", "depth": 1,
                           "bounds": {"x": 600, "y": 380, "width": 80, "height": 40}}],
        **fields,
    })


def text_builder(history="append_only"):
    adapter = get_adapter("text_actions", coordinate_mode="relative_1000")
    return PromptBuilder("windows", INSTRUCTION, GEOMETRY, adapter.tool_definitions(["computer", "control"], GEOMETRY),
                         history)


def image_count(messages):
    return sum(1 for message in messages if isinstance(message["content"], list)
               for part in message["content"] if part["type"] == "image")


def test_only_the_tail_changes_between_turns():
    builder = text_builder()
    past_turns = [PastTurn(observation(0), "Action: click(start_box='(500,500)')")]
    current = builder.messages(past_turns, observation(1, terminal={"stdout": "ok", "exit_code": 0}))
    following = builder.messages([*past_turns, PastTurn(observation(1, terminal={"stdout": "ok", "exit_code": 0}),
                                                        "Action: wait()")], observation(2))
    assert following[:len(current) - 1] == current[:-1]
    latest_text = current[-1]["content"][0]["text"]
    history_text = following[len(current) - 1]["content"][0]["text"]
    assert latest_text.startswith(history_text)
    assert "Accessibility tree" in latest_text and "Accessibility tree" not in history_text
    assert "stdout:\nok" in history_text
    assert image_count(current) == image_count(following) == 1
    assert following[-1]["content"][-1] == {"type": "image", "uri": "s3://frames/2.webp"}


def test_the_first_message_carries_the_task_and_nodes_use_model_coordinates():
    messages = text_builder().messages([], observation(0, user_reply="Only March."))
    text = messages[1]["content"][0]["text"]
    assert text.startswith(f"Task: {INSTRUCTION}\n\nStep 0.\n\nUser reply: Only March.")
    assert '[cell_b2] cell "B2" at (500,500)' in text


def test_sliding_window_keeps_the_last_three_screenshots():
    builder = text_builder("sliding_window")
    past_turns = [PastTurn(observation(index), "Action: wait()") for index in range(4)]
    messages = builder.messages(past_turns, observation(4))
    assert image_count(messages) == 3
    assert image_count(text_builder().messages(past_turns, observation(4))) == 1


def test_text_action_space_goes_into_the_system_prompt():
    builder = text_builder()
    assert builder.tools == ()
    assert "1000 by 1000 grid" in builder.system_prompt and "finished(content='answer')" in builder.system_prompt
    adapter = get_adapter("computer_tool")
    tool_builder = PromptBuilder("linux", INSTRUCTION, GEOMETRY, adapter.tool_definitions(["computer", "control"],
                                                                                          GEOMETRY))
    assert [tool["name"] for tool in tool_builder.tools][:2] == ["computer", "ask_user"]
    assert "Action space" not in tool_builder.system_prompt


def response(text="", tool_calls=()):
    return ChatResponse(text=text, tool_calls=tool_calls, served_model="my_policy")


def test_replies_take_the_shape_each_adapter_parses():
    assert adapter_output("text_actions", response("Action: wait()")) == "Action: wait()"
    function_call = {"name": "computer", "arguments": json.dumps({"action": "left_click", "coordinate": [10, 20]})}
    assert adapter_output("computer_tool", response(tool_calls=(function_call,))) == [
        {"name": "computer", "input": {"action": "left_click", "coordinate": [10, 20]}}]
    assert adapter_output("typed_actions", response(tool_calls=({"name": "done", "arguments": "{}"},))) == [
        {"type": "function_call", "name": "done", "arguments": "{}"}]
    native_item = {"type": "computer_call", "action": {"type": "wait"}}
    assert adapter_output("typed_actions", response(tool_calls=(native_item,))) == [native_item]
    with pytest.raises(ActionParseError):
        adapter_output("computer_tool", response(tool_calls=({"name": "computer", "arguments": "{oops"},)))


def test_history_replies_keep_tool_calls():
    assert assistant_reply(response("Thinking.")) == "Thinking."
    reply = assistant_reply(response("Thinking.", ({"name": "done", "arguments": "{}"},)))
    assert reply == 'Thinking.\n{"arguments": "{}", "name": "done"}'


@pytest.mark.parametrize("run_type, cache_mode", [("rl", "off"), ("eval", "read_write"), ("data", "off")])
def test_policy_requests_pin_the_model_and_cache_evals(example_payload, run_type, cache_mode):
    updates = {"type": run_type}
    if run_type != "rl":
        updates["policy"] = {**example_payload["run"]["policy"], "token_ids": False, "logprobs": False}
    document = rollout_document(example_payload, updates, {"split": "train" if run_type == "rl" else "eval"})
    gateway = ScriptedGateway()
    policy = build_policy(document, gateway)
    messages = ({"role": "user", "content": [{"type": "text", "text": "Task: x\n\nStep 0."}]},)
    policy.complete(messages, (), sample_index=3)
    request = gateway.requests[0]
    assert (request.model, request.sample_index, request.run_id, request.purpose) == ("my_policy", 3, "run_1", "policy")
    assert request.cache_mode == cache_mode
    assert request.require_token_ids == (run_type == "rl")


def test_an_empty_reply_is_a_parse_error(example_payload):
    policy = build_policy(rollout_document(example_payload), ScriptedGateway())
    with pytest.raises(ActionParseError):
        policy.parse(response("Thought: hmm"))
    assert [action.name for action in policy.parse(response("Action: type(content='1250')"))] == ["type"]
