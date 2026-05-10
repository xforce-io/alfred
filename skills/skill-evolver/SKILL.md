---
name: skill-evolver
description: Adjust an installed skill's SKILL.md based on explicit user instruction. Use when the user wants to change how a skill behaves — phrases like "把 X 改成 Y", "调整 X", "X 报告太长", "优化 X 的 prompt", "make X output Y instead". One target skill per invocation.
version: "1.0.0"
tags: [meta, slm, skill-management]
---

# Skill Evolver

Rewrites a target skill's SKILL.md per explicit user instruction and publishes a new **testing** version. The auto SLM evaluation loop remains the safety net — bad rewrites get rolled back automatically on the next 2h Skill Evaluate cycle.

## When To Use

Trigger when the user expresses an explicit adjustment intent for a specific skill:
- "把 paper-discovery 改成只显示 5 条"
- "调整 gray-rhino 的 prompt"
- "X 这个报告太长了，改短点"
- "优化 X 输出格式"
- "make web skill use bing instead"

Do **not** trigger when:
- User is reporting a bug (use `fix` skill instead)
- User is asking what a skill does (just describe it)
- User intent is unclear (ask for confirmation first)

## Workflow

Three deterministic steps. Do them all in order — no shortcuts.

### Step 1 — prepare

```bash
python skills/skill-evolver/scripts/prepare.py \
  --workspace "$WORKSPACE_ROOT" \
  --skill <target-skill-id>
```

Returns JSON to stdout:
```json
{
  "current_skill_md": "<full current SKILL.md content>",
  "new_version": "<base>-userevolve-<YYYYMMDDHHMM>",
  "tmp_file": "/abs/path/to/tmp/skill-evolver-<skill>-<ts>.md"
}
```

Read all three values. The `tmp_file` is where you must write the new SKILL.md content in step 2.

### Step 2 — rewrite (you do this directly)

Take `current_skill_md` and modify it per the user's instruction:
- Apply the user's requested change to the relevant section
- **Update the `version:` field in the frontmatter to the `new_version` from step 1** (this is mandatory)
- Keep all unrelated parts intact

Save the rewritten content to `tmp_file` using `_bash` heredoc:
```bash
cat > <tmp_file> <<'SKILL_EOF'
---
name: <skill-id>
version: "<new_version>"
...rest of frontmatter and body...
SKILL_EOF
```

### Step 3 — commit

```bash
python skills/skill-evolver/scripts/commit.py \
  --workspace "$WORKSPACE_ROOT" \
  --skill <target-skill-id> \
  --version <new_version> \
  --content-file <tmp_file>
```

Returns:
```json
{"status": "ok", "skill": "<skill-id>", "version": "<new>", "current_pointer": "<new>"}
```

If commit fails (frontmatter mismatch, validation error), the script exits non-zero and emits an error JSON. **Do not retry without consulting the error message.**

## After Commit

Reply to the user with:
- The new version number
- A one-line summary of what you changed
- A note that this is in `testing` — automatic SLM eval will validate the change on the next cycle

Example:
> 已经把 paper-discovery 改成只显示前 5 条，新版本 `2.0.0-userevolve-202605101630`（testing）。下次跑就是新版；如果输出有问题，下一轮 Skill Evaluate 会自动回退到 stable。

## Notes

- One skill per invocation. If the user wants to change multiple skills, run this skill once per target.
- Rewriting goes through `VersionManager.publish` with `skill_lock` — concurrent auto evolves serialize cleanly.
- `consecutive_evolve_count` resets to 0 on publish (user-directed is explicit intent, shouldn't inherit auto-evolve failure history).
