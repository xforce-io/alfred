# Bug Report: Unknown-tool call leaks prior LLM raw output back as `tool` message, causing self-reinforcing loop

## Summary

When the LLM emits a `tool_calls[*].function.name` that is **not registered** in the current toolkit, `explore_block_v2` does **not** feed a clean "tool not found" error back to the model. Instead, `_process_tool_result_with_hook()` falls back to the previous stage's raw output — which, in this code path, is the LLM's own pre-call streaming text — and `_append_tool_message()` writes that text into the conversation as `role="tool"`, `tool_call_id=<the unknown call>`.

The model then sees its own reasoning echoed back as if it were the tool's response. The thinking text often contains the model's own assertion that it must "call the X tool", which **reinforces the same bad call**. The next turn emits the same `tool_calls` with identical arguments. The cycle repeats until the runtime turn-budget (default `600s`) fires `RuntimeError: Turn exceeded 600s timeout`.

The user-visible failure is a 10-minute "typing…" indicator followed by a stack-trace reply. No real work was done; one turn burned ~650K input tokens across 11 LLM stages with 0 tool stages.

## Environment

| Field | Value |
|---|---|
| dolphin | `kweaver-dolphin 0.6.0` @ `cf2dc23` (`fix(code_block): pass tool kwarg to on_before_reply_app and rename local var`) |
| Host | Ubuntu, alfred everbot harness wrapping dolphin |
| LLM endpoint | `https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions` (Volcengine Ark; underlying Doubao/Seed family) |
| `model_name` reported | `gpt-4` (alias; not actually OpenAI) |
| Code block | `dolphin.core.code_block.explore_block_v2.ExploreBlockV2` |

## Reproduction

### Trigger

A normal user message to an agent whose toolkit only has `_bash / _python / _date / _read_file / _read_folder / _load_resource_skill / _get_cached_result_detail`, where the system prompt **lists `web` as a "resource skill"** that must be loaded via `_load_resource_skill("web")` first:

```
Amazing
https://thinkingmachines.ai/blog/interaction-models/
```

The model — perhaps because the system prompt also mentions a skill *named* `web` in a description block — emits an OpenAI-format tool call with `function.name = "web"` directly, ignoring the explicit warning that says:

> 直接 `function.name=<名字>` 是不存在的工具。

That is a **model-side** issue (separate from this bug), but the explosion that follows is dolphin-side.

### Observed timeline (from session trace `tg_session_alice__8576399597.json`)

| Stage | id | duration | in_tokens | out_tokens (per-stage counter) | status |
|---:|---|---:|---:|---:|---|
| 0 | `883fa374` | 96.3s | 57,851 | 0 | completed |
| 1 | `a54d98eb` | 47.4s | 58,862 | 0 | completed |
| 2 | `e92a4209` | 97.9s | 59,120 | 0 | completed |
| 3 | `06613f5d` | 86.7s | 59,324 | 0 | completed |
| 4 | `8e0a39ff` | 16.8s | 59,483 | 0 | completed |
| 5 | `e862b16a` | 71.2s | 59,718 | 0 | completed |
| 6 | `1e785151` | 75.4s | 59,997 | 0 | completed |
| 7 | `e86bf0c5` | 81.0s | 60,331 | 0 | completed |
| 8 | `1d4bdc4d` | — | 60,565 | 0 | **processing → killed at 600s** |

Aggregated from `context_trace.{llm_summary,tool_summary}`:

```
llm_summary  : total_stages=11  total_input_tokens=653,804  total_output_tokens=459  total_llm_time=772.6s
tool_summary : total_stages=0   total_tool_time=0
```

Per-iteration message growth in `input_messages`:

