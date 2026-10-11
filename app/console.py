"""Small helpers shared by the command-line tools."""
from __future__ import annotations

from typing import TextIO


def lenient(stream: TextIO) -> TextIO:
    """Make printing text the console cannot show (an emoji on a Windows code page, say) print a ``?``
    instead of raising, which would end a long run on the first such caption."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    return stream
