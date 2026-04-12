"""Storage backend implementations for MemPalace."""

from .base import BaseCollection
from .chroma import ChromaBackend, ChromaCollection

try:
    from .cloudflare import CloudflareBackend, CloudflareCollection
    __all__ = [
        "BaseCollection",
        "ChromaBackend",
        "ChromaCollection",
        "CloudflareBackend",
        "CloudflareCollection",
    ]
except ImportError:
    __all__ = ["BaseCollection", "ChromaBackend", "ChromaCollection"]