```
Stage 0 (50 msgs):  ...
                    msgs[49] = user        len=1774   ← user's "Amazing + URL"
Stage 1 (52 msgs):  ...
                    msgs[50] = assistant   len=0      tool_calls=[{name:"web", action:"fetch_page", url:...}, id:"call_w9rmd67k..."]
                    msgs[51] = tool        len=778    tool_call_id="call_w9rmd67k..."
                                                      content = "...用户现在发了一个链接...按照要求，函数调用用
                                                                  <seed:tool_call><function name=\"web\">
                                                                  <parameter name=\"action\" string=\"true\">fetch_page</parameter>
                                                                  <parameter name=\"url\" string=\"true\">https://...</paramet"  ← truncated mid-tag
Stage 2 (54 msgs):  + assistant tool_call (web, identical args, id "call_d127k677...")
                    + tool (the model's next thinking pass, also self-referential)
...
Stage 7 (64 msgs):  8 identical (assistant tool_call → tool) pairs accumulated
Stage 8           : timeout — turn_orchestrator raises asyncio.TimeoutError("Turn exceeded 600s timeout")
```

Every `tool_calls[0].function.arguments` was byte-identical:

```json
{"action": "fetch_page", "url": "https://thinkingmachines.ai/blog/interaction-models/"}
```

Only the call `id` rotated.

### Critically: the content fed back into the `tool` message slot

It is NOT an error string. It is the **LLM's own raw streamed output from the immediately-preceding LLM stage**, including:

- Chinese internal reasoning ("用户现在发了一个链接…首先我需要先获取这个页面的内容")
- Truncated Seed-style XML tool-call syntax (`<seed:tool_call><function name="web">…</paramet` cut mid-tag)
- Repeated self-instruction ("按照要求，函数调用用 …")

The model reads this on the next turn and concludes "yes, I should call `web`", and emits the same `tool_calls` again. This is the self-reinforcement engine.

## Root Cause

### Code path

`src/dolphin/core/code_block/explore_block_v2.py` calls `tool_run()` in `basic_code_block.py`. In `basic_code_block.py:1296`:

```python
async def tool_run(self, source_type, tool_name, skill_params_json={}, props=None):
    if self.context.is_toolkit_empty():
        self.context.warn(f"toolkit is None, tool_name[{tool_name}]")
        return

    tool = self.context.get_tool(tool_name)
    if not tool:
        from dolphin.lib.toolkits.system_toolkit import SystemFunctions
        tool = SystemFunctions.getTool(tool_name)

    if tool is None:
        async for result in self.yield_message(
            f"没有{tool_name}工具可以调用！", ""
        ):
            yield result
        return                                    # ← (A) early-return, no SKILL stage created,
                                                  #     no ※tool variable written,
                                                  #     no recorded tool output.
    ...
```

The early return on `tool is None` means **no new tool stage is created and no tool result is set**. Control returns to the caller `_dispatch_tool_call` in `explore_block_v2.py` (~line 620):

```python
async for resp in self.tool_run(
    source_type=SourceType.EXPLORE,
    tool_name=stream_item.tool_name,
    skill_params_json=(stream_item.tool_args or {}),
):
    yield ...

# Add tool response message
tool_response, metadata = self._process_tool_result_with_hook(stream_item.tool_name)

answer_content: str = (
    tool_response
    if tool_response is not None
    and not CognitiveToolkit.is_cognitive_tool(stream_item.tool_name)
    else ""
)
...
self._append_tool_message(tool_call_id, answer_content, metadata)   # ← (B) always appends a tool message
```

`_process_tool_result_with_hook` (`explore_block_v2.py:717`):

```python
def _process_tool_result_with_hook(self, skill_name):
    skill = self.context.get_tool(skill_name)
    if not skill:
        from dolphin.lib.toolkits.system_toolkit import SystemFunctions
        skill = SystemFunctions.getTool(skill_name)

    last_stage = self.recorder.getProgress().get_last_stage()
    reference  = last_stage.get_raw_output() if last_stage else None

    if reference and self.toolkit_hook and self.context.has_toolkit_hook():
        content, metadata = self.toolkit_hook.on_before_send_to_context(
            reference_id=reference.reference_id,
            tool=skill,                            # skill is None here when unknown
            toolkit_name=...,
            resource_tool_path=...,
        )
        return content, metadata

    return self.recorder.getProgress().get_step_answers(), {}
```

Because `tool_run()` did NOT create a SKILL stage, `get_last_stage()` returns the **previous LLM stage**, whose `raw_output` is the model's own pre-tool-call streaming text. That string is then returned as `tool_response` and written into the conversation as `role="tool"` content at (B).

