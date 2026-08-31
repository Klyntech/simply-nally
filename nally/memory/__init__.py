"""Memory — explicit long-term knowledge about users.

Usage:
    from nally.memory import MemoryManager

    mm = MemoryManager(user_id)
    context = mm.build_context_block(user_message)
    response = mm.handle_memory_command(user_text)
"""

from .manager import MemoryManager
from .models import MemoryRecord, MemoryType
from .store import MemoryStore

__all__ = ["MemoryManager", "MemoryRecord", "MemoryStore", "MemoryType"]
