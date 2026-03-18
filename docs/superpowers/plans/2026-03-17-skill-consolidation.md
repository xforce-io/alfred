# Skill Consolidation: invest + memory-review

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce skill count from 15 to 12 by merging investment-signal/tushare into invest, and removing the redundant supplement phase from memory-review.

**Architecture:** Two independent changes: (1) Move investment-signal scripts and tushare docs into the invest skill directory, update path references in tools.py, delete the two standalone skill dirs. (2) Remove the supplement/re-extract phase from memory_review.py (steps 3 and 4 in run()), keeping consolidation + USER.md compression.

**Tech Stack:** Python, file moves, no new dependencies.

---

## File Structure

### Change A: Invest Consolidation

| Action | Path | Purpose |
|--------|------|---------|
| Move | `skills/investment-signal/scripts/*.py` → `skills/invest/scripts/signals/` | Signal scripts become subdirectory of invest |
| Move | `skills/investment-signal/references/thresholds.md` → `skills/invest/references/thresholds.md` | Keep reference docs |
| Move | `skills/tushare/references/` → `skills/invest/references/tushare/` | Tushare API docs accessible from invest |
| Modify | `skills/invest/scripts/tools.py:339,346` | Update 2 hardcoded paths |
| Modify | `skills/invest/SKILL.md` | Add brief note about signal scripts and tushare reference |
| Modify | `skills/investment-signal/scripts/signal_report.py:30` | Update sys.path for sibling imports |
| Delete | `skills/investment-signal/SKILL.md` | No longer a standalone skill |
| Delete | `skills/tushare/SKILL.md` | No longer a standalone skill |

### Change B: Memory-review Simplification

| Action | Path | Purpose |
|--------|------|---------|
| Modify | `src/everbot/core/jobs/memory_review.py` | Remove steps 3-4 (supplement), keep consolidation + compress |
| Modify | `tests/unit/test_self_reflection.py` | Update tests that reference supplement behavior |

---

### Task 1: Move investment-signal scripts into invest

**Files:**
- Move: `skills/investment-signal/scripts/{macro_liquidity,china_market_signal,value_investing,box_breakout,signal_report}.py` → `skills/invest/scripts/signals/`
- Move: `skills/investment-signal/references/thresholds.md` → `skills/invest/references/thresholds.md`

- [ ] **Step 1: Create target directories and move files**

```bash
mkdir -p skills/invest/scripts/signals
cp skills/investment-signal/scripts/macro_liquidity.py skills/invest/scripts/signals/
cp skills/investment-signal/scripts/china_market_signal.py skills/invest/scripts/signals/
cp skills/investment-signal/scripts/value_investing.py skills/invest/scripts/signals/
cp skills/investment-signal/scripts/box_breakout.py skills/invest/scripts/signals/
cp skills/investment-signal/scripts/signal_report.py skills/invest/scripts/signals/
mkdir -p skills/invest/references
cp skills/investment-signal/references/thresholds.md skills/invest/references/
```

- [ ] **Step 2: Fix signal_report.py sys.path after move**

In `skills/invest/scripts/signals/signal_report.py` line 30, the `sys.path.insert(0, os.path.dirname(...))` should still work since it adds its own directory. Verify no other sibling imports break.

- [ ] **Step 3: Update tools.py path references**

In `skills/invest/scripts/tools.py`, change lines 339 and 346:

```python
# Line 339: was
path = _repo_root() / "skills" / "investment-signal" / "scripts" / "macro_liquidity.py"
# becomes
path = Path(__file__).resolve().parent / "signals" / "macro_liquidity.py"

# Line 346: was
path = _repo_root() / "skills" / "investment-signal" / "scripts" / "china_market_signal.py"
# becomes
path = Path(__file__).resolve().parent / "signals" / "china_market_signal.py"
```

- [ ] **Step 4: Verify tools.py still works**

```bash
cd skills/invest && python scripts/tools.py status
```

