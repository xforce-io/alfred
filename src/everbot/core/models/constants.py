"""Shared constants used across core and infra layers."""

# Summary marker used to identify injected summary messages in conversation history.
# Referenced by both session compressor (core) and state adapters (infra).
SUMMARY_TAG = "[context_summary]"
