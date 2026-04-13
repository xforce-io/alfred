# Alfred EverBot

English | [中文](README.md)

**Ever Running Bot** — An always-on personal AI Agent platform.

EverBot keeps your AI Agent running like a real assistant: it proactively executes tasks, communicates with you through multiple channels, and continuously accumulates memory over interactions. Just define your Agent's behavior and tasks in Markdown, and let EverBot handle the rest.

## Features

- **Always On**: Background daemon process, Agent available 24/7 with built-in watchdog
- **Heartbeat Driven**: Cron / Interval scheduled self-wake, proactively advances pending tasks, timezone-aware with night-mode throttling
- **Multi-Channel**: Web UI (FastAPI + WebSocket) + Telegram Bot, chat anytime anywhere
- **Skill System**: Extensible plugin-based skills (code review, browser automation, investment analysis, paper discovery, etc.)
- **Skill Lifecycle**: Automated skill evaluation → decision → update cycle with canary deployment and rollback
- **Persistent Memory**: Conversation history auto-persisted, LLM auto-extracts key facts to MEMORY.md
- **Markdown Driven**: Define persona with AGENTS.md, define tasks with HEARTBEAT.md — WYSIWYG

## Quick Start

```bash
git clone <repo-url> alfred && cd alfred
bin/setup                        # Install environment (Python 3.10+ required)
source .venv/bin/activate        # Activate virtual environment
./bin/everbot init my_agent      # Create Agent (auto-registered to config)
./bin/everbot start              # Start (daemon + Web)
```

> If system default Python is not 3.10+, specify the version: `PYTHON=python3.11 bin/setup`

> See [QUICKSTART.md](QUICKSTART.md) for full steps including Telegram configuration.

## CLI Commands

```bash
# Initialization
./bin/everbot init [agent_name]           # Create Agent workspace

# Start and manage
./bin/everbot start                       # Start daemon + web (background)
./bin/everbot stop                        # Stop all services
./bin/everbot status                      # View status

# Other
./bin/everbot list                        # List all Agents
./bin/everbot doctor                      # Environment check (config/skills/deps/workspace)
./bin/everbot heartbeat --agent my_agent  # Manually trigger heartbeat
./bin/everbot heartbeat --agent my_agent --force  # Ignore active hours restriction
./bin/everbot config --show               # Show current configuration
./bin/everbot config --init               # Initialize default configuration
./bin/everbot migrate-agent --agent my_agent  # Migrate/fix agent.dph

# Skill management
./bin/everbot skills list                 # List all skills
./bin/everbot skills search <query>       # Search skills
./bin/everbot skills install <source>     # Install skill
./bin/everbot skills update [skill_name]  # Update skill
./bin/everbot skills remove <skill_name>  # Remove skill
./bin/everbot skills enable <skill_name>  # Enable skill
./bin/everbot skills disable <skill_name> # Disable skill
```

Runtime files:
- `~/.alfred/everbot.pid`: Daemon PID file
- `~/.alfred/everbot-web.pid`: Web process PID file
- `~/.alfred/everbot.status.json`: Daemon status snapshot (for `status`/Web)
- `~/.alfred/logs/everbot.out`: Daemon log
- `~/.alfred/logs/everbot-web.out`: Web server log
- `~/.alfred/logs/heartbeat.log`: Heartbeat log

## Skill System

EverBot extends Agent capabilities through pluggable skill modules. Each skill is an independent directory containing `SKILL.md` (documentation) and implementation code.

| Skill | Purpose | Key Features |
|-------|---------|-------------|
| **routine-manager** | Task scheduling | Cron/Interval scheduling, timezone-aware, execution mode config |
| **invest** | Unified investment analysis | Multi-source signals (macro liquidity, value scoring, China market, box breakout), causal chains, probability distributions |
| **gray-rhino** | Risk trend alerts | News clustering, gray rhino identification, asset impact mapping |
| **daily-attractor** | Daily market monitoring | Financing pricing deviation, attractor tracking, Telegram push |
| **paper-discovery** | AI/ML paper discovery | HuggingFace + arXiv integration, heat scoring, GitHub star ranking |
| **dev-browser** | Browser automation | Persistent page state, ARIA snapshots, screenshot capability |
| **web** | Web search & browser | Multi-backend search (DuckDuckGo/Tavily), page extraction, Playwright automation |
| **skill-installer** | Dynamic skill management | Registry installation, multi-source support (Git/URL/local) |
| **ops** | Operations & observability | Status monitoring, heartbeat management, log analysis, lifecycle management, diagnostics |
| **trajectory-reviewer** | Trajectory review | Log analysis, failure detection, loop identification, latency pinpointing |

## Usage Examples

### Example 1: Basic Demo

```bash
# Run basic demo (showcases all features)
PYTHONPATH=. python examples/everbot_demo.py
```

### Example 2: Real Agent Conversation