Expected: No import errors. Status output (may show empty graph, that's fine).

- [ ] **Step 5: Commit move**

```bash
git add skills/invest/scripts/signals/ skills/invest/references/thresholds.md
git add skills/invest/scripts/tools.py
git commit -m "refactor(invest): move investment-signal scripts into invest/scripts/signals/"
```

---

### Task 2: Move tushare docs into invest

**Files:**
- Move: `skills/tushare/references/` → `skills/invest/references/tushare/`

- [ ] **Step 1: Copy tushare references**

```bash
cp -r skills/tushare/references/* skills/invest/references/tushare/
```

Note: creates `skills/invest/references/tushare/使用tushare.md`, `安装tushare.md`, `数据接口/...`

- [ ] **Step 2: Commit**

```bash
git add skills/invest/references/tushare/
git commit -m "refactor(invest): move tushare reference docs into invest/references/tushare/"
```

---

### Task 3: Delete standalone investment-signal and tushare skills

**Files:**
- Delete: `skills/investment-signal/` (entire directory)
- Delete: `skills/tushare/` (entire directory)

- [ ] **Step 1: Remove investment-signal directory**

```bash
git rm -r skills/investment-signal/
```

- [ ] **Step 2: Remove tushare directory**

```bash
git rm -r skills/tushare/
```

- [ ] **Step 3: Commit deletions**

```bash
git commit -m "refactor: remove investment-signal and tushare as standalone skills

These are now internal to the invest skill:
- investment-signal scripts → invest/scripts/signals/
- tushare reference docs → invest/references/tushare/"
```

---

### Task 4: Update invest SKILL.md

**Files:**
- Modify: `skills/invest/SKILL.md`

- [ ] **Step 1: Add signal scripts and tushare reference section**

Append before `## Notes`:

```markdown
## Signal Scripts

Lower-level signal modules live in `scripts/signals/` and are called internally by `scan`:

- `signals/macro_liquidity.py` — Net liquidity, SOFR, MOVE, JPY carry (needs `FRED_API_KEY`)
- `signals/china_market_signal.py` — Northbound flow, volume, margin (needs `TUSHARE_TOKEN`)
- `signals/value_investing.py` — US stock fundamentals (ROE, debt, FCF, moat, valuation)
- `signals/box_breakout.py` — Donchian channel breakout detection
- `signals/signal_report.py` — Aggregated signal report

These can also be run standalone for debugging:

```bash
python $SKILL_DIR/scripts/signals/macro_liquidity.py --format text
python $SKILL_DIR/scripts/signals/china_market_signal.py --format text
python $SKILL_DIR/scripts/signals/value_investing.py AAPL --format text
python $SKILL_DIR/scripts/signals/box_breakout.py AAPL --format text
```

## Tushare Reference

Tushare API documentation is available in `references/tushare/` for A-share, HK, futures, options, fund, and macro data interfaces.
```

- [ ] **Step 2: Commit**

```bash
git add skills/invest/SKILL.md
git commit -m "docs(invest): document signal scripts and tushare reference"
```

---

### Task 5: Remove supplement phase from memory_review.py

**Files:**
- Modify: `src/everbot/core/jobs/memory_review.py`

- [ ] **Step 1: Remove supplement phase (steps 3-4)**

Remove the `_detect_missed_sessions` function (lines 98-140) and the supplement block in `run()` (lines 47-63). The `run()` function should go directly from digest extraction to consolidation.

Updated `run()`:

```python
async def run(context: SkillContext) -> str:
    """Execute memory review: consolidate and compress."""
    scanner = SessionScanner(context.sessions_dir)
    state = ReflectionState.load(context.workspace_path)

    # 1. Get sessions: reuse gate result if available, otherwise query directly
    skill_wm = state.get_watermark("memory-review")
    if context.scan_result and context.scan_result.payload:
        sessions = context.scan_result.payload
    else:
        sessions = scanner.get_reviewable_sessions(skill_wm, agent_name=context.agent_name)
    if not sessions:
        return "No sessions to review"

    # 2. Extract digests, skip failed sessions
    digests, digest_session_ids = [], []
    last_successful_session = None
    for s in sessions:
        try:
            digests.append(scanner.extract_digest(s.path))
            digest_session_ids.append(s.id)
            last_successful_session = s
        except Exception as e:
            logger.warning("Failed to extract session %s: %s, skipping", s.id, e)
            continue

    if not digests:
        return "All sessions failed to extract"

    # 3. Consolidation analysis (single LLM call)
    existing = context.memory_manager.load_entries()
    review = await _analyze_memory_consolidation(context.llm, digests, existing)

    # 4. Apply consolidation + post-validation
    entries_before = len(existing)
    from ..memory.manager import IntegrityError
    try:
        review_stats = context.memory_manager.apply_review(review)
    except IntegrityError as e:
        logger.error("Memory consolidation integrity violation: %s", e)
        return f"IntegrityError: {e}"

    entries_after = len(context.memory_manager.load_entries())
    if entries_after > entries_before:
        logger.warning(
            "Entries increased after review (likely concurrent write): %d → %d",
            entries_before, entries_after,
        )

    # 5. Compress memories → USER.md
    compress_result = await _compress_to_user_profile(context)

    # 6. Advance watermark
    if last_successful_session:
        state.set_watermark("memory-review", last_successful_session.updated_at)
        state.save(context.workspace_path)

    return f"Memory review: {review_stats}, profile: {compress_result}"
```

- [ ] **Step 2: Remove the _detect_missed_sessions function entirely**

Delete lines 98-140 (the `_detect_missed_sessions` async function).

- [ ] **Step 3: Update module docstring**

```python
"""Memory review skill — consolidate and optimize agent memory.

Silent execution, no user notification.
Strategy: consolidate existing entries, then compress to USER.md profile.
"""
```

- [ ] **Step 4: Remove unused import if any**

The `List` import is still used by `_analyze_memory_consolidation`. No imports to remove.

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/test_self_reflection.py -x -v
```

Fix any test failures related to the removed supplement behavior.

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/jobs/memory_review.py
git commit -m "refactor(memory-review): remove redundant supplement phase

MemoryManager.process_session_end() already extracts memories at session save time.
The supplement/re-extract phase was duplicating this work with extra LLM calls.
Keep consolidation + USER.md compression which add independent value."
```

---

### Task 6: Update tests

**Files:**
- Modify: `tests/unit/test_self_reflection.py`

- [ ] **Step 1: Update test expectations**

Tests `test_memory_review_no_scan_result_queries_directly` and `test_memory_review_no_scan_result_empty_watermark` should still pass since they test the session scanning path which is unchanged.

The test `test_skill_prefers_scan_result_when_available` should also still work.

Verify no test references `_detect_missed_sessions` or `reextract_count`.

- [ ] **Step 2: Run full test suite**

```bash
pytest tests/unit/test_self_reflection.py tests/unit/test_skill_whitelist.py tests/unit/test_inspector.py -x -v
```

Expected: All pass.

- [ ] **Step 3: Commit if any test changes needed**

```bash
git add tests/
git commit -m "test: update tests for memory-review simplification"
```
