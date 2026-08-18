"""The in-app assistant: its tools, its safety gate, and its loop.

The properties worth testing are the ones that keep a local model from doing
damage: a write tool must not write, a bad document must come back as a
correctable error rather than a crash, and the loop must terminate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mediaengine.assistant import Assistant, ToolRegistry
from mediaengine.assistant.agent import _text_protocol_call, _tool_calls
from mediaengine.assistant.tools import MAX_RESULT_CHARS
from mediaengine.engine import MediaEngine

from .conftest import make_config, write_jpeg

GOOD_PACK = """
[pack]
id = "filters.france-landmarks"
name = "French landmarks"
display_name = "Landmark"
version = "1.0.0"
namespace = "place.landmark"
method = "vision"
accepts = ["image", "video"]
threshold = 0.45
description = "Well-known places in France."

[[label]]
name = "none"
display = "Not a French landmark"
hint = "no recognisable French landmark"

[[label]]
name = "eiffel-tower"
display = "Eiffel Tower"
aliases = ["tour eiffel", "paris tower"]
hint = "the wrought-iron lattice tower on the Champ de Mars in Paris"

[[label]]
name = "mont-saint-michel"
display = "Mont-Saint-Michel"
hint = "an island abbey on a tidal causeway in Normandy"
"""


@pytest.fixture()
def engine(tmp_path: Path) -> MediaEngine:
    root = tmp_path / "media"
    root.mkdir()
    write_jpeg(root / "PXL_20240108_072803.jpg", colour=(10, 20, 30))
    write_jpeg(root / "paris_trip_2019.jpg", colour=(200, 100, 50))
    config = make_config(tmp_path, root)
    instance = MediaEngine(config).start()
    instance.scan([root], generate_derivatives=False)
    yield instance
    instance.close()


@pytest.fixture()
def tools(engine: MediaEngine) -> ToolRegistry:
    return ToolRegistry(engine)


# ── read tools ───────────────────────────────────────────────────────────────


def test_overview_reports_what_is_actually_there(tools: ToolRegistry) -> None:
    result = tools.call("library_overview", {})
    assert result.ok
    assert result.payload["total_assets"] == 2
    assert result.payload["assets_by_media_type"]["image"] == 2
    assert any("filters.sport" in row for row in result.payload["installed_filter_packs"])


def test_read_results_stay_small_enough_for_a_4k_model(tools: ToolRegistry) -> None:
    """Tool output accumulates; a fat result crowds out the conversation."""
    for name, arguments in (
        ("library_overview", {}),
        ("list_filter_packs", {}),
        ("sample_filenames", {"limit": 40}),
        ("search_library", {"query": "", "limit": 25}),
    ):
        body = tools.call(name, arguments).as_text()
        assert len(body) <= MAX_RESULT_CHARS + 60, f"{name} returned {len(body)} chars"
        # Read results are the ones that repeat, so they must be much smaller
        # than the ceiling, which exists for the one-off template payload.
        assert len(body) < 1400, f"{name} returned {len(body)} chars"


def test_the_tool_schemas_fit_a_small_context(tools: ToolRegistry) -> None:
    """Schemas plus the system prompt are paid on every single turn."""
    import json as _json

    from mediaengine.assistant import SYSTEM_PROMPT

    overhead = len(_json.dumps(tools.schemas())) + len(SYSTEM_PROMPT)
    # ~4 chars per token. This is paid before a single word of conversation,
    # so it is a guardrail against future bloat rather than a target: history
    # trimming handles growth, but nothing can trim the fixed cost.
    assert overhead < 5400, f"{overhead} chars of fixed prompt overhead"


def test_history_is_trimmed_before_it_fills_the_window(engine: MediaEngine) -> None:
    """Tool output accumulates; on a small model it crowds out the question."""
    assistant, _ = _agent(engine, [])
    for index in range(30):
        assistant.messages.append({"role": "assistant", "content": f"step {index}"})
        assistant.messages.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": "x" * 900}
        )
    trimmed = assistant._trimmed()
    assert len(trimmed) < len(assistant.messages)
    assert trimmed[0]["role"] == "system"
    assert "dropped" in str(trimmed[1]["content"])
    # The newest exchange always survives.
    assert trimmed[-1]["content"] == assistant.messages[-1]["content"]


def test_a_trimmed_history_never_starts_with_an_orphan_tool_result(
    engine: MediaEngine,
) -> None:
    """A `tool` message whose request was dropped is a protocol error."""
    assistant, _ = _agent(engine, [])
    for index in range(20):
        assistant.messages.append({"role": "assistant", "content": "a" * 400})
        assistant.messages.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": "b" * 1200}
        )
    body = assistant._trimmed()[2:]
    assert body and body[0]["role"] != "tool"


def test_a_short_conversation_is_left_alone(engine: MediaEngine) -> None:
    assistant, _ = _agent(engine, [])
    assistant.messages.append({"role": "user", "content": "hello"})
    assert assistant._trimmed() == assistant.messages


def test_search_returns_a_count_and_samples(tools: ToolRegistry) -> None:
    result = tools.call("search_library", {"query": "paris"})
    assert result.ok
    assert result.payload["total"] == 1
    assert "paris_trip_2019.jpg" in str(result.payload["samples"])


def test_sample_filenames_is_how_it_learns_your_naming(tools: ToolRegistry) -> None:
    result = tools.call("sample_filenames", {"media_type": "image", "limit": 10})
    assert result.ok
    assert "PXL_20240108_072803.jpg" in result.payload["filenames"]


def test_reading_a_shipped_pack_gives_it_a_template(tools: ToolRegistry) -> None:
    result = tools.call("read_filter_pack", {"pack_id": "filters.sport"})
    assert result.ok
    assert "[pack]" in result.payload["toml"]


def test_reading_a_missing_pack_lists_the_real_ones(tools: ToolRegistry) -> None:
    result = tools.call("read_filter_pack", {"pack_id": "filters.nope"})
    assert not result.ok
    assert "filters.sport" in result.payload["available"]


def test_an_unknown_tool_is_reported_not_raised(tools: ToolRegistry) -> None:
    result = tools.call("rm_rf", {"path": "/"})
    assert not result.ok
    assert "no such tool" in result.payload["error"]


def test_bad_arguments_are_reported_not_raised(tools: ToolRegistry) -> None:
    result = tools.call("search_library", {"nonsense": 1})
    assert not result.ok
    assert "bad arguments" in result.payload["error"]


def test_results_are_capped_so_they_cannot_eat_the_context_window(
    tools: ToolRegistry,
) -> None:
    from mediaengine.assistant.tools import MAX_RESULT_CHARS, ToolResult

    result = ToolResult(True, {"blob": "x" * 50_000})
    assert len(result.as_text()) <= MAX_RESULT_CHARS + 40


# ── the safety gate ──────────────────────────────────────────────────────────


def test_writing_a_pack_does_not_write_anything(tools: ToolRegistry, engine: MediaEngine) -> None:
    """The whole safety model: a write tool stages, a human applies."""
    from mediaengine.filters import user_pack_dir

    result = tools.call(
        "write_filter_pack",
        {"pack_id": "filters.france-landmarks", "toml": GOOD_PACK, "reason": "test"},
    )
    assert result.ok
    assert result.pending is not None
    assert result.payload["status"] == "waiting for the user to approve"
    path = user_pack_dir(engine.config) / "filters-france-landmarks.toml"
    assert not path.exists()
    assert "filters.france-landmarks" not in engine.plugins.packs


def test_approving_is_what_writes(tools: ToolRegistry, engine: MediaEngine) -> None:
    result = tools.call(
        "write_filter_pack",
        {"pack_id": "filters.france-landmarks", "toml": GOOD_PACK, "reason": "test"},
    )
    token = result.payload["staged"]
    outcome = tools.apply(token)
    assert Path(outcome["path"]).is_file()
    assert "filters.france-landmarks" in engine.plugins.packs
    pack = engine.plugins.packs["filters.france-landmarks"]
    assert pack.namespace == "place.landmark"
    assert "eiffel-tower" in pack.label_names


def test_discarding_leaves_no_trace(tools: ToolRegistry, engine: MediaEngine) -> None:
    from mediaengine.filters import user_pack_dir

    result = tools.call(
        "write_filter_pack",
        {"pack_id": "filters.france-landmarks", "toml": GOOD_PACK, "reason": "test"},
    )
    tools.discard(result.payload["staged"])
    assert not (user_pack_dir(engine.config) / "filters-france-landmarks.toml").exists()
    assert not tools.pending


def test_a_change_cannot_be_applied_twice(tools: ToolRegistry) -> None:
    from mediaengine.errors import ConfigError

    result = tools.call(
        "write_filter_pack",
        {"pack_id": "filters.france-landmarks", "toml": GOOD_PACK, "reason": "test"},
    )
    token = result.payload["staged"]
    tools.apply(token)
    with pytest.raises(ConfigError):
        tools.apply(token)


def test_invalid_toml_comes_back_correctable(tools: ToolRegistry) -> None:
    result = tools.call(
        "write_filter_pack", {"pack_id": "filters.x", "toml": "this is not toml", "reason": ""}
    )
    assert not result.ok
    assert "TOML" in result.payload["error"]
    assert result.pending is None


def test_a_failed_write_teaches_the_format(tools: ToolRegistry) -> None:
    """The worked example rides on the failure, not on every turn's prompt."""
    result = tools.call(
        "write_filter_pack", {"pack_id": "filters.x", "toml": "nope", "reason": ""}
    )
    assert not result.ok
    assert "[[label]]" in result.payload["template"]
    assert "id = " in result.payload["template"]


