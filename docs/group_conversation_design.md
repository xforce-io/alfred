# Multi-Agent Group Conversation Design

## 1. Background

EverBot currently follows a 1:1 model: one channel session binds to one agent. This design introduces **Group Conversation** — a core-layer capability that allows multiple agents to participate in a shared conversation, each maintaining independent context.

### Design Principles

- **Core-layer abstraction**: Group conversation logic lives entirely in the core layer. Channels only handle identity presentation (e.g., Telegram uses multiple bot tokens, Web uses different avatars).
- **Independent sessions**: Each agent maintains its own session with its own system prompt, history, and context window. Other participants' messages are injected as observations, not mixed into the agent's native conversation.
- **Hybrid reply strategy**: Combines @mention (forced reply) with voluntary participation (agents decide whether to speak based on their own judgment).

---

## 2. Core Concepts

### 2.1 GroupConversation

A new first-class entity representing a multi-agent conversation space.

```python
@dataclass
class GroupConversation:
    group_id: str                      # Unique identifier, e.g. "group_abc123"
    participants: List[str]            # Agent names participating
    channel_type: str                  # "telegram", "web", "discord"
    channel_group_id: str              # Channel-side group identifier (e.g. Telegram chat_id)
    created_at: str
    config: GroupConfig                # Reply strategy, rate limits, etc.
```

### 2.2 GroupConfig

```python
@dataclass
class GroupConfig:
    reply_strategy: str = "hybrid"     # "hybrid" | "mention_only" | "round_robin"
    max_agent_turns_per_message: int = 3   # Prevent infinite agent-to-agent loops
    agent_reply_timeout: float = 30.0      # Per-agent reply timeout in seconds
    cooldown_seconds: float = 2.0          # Min interval between same agent's replies
```

### 2.3 Session Mapping

Each agent in a group maintains its own independent session:

```
GroupConversation (group_id="group_abc123")
  ├── agent "analyst"  → session "tg_session_analyst__-100123456"
  ├── agent "writer"   → session "tg_session_writer__-100123456"
  └── agent "critic"   → session "tg_session_critic__-100123456"
```

The session IDs follow the existing `ChannelSessionResolver` convention. Each session is a standard EverBot session — same locking, persistence, and context strategy.

---

## 3. Message Flow

### 3.1 User Message → Agent Responses

```
User sends "Analyze AAPL earnings" in group chat
    │
    ├── 1. Channel receives message, identifies group conversation
    │
    ├── 2. GroupRouter determines which agents should respond:
    │      ├── If @analyst mentioned → analyst is forced responder
    │      ├── All other participants receive the message as observation
    │      └── Non-mentioned agents decide independently whether to reply
    │
    ├── 3. For each responding agent (sequentially):
    │      ├── a. Compose message with group context prefix
    │      ├── b. ChannelCoreService.process_message() — standard turn pipeline
    │      ├── c. Agent response broadcast:
    │      │      ├── Send to channel (via agent's channel identity)
    │      │      └── Inject into other agents' mailboxes as observation
    │      └── d. Check: has max_agent_turns_per_message been reached?
    │
    └── 4. Secondary responses (agents reacting to other agents):
           ├── Non-mentioned agents evaluate whether to respond
           ├── Bounded by max_agent_turns_per_message
           └── Cooldown enforced per agent
```

### 3.2 Group Context Injection

When an agent receives a message in a group conversation, its user message is prefixed with group context. This uses the existing mailbox mechanism — no new injection path needed.

```
## Group Conversation Context
The following messages occurred in this group conversation since your last turn:

[User] Analyze AAPL earnings
[analyst] Based on the latest 10-Q filing, AAPL revenue grew 8% YoY...
[writer] Here's a draft summary for the newsletter: ...

## Your Turn
You are participating in a group conversation as "critic".
You may choose to respond if you have something meaningful to contribute, or stay silent.
The user's latest message: "What do you think about the analysis?"
```

