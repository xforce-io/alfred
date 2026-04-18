# LaunchAgent Guide

Use LaunchAgent on macOS when you want EverBot to survive terminal/session exit.

## Install

```bash
./bin/everbot service-install
```

This writes `~/Library/LaunchAgents/com.alfred.everbot.plist`, loads it with `launchctl`, and starts the daemon automatically.

## Check Status

```bash
./bin/everbot service-status
```

## Uninstall

```bash
./bin/everbot service-uninstall
```

## Notes

- The LaunchAgent starts the daemon only. It does not start the Web server.
- `~/.env.secrets` is sourced automatically when present.
- Logs are written to `$ALFRED_HOME/logs/everbot.out` and `$ALFRED_HOME/logs/everbot.err`.
- The daemon workspace uses the current repository root as `PYTHONPATH` and `ALFRED_PROJECT_ROOT`.