def test_a_pack_that_fails_validation_says_why(tools: ToolRegistry) -> None:
    result = tools.call(
        "write_filter_pack",
        {
            "pack_id": "filters.x",
            "toml": '[pack]\nid = "filters.x"\nnamespace = "n"\nmethod = "telepathy"\n'
            '[[label]]\nname = "a"\n',
            "reason": "",
        },
    )
    assert not result.ok
    assert "method" in result.payload["error"]


def test_a_mismatched_id_is_refused(tools: ToolRegistry) -> None:
    result = tools.call(
        "write_filter_pack",
        {"pack_id": "filters.other", "toml": GOOD_PACK, "reason": ""},
    )
    assert not result.ok
    assert "declares id" in result.payload["error"]


def test_shipped_packs_cannot_be_overwritten(tools: ToolRegistry) -> None:
    """A model must not be able to quietly rewrite the NSFW screener."""
    body = GOOD_PACK.replace("filters.france-landmarks", "filters.safety-nsfw")
    result = tools.call(
        "write_filter_pack", {"pack_id": "filters.safety-nsfw", "toml": body, "reason": ""}
    )
    assert not result.ok
    assert "built-in" in result.payload["error"]


def test_fenced_markdown_is_tolerated(tools: ToolRegistry) -> None:
    """Models wrap documents in code fences no matter what the prompt says."""
    fenced = "```toml\n" + GOOD_PACK.strip() + "\n```"
    result = tools.call(
        "write_filter_pack",
        {"pack_id": "filters.france-landmarks", "toml": fenced, "reason": ""},
    )
    assert result.ok, result.payload


