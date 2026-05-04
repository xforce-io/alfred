"""Structured memory system for EverBot agents.

Two memory layers share this package:

* **profile** — long-lived user portrait (``ProfileStore`` + ``ProfileExtractor``)
* **event** — time-anchored occurrences (``EventStore`` + ``EventExtractor``,
  added in a follow-up step)

``MemoryManager`` orchestrates both layers and is the only entry point that
external callers should depend on.
"""

from .event_extractor import EventExtractor
from .event_store import EventStore
from .manager import MemoryManager
from .merger import MemoryMerger
from .models import MemoryEntry
from .profile_extractor import ProfileExtractor
from .profile_store import ProfileStore

__all__ = [
    "MemoryEntry",
    "MemoryMerger",
    "MemoryManager",
    "ProfileStore",
    "ProfileExtractor",
    "EventStore",
    "EventExtractor",
]
