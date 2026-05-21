from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PageBlocker:
    kind: str
    message: str


class PlatformHandler(Protocol):
    name: str

    def owns(self, page_data: dict[str, Any]) -> bool:
        """Return true when this dedicated handler should own the page."""
        ...

    def page_state(self, page_data: dict[str, Any]) -> str:
        """Classify the current page into a coarse application-flow state."""
        ...

    def blocker(self, page_data: dict[str, Any]) -> PageBlocker | None:
        """Return a blocker that should stop the generic form loop."""
        ...

    def action_override(self, page_data: dict[str, Any]) -> dict[str, Any] | None:
        """Return a high-confidence navigation action before generic scoring."""
        ...