"""MTProto folder-tag synchronizer for the TEXNIKACH Business account.

The package never sends messages and never marks dialogs as read.  It only
uses Telegram's dialog-filter methods to maintain visible manager tags.
"""

from .config import FolderSettings, FolderSpec

__all__ = ["FolderSettings", "FolderSpec"]
