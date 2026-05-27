from __future__ import annotations

from typing import Any

from app.engine.platform_adapters import PLATFORM_WORKDAY, platform_for_domain
from app.engine.platforms import generic, workday
from app.engine.platforms.base import PageBlocker


DEDICATED_HANDLERS = {
    PLATFORM_WORKDAY: workday,
}


def handler_for_page(page_data: dict[str, Any]):
    platform = platform_for_domain(str(page_data.get("url") or "")).name
    handler = DEDICATED_HANDLERS.get(platform)
    if handler and handler.owns(page_data):
        return handler
    return generic


def handler_name_for_page(page_data: dict[str, Any]) -> str:
    return str(handler_for_page(page_data).name)


def page_state_for_page(page_data: dict[str, Any]) -> str:
    return str(handler_for_page(page_data).page_state(page_data))


def page_blocker(page_data: dict[str, Any]) -> PageBlocker | None:
    return handler_for_page(page_data).blocker(page_data)


def action_override_for_page(page_data: dict[str, Any]) -> dict[str, Any] | None:
    return handler_for_page(page_data).action_override(page_data)