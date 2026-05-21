from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from backend.db import ROOT_DIR
from backend.engine.platform_adapters import (
    is_third_party_apply_button,
    normalize_text,
    platform_for_domain,
)


logger = logging.getLogger(__name__)

NAVIGATION_RULES_PATH = ROOT_DIR / "rules" / "navigation_rules.json"

PageState = Literal[
    "dead_listing",
    "apply_choice",
    "auth",
    "application_form",
    "job_detail",
    "job_search",
    "confirmation",
    "unknown",
]


def _load_payload() -> dict[str, Any]:
    if not NAVIGATION_RULES_PATH.exists():
        return {"version": 1, "rules": []}
    try:
        payload = json.loads(NAVIGATION_RULES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "rules": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        return {"version": 1, "rules": []}
    return payload


def _page_haystack(page_data: dict[str, Any]) -> str:
    try:
        modal_text = " ".join(
            str(modal.get("text") or "")
            for modal in page_data.get("modals", [])
            if isinstance(modal, dict)
        )
        button_text = " ".join(
            str(button.get("text") or "")
            for button in page_data.get("buttons", [])
            if isinstance(button, dict)
        )
        field_text = " ".join(
            " ".join(
                str(field.get(key) or "")
                for key in ("label", "placeholder", "automationId", "name", "id", "type")
            )
            for field in page_data.get("fields", [])
            if isinstance(field, dict)
        )
        return normalize_text(
            " ".join(
                [
                    str(page_data.get("url") or ""),
                    str(page_data.get("title") or ""),
                    str(page_data.get("text") or ""),
                    modal_text,
                    button_text,
                    field_text,
                ]
            )
        )
    except Exception as e:
        logger.warning("_page_haystack failed: %s", e)
        return ""


def _button_texts(page_data: dict[str, Any]) -> set[str]:
    try:
        return {
            normalize_text(button.get("text"))
            for button in page_data.get("buttons", [])
            if isinstance(button, dict) and normalize_text(button.get("text"))
        }
    except Exception as e:
        logger.warning("_button_texts failed: %s", e)
        return set()


def _automation_ids(page_data: dict[str, Any]) -> set[str]:
    try:
        return {
            normalize_text(button.get("automationId"))
            for button in page_data.get("buttons", [])
            if isinstance(button, dict) and normalize_text(button.get("automationId"))
        }
    except Exception as e:
        logger.warning("_automation_ids failed: %s", e)
        return set()


def _field_count(page_data: dict[str, Any]) -> int:
    try:
        return len([field for field in page_data.get("fields", []) if isinstance(field, dict)])
    except Exception as e:
        logger.warning("_field_count failed: %s", e)
        return 0


def _password_field_count(page_data: dict[str, Any]) -> int:
    try:
        count = 0
        for field in page_data.get("fields", []):
            if not isinstance(field, dict):
                continue
            field_type = normalize_text(field.get("type"))
            if field_type == "password":
                count += 1
                continue
            field_text = normalize_text(
                " ".join(
                    str(field.get(key) or "")
                    for key in ("placeholder", "automationId", "name", "id")
                )
            )
            if "password" in field_text and field_type in {"", "text", "email"}:
                count += 1
        return count
    except Exception as e:
        logger.warning("_password_field_count failed: %s", e)
        return 0


def _rule_matches(page_data: dict[str, Any], rule: dict[str, Any]) -> bool:
    try:
        platform = str(rule.get("platform") or "").strip().lower()
        current_platform = platform_for_domain(str(page_data.get("url") or "")).name
        if platform and platform not in {"*", current_platform}:
            return False

        match = rule.get("match")
        if not isinstance(match, dict):
            return False

        haystack = _page_haystack(page_data)
        buttons = _button_texts(page_data)
        automation_ids = _automation_ids(page_data)

        for token in match.get("url_contains_all") or []:
            if normalize_text(token) not in normalize_text(page_data.get("url")):
                return False
        for token in match.get("text_contains_all") or []:
            if normalize_text(token) not in haystack:
                return False

        any_text = [normalize_text(token) for token in match.get("text_contains_any") or [] if normalize_text(token)]
        if any_text and not any(token in haystack for token in any_text):
            return False

        any_button = [normalize_text(token) for token in match.get("button_text_any") or [] if normalize_text(token)]
        if any_button and not any(token in buttons for token in any_button):
            return False

        any_automation_id = [
            normalize_text(token)
            for token in match.get("automation_id_any") or []
            if normalize_text(token)
        ]
        if any_automation_id and not any(token in automation_ids for token in any_automation_id):
            return False

        min_fields = match.get("min_fields")
        if isinstance(min_fields, int) and _field_count(page_data) < min_fields:
            return False

        max_fields = match.get("max_fields")
        if isinstance(max_fields, int) and _field_count(page_data) > max_fields:
            return False

        min_passwords = match.get("min_password_fields")
        if isinstance(min_passwords, int) and _password_field_count(page_data) < min_passwords:
            return False

        return True
    except Exception as e:
        logger.warning("_rule_matches failed: %s", e)
        return False


def matching_navigation_rules(page_data: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        rules = [rule for rule in _load_payload().get("rules", []) if isinstance(rule, dict)]
        matches = [rule for rule in rules if _rule_matches(page_data, rule)]
        matches.sort(key=lambda rule: int(rule.get("priority") or 0), reverse=True)
        return matches
    except Exception as e:
        logger.warning("matching_navigation_rules failed: %s", e)
        return []


def _has_apply_button(page_data: dict[str, Any]) -> bool:
    """Lightweight Apply-button check used as a veto before declaring dead_listing.

    Mirrors page_inspector._has_real_apply_control but lives here so navigation_rules
    avoids a circular import. A visible, non-mailto link/button labelled "apply"
    means the listing is live regardless of stray hopeful phrasing like
    'until the requisition is closed'.
    """
    try:
        for button in page_data.get("buttons", []) or []:
            if not isinstance(button, dict):
                continue
            text = normalize_text(button.get("text") or "").strip().lower()
            href = str(button.get("href") or "").lower()
            tag_name = str(button.get("tagName") or "").lower()
            button_type = str(button.get("type") or "").lower()
            automation_id = str(button.get("automationId") or "").lower()
            if automation_id in {"adventurebutton", "applymanually", "autofillwithresume"}:
                return True
            if any(
                kw in text
                for kw in (
                    "apply now",
                    "apply for this",
                    "start your application",
                    "start application",
                    "manual apply",
                    "apply manually",
                )
            ):
                return True
            if text == "apply" and (
                (href and "mailto:" not in href and "/apply" in href)
                or tag_name == "button"
                or button_type in {"button", "submit"}
            ):
                return True
        return False
    except Exception:
        return False


def classify_page_state(page_data: dict[str, Any]) -> PageState:
    try:
        has_apply = _has_apply_button(page_data)
        for rule in matching_navigation_rules(page_data):
            state = str(rule.get("page_state") or "").strip()
            if state == "dead_listing" and has_apply:
                logger.info("classify_page_state: vetoed manual-rule dead_listing because Apply control present")
                continue
            if state in {
                "dead_listing",
                "apply_choice",
                "auth",
                "application_form",
                "job_detail",
                "job_search",
                "confirmation",
                "unknown",
            }:
                return state  # type: ignore[return-value]

        haystack = _page_haystack(page_data)
        if any(
            marker in haystack
            for marker in (
                "thank you for applying",
                "application submitted",
                "application received",
                "successfully applied",
            )
        ):
            return "confirmation"
        if any(
            marker in haystack
            for marker in (
                "page you are looking for does not exist",
                "page you are looking for doesn't exist",
                "access denied",
                "candidate/error/access-denied",
                "you don't have access to enter",
                "not authorized to access this page",
                "resource not found",
                "resource-not-found",
                "job is no longer available",
                "this job is no longer available",
                "job opportunity is no longer available",
                "job opportunity is no longer active",
                "this job opportunity is no longer available",
                "this job opportunity is no longer active",
                "no longer accepting applications",
                "not accepting applications",
                "this position is no longer accepting applications",
                "this posting is no longer accepting applications",
                "position is closed",
                "job posting is closed",
                "posting has expired",
                "job posting has expired",
                "job has been closed",
                "job is closed",
                "job no longer exists",
                "opening is no longer available",
                "requisition is closed",
                "position has been filled",
                "job expired",
            )
        ):
            if has_apply:
                logger.info("classify_page_state: vetoed marker dead_listing because Apply control present")
            else:
                return "dead_listing"
        if _password_field_count(page_data) > 0:
            return "auth"
        if any(
            marker in haystack
            for marker in (
                "create an account or sign in to continue",
                "create account or sign in to continue",
                "login/register",
                "login / register",
                "sign in to apply",
                "log in to apply",
                "login to apply",
                "sign in or create an account",
                "login or create an account",
                "create an account to apply",
                "please sign in",
                "please log in",
                "sign in to continue",
                "log in to continue",
                "login with google",
                "sign in with google",
                "continue with linkedin",
                "login with linkedin",
                "sign in with linkedin",
                "/signin?callbackurl=",
                "/login?callbackurl=",
                "/auth/signin",
                "continue with email",
                "continue with google",
            )
        ):
            return "auth"
        if (
            "create account/sign in" in haystack
            and (
                "sign in with email" in _button_texts(page_data)
                or "signinwithemailbutton" in _automation_ids(page_data)
            )
        ):
            return "auth"
        _APPLICATION_FORM_KEYWORDS = (
            "first name", "last name", "email address", "phone number",
            "postal code", "zip code", "address", "work experience", "education",
            "linkedin", "salary", "start date", "authorized to work", "sponsorship",
            "years of experience", "resume", "cover letter", "pronouns", "gender",
            "race", "ethnicity", "veteran", "disability", "file",
        )
        if (
            _field_count(page_data) >= 3
            and _password_field_count(page_data) == 0
            and any(kw in haystack for kw in _APPLICATION_FORM_KEYWORDS)
        ):
            return "application_form"
        # Workday "My Experience" page (and similar) only renders 1-2 fields
        # (file upload + LinkedIn URL). The 3-field threshold misclassifies
        # them as "unknown" and the engine bails to the job-posting page,
        # creating an infinite loop. Trust the Workday footer button +
        # progress breadcrumb as a strong signal we're inside an apply flow.
        automation_ids = _automation_ids(page_data)
        button_texts = _button_texts(page_data)
        if (
            _password_field_count(page_data) == 0
            and (
                "pagefooternextbutton" in automation_ids
                or "save and continue" in button_texts
            )
            and (
                "step 1 of" in haystack
                or "step 2 of" in haystack
                or "step 3 of" in haystack
                or "step 4 of" in haystack
                or "step 5 of" in haystack
                or "step 6 of" in haystack
                or "step 7 of" in haystack
                or "current step" in haystack
                or "back to job posting" in haystack
            )
        ):
            return "application_form"
        if "apply manually" in _button_texts(page_data) or "applymanually" in _automation_ids(page_data):
            return "apply_choice"
        # Apply-button presence is a STRONGER job_detail signal than a header
        # search widget. Many Workday tenants render a "Search Jobs" navigation
        # link or a global search bar on every page, including job-detail pages
        # (Chevron, recent tenants). Without this ordering the page is
        # misclassified as job_search and the engine ignores the real Apply
        # button on the page.
        if "adventurebutton" in automation_ids or any("apply" in text for text in _button_texts(page_data)):
            return "job_detail"
        if (
            "search for jobs" in haystack
            and (
                "jobs found" in haystack
                or "keywordsearchinput" in haystack
                or "search filters" in haystack
            )
        ):
            return "job_search"
        return "unknown"
    except Exception as e:
        logger.warning("classify_page_state failed: %s", e)
        return "unknown"


def _button_matches_target(button: dict[str, Any], action: dict[str, Any]) -> bool:
    try:
        target_text = normalize_text(action.get("target_text"))
        target_text_any = [
            normalize_text(text)
            for text in action.get("target_text_any") or []
            if normalize_text(text)
        ]
        target_automation_id = normalize_text(action.get("target_automation_id"))
        text = normalize_text(button.get("text"))
        automation_id = normalize_text(button.get("automationId"))
        if is_third_party_apply_button(button):
            return False
        if target_automation_id and automation_id == target_automation_id:
            return True
        if target_text_any and text in target_text_any:
            return True
        return bool(target_text and text == target_text)
    except Exception as e:
        logger.warning("_button_matches_target failed: %s", e)
        return False


def navigation_action_override(page_data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        for rule in matching_navigation_rules(page_data):
            action = rule.get("action")
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "")
            if action_type == "click_button":
                for button in page_data.get("buttons", []):
                    if not isinstance(button, dict):
                        continue
                    if _button_matches_target(button, action):
                        return {
                            "type": "click_button",
                            "selector": button.get("selector"),
                            "text": button.get("text"),
                            "automationId": button.get("automationId"),
                            "href": button.get("href"),
                            "source": "navigation_rule",
                            "rule_id": rule.get("id"),
                        }
            elif action_type in {"defer_auth", "fill_form", "stop"}:
                return {
                    "type": action_type,
                    "source": "navigation_rule",
                    "rule_id": rule.get("id"),
                    "reason": action.get("reason") or rule.get("reason") or "",
                }
        return None
    except Exception as e:
        logger.warning("navigation_action_override failed: %s", e)
        return None