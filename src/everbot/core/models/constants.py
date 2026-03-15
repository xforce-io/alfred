"""Shared constants used across core and infra layers."""

# Summary marker used to identify injected summary messages in conversation history.
# Referenced by both session compressor (core) and state adapters (infra).
SUMMARY_TAG = "[context_summary]"

# =============================================================================
# Network Timeouts (seconds)
# =============================================================================
TIMEOUT_FAST = 5.0          # Quick operations (session locks, health checks)
TIMEOUT_SHORT = 10.0        # Connection timeout, short API calls
TIMEOUT_MEDIUM = 30.0       # Standard API calls
TIMEOUT_LONG = 60.0         # Heavy operations (polling, streaming)
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
LIMIT_SHORT = 50            # Brief display (IDs, names)
LIMIT_MEDIUM = 80           # Standard display (descriptions)
LIMIT_LONG = 200            # Detailed display
LIMIT_DETAIL = 300          # Error messages, summaries
LIMIT_MESSAGE = 500         # Long messages
LIMIT_CONTENT = 1000        # Content preview
LIMIT_FULL = 1500           # Full content display
LIMIT_MAX = 2000            # Maximum safe display

# =============================================================================
# UI / Display
# =============================================================================
MAX_DISPLAY_ITEMS = 10      # Max items to show in lists
TYPING_INDICATOR_INTERVAL = 4.0  # Seconds between typing indicators
POLLING_ERROR_SLEEP = 5.0   # Sleep after polling error
DAEMON_IDLE_SLEEP = 60.0    # Daemon idle loop sleep