This context is composed by `GroupContextStrategy` (a new `ContextStrategy` implementation) and injected via the standard `compose_message_with_mailbox_updates` path.

### 3.3 Agent Response as Observation

When agent A responds, its response is injected into other agents' sessions as a mailbox event:

```python
# Pseudo-code in GroupRouter
for other_agent in participants:
    if other_agent != responding_agent:
        await session_manager.append_mailbox_event(
            session_id=resolve_session(other_agent, group_id),
            event={
                "type": "group_message",
                "source": responding_agent,
                "content": agent_response_text,
                "timestamp": now_iso,
                "dedupe_key": f"group_{group_id}_{message_id}",
            }
        )
```

This leverages the existing `mailbox` field on `SessionData` and the existing `compose_message_with_mailbox_updates` mechanism. The key advantage: each agent sees other agents' messages as "background updates" in its own context, preserving its independent conversation framing.

---

## 4. Reply Strategy: Hybrid Mode

### 4.1 Rules

1. **@mention → forced reply**: If the user or another agent explicitly mentions `@agent_name`, that agent must respond.
2. **Voluntary participation**: All non-mentioned agents receive the message (via mailbox). On their next turn, they see the group context and can choose to respond or stay silent.
3. **Self-selection prompt**: Each agent's system prompt includes a group participation instruction:

```
You are participating in a group conversation with other agents.
When you receive group context, evaluate whether you have something
meaningful to contribute. If not, respond with exactly: [PASS]
A [PASS] response will not be sent to the group.
```

4. **Loop prevention**:
   - `max_agent_turns_per_message`: Hard cap on total agent responses per user message (default: 3)
   - `cooldown_seconds`: Minimum interval between same agent's consecutive replies
   - Agent-to-agent chains are bounded: after max turns, remaining agents only see the context on their next user-triggered turn

### 4.2 Turn Ordering

When a user message arrives:

1. **Forced responders first**: Agents explicitly @mentioned respond in mention order
2. **Voluntary round**: Remaining agents are polled in their `participants` list order. Each either responds or [PASS]es.
3. **Reactive round** (optional): If an agent's response triggers another agent (e.g., analyst provides data, writer wants to draft), allow one more round, still bounded by `max_agent_turns_per_message`.

Sequential ordering is intentional — later agents see earlier agents' responses, enabling more coherent group discussions.

---

## 5. Architecture

### 5.1 New Components

```
src/everbot/
├── core/
│   ├── channel/
│   │   ├── ...existing...
│   │   └── group_router.py          # GroupRouter: orchestrates multi-agent turns
│   ├── group/                        # New: group conversation management
│   │   ├── __init__.py
│   │   ├── models.py                 # GroupConversation, GroupConfig
│   │   ├── group_manager.py          # GroupManager: CRUD for group conversations
│   │   └── group_context.py          # GroupContextStrategy: compose group context
│   └── ...
```

### 5.2 GroupRouter

The central orchestration component. It sits between the Channel and `ChannelCoreService`:

```python
class GroupRouter:
    """Orchestrates multi-agent turns within a group conversation."""

    def __init__(
        self,
        group_manager: GroupManager,
        core_service: ChannelCoreService,
        agent_service: Any,
        session_manager: SessionManager,
    ):
        ...

    async def route_message(
        self,
        group: GroupConversation,
        user_message: str,
        user_id: str,
        on_agent_response: Callable[[str, str], Awaitable[None]],
            # (agent_name, response_text) -> send to channel
    ) -> None:
        """Route a user message through all participating agents.

        1. Determine forced responders (from @mentions)
        2. Execute forced responders sequentially
        3. Poll voluntary responders
        4. Enforce turn limits and cooldowns
        """
        ...
```

### 5.3 Integration with Existing Pipeline

