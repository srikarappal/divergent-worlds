import re

from rlworld.specs import content_digest, document_digest, layer_digests, parse_spec_document, run_spec_digest, set_path


def test_digest_format(example_payload):
    digest = run_spec_digest(parse_spec_document(example_payload))
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)


def test_content_digest_ignores_key_order():
    assert content_digest({"b": 1, "a": [1, 2]}) == content_digest({"a": [1, 2], "b": 1})


def test_key_order_in_the_spec_does_not_change_digests(example_payload):
    reordered_payload = {key: example_payload[key] for key in reversed(list(example_payload))}
    original_document = parse_spec_document(example_payload)
    reordered_document = parse_spec_document(reordered_payload)
    assert document_digest(reordered_document) == document_digest(original_document)


def test_explicit_defaults_match_omitted_fields(example_payload):
    explicit_payload = set_path(example_payload, "task.max_steps", 50)
    explicit_digests = layer_digests(parse_spec_document(explicit_payload))
    assert explicit_digests == layer_digests(parse_spec_document(example_payload))


def test_changing_one_layer_changes_only_that_digest(example_payload):
    base_digests = layer_digests(parse_spec_document(example_payload))
    changed_digests = layer_digests(parse_spec_document(set_path(example_payload, "task.max_steps", 80)))
    assert changed_digests["task"] != base_digests["task"]
    for layer_name in ("image", "world", "run"):
        assert changed_digests[layer_name] == base_digests[layer_name]


def test_experiment_notes_do_not_change_the_run_digest(example_payload):
    base_document = parse_spec_document(example_payload)
    noted_document = parse_spec_document(set_path(example_payload, "experiment.notes", "rerun after verifier fix"))
    assert run_spec_digest(noted_document) == run_spec_digest(base_document)
    assert document_digest(noted_document) != document_digest(base_document)