def test_enabling_and_running_are_also_staged(tools: ToolRegistry) -> None:
    enable = tools.call("set_filter_enabled", {"pack_id": "filters.sport", "enabled": True})
    assert enable.ok and enable.pending is not None
    run = tools.call("run_filter_pack", {"pack_id": "filters.origin", "limit": 5})
    assert run.ok and run.pending is not None
    assert len(tools.pending) == 2


def test_running_an_unknown_pack_is_refused(tools: ToolRegistry) -> None:
    assert not tools.call("run_filter_pack", {"pack_id": "filters.ghost"}).ok


def test_the_run_limit_is_clamped(tools: ToolRegistry) -> None:
    result = tools.call("run_filter_pack", {"pack_id": "filters.origin", "limit": 10_000_000})
    assert result.ok
    assert result.pending is not None
    assert "limit=500" in result.pending.preview


def test_no_tool_can_delete_anything(tools: ToolRegistry) -> None:
    """Deletion is not in the vocabulary; there is nothing to gate."""
    names = " ".join(spec.name for spec in tools.specs)
    assert "delete" not in names and "purge" not in names and "remove" not in names


def test_only_the_expected_tools_mutate(tools: ToolRegistry) -> None:
    mutating = {spec.name for spec in tools.specs if spec.mutating}
    assert mutating == {"write_filter_pack", "set_filter_enabled", "run_filter_pack"}


def test_every_tool_advertises_a_valid_schema(tools: ToolRegistry) -> None:
    for schema in tools.schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] and function["description"]
        assert function["parameters"]["type"] == "object"
        assert function["parameters"]["additionalProperties"] is False


# ── the loop ─────────────────────────────────────────────────────────────────