```
Channel (e.g. TelegramChannel)
    │
    ├── Is this a group conversation?
    │     ├── No  → existing path: ChannelCoreService.process_message()
    │     └── Yes → GroupRouter.route_message()
    │                   │
    │                   ├── For each agent turn:
    │                   │     ├── Compose group context (GroupContextStrategy)
    │                   │     ├── ChannelCoreService.process_message()  ← reuse!
    │                   │     ├── Broadcast response to other agents' mailboxes
    │                   │     └── Callback: on_agent_response(agent_name, text)
    │                   │           └── Channel sends via agent's identity
    │                   │
    │                   └── Enforce max_agent_turns_per_message
    │
    └── Channel handles identity presentation per agent
```

Key point: `ChannelCoreService.process_message()` is reused as-is. GroupRouter only adds orchestration (ordering, broadcasting, loop prevention) around the existing turn pipeline.

### 5.4 GroupManager

Manages group conversation lifecycle and persistence:

```python
class GroupManager:
    """Manages group conversation state."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir  # e.g. ~/.alfred/groups/

    async def create_group(
        self,
        participants: List[str],
        channel_type: str,
        channel_group_id: str,
        config: Optional[GroupConfig] = None,
    ) -> GroupConversation:
        """Create a new group conversation."""
        ...

    async def get_group_by_channel(
        self,
        channel_type: str,
        channel_group_id: str,
    ) -> Optional[GroupConversation]:
        """Look up group by channel identifier."""
        ...

    async def add_participant(self, group_id: str, agent_name: str) -> None: ...
    async def remove_participant(self, group_id: str, agent_name: str) -> None: ...
    async def delete_group(self, group_id: str) -> None: ...
```

Persistence: JSON files at `~/.alfred/groups/{group_id}.json`. Simple and consistent with the existing `telegram_bindings.json` pattern.

---

## 6. Channel-Layer Responsibilities

The channel layer handles only identity presentation. Core logic is entirely channel-agnostic.

### 6.1 Telegram

Each agent is a separate Telegram bot. The channel maintains a mapping: `agent_name → bot_token`.

```yaml
everbot:
  channels:
    telegram:
      enabled: true
      bots:
        analyst:
          bot_token: "${TELEGRAM_BOT_TOKEN_ANALYST}"
        writer:
          bot_token: "${TELEGRAM_BOT_TOKEN_WRITER}"
        critic:
          bot_token: "${TELEGRAM_BOT_TOKEN_CRITIC}"
      default_agent: "analyst"    # For 1:1 chats
```

Group creation: When all bots are added to the same Telegram group, the channel detects this and creates a `GroupConversation` via `GroupManager`.

Message routing: Any bot that receives a user message forwards it to `GroupRouter`. The router's `on_agent_response` callback sends the response through the corresponding bot's API.

**Telegram-specific note**: Bots cannot see other bots' messages. This is fine — all inter-agent communication goes through the core-layer mailbox mechanism, not through Telegram's message stream.

### 6.2 Web

A single chat UI with different agent identities (name, avatar, color).

```
┌──────────────────────────────────────┐
│  Group: Market Analysis Team         │
├──────────────────────────────────────┤
│  [You] Analyze AAPL earnings         │
│                                      │
│  [Analyst] Based on the 10-Q...      │
│  [Writer] Draft summary: ...         │
│  [Critic] The analysis misses...     │
└──────────────────────────────────────┘
```

The `OutboundMessage.metadata` carries `agent_name`, and the Web frontend renders it with the appropriate identity.

### 6.3 Discord

Each agent as a separate bot, or use webhooks to post as different identities in the same channel.

---

## 7. Commands

### 7.1 Group Management Commands

| Command | Description |
|---------|-------------|
| `/group create <agent1> <agent2> ...` | Create a group conversation in the current chat |
| `/group add <agent>` | Add an agent to the current group |
| `/group remove <agent>` | Remove an agent from the current group |
| `/group list` | Show current group participants and config |
| `/group dissolve` | Dissolve the group, revert to 1:1 mode |
| `/group config <key> <value>` | Update group config (e.g., max turns) |

