"""The validation library, whichever one the running Home Assistant ships.

Home Assistant 2026.9 replaced ``voluptuous`` with ``probatio``, an API-compatible
fork. Importing the library from here keeps the integration working on both.
"""

from __future__ import annotations

try:
    import probatio as vol  # noqa: TID251 - Home Assistant 2026.9 and newer
except ImportError:  # pragma: no cover - only reachable on older Home Assistant
    import voluptuous as vol  # type: ignore[no-redef]  # noqa: TID251

__all__ = ["vol"]
