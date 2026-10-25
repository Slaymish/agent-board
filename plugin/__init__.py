"""Agent board — the board in the Hermes desktop.

This agent half is deliberately inert: the plugin registers no tools, hooks or
commands. Its Python lives in ``dashboard/plugin_api.py``, which the dashboard plugin
system imports and mounts at ``/api/plugins/board/`` for the desktop half
(``desktop/plugin.js``) to call through ``ctx.rest``.

``register`` exists because the loader only treats a folder as a plugin when it carries
an ``__init__.py`` exposing one; returning without registering anything is the
supported "UI-only plugin" shape.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Nothing to contribute on the agent side."""
    return None
