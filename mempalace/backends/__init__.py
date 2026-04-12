"""Storage backend implementations for MemPalace."""

from .base import BaseCollection

try:
    from .chroma import ChromaBackend, ChromaCollection
    _chroma_available = True
except ImportError:
    _chroma_available = False

try:
    from .cloudflare import CloudflareBackend, CloudflareCollection
    _cf_available = True
except ImportError:
    _cf_available = False

__all__ = ["BaseCollection"]
if _chroma_available:
    __all__ += ["ChromaBackend", "ChromaCollection"]
if _cf_available:
    __all__ += ["CloudflareBackend", "CloudflareCollection"]
