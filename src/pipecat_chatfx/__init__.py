"""ChatFX Avatar for Pipecat: a real-time talking face for your Pipecat bot. See ChatFXVideoService."""

from .version import __version__
from .video import ChatFXException, ChatFXVideoService

__all__ = ["ChatFXVideoService", "ChatFXException", "__version__"]