So the chain is:
1. LLM emits `tool_calls=[{name:"web", ...}]` along with thinking text in the same stream.
2. `tool_run` finds no `web` tool, yields a user-facing warning ("没有web工具可以调用！") but does **not** record a tool result or create a SKILL stage.
3. `_process_tool_result_with_hook` falls back to the most recent stage's raw output, which is the LLM stage from step 1.
4. `_append_tool_message` writes that LLM raw text under the unknown tool's `tool_call_id`.
5. Next iteration, the LLM sees its own thinking as the tool's reply. The thinking literally says "I need to call the web tool"; it complies. Goto 1.
6. Loop terminates only when `turn_orchestrator.py:1126` raises `Turn exceeded 600s timeout`.

### Why "(A) early-return" is the load-bearing bug

If `tool_run` had appended a proper tool response like:

```
Tool 'web' not found. Available tools: _bash, _python, _date, _read_file,
_read_folder, _load_resource_skill, _get_cached_result_detail.
If you meant the 'web' resource skill, call _load_resource_skill("web") first.
```

…the LLM would have changed strategy on iteration 2 (we tested this hypothesis against similar models — a single explicit error message reliably breaks the cycle). The hallucinated tool name is the *trigger*; the missing error feedback is what makes it *self-reinforcing*.

## Suggested Fix

### Primary (required)

Make `tool_run()` write a proper tool error response in the unknown-tool branch instead of early-returning. Two equivalent shapes:

**Option A — emit at the caller (smaller diff):** in `explore_block_v2._dispatch_tool_call`, detect unknown tool *before* calling `tool_run` and append a tool message directly:

```python
tool = (self.context.get_tool(stream_item.tool_name)
        or SystemFunctions.getTool(stream_item.tool_name))
if tool is None:
    available = self.context.list_tool_names()  # or however toolkit lists them
    error = (
        f"Tool '{stream_item.tool_name}' not found. "
        f"Available tools: {', '.join(available)}."
    )
    self._append_tool_message(tool_call_id, error, metadata={"error": "unknown_tool"})
    return  # do not call _process_tool_result_with_hook
```

**Option B — emit inside tool_run (cleaner):** at `basic_code_block.py:1314`, replace the early return with creating a synthetic completed SKILL stage carrying the error text as `raw_output`, so the existing `_process_tool_result_with_hook` path naturally picks it up.

Either way, the invariant should be: **`role="tool"` content must come from a tool execution, not from a fallback to the previous LLM stage's raw text.**

### Secondary (defensive, recommended)

1. In `_process_tool_result_with_hook`, when the corresponding tool stage is missing or its status ≠ `completed`, return a sentinel like `"Tool produced no output."` instead of silently using the previous LLM stage's `raw_output`. The "last stage" fallback is unsafe by construction when stages can be of mixed types.

2. Add a loop-guard in `turn_orchestrator`: if the same `tool_calls` hash repeats ≥ N times (e.g. 3), inject a single corrective system message ("That tool call was rejected N times. Try a different tool or finalize your answer.") and force the next iteration to produce a terminal answer. This is a belt-and-suspenders measure to bound damage from any future variant of the same class of bug.

3. Consider not exposing resource-skill names in the system prompt's "Available …" section in a way that suggests they are directly callable. The current prompt does include a warning, but for at least some models (Doubao/Seed family observed here) the warning is insufficient. A schema-level fix (don't advertise `web` as a name at all unless `_load_resource_skill` has been called) eliminates the trigger entirely.

## Repro Asset

The full failing session is preserved at `~/.alfred/sessions/tg_session_alice__8576399597.json` on the affected host. The relevant subtree is `context_trace.call_chain[-1]`, which contains all 9 LLM stages with their full `input_messages` arrays, making it possible to replay the exact prompt sequence offline.

## Severity

**High** for any deployment where (a) the upstream model is Doubao/Seed or any other model prone to inventing tool names, and (b) the toolkit advertises resource skills by name. In our case it caused a complete 10-minute hang of one chat turn, ~650K wasted input tokens, and a poor UX (user retried "再试试", hit the same loop, gave up). No data corruption.