def _reply(content: str = "", *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": f"call{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (name, arguments) in enumerate(
                (call["name"], call["arguments"]) for call in calls
            )
        ]
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


class _FakeClient:
    """Serves canned completions in order, recording what it was sent."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.sent: list[dict[str, Any]] = []

    def choose_model(self, preferred: str | None = None) -> str:
        return preferred or "fake-model"

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return self.replies.pop(0) if self.replies else _reply("out of replies")


def _agent(engine: MediaEngine, replies: list[dict[str, Any]]) -> tuple[Assistant, _FakeClient]:
    assistant = Assistant(engine, model="fake-model")
    client = _FakeClient(replies)
    assistant._client = lambda: client  # type: ignore[method-assign]
    return assistant, client


def test_a_plain_answer_ends_the_loop(engine: MediaEngine) -> None:
    assistant, client = _agent(engine, [_reply("Two images, both photos.")])
    events = list(assistant.ask("what is in my library?"))
    assert [event.kind for event in events] == ["message"]
    assert events[0].text == "Two images, both photos."
    assert len(client.sent) == 1


def test_a_tool_call_is_run_and_fed_back(engine: MediaEngine) -> None:
    assistant, client = _agent(
        engine,
        [
            _reply(calls=[{"name": "library_overview", "arguments": {}}]),
            _reply("You have two images."),
        ],
    )
    kinds = [event.kind for event in assistant.ask("what is in my library?")]
    assert kinds == ["tool", "tool_result", "message"]
    # The second request carries the tool result back to the model.
    assert any(item.get("role") == "tool" for item in client.sent[1]["messages"])


def test_tool_schemas_are_offered_to_the_model(engine: MediaEngine) -> None:
    assistant, client = _agent(engine, [_reply("hi")])
    list(assistant.ask("hello"))
    assert client.sent[0]["tools"]
    assert client.sent[0]["tool_choice"] == "auto"


def test_a_staged_change_surfaces_as_a_pending_event(engine: MediaEngine) -> None:
    assistant, _ = _agent(
        engine,
        [
            _reply(
                calls=[
                    {
                        "name": "write_filter_pack",
                        "arguments": {
                            "pack_id": "filters.france-landmarks",
                            "toml": GOOD_PACK,
                            "reason": "landmarks",
                        },
                    }
                ]
            ),
            _reply("I have drafted it for you."),
        ],
    )
    events = list(assistant.ask("make a france pack"))
    pending = [event for event in events if event.kind == "pending"]
    assert len(pending) == 1
    assert pending[0].change is not None
    assert "eiffel-tower" in pending[0].change.preview
    assert pending[0].token in assistant.tools.pending


def test_approving_through_the_agent_tells_the_model(engine: MediaEngine) -> None:
    assistant, _ = _agent(
        engine,
        [
            _reply(
                calls=[
                    {
                        "name": "write_filter_pack",
                        "arguments": {
                            "pack_id": "filters.france-landmarks",
                            "toml": GOOD_PACK,
                            "reason": "landmarks",
                        },
                    }
                ]
            ),
            _reply("Drafted."),
        ],
    )
    token = next(e.token for e in assistant.ask("make it") if e.kind == "pending")
    assistant.approve(token)
    assert "filters.france-landmarks" in engine.plugins.packs
    assert "approved" in assistant.messages[-1]["content"]


def test_rejecting_tells_the_model_not_to_retry(engine: MediaEngine) -> None:
    assistant, _ = _agent(
        engine,
        [
            _reply(
                calls=[
                    {
                        "name": "write_filter_pack",
                        "arguments": {
                            "pack_id": "filters.france-landmarks",
                            "toml": GOOD_PACK,
                            "reason": "x",
                        },
                    }
                ]
            ),
            _reply("Drafted."),
        ],
    )
    token = next(e.token for e in assistant.ask("make it") if e.kind == "pending")
    assistant.reject(token)
    assert not assistant.tools.pending
    assert "declined" in assistant.messages[-1]["content"]


def test_a_claim_of_success_is_contradicted_when_nothing_was_staged(
    engine: MediaEngine,
) -> None:
    """Observed live: every write failed and the model still said it was done."""
    bad = {"pack_id": "filters.x", "toml": "[[label]]\nname='a'", "reason": ""}
    assistant, _ = _agent(
        engine,
        [
            _reply(calls=[{"name": "write_filter_pack", "arguments": bad}]),
            _reply("The pack has been staged for your approval."),
        ],
    )
    events = list(assistant.ask("make a pack"))
    assert events[-2].kind == "message"
    assert events[-1].kind == "error"
    assert "Nothing was staged" in events[-1].text


def test_a_successful_stage_is_not_contradicted(engine: MediaEngine) -> None:
    assistant, _ = _agent(
        engine,
        [
            _reply(
                calls=[
                    {
                        "name": "write_filter_pack",
                        "arguments": {
                            "pack_id": "filters.france-landmarks",
                            "toml": GOOD_PACK,
                            "reason": "x",
                        },
                    }
                ]
            ),
            _reply("Staged for you."),
        ],
    )
    events = list(assistant.ask("make it"))
    assert events[-1].kind == "message"


def test_resending_an_identical_failing_document_is_called_out(
    tools: ToolRegistry,
) -> None:
    """Returning the same error a third time teaches a small model nothing."""
    bad = {"pack_id": "filters.x", "toml": "[[label]]\nname='a'", "reason": ""}
    first = tools.call("write_filter_pack", bad)
    second = tools.call("write_filter_pack", bad)
    assert not first.ok and not second.ok
    assert "identical" not in first.payload["error"]
    assert "identical document 2 times" in second.payload["error"]
    assert tools.write_failures == 2


def test_a_model_that_never_stops_calling_tools_is_cut_off(engine: MediaEngine) -> None:
    """A local model that misreads a tool will loop forever given the chance."""
    replies = [_reply(calls=[{"name": "library_overview", "arguments": {}}]) for _ in range(30)]
    assistant, _ = _agent(engine, replies)
    assistant.max_steps = 4
    events = list(assistant.ask("go"))
    assert events[-1].kind == "error"
    assert "steps" in events[-1].text
    assert sum(1 for event in events if event.kind == "tool") == 4


def test_a_failing_tool_does_not_end_the_conversation(engine: MediaEngine) -> None:
    assistant, _ = _agent(
        engine,
        [
            _reply(calls=[{"name": "read_filter_pack", "arguments": {"pack_id": "nope"}}]),
            _reply("That pack does not exist."),
        ],
    )
    events = list(assistant.ask("read nope"))
    failed = [event for event in events if event.kind == "tool_result"]
    assert failed and failed[0].ok is False
    assert events[-1].kind == "message"


def test_an_answer_hidden_in_reasoning_content_is_still_read(engine: MediaEngine) -> None:
    reply = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "reasoning_content": "Two images."},
                "finish_reason": "stop",
            }
        ]
    }
    assistant, _ = _agent(engine, [reply])
    events = list(assistant.ask("count"))
    assert events[-1].kind == "message"
    assert events[-1].text == "Two images."


def test_reset_clears_history_and_staged_changes(engine: MediaEngine) -> None:
    assistant, _ = _agent(engine, [_reply("hi")])
    list(assistant.ask("hello"))
    assistant.tools.call(
        "write_filter_pack",
        {"pack_id": "filters.france-landmarks", "toml": GOOD_PACK, "reason": ""},
    )
    assistant.reset()
    assert len(assistant.messages) == 1
    assert not assistant.tools.pending


# ── protocol parsing ─────────────────────────────────────────────────────────


def test_native_tool_calls_are_normalised() -> None:
    calls = _tool_calls(
        {
            "tool_calls": [
                {"id": "a", "function": {"name": "search_library", "arguments": '{"query":"x"}'}},
                {"function": {"name": "library_overview", "arguments": {}}},
                {"function": {"name": "broken", "arguments": "not json"}},
                {"nonsense": True},
            ]
        }
    )
    assert [call["name"] for call in calls] == ["search_library", "library_overview", "broken"]
    assert calls[0]["arguments"] == {"query": "x"}
    assert calls[2]["arguments"] == {}


def test_a_text_protocol_call_is_recovered_from_prose() -> None:
    known = {"search_library"}
    found = _text_protocol_call(
        'I should look first.\n{"tool": "search_library", "arguments": {"query": "paris"}}',
        known,
    )
    assert found is not None
    assert found["name"] == "search_library"
    assert found["arguments"] == {"query": "paris"}


def test_the_last_json_object_wins() -> None:
    """Models reason about a call before settling on one."""
    text = (
        '{"tool": "search_library", "arguments": {"query": "first"}} ... on reflection '
        '{"tool": "search_library", "arguments": {"query": "second"}}'
    )
    found = _text_protocol_call(text, {"search_library"})
    assert found is not None and found["arguments"]["query"] == "second"


def test_unknown_names_are_not_recovered() -> None:
    assert _text_protocol_call('{"tool": "rm", "arguments": {}}', {"search_library"}) is None
    assert _text_protocol_call("no json here", {"search_library"}) is None
