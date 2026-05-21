from __future__ import annotations

import logging
from typing import Any

from backend.engine.navigation_rules import classify_page_state, navigation_action_override
from backend.engine.platform_adapters import (
    normalize_text,
    platform_for_domain,
    verification_code_blocker_detected,
)
from backend.engine.platform_handlers.base import PageBlocker


logger = logging.getLogger(__name__)
name = "workday"


def owns(page_data: dict[str, Any]) -> bool:
    try:
        return platform_for_domain(str(page_data.get("url") or "")).name == "workday"
    except Exception as e:
        logger.warning("workday.owns failed: %s", e)
        return False


def page_state(page_data: dict[str, Any]) -> str:
    try:
        current_blocker = blocker(page_data)
        if current_blocker is not None:
            return current_blocker.kind
        return classify_page_state(page_data)
    except Exception as e:
        logger.warning("workday.page_state failed: %s", e)
        return "unknown"


def blocker(page_data: dict[str, Any]) -> PageBlocker | None:
    try:
        text = normalize_text(page_data.get("text"))
        if not text:
            return None

        if any(
            token in text
            for token in (
                "captcha",
                "security code",
                "confirm you're a human",
                "human verification",
                "protected by hcaptcha",
            )
        ):
            return PageBlocker("captcha", "Workday security challenge requires manual completion")

        if verification_code_blocker_detected(page_data):
            return PageBlocker("otp", "Workday verification/security code requires manual completion")

        if (
            "verify your account before you sign in" in text
            or "account may need verification" in text
            or "email verification" in text
            or "verification email" in text
            or "resend account verification" in text
        ):
            return PageBlocker("email_verification", "Workday email verification is required")

        return None
    except Exception as e:
        logger.warning("workday.blocker failed: %s", e)
        return None


def action_override(page_data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return navigation_action_override(page_data)
    except Exception as e:
        logger.warning("workday.action_override failed: %s", e)
        return None