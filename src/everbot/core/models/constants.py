"""Shared constants used across core and infra layers."""

# Summary marker used to identify injected summary messages in conversation history.
# Referenced by both session compressor (core) and state adapters (infra).
SUMMARY_TAG = "[context_summary]"

# =============================================================================
# Network Timeouts (seconds)
# =============================================================================
TIMEOUT_FAST = 5.0          # Quick operations (session locks, health checks)
TIMEOUT_MEDIUM = 30.0       # Standard API calls
TIMEOUT_UPLOAD = 120.0      # File upload operations

# =============================================================================
# Queue & Concurrency
# =============================================================================
QUEUE_MAX_SIZE = 100        # Main inbound queue
QUEUE_MAX_SIZE_PER_CHAT = 20  # Per-chat processing queue

# =============================================================================
# Retry Configuration
# =============================================================================
MAX_RETRIES = 3             # Default retry count for failed operations

# =============================================================================
# String Length Limits
# =============================================================================
LIMIT_CAPTION = 1024        # Telegram caption limit

# =============================================================================
# UI / Display
# =============================================================================
TYPING_INDICATOR_INTERVAL = 4.0  # Seconds between typing indicators
POLLING_ERROR_SLEEP = 5.0   # Sleep after polling error
POLLING_TIMEOUT = 10        # Telegram server-side long-poll timeout (s); shorter = less gvisor idle-drop risk
POLLING_MAX_CONSECUTIVE_ERRORS = 3  # Recreate httpx client after this many consecutive errors
DAEMON_IDLE_SLEEP = 60.0    # Daemon idle loop sleep