```bash
# Create and chat with a real Agent
PYTHONPATH=. python examples/real_agent_demo.py

# View Agent info
PYTHONPATH=. python examples/real_agent_demo.py info
```

### Example 3: Programmatic Usage

```python
from src.everbot import create_agent, UserDataManager
from pathlib import Path

# Initialize
user_data = UserDataManager()
user_data.init_agent_workspace("my_agent")

# Create Agent
agent_dir = user_data.get_agent_dir("my_agent")
agent = await create_agent("my_agent", agent_dir)

# Chat
async for event in agent.continue_chat(message="Hello!", stream_mode="delta"):
    # Process response...
    pass
```

## Workspace Files

### AGENTS.md - Behavior Spec

Defines Agent's identity, responsibilities, and communication style:

```markdown
# Agent Behavior Spec

## Identity
You are XXX assistant, responsible for...

## Core Responsibilities
1. ...
2. ...

## Communication Style
- Concise and professional
- Data-driven
```

### HEARTBEAT.md - Task Checklist

Defines periodically executed tasks:

```markdown
# Heartbeat Tasks

## Todo
- [ ] Task 1
- [ ] Task 2

## Completed
- [x] Task 0 (2026-02-01)
```

### agent.dph - Agent Definition

Dolphin-format Agent definition file with variable injection:

```
'''
Agent Name

$workspace_instructions
''' -> system

/explore/(model="$model_name", tools=[_bash, _python])
...
-> answer
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run by type
python -m pytest tests/unit/ -v           # Unit tests (isolated, no external deps)
python -m pytest tests/integration/ -v    # Integration tests (cross-module, may need network)
python -m pytest tests/e2e/ -v            # End-to-end tests (WebSocket/API)

# Use unified runner script
tests/run_tests.sh unit                   # Run unit tests
tests/run_tests.sh all --coverage         # Run all tests with coverage

# Run specific test
python -m pytest tests/unit/test_agent_factory.py -v
```

Test coverage (~98 test files):

- **Unit tests** (~64): Agent factory, session management, memory system, channel routing, heartbeat constraints, Telegram security, web auth, skill loading, process management, workflow engine, etc.
- **Integration tests** (~18): Daemon lifecycle, session recovery & locks, heartbeat execution flow, deep review flow, workspace instruction recovery, token optimization, workflow E2E, etc.
- **CLI tests** (~3): CLI entry points, session management, chat commands
- **E2E tests** (~7): WebSocket conversations (normal/interrupted/multi-session), session reset API, fault fallback, ops E2E

## Architecture

```
EverBot Daemon
    │
    ├── AgentFactory ─────────► DolphinAgent (LLM)
    │                               │
    │                          SkillKit (skill loading)
    │
    ├── HeartbeatRunner (Agent A)
    │   ├── Read HEARTBEAT.md
    │   ├── RoutineManager (Cron/Interval scheduling)
    │   ├── Inspector (execution monitoring, periodic push)
    │   ├── Inject Context
    │   ├── Execute Agent
    │   └── Persist Session
    │
    ├── Skill Lifecycle Manager (SLM)
    │   ├── SegmentLogger (evaluation segment logging)
    │   ├── LLM Judge (scoring & evaluation)
    │   ├── DecisionEngine (update/rollback decisions)
    │   └── VersionManager (version tracking)
    │
    ├── MemorySystem
    │   ├── MemoryExtractor (LLM-based key fact extraction)
    │   ├── MemoryMerger (dedup & merge)
    │   └── MemoryStore (persist to MEMORY.md)
    │
    ├── ChannelService
    │   ├── SessionResolver (channel → Agent mapping)
    │   ├── TelegramChannel
    │   └── WebChannel (FastAPI + WebSocket)
    │
    ├── SessionManager (JSONL persistence + concurrency locks)
    │
    └── Web Dashboard (FastAPI)
        ├── Chat API (WebSocket real-time conversation)
        ├── Agent/Session management API
        └── API Key authentication
```

Core components:

- **AgentFactory**: Creates and initializes Dolphin Agents with workspace instruction injection
- **UserDataManager**: Unified data management (workspace, config, logs)
- **WorkspaceLoader**: Workspace file loading (AGENTS.md, HEARTBEAT.md, MEMORY.md, USER.md)
- **SessionManager**: Session management (JSONL persistence, concurrency locks, session recovery)
- **MemoryManager**: Long-term memory management (fact extraction → dedup → merge → archive)
- **ChannelService**: Multi-channel access (Telegram Bot, Web UI)
- **RoutineManager**: Task scheduling (Cron expressions, Interval, timezone-aware, night throttling)
- **HeartbeatRunner**: Heartbeat executor (task reading, context injection, result persistence)
- **Inspector**: Execution monitoring (periodic force push, silence detection)
- **SLM**: Skill Lifecycle Management (evaluation → scoring → decision → version tracking)
- **EverBotDaemon**: Daemon main logic (multi-Agent management, signal handling, status snapshots)

## Project Structure

