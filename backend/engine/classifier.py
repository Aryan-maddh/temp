from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)


PageKind = Literal["login", "signup", "listing", "application", "done", "unknown"]

SUCCESS_KEYWORDS = (
    "thank you",
    "submitted",
    "application received",
    "application complete",
    "we received your application",
    "successfully applied",
)
LOGIN_KEYWORDS = ("sign in", "login", "log in", "password", "authenticate")
LISTING_KEYWORDS = ("apply", "apply now", "start application", "submit application")
APPLICATION_KEYWORDS = (
    "resume",
    "cover letter",
    "work experience",
    "education",
    "first name",
    "last name",
    "email",
    "phone",
)


async def _visible_text(page: object) -> str:
    try:
        return await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


async def _field_labels(page: object) -> list[str]:
    script = """
    () => {
      const labels = Array.from(document.querySelectorAll('label'))
        .map((node) => node.innerText || node.textContent || '');
      const placeholders = Array.from(document.querySelectorAll('input, textarea, select'))
        .map((node) => node.getAttribute('aria-label') || node.getAttribute('placeholder') || node.getAttribute('name') || '');
      return labels.concat(placeholders).map((value) => value.trim()).filter(Boolean);
    }
    """
    try:
        values = await page.evaluate(script)
    except Exception:
        return []
    return [str(value) for value in values if value]


async def _count_inputs(page: object, selector: str) -> int:
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


def _score(haystack: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in haystack)


async def classify_page(page: object) -> PageKind:
    try:
        try:
            title = await page.title()
        except Exception:
            title = ""

        url = getattr(page, "url", "") or ""
        text = await _visible_text(page)
        labels = " ".join(await _field_labels(page))
        haystack = f"{url} {title} {text} {labels}".lower()

        password_count = await _count_inputs(page, "input[type='password']")
        email_count = await _count_inputs(page, "input[type='email'], input[name*='email' i]")
        text_input_count = await _count_inputs(
            page,
            "input:not([type='hidden']):not([type='submit']):not([type='button']), textarea, select",
        )
        file_count = await _count_inputs(page, "input[type='file']")
        apply_button_count = await _count_inputs(
            page,
            "a:has-text('Apply'), button:has-text('Apply'), input[value*='Apply' i]",
        )

        if _score(haystack, SUCCESS_KEYWORDS) > 0:
            return "done"

        # 2+ password inputs = signup form (Password + Verify Password). 1 = login.
        # Without this distinction, a re-rendered Create Account page after a failed
        # signup (consent unchecked, click_filter wrapper not actually submitting,
        # email already taken) would be misread as a login page, and the engine
        # would click the "Already have an account? Sign In" link, navigating away
        # from the signup form entirely.
        if password_count >= 2:
            return "signup"
        if password_count > 0 or any(term in url.lower() for term in ("signin", "login", "auth")):
            return "login"

        application_score = _score(haystack, APPLICATION_KEYWORDS)
        if text_input_count >= 3 and (application_score >= 2 or email_count > 0 or file_count > 0):
            return "application"

        if apply_button_count > 0 and text_input_count < 3:
            return "listing"

        if any(term in url.lower() for term in ("job", "career", "opening")) and _score(haystack, LISTING_KEYWORDS) > 0:
            return "listing"

        if _score(haystack, LOGIN_KEYWORDS) >= 2:
            return "login"

        return "unknown"
    except Exception as e:
        logger.warning("classify_page failed: %s", e)
        return "unknown"