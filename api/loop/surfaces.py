"""Output surface definitions for the routing layer.

Every piece of Fathom output has a destination surface. This module
defines the vocabulary so callers don't scatter string literals.

Priority ladder (most specific → most general):
  SUPPRESS      noise / already-addressed — emit nothing
  HELPER        executable task dispatched to a helper agent
  NOTIFICATION  urgent interrupt → feed alert bell
  FEED          curated, resonant, bubbled-up content
  CHAT          floor — ambient and conversational, default destination
"""

from enum import StrEnum


class Surface(StrEnum):
    CHAT = "chat"
    FEED = "feed"
    HELPER = "helper"
    NOTIFICATION = "notification"
    SUPPRESS = "suppress"
