from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


PLATFORM_GENERIC = "generic"
PLATFORM_WORKDAY = "workday"
PLATFORM_GREENHOUSE = "greenhouse"
PLATFORM_RULE_SCOPES = {PLATFORM_WORKDAY}


POST_APPLICATION_FOLLOWUP_TEXTS = (
    "create alert",
    "job alert",
    "job alerts",
    "newsletter",
    "receive similar jobs",
    "similar jobs by email",
    "new jobs delivered",
    "getting new roles delivered to your inbox",
    "track your status",
    "mygreenhouse",
)


BUTTON_SKIP_KEYWORDS = (
    "search",
    "search for jobs",
    "skip to main content",
    "home",
    "back",
    "cancel",
    "close",
    "menu",
    "learn more",
    "create alert",
    "job alert",
    "job alerts",
    "subscribe",
    "newsletter",
    "receive similar jobs",
    "similar jobs",
    "how to apply",
    "how do i apply",
    "application process",
    "send security code",
    "track your status",
    "create account",
    "sign in",
    "log in",
    "login",
    # OAuth / third-party apply buttons — always prefer the direct application flow
    "apply with linkedin",
    "apply with indeed",
    "apply with zip recruiter",
    "easy apply",
    "linkedin easy apply",
)


BUTTON_APPLY_KEYWORDS = (
    "apply now",
    "apply for this job",
    "apply for this",
    "start your application",
    "start application",
    "manual apply",
    "apply manually",
    "submit your resume",
    "i'm interested",
    "i am interested",
    "im interested",
    "express interest",
)
BUTTON_NEXT_KEYWORDS = ("next", "continue", "proceed", "save and continue")
BUTTON_SUBMIT_KEYWORDS = ("submit", "submit application", "send application", "complete application")


@dataclass(frozen=True)
class PlatformAdapter:
    name: str
    rule_scope: str = ""


def _hostname(value: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text if "://" in text else f"https://{text}")
    return (parsed.hostname or text).lower().removeprefix("www.")


def platform_for_domain(domain_or_url: str) -> PlatformAdapter:
    hostname = _hostname(domain_or_url)
    if "myworkdayjobs.com" in hostname or "workdayjobs.com" in hostname:
        return PlatformAdapter(PLATFORM_WORKDAY, PLATFORM_WORKDAY)
    if "greenhouse.io" in hostname or "greenhouse.com" in hostname:
        return PlatformAdapter(PLATFORM_GREENHOUSE, "")
    return PlatformAdapter(PLATFORM_GENERIC, "")


def platform_scope_for_domain(domain_or_url: str) -> str:
    return platform_for_domain(domain_or_url).rule_scope


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


def is_third_party_apply_button(button: dict[str, object]) -> bool:
    text = normalize_text(button.get("text"))
    href = str(button.get("href") or "").strip().lower()
    automation_id = normalize_text(button.get("automationId"))
    aria_label = normalize_text(button.get("ariaLabel") or button.get("aria-label") or button.get("title"))
    haystack = " ".join(part for part in (text, href, automation_id, aria_label) if part)
    if not haystack:
        return False
    if (
        "applywithlinkedin" in href
        or "/awli/" in href
        or "linkedin.com/jobs/apply" in href
        or ("ziprecruiter.com" in href and "apply" in href)
    ):
        return True
    third_party = r"(linkedin|linked in|indeed|zip\s*recruiter|ziprecruiter|glassdoor|seek)"
    third_party_apply_patterns = (
        rf"\bapply\s+(with|via|using|through)\s+{third_party}\b",
        rf"\b{third_party}\s+(easy\s+)?apply\b",
        rf"\beasy\s+apply\b",
        rf"\buse\s+(my\s+)?{third_party}\b",
        rf"\bautofill\s+with\s+{third_party}\b",
    )
    return any(re.search(pattern, haystack) for pattern in third_party_apply_patterns)


def is_sensitive_auth_field(label: object, field_type: object = "") -> bool:
    normalized_label = normalize_text(label)
    normalized_type = str(field_type or "").lower()
    return normalized_type == "password" or any(
        token in normalized_label
        for token in (
            "password",
            "verify new password",
            "security code",
            "verification code",
            "one time code",
            "one-time code",
            "otp",
            "confirm you're a human",
        )
    )


def is_optional_conditional_field(label: object, required: bool) -> bool:
    normalized = normalize_text(label)
    return not required and normalized.startswith("if ") and "other" in normalized


def is_verification_or_security_label(label: object, field_type: object = "") -> bool:
    normalized = normalize_text(label)
    return is_sensitive_auth_field(normalized, field_type) or "captcha" in normalized


def _field_text(field: dict[str, Any]) -> str:
    return normalize_text(
        " ".join(
            str(field.get(key) or "")
            for key in ("label", "placeholder", "automationId", "selector", "name", "id")
        )
    )


