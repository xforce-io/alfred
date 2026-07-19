---
name: web
description: Web search and browser automation. Use when the user asks to search the web, fetch page content, navigate websites, fill forms, take screenshots, or automate browser workflows.
---

# Web

Two capabilities in one skill: **search** and **browser**.

## When to Use What

- **Search**: User asks for news, prices, facts, or real-time information → use `search.py`
- **Browser**: User asks to open a page, click, fill forms, take screenshots, or interact with a website → start the browser server, then write Playwright scripts

Most tasks only need search. Only start the browser when you need to **interact** with a page.

---

## Search

```bash
S="python $SKILL_DIR/scripts/search.py"

# Basic search
$S "OpenAI latest news"

# News search
$S "Fed rate decision" --type news --timelimit d

# Extract top pages → material cards (summary + content_id), not full text
$S "Python PEP 723" --extract --extract-top 2 --output json

# Read one page of a cached extract (limit ≤ 6000 chars)
python $SKILL_DIR/scripts/read_extract.py \
  --content-id <64-hex> --offset 0 --limit 6000
```

Arguments: `--backend auto|ddgs|tavily`, `--type text|news`, `--max-results N`, `--timelimit d|w|m|y`, `--extract`, `--extract-top N`, `--visible-chars N` (default 4000), `--output text|json`, `--no-fallback`, `--full-extract` (**debug only**)

Env:

- `TAVILY_API_KEY` (optional, enables tavily backend)
- `ALFRED_WEB_EXTRACT_CACHE_DIR` (default `~/.alfred/cache/web-extract`)
- `ALFRED_WEB_EXTRACT_CACHE_MAX_BYTES` (default 128 MiB)
- `ALFRED_WEB_EXTRACT_VISIBLE_CHARS` (default 4000 when CLI not set)

### Material cards (default extract output)

With `--extract`, stdout is a **short structured material card**, not the full page body:

- Fields: title, url, snippet, `extract.preview`, `extract.content_id`, `extract.chars_full`, `extract.read_command`, `stats`
- All LLM-visible free text (`snippet` + `extract.preview`) shares a budget: default **≤ 4000** characters (`--visible-chars` / `ALFRED_WEB_EXTRACT_VISIBLE_CHARS`)
- JSON is authoritative (`schema_version: 2`). `extracted_text` is a **deprecated** alias of `extract.preview` (compat only)
- `materials_hint`: `extract_available` | `no_extract`
  - `extract_available` only means **readable extract material exists**
  - It does **not** mean the user's question is fully covered

### Full text via content_id (paged read)

1. Take `content_id` (`sha256:<hex>`) from a successful extract
2. Page with `read_extract.py --content-id <hex> --offset N --limit 6000` (max limit **6000**)
3. Loop `offset += limit` until `eof` / empty page; concatenate pages to restore the full body
4. When quoting a passage, **cite the page's runtime objectId** from that `run_command` call — do **not** treat the summary-card objectId as a full-text handle

**Never** `cat` cache files, invent filesystem paths, or pass `--all`. The reader only accepts a validated SHA-256 `content_id` and a safe range. Absolute cache paths are never printed.

### Stop searching when materials suffice (agent judgment)

After you already have **≥1 useful extract** (`materials_hint=extract_available`) and the materials cover:

- key entities / claims needed for the task
- freshness when time-sensitive
- source diversity when required

→ **stop** issuing more search queries and move to analysis. Coverage is judged by the agent, not by a forced binary “done” flag. Do not keep rotating queries just because more pages exist.

### Relationship to #160 (run_command projection)

Milkie/run_command may still apply a generic projection (e.g. `tail@8000`) and a raw stream cap (~30k). This skill **proactively** keeps default visible free text ≤ 4000 and requires paged reads for long bodies so agents do not rely on stuffing full extracts into one stdout.

### Debug escape hatch

`--full-extract` dumps large extract text into stdout for offline debugging only. **Do not use it in agent runtime** as a cite/trace path — it inflates context and may still be truncated by #160 projection.

---

## Browser

### Start server first

```bash
$SKILL_DIR/server.sh &
```

Wait for the `Ready` message. First run installs dependencies and Chromium.

### ⚠️ Rules — do NOT break these

1. **Never use `~/Library/Application Support/Google/Chrome/...` or your system Chrome profile.** It's locked by the live Chrome app, and tilde doesn't expand inside double quotes — you'll silently get a brand-new empty profile and loop on timeouts. Always use the server's persistent profile at `$SKILL_DIR/profiles/browser-data` (automatic via `connect()`).
2. **Never call `playwright` CLI directly** (`playwright screenshot`, `playwright install`, etc.). Always go through `connect()` so you share the running server's browser context and its persisted cookies.
3. **Never try to "repair" the browser by reinstalling chromium or pkill-ing `Google Chrome for Testing`.** If `connect()` fails, read `skills/web/server.log`, check `lsof -iTCP:9222` — `localhost:teamcoherence` in `lsof` output **is** port 9222, don't kill it.
4. **If a site needs login** (x.com, twitter.com, github private, etc.) and the first snapshot shows only `Sign in / Sign up / Join today` — **stop and tell the user**. Do NOT fabricate content from search results and present it as the page content. Ask the user to log in once via the persistent profile; thereafter headless sessions reuse the cookies.
5. **Don't retry the same failing command with only the timeout changed.** After 2 identical failures, change strategy or report the failure.
6. **Never inspect or kill browser PIDs from an agent task.** Use `bash "$SKILL_DIR/server.sh" status` and at most one `start` or `restart`; lifecycle ownership belongs to `server.sh`.

### Run scripts

**CRITICAL: Always run from `skills/web/`** (the `@/` import alias requires it).

```bash
cd skills/web && npx tsx <<'EOF'
import { connect, waitForPageLoad } from "@/client.js";

const client = await connect();
const page = await client.page("main");
await page.setViewportSize({ width: 1280, height: 800 });

await page.goto("https://example.com");
await waitForPageLoad(page);

console.log({ title: await page.title(), url: page.url() });
await client.disconnect();
EOF
```

### Key methods

```typescript
const client = await connect();
const page = await client.page("name");    // get or create named page
const pages = await client.list();          // list all pages
await client.close("name");                 // close a page
await client.disconnect();                  // disconnect (pages persist)

// ARIA snapshot for element discovery
const snapshot = await client.getAISnapshot("name");
const element = await client.selectSnapshotRef("name", "e5");
```

The `page` object is a standard Playwright Page. Pages persist across scripts.

### Screenshots

```typescript
await page.screenshot({ path: "tmp/screenshot.png" });
```

### Element discovery (ARIA snapshot)

Use `getAISnapshot()` when you don't know the page layout:

```typescript
const snapshot = await client.getAISnapshot("main");
console.log(snapshot);
// Output: - link "Login" [ref=e1]  - button "Submit" [ref=e2]  ...

const el = await client.selectSnapshotRef("main", "e1");
await el.click();
```

### Workflow

1. Write a small script to do ONE thing
2. Run it, check the output
3. Repeat until done