```
alfred/
├── src/everbot/              # EverBot core modules
│   ├── cli/                  # CLI entry points
│   ├── channels/             # Channel adapter layer
│   ├── web/                  # Web service (FastAPI + WebSocket)
│   │   ├── app.py            # FastAPI application
│   │   ├── auth.py           # API Key authentication
│   │   ├── services/         # Agent/Chat service layer
│   │   ├── static/           # Static assets
│   │   └── templates/        # Page templates
│   ├── core/                 # Business logic
│   │   ├── agent/            # Agent factory & Dolphin SDK integration
│   │   ├── channel/          # Multi-channel access (Telegram, Web)
│   │   ├── jobs/             # Background jobs
│   │   ├── memory/           # Memory system (extraction, merging, storage)
│   │   ├── models/           # System event models
│   │   ├── runtime/          # Heartbeat execution, Turn orchestration, Scheduler, Inspector
│   │   ├── scanners/         # Scanners (task discovery, etc.)
│   │   ├── session/          # Session persistence, compression, history management
│   │   ├── slm/              # Skill Lifecycle Management (SLM)
│   │   ├── tasks/            # Task scheduling (RoutineManager)
│   │   └── workflow/         # Workflow engine
│   └── infra/                # Infrastructure (config, workspace, process management)
│
├── skills/                   # Extensible skill modules
│   ├── routine-manager/      # Task scheduling
│   ├── invest/               # Unified investment analysis (macro liquidity, value scoring, China market signals)
│   ├── gray-rhino/           # Risk trend alerts
│   ├── daily-attractor/      # Daily market monitoring
│   ├── paper-discovery/      # AI/ML paper discovery
│   ├── dev-browser/          # Browser automation
│   ├── web/                  # Web search & browser automation
│   ├── skill-installer/      # Dynamic skill installation
│   ├── ops/                  # Operations & observability
│   └── trajectory-reviewer/  # Trajectory review
│
├── tests/                    # Tests (~98 test files)
│   ├── unit/                 # Unit tests (isolated, no external deps)
│   ├── integration/          # Integration tests (cross-module, may need network)
│   ├── cli/                  # CLI tests
│   └── e2e/                  # End-to-end tests (WebSocket/API)
│
├── docs/                     # Design & technical documentation
│   ├── EVERBOT_DESIGN.md     # Architecture design
│   ├── runtime_design.md     # Runtime design
│   ├── memory_system_design.md # Memory system design
│   ├── channel_design.md     # Multi-channel design
│   ├── skill_lifecycle_design.md # Skill lifecycle design
│   ├── SKILLS_GUIDE.md       # Skill development guide
│   ├── glossary.md           # Glossary
│   └── skills/               # Skill-level design docs
│
├── examples/                 # Usage examples
│   ├── everbot_demo.py       # Basic feature demo
│   └── real_agent_demo.py    # Real Agent conversation example
│
├── config/                   # Configuration templates
│   └── everbot.example.yaml  # EverBot config example
│
├── bin/                      # Executable scripts
│   ├── everbot               # CLI entry point
│   ├── everbot-watchdog      # Watchdog script
│   └── setup                 # Installation script
│
└── requirements.txt          # Python dependencies
```

## FAQ

### Q: How to change heartbeat interval?

A: Edit `~/.alfred/config.yaml`, modify `everbot.agents.<agent_name>.heartbeat.interval` (in minutes). You can also set `night_interval_minutes` to reduce frequency during night hours.

### Q: Will heartbeat tasks pollute user conversation history?

A: No. By default, `isolated` mode is used — heartbeats use a separate Session (`heartbeat_<agent_name>`).

### Q: Will history grow indefinitely?

A: No. `HistoryManager` automatically trims long histories, keeping the most recent 10 turns and archiving the rest to `MEMORY.md`. MemorySystem automatically extracts key facts and deduplicates.

### Q: How to view heartbeat logs?

A: Check `~/.alfred/logs/heartbeat.log`.

### Q: How to customize an Agent's .dph file?

A: Edit `~/.alfred/agents/<agent_name>/agent.dph` using Dolphin syntax to define Agent behavior.

### Q: How to develop custom skills?

A: Refer to [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md). Create a new directory under `skills/` containing `SKILL.md` and implementation code.

## Roadmap

- [ ] macOS launchd deep integration
- [ ] Metrics and monitoring alerts
- [ ] Multi-user permission management
- [ ] Skill marketplace (remote registry)
- [ ] Advanced memory features (RAG, vector retrieval)

## License

(TBD)

## References

- Architecture design: [docs/EVERBOT_DESIGN.md](docs/EVERBOT_DESIGN.md)
- Runtime design: [docs/runtime_design.md](docs/runtime_design.md)
- Memory system: [docs/memory_system_design.md](docs/memory_system_design.md)
- Multi-channel design: [docs/channel_design.md](docs/channel_design.md)
- Skill lifecycle: [docs/skill_lifecycle_design.md](docs/skill_lifecycle_design.md)
- Skill development: [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md)