def verification_code_blocker_detected(page_data: dict[str, object]) -> bool:
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
    if (
        "recaptcha challenge" in iframe_text
        or "hcaptcha challenge" in iframe_text
        or "google.com recaptcha api2 bframe" in iframe_text
        or "hcaptcha.com captcha" in iframe_text
    ):
        return True
    if not text:
        return False
    button_texts = " ".join(
        normalize_text(button.get("text"))
        for button in page_data.get("buttons", [])
        if isinstance(button, dict)
    )
    code_text = (
        "security code" in text
        or "verification code" in text
        or "one time code" in text
        or "one-time code" in text
        or "we sent" in text and "code" in text
        or "confirm you're a human" in text
    )
    code_fields = [
        field
        for field in page_data.get("fields", [])
        if isinstance(field, dict)
        and any(
            token in _field_text(field)
            for token in (
                "verification code",
                "security code",
                "one time code",
                "one-time code",
                "confirm you're a human",
                "security input",
                "security-input",
                "otp",
            )
        )
    ]
    if "confirm you're a human" in text and "begin" in button_texts and not code_fields:
        return False
    return (code_text and bool(code_fields)) or len(code_fields) >= 2


def workday_auth_page_detected(page_data: dict[str, object]) -> bool:
    platform = platform_for_domain(str(page_data.get("url") or ""))
    if platform.name != PLATFORM_WORKDAY:
        return False
    text = normalize_text(page_data.get("text"))
    if not text:
        return False
    has_password_field = any(
        isinstance(field, dict)
        and (
            str(field.get("type") or "").lower() == "password"
            or "password" in _field_text(field)
        )
        for field in page_data.get("fields", [])
    )
    has_auth_step = (
        "create account/sign in" in text
        or ("create account" in text and "sign in" in text)
        or ("sign up" in text and ("sign in" in text or "log in" in text or "login" in text))
        or ("create a new account" in text)
        or "already have an account" in text
        or "don't have an account yet" in text
        or "already registered" in text
    )
    return has_auth_step and has_password_field


def post_application_followup_text_detected(text: object, done_texts: tuple[str, ...]) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(done_text in normalized for done_text in done_texts):
        return True
    has_followup_text = any(phrase in normalized for phrase in POST_APPLICATION_FOLLOWUP_TEXTS)
    has_application_confirmation = any(
        phrase in normalized
        for phrase in (
            "application",
            "applied",
            "submitted",
            "confirmation",
        )
    )
    return has_followup_text and has_application_confirmation


def is_add_detail_button_text(text: object) -> bool:
    normalized = normalize_text(str(text or "").replace("+", " + "))
    has_add_intent = "add" in normalized or normalized.startswith("+")
    detail_terms = (
        "experience details",
        "work experience",
        "employment history",
        "education details",
        "education",
        "qualification",
        "academic",
    )
    return has_add_intent and any(term in normalized for term in detail_terms)


def score_action_button(button: dict[str, object], platform: str = PLATFORM_GENERIC) -> int:
    del platform
    text = normalize_text(button.get("text"))
    href = str(button.get("href") or "").strip().lower()
    automation_id = normalize_text(button.get("automationId"))
    if is_third_party_apply_button(button):
        return -1
    if (
        automation_id
        in {
            "navigationitem search and apply",
            "navigationitem join our talent community!",
            "privacylink",
            "accessibilityskiptomaincontent",
        }
        or text
        in {
            "search and apply",
            "join our talent community!",
            "gdit privacy notice and california privacy notice",
        }
    ):
        return -1
    if text in {"how to apply", "how do i apply", "application process"}:
        return -1
    if re.fullmatch(r"(next|previous) slide", text):
        return -1
    if re.fullmatch(r"slide \d+", text):
        return -1
    # Block OAuth / third-party apply buttons by TEXT — must come before the generic
    # "\bapply\b" check below, which would otherwise give these a score of 90.
    if re.search(r"\bapply\s+with\b", text) or "easy apply" in text:
        return -1
    # Check positive action keywords FIRST so compound phrases like
    # "sign in to apply" or "log in and continue" score correctly.
    if is_add_detail_button_text(text):
        return 110
    if text in {"submit application", "send application", "complete application"}:
        return 105
    if text in {"submit", "send", "finish"}:
        return 100
    if text in {"apply now", "apply for this job", "apply for this"}:
        return 95
    if any(keyword in text for keyword in BUTTON_SUBMIT_KEYWORDS):
        return 100
    if any(keyword in text for keyword in BUTTON_APPLY_KEYWORDS):
        return 90
    if re.search(r"\bapply\b", text) and "applyiq" not in text:
        return 90
    if any(keyword in text for keyword in BUTTON_NEXT_KEYWORDS):
        return 80
    if button.get("type") == "submit":
        return 70
    if href.endswith("#"):
        return -1
    # Negative checks only after positive keywords ruled out.
    if any(keyword in text for keyword in BUTTON_SKIP_KEYWORDS):
        return -1
    if "?" in text:
        return -1
    return 0