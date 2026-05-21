from __future__ import annotations

import logging
from typing import Any

from backend.engine.navigation_rules import classify_page_state
from backend.engine.platform_adapters import (
    is_third_party_apply_button,
    normalize_text,
    verification_code_blocker_detected,
)
from backend.engine.platform_handlers.base import PageBlocker


logger = logging.getLogger(__name__)
name = "generic"


def owns(page_data: dict[str, Any]) -> bool:
    del page_data
    return True


def page_state(page_data: dict[str, Any]) -> str:
    try:
        current_blocker = blocker(page_data)
        if current_blocker is not None:
            return current_blocker.kind
        return classify_page_state(page_data)
    except Exception as e:
        logger.warning("generic.page_state failed: %s", e)
        return "unknown"


def blocker(page_data: dict[str, Any]) -> PageBlocker | None:
    try:
        text = normalize_text(page_data.get("text"))
        iframe_text = " ".join(
            normalize_text(
                " ".join(
                    str(frame.get(key) or "")
                    for key in ("title", "ariaLabel", "src")
                )
            )
            for frame in page_data.get("iframes", [])
            if isinstance(frame, dict)
        )
        haystack = f"{text} {iframe_text}"
        if any(
            token in haystack
            for token in (
                "recaptcha",
                "hcaptcha",
                "captcha challenge",
                "i'm not a robot",
                "confirm you're a human",
                "human verification",
                "security challenge",
            )
        ):
            return PageBlocker("captcha", "CAPTCHA/security challenge requires manual completion")

        if verification_code_blocker_detected(page_data):
            return PageBlocker("otp", "Verification/security code requires manual completion")

        if any(
            token in text
            for token in (
                "verify your email",
                "verify your account",
                "email verification",
                "verification email",
                "resend account verification",
                "check your email",
            )
        ):
            return PageBlocker("email_verification", "Email verification requires manual completion")

        if "record video" in text and "upload video" in text:
            return PageBlocker("video_interview", "Video interview/upload step requires manual completion")

        state = classify_page_state(page_data)
        if state == "dead_listing":
            return PageBlocker("dead_listing", "Job posting is unavailable or expired")

        if state == "auth":
            return PageBlocker("auth", "Login/sign-up is required before application can continue")

        return None
    except Exception as e:
        logger.warning("generic.blocker failed: %s", e)
        return None


def _apply_navigation_score(button: dict[str, Any]) -> int:
    try:
        if is_third_party_apply_button(button):
            return -1
        text = normalize_text(button.get("text"))
        href = str(button.get("href") or "").strip().lower()
        automation_id = normalize_text(button.get("automationId"))
        haystack = " ".join(part for part in (text, href, automation_id) if part)
        if not haystack:
            return 0
        if any(token in text for token in ("sign in", "login", "create account", "register")):
            return -1
        if any(token in text for token in ("submit application", "submit", "send application", "complete application")):
            return 0
        if text in {"apply manually", "manual apply"}:
            return 120
        if any(token in text for token in ("apply now", "apply for this job", "apply for this")):
            return 110
        if text in {"apply", "start application", "start your application"}:
            return 100
        if any(token in text for token in ("i'm interested", "i am interested", "express interest")):
            return 90
        if "/apply" in href and "mailto:" not in href:
            return 85
        return 0
    except Exception as e:
        logger.warning("_apply_navigation_score failed: %s", e)
        return 0


def action_override(page_data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        state = page_state(page_data)
        if state not in {"job_detail", "apply_choice", "job_search", "unknown"}:
            return None

        scored: list[tuple[int, dict[str, Any]]] = []
        for button in page_data.get("buttons", []):
            if not isinstance(button, dict):
                continue
            score = _apply_navigation_score(button)
            if score > 0:
                scored.append((score, button))
        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        button = scored[0][1]
        return {
            "type": "click_button",
            "selector": button.get("selector"),
            "text": button.get("text"),
            "automationId": button.get("automationId"),
            "href": button.get("href"),
            "source": "generic_platform_handler",
        }
    except Exception as e:
        logger.warning("generic.action_override failed: %s", e)
        return None