### 7.2 In-Conversation

| Syntax | Behavior |
|--------|----------|
| `@analyst what do you think?` | Force analyst to respond |
| `Hey everyone, thoughts?` | All agents evaluate, voluntary response |
| `/ask analyst writer` | Force multiple agents to respond |

---

## 8. Data Model Changes

### 8.1 New Files

- `~/.alfred/groups/{group_id}.json` — Group conversation metadata and config

### 8.2 Existing Models — No Changes

- `SessionData`: No schema changes. Group messages arrive through the existing `mailbox` field.
- `InboundMessage`: Add optional `group_id` to `metadata` dict (no schema change).
- `OutboundMessage`: Add `agent_name` to `metadata` dict for identity presentation (no schema change).

### 8.3 Session ID Convention

Group sessions follow the existing pattern but are per-agent:

```
tg_session_analyst__-100123456    # analyst's session in Telegram group -100123456
tg_session_writer__-100123456     # writer's session in the same group
```

The `channel_group_id` (e.g., `-100123456`) is shared, allowing `GroupManager` to look up which group a message belongs to.

---

## 9. Edge Cases and Safeguards

### 9.1 Infinite Loop Prevention

- **Hard cap**: `max_agent_turns_per_message` (default: 3) limits total agent responses per user message
- **Cooldown**: Same agent cannot respond twice within `cooldown_seconds`
- **[PASS] detection**: If an agent responds with `[PASS]`, it's suppressed from the group
- **Depth tracking**: GroupRouter tracks the current "depth" (user message → agent response → agent reaction) and stops at depth 2

### 9.2 Agent Failure Isolation

If one agent fails during its turn (timeout, error), GroupRouter:
1. Logs the failure
2. Sends an error indicator to the channel (e.g., "[analyst encountered an error]")
3. Continues with the next agent — other agents are not affected

### 9.3 Concurrent User Messages

If a user sends multiple messages rapidly while agents are still responding:
- Messages are queued per group (not per agent)
- The group-level queue ensures messages are processed in order
- Per-agent session locks (existing mechanism) prevent concurrent access to the same agent's session

### 9.4 Large Groups

For groups with many agents (5+), voluntary polling becomes expensive. Mitigation:
- Voluntary responses run with shorter timeouts
- Consider adding a `max_voluntary_responders` config to cap voluntary participation
- Agents that consistently [PASS] can be skipped in future rounds (adaptive)

---

## 10. Implementation Plan

### Phase 1: Core Models and GroupManager

1. Create `core/group/models.py` — `GroupConversation`, `GroupConfig`
2. Create `core/group/group_manager.py` — CRUD, persistence
3. Unit tests for group lifecycle

### Phase 2: GroupRouter and Context Strategy

1. Create `core/channel/group_router.py` — Orchestration logic
2. Create `core/group/group_context.py` — Group context composition
3. Integrate with `ChannelCoreService.process_message()` (no changes to core_service itself)
4. Unit tests with mock agents

### Phase 3: Telegram Multi-Bot Support

1. Extend `TelegramChannel` to support multiple bot tokens
2. Implement group detection (bots added to same Telegram group)
3. Wire up `/group` commands
4. End-to-end testing with real Telegram bots

### Phase 4: Web Channel Group Support

1. Extend Web frontend to render multi-agent conversations
2. Add group management UI
3. Wire up `OutboundMessage.metadata.agent_name` for identity rendering

---

## 11. Open Questions

1. **Should agents have awareness of who the other participants are?** Current design injects this via group context prefix. Alternative: add participant list to agent's system prompt.

2. **Persistent group context vs. per-turn injection?** Current design uses mailbox (per-turn). For long-running groups, agents may lose context of earlier messages. Consider a group-level summary mechanism.

3. **Agent addressing syntax**: `@agent_name` works in text, but should we also support structured mention (like Telegram's @username)?
