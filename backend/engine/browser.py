from __future__ import annotations

import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import ROOT_DIR, SessionLocal
from backend.engine import anti_ban
from backend.engine.auth_handler import ManualLoginRequired, handle_login, _strong_password
from backend.engine.checkpoint import (
    detect_step_key,
    load_checkpoint,
    save_checkpoint,
    step_index_for_key,
)
from backend.engine.page_inspector import (
    decide_action,
    embedded_application_url,
    inspect_page,
    is_listing_surface,
    page_blocker_reason,
    probe_typeahead_popup,
)
from backend.engine.platform_handlers import action_override_for_page, handler_name_for_page, page_blocker, page_state_for_page
from backend.engine.platform_adapters import (
    is_add_detail_button_text,
    is_third_party_apply_button,
    platform_for_domain,
    post_application_followup_text_detected,
    score_action_button,
    verification_code_blocker_detected,
    workday_auth_page_detected,
)
from backend.engine.site_rules import domain_from_url, record_rule_failure, save_field_rule
from backend.logs import write_run_log
from backend.models import Application, Candidate, Credential, FormAnswer, Job, UnansweredQuestion


MAX_STEPS = 35
MAX_REPEATED_VALIDATION_ERRORS = 3
MANUAL_AUTH_WAIT_SECONDS = 600
SCREENSHOT_ROOT = ROOT_DIR / "screenshots"
SAFE_LIVE_TEST_ENV = "SAFE_LIVE_TEST_STOP_BEFORE_SUBMIT"

# Minimal country -> dialing-code map. Used as a fallback when the candidate has not
# explicitly stored a phone country code but a country/location is known. Keeping this
# small avoids guessing for ambiguous countries; unknown countries fall through and the
# raw phone value is used as-is, never coerced into +1.
COUNTRY_DIAL_CODES: dict[str, str] = {
    "india": "+91",
    "in": "+91",
    "bharat": "+91",
    "united states": "+1",
    "usa": "+1",
    "us": "+1",
    "u.s.": "+1",
    "u.s.a.": "+1",
    "america": "+1",
    "canada": "+1",
    "ca": "+1",
    "united kingdom": "+44",
    "uk": "+44",
    "great britain": "+44",
    "england": "+44",
    "scotland": "+44",
    "wales": "+44",
    "ireland": "+353",
    "australia": "+61",
    "au": "+61",
    "new zealand": "+64",
    "nz": "+64",
    "germany": "+49",
    "deutschland": "+49",
    "france": "+33",
    "spain": "+34",
    "italy": "+39",
    "netherlands": "+31",
    "the netherlands": "+31",
    "belgium": "+32",
    "switzerland": "+41",
    "sweden": "+46",
    "norway": "+47",
    "denmark": "+45",
    "finland": "+358",
    "poland": "+48",
    "portugal": "+351",
    "japan": "+81",
    "south korea": "+82",
    "korea": "+82",
    "china": "+86",
    "hong kong": "+852",
    "taiwan": "+886",
    "singapore": "+65",
    "sg": "+65",
    "malaysia": "+60",
    "indonesia": "+62",
    "philippines": "+63",
    "vietnam": "+84",
    "thailand": "+66",
    "uae": "+971",
    "united arab emirates": "+971",
    "saudi arabia": "+966",
    "qatar": "+974",
    "bahrain": "+973",
    "kuwait": "+965",
    "oman": "+968",
    "israel": "+972",
    "turkey": "+90",
    "south africa": "+27",
    "nigeria": "+234",
    "kenya": "+254",
    "egypt": "+20",
    "brazil": "+55",
    "argentina": "+54",
    "mexico": "+52",
    "chile": "+56",
    "colombia": "+57",
    "russia": "+7",
    "ukraine": "+380",
    "pakistan": "+92",
    "bangladesh": "+880",
    "sri lanka": "+94",
    "nepal": "+977",
}


def _candidate_country_dial_code(candidate: object, extra_answers: object) -> str:
    """Best-effort country dialing code (e.g. '+91') without defaulting to +1.

    Order: explicit phone-country-code answer -> declared country -> location text.
    Returns "" if nothing maps cleanly; callers must NOT fall back to +1 in that case.
    """

    def _normalize(value: object) -> str:
        return re.sub(r"[^a-z ]", " ", str(value or "").lower()).strip()

    if isinstance(extra_answers, dict):
        for key in ("phone country code", "country phone code", "dialing code", "calling code"):
            match = re.search(r"\+\d{1,4}", str(extra_answers.get(key) or ""))
            if match:
                return match.group(0)
        for key in ("country", "address country", "current country", "nationality", "country of residence"):
            normalized = _normalize(extra_answers.get(key))
            if not normalized:
                continue
            if normalized in COUNTRY_DIAL_CODES:
                return COUNTRY_DIAL_CODES[normalized]
            for country_key, code in COUNTRY_DIAL_CODES.items():
                if country_key in normalized:
                    return code

    location_text = _normalize(getattr(candidate, "location", "") or "")
    if location_text:
        for country_key, code in COUNTRY_DIAL_CODES.items():
            if country_key in location_text:
                return code
    return ""


def _local_phone_number_value(candidate: object, value: object) -> str:
    """Return local phone digits for plain phone-number fields.

    Manual answers sometimes store a country-code dropdown choice under a generic
    label like "Phone". If that value is reused for a number input, use the
    candidate's actual phone instead of filling only +91/+92.
    """

    extra_answers = getattr(candidate, "extra_answers", None) or {}
    country_code = _candidate_country_dial_code(candidate, extra_answers)

    def local_digits(raw: object) -> str:
        text = re.sub(r"^[A-Za-z\s]+", "", str(raw or "")).strip()
        if country_code and text.startswith(country_code):
            text = text[len(country_code):].strip()
        elif text.startswith("+"):
            text = re.sub(r"^\+\d{1,4}\s*", "", text).strip()
        digits = re.sub(r"\D+", "", text)
        code_digits = re.sub(r"\D+", "", country_code)
        if code_digits and digits.startswith(code_digits) and len(digits) > len(code_digits) + 4:
            digits = digits[len(code_digits):]
        return digits

    digits = local_digits(value)
    if len(digits) >= 7:
        return digits
    fallback_digits = local_digits(getattr(candidate, "phone", ""))
    return fallback_digits or str(value or "")


DONE_TEXTS = (
    "thank you for applying",
    "thank you for your application",
    "thanks for applying",
    "thanks for your application",
    "application submitted",
    "application has been submitted",
    "your application has been submitted",
    "successfully applied",
    "application received",
    "we received your application",
    "we've received your application",
    "your application was received",
    "application complete",
    "application completed",
    "you have applied",
    "your application is complete",
)
DONE_FALSE_POSITIVE_TEXTS = (
    "does not mean that you have applied",
    "does not mean you have applied",
    "saving a job does not mean",
    "resource not found",
    "resource-not-found",
    "job opportunity is no longer available",
    "job opportunity is no longer active",
    "this job opportunity is no longer available",
    "this job opportunity is no longer active",
    "no longer accepting applications",
    "not accepting applications",
    "this position is no longer accepting applications",
    "position is closed",
    "job posting is closed",
    "posting has expired",
    "job posting has expired",
    "job has been closed",
    "job is closed",
    "job no longer exists",
    "job is no longer available",
    "this job is no longer available",
    "there are no saved jobs",
)
GENERIC_MANUAL_LABELS = {
    "select",
    "select...",
    "select one",
    "select one required",
    "select-one",
    "choose",
    "choose...",
    "choose one",
    "choose one required",
    "required",
}
GUESS_ONLY_OPTION_SETS = {
    frozenset({"yes", "no"}),
    frozenset({"yes", "no", "other"}),
}


async def log(application_id: UUID, level: str, message: str, db: AsyncSession) -> None:
    await write_run_log(db, application_id, level, message)
    await db.commit()


async def _log(application_id: UUID, level: str, message: str) -> None:
    async with SessionLocal() as session:
        await log(application_id, level, message, session)


async def _load_application(session: AsyncSession, application_id: UUID) -> tuple[Application, Candidate, Job]:
    application = await session.get(Application, application_id)
    if application is None:
        raise RuntimeError(f"Application {application_id} not found")
    candidate = await session.get(Candidate, application.candidate_id)
    if candidate is None:
        raise RuntimeError(f"Candidate {application.candidate_id} not found")
    job = await session.get(Job, application.job_id)
    if job is None:
        raise RuntimeError(f"Job {application.job_id} not found")
    return application, candidate, job


def _latest_saved_step_url(application_id: UUID) -> str | None:
    directory = SCREENSHOT_ROOT / str(application_id)
    if not directory.exists():
        return None
    step_files = sorted(
        directory.glob("step_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in step_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _saved_step_has_previous_worker_draft(payload):
            return None
        url = str(payload.get("url") or "").strip()
        text = str(payload.get("text") or "").lower()
        if (
            url
            and url.lower().startswith(("http://", "https://"))
            and not url.lower().startswith(("about:", "chrome:", "edge:"))
            and "myworkdayjobs.com" in url
            and "create account/sign in" not in text
        ):
            return url
    return None


def _saved_step_has_previous_worker_draft(payload: dict[str, object]) -> bool:
    fields = payload.get("fields") or []
    if not isinstance(fields, list):
        return False
    has_previous_worker_details = False
    has_previous_worker_yes = False
    for field in fields:
        if not isinstance(field, dict):
            continue
        selector = str(field.get("selector") or "")
        label = str(field.get("label") or "")
        automation_id = str(field.get("automationId") or "")
        if "previousWorker--" in selector:
            has_previous_worker_details = True
        if str(field.get("type") or "").lower() != "radio":
            continue
        radio_text = " ".join((label, selector, automation_id))
        if not _is_previous_worker_label_text(radio_text):
            continue
        for option in field.get("radioOptions") or []:
            if not isinstance(option, dict) or not option.get("checked"):
                continue
            option_text = " ".join(
                str(option.get(key) or "")
                for key in ("label", "value")
            ).strip().lower()
            if "yes" in option_text or option_text == "true":
                has_previous_worker_yes = True
    return has_previous_worker_details and has_previous_worker_yes


def _has_saved_previous_worker_draft(application_id: UUID) -> bool:
    directory = SCREENSHOT_ROOT / str(application_id)
    if not directory.exists():
        return False
    for path in directory.glob("step_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _saved_step_has_previous_worker_draft(payload):
            return True
    return False


async def _set_status(
    session: AsyncSession,
    application: Application,
    status: str,
    *,
    error: str | None = None,
    screenshot_path: str | None = None,
) -> None:
    application.status = status
    if status == "running" and application.started_at is None:
        application.started_at = datetime.now(timezone.utc)
    if status in {"completed", "failed", "needs_manual", "validation_error"}:
        application.completed_at = datetime.now(timezone.utc)
    if error is not None:
        application.last_error = error
    if screenshot_path is not None:
        application.screenshot_path = screenshot_path
    await session.commit()


async def _screenshot(page: object, application_id: UUID, step: int, db: AsyncSession | None = None) -> str:
    directory = SCREENSHOT_ROOT / str(application_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"step_{step}.png"
    # Workday tenants serve custom web fonts that occasionally never resolve
    # the FontFaceSet ready promise. Playwright's default screenshot path
    # awaits fonts and times out after 30s, killing the entire run before any
    # step can execute. Cap the screenshot at 8s and fall back to a viewport
    # shot — losing a screenshot is far better than failing the application.
    try:
        await page.screenshot(path=str(path), full_page=True, timeout=8000)
    except Exception:
        try:
            await page.screenshot(path=str(path), full_page=False, timeout=8000)
        except Exception as exc:
            logger.warning("screenshot failed for step %s: %s", step, exc)
            # Write a 1-byte placeholder so downstream code that expects the
            # file to exist doesn't crash.
            try:
                path.write_bytes(b"")
            except Exception:
                pass
    relative_path = str(path.relative_to(ROOT_DIR))

    if db is not None:
        application = await db.get(Application, application_id)
        if application is not None:
            application.screenshot_path = relative_path
            await db.commit()

    return relative_path


async def _save_step_context(
    page: object,
    application_id: UUID,
    step: int,
    page_data: dict[str, object],
) -> tuple[str | None, str | None]:
    directory = SCREENSHOT_ROOT / str(application_id)
    directory.mkdir(parents=True, exist_ok=True)

    html_relative_path: str | None = None
    json_relative_path: str | None = None

    try:
        html_path = directory / f"step_{step}.html"
        html_path.write_text(await page.content(), encoding="utf-8")
        html_relative_path = str(html_path.relative_to(ROOT_DIR))
    except Exception:
        html_relative_path = None

    try:
        json_path = directory / f"step_{step}.json"
        json_path.write_text(json.dumps(page_data, indent=2, ensure_ascii=False), encoding="utf-8")
        json_relative_path = str(json_path.relative_to(ROOT_DIR))
    except Exception:
        json_relative_path = None

    return html_relative_path, json_relative_path


async def _needs_manual(application_id: UUID, message: str, screenshot_path: str | None) -> None:
    async with SessionLocal() as session:
        application = await session.get(Application, application_id)
        if application is not None:
            error = (
                application.last_error
                if message == "Browser stopped because a manual answer or blocker remained"
                and application.last_error
                else message
            )
            await _set_status(session, application, "needs_manual", error=error, screenshot_path=screenshot_path)
            await log(application_id, "warn", message, session)


async def _validation_error(application_id: UUID, message: str, screenshot_path: str | None) -> None:
    async with SessionLocal() as session:
        application = await session.get(Application, application_id)
        if application is not None:
            error = (
                application.last_error
                if message == "Browser stopped because a validation error remained"
                and application.last_error
                else message
            )
            await _set_status(session, application, "validation_error", error=error, screenshot_path=screenshot_path)
            await log(application_id, "warn", message, session)


async def _complete(application_id: UUID, screenshot_path: str | None) -> None:
    async with SessionLocal() as session:
        application = await session.get(Application, application_id)
        if application is not None:
            application.last_error = None
            await _set_status(session, application, "completed", screenshot_path=screenshot_path)
            await log(application_id, "info", "application DONE - success text found", session)


async def _purge_previous_worker_form_answers(db: AsyncSession, candidate_id: object) -> None:
    """Delete stale FormAnswer rows where field_label matches previous-worker patterns.

    Called once per application run so these rows never shadow the hardcoded 'No'.
    """
    from sqlalchemy import delete as _sa_delete
    _PATTERNS = (
        "%previous%worker%",
        "%worked%before%",
        "%previously%employed%",
        "%former%employee%",
        "%candidateispreviousworker%",
    )
    try:
        from sqlalchemy import or_ as _or
        conditions = [FormAnswer.question_text.ilike(p) for p in _PATTERNS]
        await db.execute(
            _sa_delete(FormAnswer).where(
                FormAnswer.candidate_id == candidate_id,
                _or(*conditions),
            )
        )
        await db.commit()
    except Exception as exc:
        logger.warning("_purge_previous_worker_form_answers failed: %s", exc)


async def _body_text(page: object) -> str:
    try:
        return (await page.evaluate("document.body ? document.body.innerText : ''")).lower()
    except Exception:
        return ""


async def _workday_auth_blocker_message(page: object) -> str:
    text = await _body_text(page)
    if "verify your account before you sign in" in text or "account may need verification" in text:
        return "Workday requires email account verification before sign-in can continue"
    if "wrong email address or password" in text or "account might be locked" in text:
        return "Workday rejected the saved email/password or the account is locked; manual sign-in is required"
    if (
        "verify your email" in text
        or "verification code" in text
        or "enter the code" in text
        or "check your email" in text
    ):
        return "Workday requires email verification before the application can continue"
    if "captcha" in text or "security code" in text:
        return "Workday requires a security challenge before the application can continue"
    return "Workday login/signup did not complete; manual sign-in is required"


def _security_challenge_surface(page_data: dict[str, object]) -> bool:
    text = " ".join(str(page_data.get("text") or "").lower().split())
    if any(
        marker in text
        for marker in (
            "let's confirm you are human",
            "complete the security check before continuing",
            "human verification",
            "protected by hcaptcha",
            "captcha challenge",
        )
    ):
        return True
    for frame in page_data.get("iframes", []):
        if not isinstance(frame, dict):
            continue
        frame_text = " ".join(
            " ".join(str(frame.get(key) or "").lower().split())
            for key in ("src", "title", "ariaLabel")
        )
        if "recaptcha/api2/anchor" in frame_text:
            continue
        if "hcaptcha" in frame_text or "captcha challenge" in frame_text or "recaptcha/api2/bframe" in frame_text or "challenge" in frame_text:
            return True
    return False


async def _wait_for_manual_auth_completion(
    page: object,
    application_id: UUID,
    db: AsyncSession,
    step: int,
    message: str,
) -> str:
    await log(
        application_id,
        "warn",
        f"Step {step}: {message}. Complete sign-in in the browser window; waiting up to {MANUAL_AUTH_WAIT_SECONDS // 60} minutes.",
        db,
    )
    deadline = asyncio.get_running_loop().time() + MANUAL_AUTH_WAIT_SECONDS
    last_url = getattr(page, "url", "")

    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(5)
        try:
            page_data = await _inspect_page_with_retry(
                page,
                application_id,
                db,
                step,
                "manual auth wait",
            )
        except Exception as exc:
            if _is_transient_page_error(exc):
                await _wait_for_page_stability(page)
                continue
            raise

        current_url = str(page_data.get("url") or getattr(page, "url", ""))
        domain = domain_from_url(current_url)
        if current_url != last_url:
            await log(application_id, "info", f"Step {step}: manual auth URL changed to {current_url}", db)
            last_url = current_url

        if await _done_detected(page):
            return "completed"
        if "myworkdayjobs.com" in domain and _workday_auth_page_detected(page_data):
            continue

        text = str(page_data.get("text") or "").lower()
        if any(
            marker in text
            for marker in (
                "my information",
                "my experience",
                "application questions",
                "voluntary disclosures",
                "review",
            )
        ):
            await log(application_id, "info", f"Step {step}: manual auth appears complete; continuing automation", db)
            return "continue"

    await log(application_id, "warn", f"Step {step}: manual auth wait timed out", db)
    return "needs_manual"


def _is_transient_page_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "execution context was destroyed" in message or "most likely because of a navigation" in message


async def _wait_for_page_stability(page: object) -> None:
    for state in ("domcontentloaded", "load", "networkidle"):
        try:
            await page.wait_for_load_state(state, timeout=5000)
        except Exception:
            continue


# Workday's React app renders the apply UI AFTER networkidle fires.
# Marker elements (selected by data-automation-id) are present on every Workday
# page worth inspecting: auth form, apply-choice picker, application form steps,
# review page. If none appear within the timeout, the page is genuinely empty
# (dead listing) or anti-bot blocked — both are correctly handled downstream.
_WORKDAY_CONTENT_SELECTORS = ", ".join((
    # Auth: Create Account / Sign In forms
    "[data-automation-id='email']",
    "[data-automation-id='emailAddress']",
    "[data-automation-id='createAccountSubmitButton']",
    "[data-automation-id='signInSubmitButton']",
    "[data-automation-id='password']",
    # Apply choice picker
    "[data-automation-id='applyManually']",
    "[data-automation-id='useMyLastApplication']",
    "[data-automation-id='autofillWithResume']",
    # Application form / review / submit
    "[data-automation-id='wizardNavigationItem']",
    "[data-automation-id='pageHeader']",
    "[data-automation-id='formField-legalNameSection']",
    "[data-automation-id='fileUploadInputAuto']",
    "[data-automation-id='bottom-navigation-next-button']",
    "[data-automation-id='reviewSubmit']",
    # Generic Workday page chrome that only appears once content is mounted
    "[data-automation-id='multiViewContainer']",
    "[data-automation-widget='multiViewContainer']",
))


async def _wait_for_workday_content_ready(page: object, timeout_ms: int = 15000) -> bool:
    """Wait for any real Workday content marker to appear after a navigation.

    Networkidle fires before Workday's lazy-loaded React content mounts, so
    inspecting at that point captures only the header chrome and the engine
    misclassifies the page as 'unknown'. Returns True when content appeared,
    False on timeout (caller continues — downstream handlers cope with empty).
    """
    try:
        await page.wait_for_selector(_WORKDAY_CONTENT_SELECTORS, timeout=timeout_ms, state="attached")
        return True
    except Exception:
        return False


async def _inspect_page_with_retry(
    page: object,
    application_id: UUID,
    db: AsyncSession,
    step: int,
    phase: str,
) -> dict[str, object]:
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            return await inspect_page(page)
        except Exception as exc:
            last_exc = exc
            if not _is_transient_page_error(exc) or attempt == 3:
                raise
            await log(
                application_id,
                "warn",
                f"Step {step}: transient navigation during {phase}; retrying inspect ({attempt}/3)",
                db,
            )
            await _wait_for_page_stability(page)
            await asyncio.sleep(1)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("inspect_page retry failed without exception")


async def _safe_scroll(page: object, amount: int) -> bool:
    try:
        await page.evaluate("(amount) => window.scrollBy(0, amount)", amount)
        return True
    except Exception as exc:
        if _is_transient_page_error(exc):
            await _wait_for_page_stability(page)
            return False
        raise


async def _done_detected(page: object) -> bool:
    text = await _body_text(page)
    if any(phrase in text for phrase in DONE_FALSE_POSITIVE_TEXTS):
        return False
    return any(phrase in text for phrase in DONE_TEXTS)


async def _post_application_followup_detected(page: object) -> bool:
    text = await _body_text(page)
    return post_application_followup_text_detected(text, DONE_TEXTS)


def _verification_code_blocker_detected(page_data: dict[str, object]) -> bool:
    return verification_code_blocker_detected(page_data)


def _workday_auth_page_detected(page_data: dict[str, object]) -> bool:
    return workday_auth_page_detected(page_data)


async def _saved_answers(db: AsyncSession, candidate: Candidate, page_url: str) -> dict[str, str]:
    domain = domain_from_url(page_url)
    is_workday_domain = "workdayjobs.com" in domain

    # Pull every UnansweredQuestion this candidate has actually answered, and
    # promote them into FormAnswer rows (lazy upsert, newer-wins) so a manual
    # answer entered once is reused for every future application.
    answered_uq_result = await db.execute(
        select(UnansweredQuestion)
        .where(UnansweredQuestion.candidate_id == candidate.id)
        .where(UnansweredQuestion.answered_at.is_not(None))
        .where(UnansweredQuestion.recruiter_answer.is_not(None))
        .order_by(UnansweredQuestion.answered_at.desc())
    )
    answered_uq_rows = list(answered_uq_result.scalars().all())
    promoted_keys: set[tuple[str, str, str]] = set()
    for uq in answered_uq_rows:
        uq_label = str(uq.field_label or "").strip()
        uq_answer = str(uq.recruiter_answer or "").strip()
        uq_domain = str(uq.domain or "").strip() or None
        if not uq_label or not uq_answer:
            continue
        key = (str(candidate.id), uq_domain or "", uq_label)
        if key in promoted_keys:
            continue
        promoted_keys.add(key)
        existing = await db.execute(
            select(FormAnswer)
            .where(FormAnswer.candidate_id == candidate.id)
            .where(FormAnswer.domain.is_(None) if uq_domain is None else FormAnswer.domain == uq_domain)
            .where(FormAnswer.question_text == uq_label)
            .order_by(FormAnswer.created_at.desc())
            .limit(1)
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row is None:
            db.add(FormAnswer(
                candidate_id=candidate.id,
                domain=uq_domain,
                question_text=uq_label,
                answer=uq_answer,
            ))
        elif (existing_row.answer or "").strip() != uq_answer:
            existing_row.answer = uq_answer
    if promoted_keys:
        try:
            await db.flush()
        except Exception as exc:
            logger.warning("FormAnswer promotion flush failed: %s", exc)

    result = await db.execute(
        select(FormAnswer)
        .where(FormAnswer.candidate_id == candidate.id)
        .order_by(FormAnswer.created_at.desc())
    )
    rows = list(result.scalars().all())
    priority_rows = sorted(
        rows,
        key=lambda row: 0
        if domain and row.domain == domain
        else 1
        if not row.domain
        else 2,
    )
    answers: dict[str, str] = {}
    domain_answers: dict[str, str] = {}
    reusable_answers: dict[str, str] = {}

    def is_bad_phone_answer(label: str, value: str) -> bool:
        normalized_label = " ".join(label.lower().replace("_", " ").replace("-", " ").split())
        normalized_value = " ".join(value.lower().replace("_", " ").replace("-", " ").split())
        has_dial_code = bool(re.search(r"\+\d{1,4}", value))
        digit_count = len(re.sub(r"\D+", "", value))
        if any(token in normalized_label for token in ("phone device", "phone type")):
            return has_dial_code
        if "phone extension" in normalized_label or ("extension" in normalized_label and "phone" in normalized_label):
            return has_dial_code
        if any(token in normalized_label for token in ("country phone code", "phone code", "dialing code", "calling code")):
            return normalized_value in {"mobile", "landline", "phone", "telephone"}
        if not any(token in normalized_label for token in ("phone", "mobile", "cell", "telephone")):
            return False
        return has_dial_code and digit_count <= 4

    for answer in priority_rows:
        label = str(answer.question_text or "").strip()
        value = str(answer.answer or "").strip()
        if label and value and is_bad_phone_answer(label, value):
            continue
        if label and value and label not in answers:
            answers[label] = value
        if label and value and domain and answer.domain == domain and label not in domain_answers:
            domain_answers[label] = value
        source_like = any(
            token in label.lower()
            for token in ("how did you hear", "where did you hear", "hear about us", "learn about this opportunity", "source")
        )
        if label and value and label not in reusable_answers and not (is_workday_domain and source_like):
            reusable_answers[label] = value

    # Domain-specific answers win, but profile-style manual answers should carry across sites.
    for label, value in reusable_answers.items():
        answers.setdefault(label, value)
    answers.update(domain_answers)
    return answers


def _candidate_dict(candidate: Candidate, saved_answers: dict[str, str] | None = None) -> dict[str, object]:
    resume_path = getattr(candidate, "resume_path", None) or ""
    if resume_path:
        path = Path(str(resume_path))
        if not path.is_absolute():
            path = ROOT_DIR / path
        resume_path = str(path.resolve())

    def normalize_linkedin_url(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("http://"):
            text = "https://" + text.removeprefix("http://")
        elif not text.startswith("https://"):
            text = "https://" + text
        if "linkedin.com/" in text and "www.linkedin.com/" not in text:
            text = text.replace("linkedin.com/", "www.linkedin.com/", 1)
        return text

    normalized_saved_answers = dict(saved_answers or {})
    for key, value in list(normalized_saved_answers.items()):
        if "linkedin" in key.lower():
            normalized_saved_answers[key] = normalize_linkedin_url(value)

    return {
        "name": candidate.name,
        "email": candidate.email,
        "phone": candidate.phone or "",
        "location": candidate.location or "",
        "experience_years": candidate.experience_years or 0,
        "skills": candidate.skills or [],
        "desired_titles": candidate.desired_titles or [],
        "linkedin_url": normalize_linkedin_url(candidate.linkedin_url),
        "portfolio_url": candidate.portfolio_url or "",
        "resume_path": resume_path,
        "extra_answers": candidate.extra_answers or {},
        "saved_answers": normalized_saved_answers,
    }


async def _workday_generic_auth_answers(
    db: AsyncSession,
    candidate: Candidate,
    page_data: dict[str, object],
) -> dict[str, str]:
    if not _workday_auth_page_detected(page_data):
        return {}

    fields = [field for field in page_data.get("fields", []) if isinstance(field, dict)]
    password_fields = [
        field
        for field in fields
        if str(field.get("type") or "").lower() == "password"
        or "password" in " ".join(
            str(field.get(key) or "")
            for key in ("label", "placeholder", "automationId")
        ).lower()
    ]
    if not password_fields:
        return {}

    domain = domain_from_url(str(page_data.get("url") or ""))
    result = await db.execute(
        select(Credential).where(
            Credential.candidate_id == candidate.id,
            Credential.domain == domain,
        )
    )
    credential = result.scalar_one_or_none()
    signup_surface = len(password_fields) >= 2
    password = str(getattr(credential, "password", "") or "")
    if signup_surface:
        visible_password = str(password_fields[0].get("value") or "").strip()
        password = visible_password or password or _strong_password()
        if credential is None:
            db.add(
                Credential(
                    candidate_id=candidate.id,
                    domain=domain,
                    email=candidate.email,
                    password=password,
                )
            )
        else:
            credential.email = candidate.email
            credential.password = password
        await db.commit()

    if not password:
        return {}

    return {
        "email address": candidate.email,
        "email": candidate.email,
        "password": password,
        "verify new password": password,
        "verify password": password,
        "confirm password": password,
    }


async def _applicantstack_signup_answers(
    db: AsyncSession,
    candidate: Candidate,
    page_data: dict[str, object],
) -> dict[str, str]:
    url = str(page_data.get("url") or "").lower()
    page_text = " ".join(str(page_data.get("text") or "").lower().split())
    if "applicantstack.com" not in url or "create an account" not in page_text:
        return {}

    fields = [field for field in page_data.get("fields", []) if isinstance(field, dict)]
    password_fields = [
        field
        for field in fields
        if str(field.get("type") or "").lower() == "password"
        or "password" in " ".join(
            str(field.get(key) or "")
            for key in ("label", "placeholder", "automationId")
        ).lower()
    ]
    if len(password_fields) < 2:
        return {}

    domain = domain_from_url(str(page_data.get("url") or ""))
    result = await db.execute(
        select(Credential).where(
            Credential.candidate_id == candidate.id,
            Credential.domain == domain,
        )
    )
    credential = result.scalar_one_or_none()
    password = str(getattr(credential, "password", "") or "") or _strong_password()
    if credential is None:
        db.add(
            Credential(
                candidate_id=candidate.id,
                domain=domain,
                email=candidate.email,
                password=password,
            )
        )
    else:
        credential.email = candidate.email
        credential.password = password
    await db.commit()

    return {
        "username": candidate.email,
        "email": candidate.email,
        "email address": candidate.email,
        "name": candidate.name,
        "password": password,
        "confirm password": password,
        "verify password": password,
    }


def _valid_manual_question(label: str, field_type: str, options: list[str]) -> bool:
    normalized_label = " ".join(label.lower().replace("_", " ").replace("-", " ").split())
    normalized_options = [
        " ".join(option.lower().replace("_", " ").replace("-", " ").split())
        for option in options
    ]
    meaningful_options = [
        option
        for option in normalized_options
        if option and option not in GENERIC_MANUAL_LABELS
    ]
    if not normalized_label or normalized_label in GENERIC_MANUAL_LABELS:
        return False
    if re.fullmatch(r":?r\d+:?\s*\d*", normalized_label) or re.fullmatch(r"[:#._\-\s\d]+", normalized_label):
        return False
    if field_type == "radio" and not meaningful_options:
        return False
    if _looks_like_stale_manual_options(normalized_label, normalized_options, field_type):
        return False
    return True


def _looks_like_stale_manual_options(label: str, options: list[str], field_type: str | None = None) -> bool:
    normalized_type = str(field_type or "").lower()
    if "select" not in normalized_type and normalized_type != "radio":
        return False
    option_set = frozenset(option for option in options if option)
    if option_set and option_set <= {"no items", "no items."}:
        return True
    if "type to add" in label and "skill" in label and not option_set:
        return True
    if option_set in GUESS_ONLY_OPTION_SETS and not re.match(r"^(are|do|does|did|have|has|is|will|would|can|could|should)\b", label):
        return True
    if option_set and any(token in label for token in ("how did you hear", "hear about us", "source")) and option_set <= {"yes", "no", "other"}:
        return True
    if option_set and any(token in label for token in ("how did you hear", "hear about us", "source")):
        phone_code_like = [option for option in options if re.search(r"\+\d{1,4}", option)]
        if len(phone_code_like) == len([option for option in options if option]):
            return True
    if any(token in label for token in ("phone device", "phone type")) and options:
        phone_code_like = [option for option in options if re.search(r"\+\d{1,4}", option)]
        if len(phone_code_like) == len([option for option in options if option]):
            return True
    if "country phone code" in label and any(option in {"landline", "mobile"} for option in options):
        return True
    return False


def _options_need_recapture(options: list[str]) -> bool:
    if not options:
        return True
    normalized = [_normalize_option_text(option) for option in options]
    normalized = [option for option in normalized if option]
    if len(normalized) <= 1:
        return True
    if len(set(option.lower() for option in normalized)) < len(normalized):
        return True
    return any("press delete to clear value" in str(option).lower() for option in options)


def _looks_like_validation_option(label: str | None, option: str) -> bool:
    normalized_label = " ".join(str(label or "").lower().replace("_", " ").replace("-", " ").split())
    normalized_option = " ".join(str(option or "").lower().replace("_", " ").replace("-", " ").split()).strip("'\" .:;")
    if not normalized_option:
        return False
    if normalized_option in {
        "this field is required",
        "field is required",
        "required field",
        "required",
        "please fill out this field",
        "please complete this field",
        "please enter a value",
        "please enter value",
        "please select a value",
        "please select an option",
        "please choose an option",
    }:
        return True
    if normalized_option.startswith("error:"):
        return True
    if normalized_option.startswith("must be a valid "):
        return True
    if normalized_option in {
        "invalid value",
        "invalid format",
        "please enter a valid value",
        "please enter a valid option",
        "please enter valid value",
        "please enter valid option",
    }:
        return True
    if normalized_option.startswith(("error ", "invalid ", "please enter a valid ", "please select a valid ")):
        return True
    if any(
        marker in normalized_option
        for marker in (
            " is not in a valid format",
            " is not valid",
            " not in a valid format",
            " invalid date",
            " invalid format",
        )
    ):
        return True
    if normalized_option.endswith(" is required"):
        field_name = normalized_option[: -len(" is required")].strip("'\" ")
        return (
            not field_name
            or field_name in {"this field", "field", "selection", "value", "answer", "response"}
            or field_name == normalized_label
            or field_name in normalized_label
            or normalized_label in field_name
        )
    return False


def _clean_manual_options(label: str, field_type: str, options: list[str]) -> list[str]:
    normalized_type = str(field_type or "").lower()
    if "select" not in normalized_type and normalized_type != "radio":
        return [
            str(option or "").strip()
            for option in options
            if str(option or "").strip() and not _looks_like_validation_option(label, str(option or ""))
        ]
    normalized_label = " ".join(label.lower().replace("_", " ").replace("-", " ").split())
    cleaned: list[str] = []
    seen: set[str] = set()
    for option in options:
        text = _normalize_option_text(option)
        key = " ".join(text.lower().replace("_", " ").replace("-", " ").split())
        if _looks_like_validation_option(label, text):
            continue
        if not key or key in GENERIC_MANUAL_LABELS or key in {"select an option", "no items", "no items.", "expanded", "collapsed"}:
            continue
        if re.fullmatch(r"\d+\s+items?\s+selected", key):
            continue
        if any(token in normalized_label for token in ("how did you hear", "hear about us", "source")) and re.search(r"\+\d{1,4}", text):
            continue
        if any(token in normalized_label for token in ("phone device", "phone type")) and re.search(r"\+\d{1,4}", text):
            continue
        if "country phone code" in normalized_label and key in {"landline", "mobile"}:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def _fallback_manual_options(label: str, field_type: str) -> list[str]:
    normalized_type = str(field_type or "").lower()
    if normalized_type not in {"select-one", "select-multiple", "radio"}:
        return []
    text = " ".join(str(label or "").lower().replace("_", " ").replace("-", " ").split())
    if any(token in text for token in ("disability", "disabled")):
        return [
            "Yes, I have a disability (or previously had a disability)",
            "No, I don't have a disability",
            "I don't wish to answer",
        ]
    if any(token in text for token in ("veteran", "protected veteran")):
        return [
            "I am not a protected veteran",
            "I identify as one or more of the classifications of a protected veteran",
            "I don't wish to answer",
        ]
    if "gender" in text:
        return ["Male", "Female", "Non-binary", "I don't wish to answer"]
    if "ethnicity" in text or "race" in text:
        return [
            "Hispanic or Latino",
            "White (Not Hispanic or Latino)",
            "Black or African American (Not Hispanic or Latino)",
            "Asian (Not Hispanic or Latino)",
            "Two or More Races (Not Hispanic or Latino)",
            "I don't wish to answer",
        ]
    if "hispanic" in text or "latino" in text:
        return [
            "Hispanic or Latino",
            "Not Hispanic or Latino",
            "I don't wish to answer",
        ]
    if re.match(r"^(are|is|do|does|did|have|has|will|would|can|could)\b", text):
        return ["Yes", "No"]
    if re.search(r"(?:[.:\n]\s*|\b)(are|is|do|does|did|have|has|will|would|can|could)\b", text):
        return ["Yes", "No"]
    return []


def _normalized_manual_field_type(label: str, field_type: str, options: list[str]) -> str:
    normalized_type = str(field_type or "").strip().lower()
    label_text = " ".join(str(label or "").lower().replace("_", " ").replace("-", " ").split())

    def has_label_term(*terms: str) -> bool:
        return any(re.search(rf"\b{re.escape(term)}\b", label_text) for term in terms)

    if normalized_type in {"select", "select-one", "select-multiple"} and not options:
        if has_label_term("phone", "mobile", "telephone") and not has_label_term(
            "country phone code", "phone code", "dialing code", "calling code", "phone type", "phone device"
        ):
            return "tel"
        if has_label_term("email", "e-mail"):
            return "email"
        if has_label_term("first name", "last name", "full name", "address", "city", "zip", "postal"):
            return "text"
        if has_label_term("date"):
            return "date"
    return normalized_type or "text"


def _normalize_option_text(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    text = re.sub(r",?\s*press delete to clear value\.?", "", text, flags=re.I).strip()
    lowered = text.lower()
    for prefix in ("0 items selected", "1 item selected", "1 items selected"):
        if lowered.startswith(prefix):
            text = text[len(prefix):].lstrip(" ,:-")
            lowered = text.lower()
    text = text.replace("not checked", " ").replace("checked", " ")
    return " ".join(text.split()).strip()


def _split_option_path(value: object) -> list[str]:
    return [
        segment
        for segment in (_normalize_option_text(part) for part in str(value or "").split(" > "))
        if segment
    ]


def _join_option_path(parts: list[str]) -> str:
    return " > ".join(_normalize_option_text(part) for part in parts if _normalize_option_text(part))


async def _capture_select_options(page: object, action: dict[str, object]) -> list[str]:
    selector = str(action.get("selector") or "").strip()
    field_type = str(action.get("field_type") or "").lower()
    control_kind = str(action.get("control_kind") or action.get("controlKind") or "").strip().lower()
    trace_label = str(action.get("label") or "")
    trace_enabled = "how did you hear about us" in trace_label.lower()
    if not selector or "select" not in field_type:
        return []
    if not all(hasattr(page, name) for name in ("evaluate", "locator")):
        return []

    async def collect() -> list[str]:
        rows = await page.evaluate(
            """
            ({selector, controlKind}) => {
              const visible = (node) => {
                if (!node) return false;
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const textOf = (node) => (
                node?.getAttribute('data-automation-label')
                || node?.getAttribute('aria-label')
                || node?.innerText
                || node?.textContent
                || ''
              ).trim();
              const el = document.querySelector(selector);
              if (!el) return [];

              if ((el.tagName || '').toLowerCase() === 'select') {
                return Array.from(el.options || []).map(option => option.text || option.label || option.value || '');
              }

              // For custom comboboxes, check for a hidden native <select> sibling
              // (many sites hide the native select and render a custom UI on top).
              const nativeContainer = el.closest('[data-automation-id*="formField"], .form-group, fieldset, label');
              const nativeSibling = nativeContainer && Array.from(nativeContainer.querySelectorAll('select'))
                .find(s => s !== el && (s.options || []).length > 1);
              if (nativeSibling) {
                const nativeOpts = Array.from(nativeSibling.options)
                  .filter(o => o.value !== '' && o.value !== undefined)
                  .map(o => (o.text || o.label || o.value || '').trim())
                  .filter(Boolean);
                if (nativeOpts.length > 0) return nativeOpts;
              }

              const normalizedControlKind = String(controlKind || '').toLowerCase();
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              const activeOverlayRoots = () => {
                const roots = [];
                const controlledId = el.getAttribute('aria-controls') || '';
                if (controlledId) {
                  const controlled = document.getElementById(controlledId);
                  if (controlled && visible(controlled)) {
                    roots.push(controlled);
                    // aria-controls binds this widget to its own listbox.
                    // Return ONLY that listbox — never fall through to the
                    // page-wide overlay scan, which would mix in options
                    // from other open dropdowns.
                    return roots;
                  }
                }
                // popup_select_button widgets that haven't been opened yet
                // (no aria-controls target) MUST NOT fall through to the
                // global overlay scan — otherwise we pick up listbox-chips
                // from OTHER fields' selected-item widgets and report them
                // as if they were this field's options. Force the outer
                // logic to click the button (which sets aria-controls) by
                // returning an empty roots list here.
                const isUnopenedPopupButton = (
                  (el.tagName || '').toLowerCase() === 'button'
                  && (el.getAttribute('aria-haspopup') || '').toLowerCase() === 'listbox'
                  && !roots.length
                );
                if (isUnopenedPopupButton) {
                  return roots;
                }
                const overlaySelectors = [
                  '[role="listbox"]',
                  '[data-automation-id="activeListContainer"]',
                  '[data-automation-id="menu"]',
                  '[data-automation-id="promptOptionList"]',
                  '.ant-select-dropdown:not(.ant-select-dropdown-hidden)',
                  '.select2-container--open .select2-results',
                  '[id*="-listbox"]',
                  '.dropdown-menu:not([hidden])',
                  '[class*="dropdown-list"]:not([hidden])',
                  '[class*="select-menu"]:not([hidden])',
                  '[class*="options-list"]:not([hidden])'
                ].join(',');
                for (const node of Array.from(document.querySelectorAll(overlaySelectors))) {
                  if (visible(node) && !roots.includes(node)) roots.push(node);
                }
                return roots;
              };
              const roots = prompt
                ? [prompt]
                : (normalizedControlKind && !normalizedControlKind.startsWith('native_') ? activeOverlayRoots() : []);
              const selectors = [
                '[role=option]',
                '[data-automation-id="menuItem"]',
                '[data-automation-id="promptLeafNode"]',
                '[data-automation-id="promptOption"]',
                '[role=listbox] li',
                '[role=listbox] button',
                '.ant-select-item-option',
                '.select2-results__option',
                '[id*="-listbox"] [id*="-option-"]',
                '.dropdown-item:not(.disabled)',
                'li[aria-selected]'
              ].join(',');
              const items = [];
              for (const root of roots) {
                for (const node of Array.from(root.querySelectorAll(selectors))) {
                  const text = textOf(node);
                  if (visible(node) && text && text.length <= 200) {
                    items.push(text);
                  }
                }
              }
              return items;
            }
            """,
            {"selector": selector, "controlKind": control_kind},
        )
        options: list[str] = []
        seen: set[str] = set()
        for row in rows or []:
            option = _normalize_option_text(row)
            lowered = option.lower()
            if not option or lowered in seen or lowered in GENERIC_MANUAL_LABELS:
                continue
            seen.add(lowered)
            options.append(option)
        return options

    async def collect_prompt_rows() -> list[dict[str, object]]:
        rows = await page.evaluate(
            """
            (selector) => {
              const visible = (node) => {
                if (!node) return false;
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const textOf = (node) => (
                node?.getAttribute('data-automation-label')
                || node?.getAttribute('aria-label')
                || node?.innerText
                || node?.textContent
                || ''
              ).trim();
              const el = document.querySelector(selector);
              if (!el) return [];
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              if (!prompt) return [];
              const root = prompt;
              const containers = Array.from(root.querySelectorAll(
                '[data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [role=option], [role=listbox] li'
              ));
              return containers.map((node) => {
                const row = node.closest('[data-automation-id="menuItem"], [role=option], li') || node;
                const text = textOf(node);
                const rowText = textOf(row);
                const iconText = textOf(row.querySelector('svg, [data-automation-id*="icon" i], i'));
                const ariaExpanded = String(row.getAttribute('aria-expanded') || '').toLowerCase();
                const hasChevron = /chevron|arrow|expand|next/i.test(
                  [
                    row.getAttribute('data-icon'),
                    row.getAttribute('aria-label'),
                    row.getAttribute('class'),
                    iconText,
                  ].filter(Boolean).join(' ')
                );
                return {
                  text: text || rowText,
                  hasChildren: hasChevron || ariaExpanded === 'false' || ariaExpanded === 'true',
                  visible: visible(row),
                };
              }).filter((item) => item.visible && item.text && item.text.length <= 200);
            }
            """,
            selector,
        )
        cleaned: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows or []:
            text = _normalize_option_text((row or {}).get("text"))
            lowered = text.lower()
            if not text or lowered in seen or lowered in GENERIC_MANUAL_LABELS:
                continue
            seen.add(lowered)
            cleaned.append({"text": text, "hasChildren": bool((row or {}).get("hasChildren"))})
        return cleaned

    async def click_row_center(option_text: str) -> bool:
        try:
            target_box = await page.evaluate(
                """
                ({selector, optionText}) => {
                  const el = document.querySelector(selector);
                  if (!el) return null;
                  const widgetId = el.getAttribute('data-uxi-multiselect-id')
                    || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                    || '';
                  const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                  if (!prompt) return null;
                  const root = prompt;
                  const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const textOf = (node) => (
                    node?.getAttribute('data-automation-label')
                    || node?.getAttribute('aria-label')
                    || node?.innerText
                    || node?.textContent
                    || ''
                  ).trim();
                  const target = normalize(optionText);
                  const nodes = Array.from(root.querySelectorAll(
                    '[data-automation-id="menuItem"], [role=option], [role=listbox] li, .ant-select-item-option'
                  )).filter(visible);
                  const match = nodes.find((node) => normalize(textOf(node)) === target)
                    || nodes.find((node) => normalize(textOf(node)).includes(target))
                    || nodes.find((node) => target.includes(normalize(textOf(node))));
                  if (!match) return null;
                  const rect = match.getBoundingClientRect();
                  return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
                }
                """,
                {"selector": selector, "optionText": option_text},
            )
            if target_box and target_box.get("x") and target_box.get("y"):
                await page.mouse.click(float(target_box["x"]), float(target_box["y"]))
                await asyncio.sleep(0.6)
                return True
        except Exception:
            return False
        return False

    async def prompt_snapshot() -> dict[str, object]:
        try:
            return await page.evaluate(
                """
                (selector) => {
                  const textOf = (node) => (
                    node?.getAttribute?.('data-automation-label')
                    || node?.getAttribute?.('aria-label')
                    || node?.innerText
                    || node?.textContent
                    || ''
                  ).trim();
                  const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const el = document.querySelector(selector);
                  if (!el) return {};
                  const widgetId = el.getAttribute('data-uxi-multiselect-id')
                    || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                    || '';
                  const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                  if (!prompt) return {};
                  const root = prompt;
                  const options = Array.from(root.querySelectorAll(
                    '[data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [role=option], [role=listbox] li'
                  )).filter(visible).map((node) => textOf(node)).filter(Boolean);
                  return {
                    promptOpen: Boolean(prompt),
                    hasBackButton: Boolean(prompt?.querySelector('[data-automation-id="backButton"]')),
                    firstText: options[0] || '',
                    texts: options.slice(0, 8),
                    count: options.length,
                    value: String(el.value || '').trim(),
                  };
                }
                """,
                selector,
            )
        except Exception:
            return {}

    def snapshot_changed(before: dict[str, object], after: dict[str, object]) -> bool:
        before_texts = [str(item).strip() for item in (before.get("texts") or []) if str(item).strip()]
        after_texts = [str(item).strip() for item in (after.get("texts") or []) if str(item).strip()]
        return (
            str(before.get("firstText") or "").strip() != str(after.get("firstText") or "").strip()
            or before_texts != after_texts
            or bool(before.get("hasBackButton")) != bool(after.get("hasBackButton"))
            or int(before.get("count") or 0) != int(after.get("count") or 0)
        )

    async def click_and_detect(option_text: str) -> str:
        before = await prompt_snapshot()
        clicked = await _click_prompt_option(option_text)
        if not clicked:
            return "no_change"
        await asyncio.sleep(0.6)
        after = await prompt_snapshot()
        if snapshot_changed(before, after) or bool(after.get("hasBackButton")):
            return "submenu_opened"
        if not bool(after.get("promptOpen")):
            return "leaf_selected"
        before_value = _normalize_option_text(before.get("value"))
        after_value = _normalize_option_text(after.get("value"))
        if after_value and after_value != before_value:
            return "leaf_selected"
        return "no_change"

    async def capture_nested_paths(root_options: list[str]) -> tuple[list[str], bool]:
        if not root_options:
            return [], False
        try:
            tag_name = str(
                await page.evaluate(
                    """(selector) => {
                      const el = document.querySelector(selector);
                      return (el?.tagName || '').toLowerCase();
                    }""",
                    selector,
                )
            ).lower()
        except Exception:
            tag_name = ""
        if tag_name == "select":
            return [], False

        async def open_prompt() -> bool:
            try:
                await page.locator(selector).first.click(force=True, timeout=2500)
                await asyncio.sleep(0.5)
                return True
            except Exception:
                try:
                    if await _click_selector(page, selector):
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    pass
            return False

        async def back_to_root() -> None:
            for _ in range(3):
                backed = False
                try:
                    backed = bool(
                        await page.evaluate(
                            """
                            (selector) => {
                              const el = document.querySelector(selector);
                              if (!el) return false;
                              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                                || '';
                              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                              const back = prompt?.querySelector('[data-automation-id="backButton"]');
                              if (!back) return false;
                              back.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                              return true;
                            }
                            """,
                            selector,
                        )
                    )
                except Exception:
                    backed = False
                if not backed:
                    break
                await asyncio.sleep(0.4)

        async def submenu_opened() -> bool:
            try:
                return bool(
                    await page.evaluate(
                        """
                        (selector) => {
                          const el = document.querySelector(selector);
                          if (!el) return false;
                          const widgetId = el.getAttribute('data-uxi-multiselect-id')
                            || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                            || '';
                          const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                          return Boolean(prompt?.querySelector('[data-automation-id="backButton"]'));
                        }
                        """,
                        selector,
                    )
                )
            except Exception:
                return False

        async def current_selected_value() -> str:
            try:
                value = await page.evaluate(
                    """
                    (selector) => {
                      const el = document.querySelector(selector);
                      if (!el) return '';
                      const widgetId = el.getAttribute('data-uxi-multiselect-id')
                        || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                        || '';
                      const container = el.closest('[data-automation-id="multiSelectContainer"]')
                        || (widgetId ? document.getElementById(widgetId) : null)
                        || el.closest('[data-automation-id*="formField" i], label, div, section, fieldset');
                      const textOf = (node) => (node?.innerText || node?.textContent || node?.getAttribute?.('aria-label') || '').trim();
                      return textOf(container?.querySelector('[data-automation-id="selectedItem"]'))
                        || textOf(container?.querySelector('[data-automation-id="promptSelectionLabel"]'))
                        || textOf(container?.querySelector('[data-automation-id="promptAriaInstruction"]'))
                        || '';
                    }
                    """,
                    selector,
                )
            except Exception:
                value = ""
            return _normalize_option_text(value)

        paths: list[str] = []
        seen_paths: set[str] = set()
        root_set = {option.lower() for option in root_options}
        prompt_rows = await collect_prompt_rows()
        row_map = {str(row.get("text") or "").lower(): row for row in prompt_rows}
        hierarchical_detected = any(bool(row.get("hasChildren")) for row in prompt_rows)

        if not await open_prompt():
            return [], hierarchical_detected
        await back_to_root()

        for parent in root_options:
            await back_to_root()
            before_snapshot = await prompt_snapshot()
            clicked = await click_row_center(parent)
            if not clicked:
                continue
            after_snapshot = await prompt_snapshot()
            outcome = "submenu_opened" if snapshot_changed(before_snapshot, after_snapshot) or bool(after_snapshot.get("hasBackButton")) else "no_change"
            branch_row = row_map.get(parent.lower(), {})
            branch_expected = bool(branch_row.get("hasChildren"))
            branch_open = await submenu_opened()
            hierarchical_detected = hierarchical_detected or branch_expected or branch_open or outcome == "submenu_opened"
            child_options = await collect()
            # Workday submenus render their list inside a react-virtualized
            # scroller (data-automation-id="activeListContainer"): only a
            # window of ~6 rows is in the DOM at any time. Without scrolling
            # we miss the rest. Scroll the active container in steps and
            # accumulate every new option we see — isolated to the nested
            # walk, so the root collect() and other call sites are unaffected.
            try:
                scrolled_any = False
                seen_child = {opt.lower() for opt in child_options}
                for _scroll_step in range(80):
                    scrolled = await page.evaluate(
                        """
                        (selector) => {
                          const el = document.querySelector(selector);
                          if (!el) return false;
                          const widgetId = el.getAttribute('data-uxi-multiselect-id')
                            || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                            || '';
                          const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                          if (!prompt) return false;
                          const scroller = prompt.querySelector('[data-automation-id="activeListContainer"]');
                          if (!scroller || !scroller.scrollHeight) return false;
                          if (scroller.scrollHeight <= scroller.clientHeight + 5) return false;
                          const max = scroller.scrollHeight - scroller.clientHeight;
                          if (scroller.scrollTop >= max - 2) return false;
                          const step = Math.max(80, scroller.clientHeight - 40);
                          scroller.scrollTop = Math.min(max, scroller.scrollTop + step);
                          return true;
                        }
                        """,
                        selector,
                    )
                    if not scrolled:
                        break
                    scrolled_any = True
                    await asyncio.sleep(0.18)
                    new_options = await collect()
                    for opt in new_options:
                        if opt.lower() in seen_child:
                            continue
                        seen_child.add(opt.lower())
                        child_options.append(opt)
                if scrolled_any:
                    # Reset scroll position so subsequent prompt_snapshot /
                    # back_to_root calls observe the menu from the top.
                    await page.evaluate(
                        """
                        (selector) => {
                          const el = document.querySelector(selector);
                          if (!el) return;
                          const widgetId = el.getAttribute('data-uxi-multiselect-id')
                            || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                            || '';
                          const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                          const scroller = prompt?.querySelector('[data-automation-id="activeListContainer"]');
                          if (scroller) scroller.scrollTop = 0;
                        }
                        """,
                        selector,
                    )
            except Exception:
                # Scroll accumulation is best-effort — fall back to whatever
                # collect() returned in DOM at click time.
                pass
            selected_value = await current_selected_value()
            child_values = [
                child
                for child in child_options
                if child.lower() not in root_set
                and child.lower() != parent.lower()
            ]
            if child_values:
                for child in child_values:
                    path = _join_option_path([parent, child])
                    if path and path.lower() not in seen_paths:
                        seen_paths.add(path.lower())
                        paths.append(path)
                await back_to_root()
                continue
            if branch_expected or branch_open or outcome == "submenu_opened":
                await back_to_root()
                continue
            leaf = selected_value or parent
            path = _join_option_path([leaf])
            if path and path.lower() not in seen_paths:
                seen_paths.add(path.lower())
                paths.append(path)
            await open_prompt()
        try:
            if hasattr(page, "keyboard"):
                await page.keyboard.press("Escape")
        except Exception:
            pass
        return paths, hierarchical_detected

    try:
        options = await collect()
        if trace_enabled:
            print(f"[TRACE_SELECT_CAPTURE] initial collect label={trace_label!r} selector={selector!r} options={options!r}")
        if options:
            nested_paths, hierarchical_detected = await capture_nested_paths(options)
            if trace_enabled:
                print(
                    "[TRACE_SELECT_CAPTURE] initial nested "
                    f"label={trace_label!r} nested_paths={nested_paths!r} hierarchical_detected={hierarchical_detected!r}"
                )
            if nested_paths:
                return nested_paths
            if hierarchical_detected:
                action["_hierarchical_capture_incomplete"] = True
                return []
            return options

        try:
            await page.evaluate(
                """(selector) => {
                  const el = document.querySelector(selector);
                  if (!el) return;
                  el.scrollIntoView({block: 'center', inline: 'nearest'});
                }""",
                selector,
            )
        except Exception:
            pass

        try:
            await page.locator(selector).first.click(force=True, timeout=2500)
        except Exception:
            await _click_selector(page, selector)

        # Wait for the listbox to actually render an option (content-based),
        # not a fixed sleep. Workday React portals sometimes take >700 ms.
        try:
            await page.wait_for_selector(
                '[data-automation-id="activeListContainer"] [role="option"],'
                '[role="listbox"][aria-expanded="true"] [role="option"],'
                '[role="listbox"] [role="option"]',
                state="visible",
                timeout=4000,
            )
        except Exception:
            pass  # fall through; collect() retries below

        options = await collect()
        for _ in range(3):
            if options:
                break
            await asyncio.sleep(0.6)
            options = await collect()
        # Workday renders every selectedItem chip inside a listbox-shaped
        # container; collect's broad overlay scan picks them up too. Strip
        # values that came from chips outside the target field so the captured
        # option list reflects ONLY this field's choices (e.g. on the State
        # popup we were leaking "LinkedIn" and "India (+91)" from the
        # HDYHAU/phone-code chips).
        try:
            external_chip_values = await page.evaluate(
                """
                (selector) => {
                  const target = document.querySelector(selector);
                  const targetField = target?.closest('[data-automation-id*="formField" i], [data-fkit-id], [data-automation-id="multiSelectContainer"]')
                    || target?.closest('[data-automation-id*="formField" i]');
                  const chips = Array.from(document.querySelectorAll('[data-automation-id="selectedItem"]'));
                  const out = new Set();
                  for (const chip of chips) {
                    if (targetField && targetField.contains(chip)) continue;
                    const txt = (chip.innerText || chip.textContent || '').trim();
                    if (txt) out.add(txt.toLowerCase());
                  }
                  return Array.from(out);
                }
                """,
                selector,
            )
            external_chips_set = {str(v).lower() for v in (external_chip_values or [])}
            if external_chips_set:
                options = [o for o in options if _normalize_option_text(o).lower() not in external_chips_set]
        except Exception:
            pass
        if trace_enabled:
            print(f"[TRACE_SELECT_CAPTURE] after open collect label={trace_label!r} selector={selector!r} options={options!r}")
        try:
            if hasattr(page, "keyboard"):
                await page.keyboard.press("Escape")
        except Exception:
            pass
        if options:
            nested_paths, hierarchical_detected = await capture_nested_paths(options)
            if trace_enabled:
                print(
                    "[TRACE_SELECT_CAPTURE] after open nested "
                    f"label={trace_label!r} nested_paths={nested_paths!r} hierarchical_detected={hierarchical_detected!r}"
                )
            if nested_paths:
                return nested_paths
            if hierarchical_detected:
                action["_hierarchical_capture_incomplete"] = True
                return []
        return options
    except Exception:
        if trace_enabled:
            print(f"[TRACE_SELECT_CAPTURE] exception label={trace_label!r} selector={selector!r}", flush=True)
        return []


async def _save_unanswered(
    db: AsyncSession,
    page: object,
    candidate: Candidate,
    application_id: UUID,
    action: dict[str, object],
) -> None:
    label = str(action.get("label") or "").strip()
    field_type = str(action.get("field_type") or "").strip()
    control_kind = str(action.get("control_kind") or action.get("controlKind") or "").strip().lower()
    hierarchical_capture_incomplete = bool(action.get("_hierarchical_capture_incomplete"))
    options = [str(option) for option in (action.get("options") or [])]
    normalized_options = [_normalize_option_text(option) for option in options]
    force_recapture = control_kind in {"custom_select", "popup_select_button"} and "select" in field_type.lower()
    trace_enabled = "how did you hear about us" in label.lower()
    if force_recapture or _options_need_recapture(options):
        captured_options = await _capture_select_options(page, action)
        if trace_enabled:
            print(
                "[TRACE_SAVE_UNANSWERED] recapture "
                f"label={label!r} selector={action.get('selector')!r} control_kind={control_kind!r} "
                f"existing_options={options!r} captured_options={captured_options!r} "
                f"hier_incomplete={hierarchical_capture_incomplete!r}"
            )
        if captured_options:
            options = captured_options
            normalized_options = [_normalize_option_text(option) for option in options]
            action["options"] = options
            hierarchical_capture_incomplete = False
        else:
            # FNB-style typeahead probe: on some Workday tenants the dropdown
            # trigger opens an inline text input rather than a static list.
            # If we have an intended answer (rule/candidate-provided), try the
            # typeahead handshake before declaring the field unanswered.
            probe_answer = str(
                action.get("answer")
                or action.get("value")
                or ""
            ).strip()
            if probe_answer and control_kind in {"popup_select_button", "combobox_button", "custom_select"}:
                try:
                    probe_result = await probe_typeahead_popup(page, action, probe_answer)
                except Exception as probe_exc:
                    logger.debug("probe_typeahead_popup raised: %s", probe_exc)
                    probe_result = None
                if probe_result and probe_result.get("clicked_label"):
                    # Typeahead successfully committed a value — no need to
                    # surface this as unanswered. Mark as handled and return.
                    if trace_enabled:
                        print(
                            "[TRACE_SAVE_UNANSWERED] typeahead probe committed value "
                            f"label={label!r} clicked={probe_result.get('clicked_label')!r}"
                        )
                    return
            if options and (force_recapture or _options_need_recapture(options)):
                options = [] if hierarchical_capture_incomplete else normalized_options
                action["options"] = options
    elif hierarchical_capture_incomplete:
        options = []
        action["options"] = options
    # For demographic fields: when capture returned nothing, use hardcoded
    # fallback options immediately so the UnansweredQuestion row always has
    # choices the user can pick from.
    _DEMOGRAPHIC_TOKENS = ("gender", "ethnicity", "race", "hispanic", "latino", "veteran", "disability", "disabled")
    if not options and any(tok in label.lower() for tok in _DEMOGRAPHIC_TOKENS):
        demographic_fallback = _fallback_manual_options(label, field_type)
        if demographic_fallback:
            options = demographic_fallback
            action["options"] = options
    options = _clean_manual_options(label, field_type, options)
    if not options:
        options = _fallback_manual_options(label, field_type)
    # Detect mis-captured label: "phone device type" with source/referral options
    # means getLabel() grabbed text from the wrong container element.  Correct it
    # so the user sees the real question ("How did you hear about us?").
    if any(token in label.lower() for token in ("phone device", "phone type")) and options:
        _not_phone_codes = not all(bool(re.search(r"\+\d{1,4}", opt)) for opt in options if opt)
        if _not_phone_codes:
            _source_hints = frozenset({
                "facebook", "linkedin", "indeed", "twitter", "website", "referral",
                "home", "email", "friend", "google", "job board", "career", "newspaper",
                "instagram", "glassdoor", "naukri", "social media",
            })
            source_like = [opt for opt in options if opt and any(h in opt.lower() for h in _source_hints)]
            if source_like:
                label = "How did you hear about us?"
                field_type = "select-one"
                action["label"] = label
                action["field_type"] = field_type
    normalized_field_type = _normalized_manual_field_type(label, field_type, options)
    if normalized_field_type != field_type.lower():
        field_type = normalized_field_type
        if "select" not in field_type and field_type != "radio":
            options = []
    if trace_enabled:
        print(
            "[TRACE_SAVE_UNANSWERED] final "
            f"label={label!r} selector={action.get('selector')!r} options={options!r} "
            f"valid={_valid_manual_question(label, field_type, options)!r}"
        )
    action["options"] = options
    if not _valid_manual_question(label, field_type, options):
        return
    domain = anti_ban.domain_from_url(getattr(page, "url", ""))
    existing_result = await db.execute(
        select(UnansweredQuestion)
        .where(UnansweredQuestion.application_id == application_id)
        .where(UnansweredQuestion.candidate_id == candidate.id)
        .where(UnansweredQuestion.domain == domain)
        .where(UnansweredQuestion.field_label == label)
        .where(UnansweredQuestion.field_type == field_type)
        .where(UnansweredQuestion.answered_at.is_(None))
        .order_by(UnansweredQuestion.created_at.desc())
    )
    existing_rows = existing_result.scalars().all()
    for existing in existing_rows:
        if list(existing.options or []) == options:
            return
    for existing in existing_rows:
        await db.delete(existing)

    db.add(
        UnansweredQuestion(
            application_id=application_id,
            candidate_id=candidate.id,
            domain=domain,
            platform=platform_for_domain(domain).name if domain else None,
            field_label=label,
            field_type=field_type,
            options=options or None,
            is_required=bool(action.get("required", False)),
        )
    )
    await db.commit()


async def _click_selector(page: object, selector: str) -> bool:
    return bool(
        await page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                return true;
            }""",
            selector,
        )
    )


async def _clear_input(page: object, selector: str) -> None:
    await page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            if (!el) return;
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set;
            if (setter) {
                setter.call(el, '');
            } else {
                el.value = '';
            }
            el.dispatchEvent(new Event('input', {bubbles:true}));
        }""",
        selector,
    )


async def _dispatch_change(page: object, selector: str) -> None:
    await page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            if (el) el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        selector,
    )


async def _checkbox_is_checked(page: object, selector: str) -> bool:
    return bool(
        await page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                return Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
            }""",
            selector,
        )
    )


async def _radio_is_selected(page: object, selector: str, answer: str = "") -> bool:
    return bool(
        await page.evaluate(
            """({selector, answer}) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const el = document.querySelector(selector);
                if (!el) return false;
                const selected = Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
                if (!selected) return false;
                const expected = normalize(answer);
                if (!expected) return true;
                const textOf = (node) => (
                  node?.innerText
                  || node?.textContent
                  || node?.getAttribute?.('aria-label')
                  || node?.value
                  || ''
                ).trim();
                const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
                const optionText = normalize(textOf(label) || el.getAttribute('aria-label') || el.value);
                return optionText === expected || optionText.endsWith(expected) || expected.endsWith(optionText);
            }""",
            {"selector": selector, "answer": answer},
        )
    )


async def _resolve_radio_answer_selector(page: object, selector: str, answer: str) -> str:
    return str(
        await page.evaluate(
            """({selector, answer}) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const cssSelectorFor = (node) => {
                  if (!node) return '';
                  if (node.id) return `#${CSS.escape(node.id)}`;
                  const name = node.getAttribute('name');
                  const value = node.getAttribute('value');
                  if (name && value) {
                    return `${node.tagName.toLowerCase()}[name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`;
                  }
                  return '';
                };
                const textOf = (node) => (
                  node?.innerText
                  || node?.textContent
                  || node?.getAttribute?.('aria-label')
                  || node?.getAttribute?.('value')
                  || ''
                ).trim();
                const optionText = (radio) => {
                  const label = radio.id ? document.querySelector(`label[for="${CSS.escape(radio.id)}"]`) : null;
                  return normalize([
                    radio.getAttribute('value'),
                    radio.getAttribute('aria-label'),
                    textOf(label),
                    textOf(radio.closest('label')),
                  ].join(' '));
                };
                const matches = (radio) => {
                  const expected = normalize(answer);
                  if (!expected) return false;
                  const text = optionText(radio);
                  return text === expected || text.includes(expected) || expected.includes(text);
                };
                const el = document.querySelector(selector);
                if (!el) return selector;
                if (matches(el)) return cssSelectorFor(el) || selector;
                const radios = Array.from(
                  el.name
                    ? document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"], [role="radio"][name="${CSS.escape(el.name)}"]`)
                    : (el.closest('[role="radiogroup"], fieldset, [data-automation-id*="radio" i], div')
                        ?.querySelectorAll('input[type="radio"], [role="radio"]') || [])
                );
                const match = radios.find(matches);
                return cssSelectorFor(match) || selector;
            }""",
            {"selector": selector, "answer": answer},
        )
    )


def _is_previous_worker_label_text(text: str) -> bool:
    normalized = text.lower()
    previous_terms = ("previous", "previously", "before", "past", "former", "prior")
    return (
        "candidateispreviousworker" in normalized
        or ("worked" in normalized and any(token in normalized for token in previous_terms))
        or ("employed" in normalized and any(token in normalized for token in previous_terms))
        or ("employee" in normalized and any(token in normalized for token in previous_terms))
    )


def _is_previous_worker_no_action(action: dict[str, object]) -> bool:
    answer = str(action.get("answer") or action.get("value") or "").strip().lower()
    if answer not in {"no", "false"}:
        return False
    text = " ".join(
        str(action.get(key) or "")
        for key in ("label", "selector", "field_type")
    )
    return _is_previous_worker_label_text(text)


async def _click_workday_radio_row(page: object, selector: str) -> bool:
    token = f"cviance-radio-{abs(hash(selector))}"
    row_selector = await page.evaluate(
        """({selector, token}) => {
            const el = document.querySelector(selector);
            if (!el) return "";
            const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const group = el.closest('[name="candidateIsPreviousWorker"], [role="radiogroup"], fieldset');
            const containsOtherRadio = (node) => (
              node
              && Array.from(node.querySelectorAll('input[type="radio"], [role="radio"]'))
                .some((radio) => radio !== el)
            );
            const candidates = [
              label?.parentElement,
              el.closest('div')?.parentElement,
              Array.from(group?.children || []).find((child) => child.contains(el)),
              label?.closest('[role="radio"]'),
              el.closest('[role="radio"]'),
              el.closest('label'),
              label,
              el.parentElement,
            ].filter(Boolean);
            const row = candidates.find((node) => !containsOtherRadio(node));
            if (!row) return "";
            row.setAttribute('data-cviance-radio-click-target', token);
            return `[data-cviance-radio-click-target="${token}"]`;
        }""",
        {"selector": selector, "token": token},
    )
    if not row_selector:
        return False
    try:
        radio = page.locator(selector).first
        box = await radio.bounding_box(timeout=2000)
        if box:
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.mouse.down()
            await page.mouse.up()
            await asyncio.sleep(0.8)
            if await _radio_is_selected(page, selector):
                return True
        row = page.locator(str(row_selector)).first
        await row.scroll_into_view_if_needed(timeout=2000)
        await row.click(timeout=2500)
        await asyncio.sleep(0.6)
    except Exception:
        try:
            box = await page.locator(str(row_selector)).first.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await asyncio.sleep(0.6)
        except Exception:
            return False
    return await _radio_is_selected(page, selector)


async def _keyboard_select_radio(page: object, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        await locator.focus(timeout=2000)
        await page.keyboard.press("Space")
        await asyncio.sleep(0.8)
        return await _radio_is_selected(page, selector)
    except Exception:
        return False


async def _click_radio_selector(page: object, selector: str, answer: str) -> bool:
    return bool(
        await page.evaluate(
            """({selector, answer}) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const visible = (node) => {
                  if (!node) return false;
                  const rect = node.getBoundingClientRect();
                  const style = window.getComputedStyle(node);
                  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const el = document.querySelector(selector);
                if (!el) return false;
                const expected = normalize(answer);
                const textOf = (node) => (
                  node?.innerText
                  || node?.textContent
                  || node?.getAttribute?.('aria-label')
                  || node?.value
                  || ''
                ).trim();
                const optionMatches = (node) => {
                  if (!expected) return true;
                  const text = normalize(textOf(node));
                  return text === expected || text.endsWith(expected) || expected.endsWith(text);
                };
                const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
                const targets = [label, el.closest('label'), el].filter(Boolean);
                for (const target of targets) {
                  if (target !== el && !optionMatches(target)) continue;
                  if (visible(target) && typeof target.click === 'function') {
                    target.click();
                  } else {
                    target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                  }
                  if (Boolean(el.checked) || el.getAttribute('aria-checked') === 'true') return true;
                }
                return Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
            }""",
            {"selector": selector, "answer": answer},
        )
    )


async def _force_native_radio_selection(page: object, selector: str) -> bool:
    return bool(
        await page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                if (String(el.type || '').toLowerCase() !== 'radio') return false;
                const setChecked = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
                if (el.name) {
                  for (const other of Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`))) {
                    if (other !== el) {
                      if (setChecked) setChecked.call(other, false);
                      else other.checked = false;
                      other.dispatchEvent(new Event('input', {bubbles:true}));
                      other.dispatchEvent(new Event('change', {bubbles:true}));
                    }
                  }
                }
                if (setChecked) setChecked.call(el, true);
                else el.checked = true;
                el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur?.();
                return Boolean(el.checked);
            }""",
            selector,
        )
    )


async def _dispatch_checkbox_change(page: object, selector: str) -> None:
    await page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            if (!el) return;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.blur?.();
        }""",
        selector,
    )


async def _execute_checkbox_action(
    page: object,
    action: dict[str, object],
    application_id: UUID,
    db: AsyncSession,
    step: int,
) -> None:
    selector = str(action.get("selector") or "")
    if not selector:
        return

    await log(application_id, "info", f"Step {step}: checking checkbox [{selector}]", db)

    if await _checkbox_is_checked(page, selector):
        return

    try:
        await page.locator(selector).first.check(force=True, timeout=2000)
    except Exception:
        pass
    await asyncio.sleep(0.3)
    if await _checkbox_is_checked(page, selector):
        await _dispatch_checkbox_change(page, selector)
        return

    try:
        await page.locator(selector).first.click(force=True, timeout=2000)
    except Exception:
        pass
    await asyncio.sleep(0.3)
    if await _checkbox_is_checked(page, selector):
        await _dispatch_checkbox_change(page, selector)
        return

    toggled = await page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            if (!el) return false;
            const isChecked = () => Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
            if (isChecked()) return true;

            const click = (node) => {
                if (!node) return false;
                node.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                return true;
            };

            click(el);
            if (isChecked()) return true;

            const label = (el.id ? document.querySelector(`label[for="${el.id}"]`) : null) || el.closest('label');
            click(label);
            if (isChecked()) return true;

            const container = el.closest('[data-automation-id*="formField" i], label, div, section, fieldset');
            const candidates = Array.from(container?.querySelectorAll('*') || []).filter(node => {
                const text = (node.innerText || node.textContent || '').trim();
                const cls = String(node.getAttribute?.('class') || '');
                const role = String(node.getAttribute?.('role') || '').toLowerCase();
                const tag = String(node.tagName || '').toLowerCase();
                const rect = node.getBoundingClientRect?.();
                return rect && rect.width > 0 && rect.height > 0 && (
                    role === 'checkbox'
                    || tag === 'label'
                    || tag === 'input'
                    || /check|box|toggle|control|label/i.test(cls)
                    || (text && text.length < 120)
                );
            });

            for (const node of candidates) {
                click(node);
                if (isChecked()) return true;
            }

            if ('checked' in el) {
                el.checked = true;
            }
            el.setAttribute('aria-checked', 'true');
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            return isChecked();
        }""",
        selector,
    )
    await asyncio.sleep(0.3)
    if toggled or await _checkbox_is_checked(page, selector):
        await _dispatch_checkbox_change(page, selector)
        return

    raise RuntimeError(f"Checkbox did not become checked for {selector}")


def _page_fingerprint(page_data: dict[str, object]) -> str:
    return f"{page_data.get('url')}|fields={len(page_data.get('fields', []))}"


def _is_workday_start_application_choice(page_data: dict[str, object]) -> bool:
    domain = domain_from_url(str(page_data.get("url") or ""))
    if "workdayjobs.com" not in domain:
        return False
    haystack = " ".join(
        str(part or "")
        for part in (
            page_data.get("title"),
            page_data.get("text"),
            " ".join(
                str(modal.get("text") or "")
                for modal in page_data.get("modals", [])
                if isinstance(modal, dict)
            ),
        )
    ).lower()
    automation_ids = {
        str(button.get("automationId") or "").strip().lower()
        for button in page_data.get("buttons", [])
        if isinstance(button, dict)
    }
    button_texts = {
        " ".join(str(button.get("text") or "").lower().split())
        for button in page_data.get("buttons", [])
        if isinstance(button, dict)
    }
    if "start your application" not in haystack:
        has_manual_or_oauth_choice = any(
            text in button_texts
            for text in (
                "apply manually",
                "autofill with resume",
                "use my last application",
            )
        ) or any(
            is_third_party_apply_button(button)
            for button in page_data.get("buttons", [])
            if isinstance(button, dict)
        )
        if not has_manual_or_oauth_choice:
            return False
    # Standard case: choices are rendered as <a> / <button> elements
    if (
        {"applymanually", "autofillwithresume", "usemylastapplication"} & automation_ids
        or {"apply manually", "autofill with resume", "use my last application"} & button_texts
    ):
        return True
    # Some Workday tenants render these choices as <input type="radio"> instead.
    # Detect by looking for radio option labels in the fields array.
    choice_labels = {"apply manually", "autofill with resume", "use my last application"}
    for field in page_data.get("fields", []):
        if not isinstance(field, dict) or str(field.get("type") or "").lower() != "radio":
            continue
        for opt in field.get("radioOptions") or []:
            if str(opt.get("label") or "").strip().lower() in choice_labels:
                return True
    return False


def _field_has_value(field: dict[str, object]) -> bool:
    field_type = str(field.get("type") or "").lower()
    if field_type == "file":
        return bool(field.get("fileCount")) or bool(str(field.get("value") or "").strip())
    if field_type == "checkbox":
        return bool(field.get("checked"))
    if field_type == "radio":
        return any(option.get("checked") for option in field.get("radioOptions") or [])
    if field_type in {"select-one", "select-multiple"}:
        value = str(field.get("value") or "").strip()
        selected_text = str(field.get("selectedText") or "").strip().lower()
        placeholder = str(field.get("placeholder") or "").strip().lower()
        automation_id = str(field.get("automationId") or "").strip().lower()
        if not selected_text and (placeholder == "search" or automation_id == "searchbox"):
            return False
        display_value = selected_text or value.lower()
        return bool(display_value) and display_value not in {
            "select",
            "select...",
            "select one",
            "select option",
            "select an option",
            "choose",
            "choose...",
            "choose one",
            "please select",
            "please select an option",
        }
    selected_text = str(field.get("selectedText") or "").strip().lower()
    if selected_text and selected_text not in {
        "select",
        "select...",
        "select one",
        "select option",
        "select an option",
        "choose",
        "choose...",
        "choose one",
        "please select",
        "please select an option",
        "0 items selected",
        "0 item selected",
    }:
        return True
    # For chip-picker inputs (placeholder="Search"), check if errorText shows a chip was added.
    placeholder_lower = str(field.get("placeholder") or "").strip().lower()
    if placeholder_lower == "search":
        error_raw = str(field.get("errorText") or "").strip()
        if error_raw and not error_raw.lower().startswith("0 items"):
            normalized = _normalize_option_text(error_raw)
            if normalized:
                return True
    return bool(str(field.get("value") or "").strip())


def _is_honeypot_field(field: dict[str, object]) -> bool:
    text = " ".join(
        str(field.get(key) or "")
        for key in ("label", "placeholder", "automationId", "selector")
    ).lower()
    return any(
        term in text
        for term in (
            "for robots only",
            "do not enter",
            "do not fill",
            "leave blank",
            "leave this blank",
            "if you're human",
            "if you are human",
            "bot field",
            "honeypot",
        )
    )


def _empty_required_fields(page_data: dict[str, object]) -> list[str]:
    empty: list[str] = []
    checkbox_satisfied_labels = {
        str(field.get("label") or field.get("placeholder") or field.get("automationId") or "").strip().lower()
        for field in page_data.get("fields", [])
        if isinstance(field, dict)
        and str(field.get("type") or "").lower() == "checkbox"
        and bool(field.get("checked"))
    }
    checkbox_input_labels = {
        str(field.get("label") or field.get("placeholder") or field.get("automationId") or "").strip().lower()
        for field in page_data.get("fields", [])
        if isinstance(field, dict)
        and str(field.get("type") or "").lower() == "checkbox"
        and str(field.get("tagName") or "").lower() == "input"
    }
    for field in page_data.get("fields", []):
        if not isinstance(field, dict):
            continue
        field_type = str(field.get("type") or "").lower()
        if (
            field_type in {"select-one", "select-multiple"}
            and str(field.get("tagName") or "").lower() == "span"
            and not field.get("options")
        ):
            continue
        if _is_honeypot_field(field):
            continue
        label = str(field.get("label") or field.get("placeholder") or field.get("automationId") or "unknown")
        label_key = label.strip().lower()
        if field_type == "checkbox":
            if label_key in checkbox_satisfied_labels:
                continue
            if str(field.get("tagName") or "").lower() != "input" and label_key in checkbox_input_labels:
                continue
        error_text = str(field.get("errorText") or "").lower()
        is_required = (
            bool(field.get("required"))
            or "*" in label
            or " required" in label.lower()
            or (
                bool(field.get("invalid"))
                and (
                    not _field_has_value(field)
                    or any(term in error_text for term in ("required", "please enter", "please fill"))
                )
            )
        )
        if is_required and not _field_has_value(field):
            empty.append(label)
    return empty


def _is_previous_worker_detail_field(field: dict[str, object]) -> bool:
    text = " ".join(
        str(field.get(key) or "")
        for key in ("label", "placeholder", "automationId", "selector")
    ).lower()
    return "previousworker--" in text and "candidateispreviousworker" not in text


def _has_previous_worker_controller(page_data: dict[str, object]) -> bool:
    for field in page_data.get("fields", []):
        if not isinstance(field, dict):
            continue
        if str(field.get("type") or "").lower() != "radio":
            continue
        text = " ".join(
            str(field.get(key) or "")
            for key in ("label", "selector", "automationId")
        )
        if _is_previous_worker_label_text(text):
            return True
    return False


def _previous_worker_no_selected(page_data: dict[str, object]) -> bool:
    for field in page_data.get("fields", []):
        if not isinstance(field, dict):
            continue
        if str(field.get("type") or "").lower() != "radio":
            continue
        text = " ".join(
            str(field.get(key) or "")
            for key in ("label", "selector", "automationId")
        )
        if not _is_previous_worker_label_text(text):
            continue
        for option in field.get("radioOptions") or []:
            if not isinstance(option, dict) or not option.get("checked"):
                continue
            option_text = " ".join(
                str(option.get(key) or "")
                for key in ("label", "value")
            ).strip().lower()
            return "no" in option_text or option_text == "false"
    return False


def _empty_previous_worker_detail_fields(page_data: dict[str, object]) -> list[str]:
    labels: list[str] = []
    for field in page_data.get("fields", []):
        if not isinstance(field, dict):
            continue
        if not _is_previous_worker_detail_field(field):
            continue
        label = str(field.get("label") or field.get("placeholder") or field.get("automationId") or "unknown")
        error_text = str(field.get("errorText") or "").lower()
        is_required = (
            bool(field.get("required"))
            or "*" in label
            or " required" in label.lower()
            or (
                bool(field.get("invalid"))
                and (
                    not _field_has_value(field)
                    or any(term in error_text for term in ("required", "please enter", "please fill"))
                )
            )
        )
        if is_required and not _field_has_value(field):
            labels.append(label)
    return labels


def _has_previous_worker_detail_fields(page_data: dict[str, object]) -> bool:
    return any(
        isinstance(field, dict) and _is_previous_worker_detail_field(field)
        for field in page_data.get("fields", [])
    )


def _field_lookup(page_data: dict[str, object]) -> dict[str, dict[str, object]]:
    lookup: dict[str, dict[str, object]] = {}
    for field in page_data.get("fields", []):
        if not isinstance(field, dict):
            continue
        selector = str(field.get("selector") or "")
        if selector:
            lookup[selector] = field
    return lookup


async def _save_learned_rule(
    page: object,
    page_data: dict[str, object],
    action: dict[str, object],
    application_id: UUID,
    db: AsyncSession,
    step: int,
    reason: str,
) -> None:
    source = str(action.get("source") or "")
    if source not in {"required_checkbox", "rule"}:
        return

    action_type = str(action.get("type") or "")
    if action_type not in {"fill", "select", "radio", "check"}:
        return

    field = _field_lookup(page_data).get(str(action.get("selector") or ""), {})
    label = str(action.get("label") or field.get("label") or field.get("placeholder") or field.get("automationId") or "")
    field_type = str(action.get("field_type") or field.get("type") or "")
    control_kind = str(action.get("control_kind") or field.get("controlKind") or "")
    value = action.get("value")
    if action_type == "radio":
        value = action.get("value") or action.get("answer")
    elif action_type == "check":
        value = "true"

    options: list[str] = []
    for option in field.get("options") or field.get("radioOptions") or []:
        if isinstance(option, dict):
            option_text = str(option.get("text") or option.get("label") or option.get("value") or "").strip()
            if option_text:
                options.append(option_text)

    saved = save_field_rule(
        domain=domain_from_url(getattr(page, "url", "")),
        label=label,
        field_type=field_type,
        action=action_type,
        value=value,
        source=source,
        reason=reason,
        options=options,
        control_kind=control_kind,
    )
    if saved:
        await log(application_id, "info", f"Step {step}: learned rule for [{label}] via {source}", db)


def _score_button(page_data: dict[str, object], button: dict[str, object]) -> int:
    text = " ".join(str(button.get("text") or "").lower().split())
    automation_id = str(button.get("automationId") or "").strip().lower()
    page_text = " ".join(str(page_data.get("text") or "").lower().split())
    title = " ".join(str(page_data.get("title") or "").lower().split())
    modal_text = " ".join(
        str(modal.get("text") or "").lower()
        for modal in page_data.get("modals", [])
        if isinstance(modal, dict)
    )
    if is_third_party_apply_button(button):
        return -1
    if automation_id in {
        "navigationitem-search and apply",
        "navigationitem-join our talent community!",
        "privacylink",
        "accessibilityskiptomaincontent",
    } or text in {
        "search and apply",
        "join our talent community!",
        "gdit privacy notice and california privacy notice",
    }:
        return -1
    if _is_workday_start_application_choice(page_data):
        if automation_id == "applymanually" or text == "apply manually":
            return 125
        if automation_id == "autofillwithresume" or text == "autofill with resume":
            return 115
        if automation_id == "usemylastapplication" or text == "use my last application":
            return -1
        if text in {"close", "search and apply", "sign in"}:
            return -1
        # Never follow LinkedIn/Indeed OAuth apply from the start-application modal
        if "linkedin" in text or "indeed" in text or "zip recruiter" in text:
            return -1
    if "chatbot" in page_text or "chatbot" in modal_text:
        if text == "close chatbot window":
            return 125
        if text in {"i'm interested", "i am interested", "im interested"}:
            return -1
    if "cookie" in page_text or any("cookie" in str(modal.get("text") or "").lower() for modal in page_data.get("modals", []) if isinstance(modal, dict)):
        if text in {"accept cookies", "accept all cookies", "accept all", "accept", "allow", "allow all"}:
            return 120
        if text in {"reject cookies", "reject", "manage preferences", "customize", "cookie settings"}:
            return -1
    if "if you have not yet created an account" in page_text or "please create an account" in page_text:
        if text == "create an account":
            return 95
        if text in {"login", "log in", "sign in"}:
            return -1
    if "how to apply" in title:
        if text in {"search jobs", "browse all jobs", "view all open opportunities"}:
            return 95
        if text == "submit resume":
            return -1
    if _security_challenge_surface(page_data):
        if text in {"begin", "verify", "continue"}:
            return 95
    if "verify your account before you sign in" in page_text and text == "resend account verification":
        return 100
    if "privacy agreement" in title or "privacy agreement" in page_text:
        if text in {"i accept", "accept"}:
            return 100
        if text in {"i decline", "decline"}:
            return -1
    if _workday_auth_page_detected(page_data):
        fields = [field for field in page_data.get("fields", []) if isinstance(field, dict)]
        password_count = sum(
            1
            for field in fields
            if str(field.get("type") or "").lower() == "password"
            or "password" in " ".join(
                str(field.get(key) or "")
                for key in ("label", "placeholder", "automationId")
            ).lower()
        )
        signup_surface = password_count >= 2
        login_surface = password_count == 1
        automation_id = str(button.get("automationId") or "").lower()
        if signup_surface and (text == "create account" or automation_id in {"click_filter", "createaccountsubmitbutton"}):
            return 95
        if login_surface and text == "sign in" and automation_id not in {"utilitybuttonsignin", "signinlink"}:
            return 90
        if text in {"create account", "sign in", "log in", "login"}:
            return -1
    return score_action_button(button)


def _find_matching_frame(page: object, frame_url: str) -> object | None:
    target = str(frame_url or "").strip().lower()
    if not target:
        return None
    for frame in getattr(page, "frames", []):
        current = str(getattr(frame, "url", "") or "").strip().lower()
        if current and (current == target or current in target or target in current):
            return frame
    return None


def _embedded_frame_candidate(frame_url: str) -> bool:
    normalized = str(frame_url or "").strip().lower()
    return any(
        token in normalized
        for token in (
            "recruitingbypaycor.com",
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "icims.com",
        )
    )


async def _embedded_frame_surface(page: object, page_data: dict[str, object]) -> tuple[object, dict[str, object], str] | None:
    for frame_info in page_data.get("iframes", []):
        if not isinstance(frame_info, dict):
            continue
        frame_url = str(frame_info.get("src") or "").strip()
        if not frame_url or not _embedded_frame_candidate(frame_url):
            continue
        frame = _find_matching_frame(page, frame_url)
        if frame is None:
            continue
        try:
            frame_data = await inspect_page(frame)
        except Exception:
            continue
        resolved_url = str(getattr(frame, "url", "") or frame_url)
        if frame_data.get("fields") or _best_button_action(frame_data):
            return frame, frame_data, resolved_url
    return None


async def _embedded_frame_button_action(page: object, page_data: dict[str, object]) -> dict[str, object] | None:
    surface = await _embedded_frame_surface(page, page_data)
    if surface is None:
        return None
    _frame, frame_data, frame_url = surface
    action = _best_button_action(frame_data)
    if action:
        action["frame_url"] = frame_url
        return action
    return None


def _is_final_submit_button_text(text: object) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return any(
        phrase in normalized
        for phrase in (
            "submit",
            "submit application",
            "send application",
            "complete application",
            "finish application",
        )
    )


def _safe_live_test_stop_before_submit() -> bool:
    return str(os.getenv(SAFE_LIVE_TEST_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_add_detail_button_text(text: object) -> bool:
    return is_add_detail_button_text(text)


def _best_button_action(page_data: dict[str, object], avoid_texts: set[str] | None = None) -> dict[str, object] | None:
    avoid_texts = {text.lower() for text in avoid_texts or set()}
    navigation_action = action_override_for_page(page_data)
    if navigation_action and navigation_action.get("type") == "click_button":
        navigation_text = str(navigation_action.get("text") or "").lower()
        if navigation_text not in avoid_texts:
            return navigation_action
    scored = [(_score_button(page_data, button), button) for button in page_data.get("buttons", []) if isinstance(button, dict)]
    scored = [(score, button) for score, button in scored if score > 0]
    scored = [
        (score, button)
        for score, button in scored
        if str(button.get("text") or "").lower() not in avoid_texts
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None
    best_score, button = scored[0]
    if best_score <= 0:
        return None
    return {
        "type": "click_button",
        "selector": button.get("selector"),
        "text": button.get("text"),
        "automationId": button.get("automationId"),
        "href": button.get("href"),
    }


def _canonical_action_url(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.split(r"[?#]", text, maxsplit=1)[0].rstrip("/")


def _target_job_action(
    page_data: dict[str, object],
    target_title: str,
    target_url: str,
    avoid_texts: set[str] | None = None,
) -> dict[str, object] | None:
    normalized_title = " ".join(str(target_title or "").lower().split())
    normalized_url = _canonical_action_url(target_url)
    current_url = _canonical_action_url(page_data.get("url"))
    if normalized_url and current_url == normalized_url:
        return None
    avoid = {text.lower() for text in avoid_texts or set()}
    matches: list[tuple[int, dict[str, object]]] = []
    for button in page_data.get("buttons", []):
        if not isinstance(button, dict):
            continue
        text = " ".join(str(button.get("text") or "").lower().split())
        if text.startswith("skip ") or text in {"skip to content", "skip to main content"}:
            continue
        raw_href = str(button.get("href") or "").strip().lower()
        href = _canonical_action_url(button.get("href"))
        if not href or text in avoid:
            continue
        if "#" in raw_href and current_url and href == current_url:
            continue
        if normalized_url and href == normalized_url:
            matches.append((100, button))
            continue
        if normalized_url and (href.endswith(normalized_url) or normalized_url.endswith(href)):
            matches.append((95, button))
            continue
        if normalized_title and normalized_title in text:
            matches.append((80, button))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    button = matches[0][1]
    return {
        "type": "click_button",
        "selector": button.get("selector"),
        "text": button.get("text"),
        "automationId": button.get("automationId"),
        "href": button.get("href"),
    }


async def _execute_select_action(
    page: object,
    action: dict[str, object],
    application_id: UUID,
    db: AsyncSession,
    step: int,
) -> None:
    selector = str(action.get("selector") or "")
    answer = str(action.get("answer") or action.get("value") or "")
    control_kind = str(action.get("control_kind") or action.get("controlKind") or "").strip().lower()
    if not selector or not answer:
        return
    page_url = str(getattr(page, "url", "") or "").lower()
    is_workday_page = "myworkdayjobs.com" in page_url or "workdayjobs.com" in page_url
    label_lower = str(action.get("label") or "").lower()
    normalized_answer = " ".join(answer.lower().replace("_", " ").replace("-", " ").split())
    if is_workday_page and any(token in label_lower for token in ("phone device", "phone type")) and re.search(r"\+\d{1,4}", answer):
        answer = "Mobile"
    if any(token in label_lower for token in ("country phone code", "phone code", "dialing code", "calling code")) and normalized_answer in {"mobile", "landline"}:
        await log(application_id, "info", f"Step {step}: skipping incompatible phone-code selection [{action.get('label')}] -> [{answer}]", db)
        return
    trace_select = (
        "how did you hear about us" in label_lower
        and str(os.getenv("TRACE_SELECT_REPLAY") or "").strip().lower() in {"1", "true", "yes", "on"}
    )
    answer_path = _split_option_path(answer)
    top_level_answer = answer_path[0] if answer_path else answer
    nested_answer = _join_option_path(answer_path[1:]) if len(answer_path) > 1 else ""

    def _normalize_option_text(value: object) -> str:
        text = " ".join(str(value or "").split()).strip()
        lowered = text.lower()
        for prefix in ("0 items selected", "1 item selected", "1 items selected"):
            if lowered.startswith(prefix):
                text = text[len(prefix):].lstrip(" ,:-")
                lowered = text.lower()
        text = text.replace("not checked", " ").replace("checked", " ")
        text = " ".join(text.split()).strip()
        return text

    def _alias_option_match(answer_text: str, options: list[str], label_text: str) -> str | None:
        answer_lower = answer_text.lower()
        label_lower = label_text.lower()
        normalized_options = {option.lower(): option for option in options}

        def is_decline_text(value: str) -> bool:
            normalized = " ".join(str(value or "").lower().replace("-", " ").split())
            return any(
                token in normalized
                for token in (
                    "choose not to disclose",
                    "choose not to respond",
                    "not to disclose",
                    "not to respond",
                    "do not wish",
                    "do not want",
                    "decline",
                    "prefer not",
                    "prefer not to say",
                    "not prefer to say",
                    "not disclose",
                    "not self identify",
                    "self identify",
                    "no answer",
                    "no response",
                )
            )

        if any(token in label_lower for token in ("gender", "ethnicity", "race", "veteran")) and is_decline_text(answer_text):
            for option in options:
                if is_decline_text(option):
                    return option

        phone_code = re.search(r"\+\d{1,4}", answer_text)
        if phone_code and any(token in label_lower for token in ("phone", "dialing", "calling", "country code")):
            code = phone_code.group(0)
            for option in options:
                if code in re.findall(r"\+\d{1,4}", option):
                    return option
            return None

        if "country" in label_lower and "phone" not in label_lower:
            normalized_answer = _normalize_option_text(answer_text).lower()
            for option in options:
                normalized_option = _normalize_option_text(option).lower()
                if normalized_option == normalized_answer or normalized_option.startswith(f"{normalized_answer} "):
                    return option
            return None

        if any(token in label_lower for token in ("how did you hear", "hear about us", "source")):
            if any(token in answer_lower for token in ("linkedin", "indeed", "glassdoor", "naukri", "monster", "job site", "social media", "social")):
                if "linkedin" in answer_lower or "linked in" in answer_lower:
                    for key, option in normalized_options.items():
                        if ("linkedin" in key or "linked in" in key) and any(
                            source_token in key
                            for source_token in ("social media", "social", "job site", "job board", "job boards")
                        ):
                            return option
                    for key, option in normalized_options.items():
                        if "social media" in key or key == "social" or " social" in key:
                            return option
                    for key, option in normalized_options.items():
                        if "job site" in key:
                            return option
                    for key, option in normalized_options.items():
                        if "linkedin" in key or "linked in" in key:
                            return option
                    for key, option in normalized_options.items():
                        if "job board" in key or "job boards" in key:
                            return option
                for key, option in normalized_options.items():
                    if "job site" in key or "social media" in key:
                        return option
                for key, option in normalized_options.items():
                    if "job board" in key or "job boards" in key:
                        return option
            if any(token in answer_lower for token in ("recruiter", "talent acquisition", "contacted directly")):
                for key, option in normalized_options.items():
                    if "recruiter" in key:
                        return option
            if any(token in answer_lower for token in ("referral", "referred", "friend", "employee")):
                for key, option in normalized_options.items():
                    if "referral" in key:
                        return option
            if any(token in answer_lower for token in ("website", "company site", "career site")):
                for key, option in normalized_options.items():
                    if "company website" in key:
                        return option
            if any(token in answer_lower for token in ("internal", "employee")):
                for key, option in normalized_options.items():
                    if key == "internal":
                        return option
            if any(token in answer_lower for token in ("college", "university", "campus")):
                for key, option in normalized_options.items():
                    if "college" in key or "university" in key:
                        return option
        return None

    async def _element_meta() -> dict[str, object]:
        return await page.evaluate(
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el) return {tagName: '', type: '', role: '', automationId: '', placeholder: '', widgetId: '', promptOpen: false};
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const promptOpen = Boolean(
                widgetId && document.querySelector(`[data-associated-widget="${widgetId}"]`)
              );
              return {
                tagName: (el.tagName || '').toLowerCase(),
                type: (el.getAttribute('type') || '').toLowerCase(),
                role: (el.getAttribute('role') || '').toLowerCase(),
                automationId: (el.getAttribute('data-automation-id') || '').toLowerCase(),
                placeholder: (el.getAttribute('placeholder') || '').toLowerCase(),
                widgetId,
                promptOpen,
              };
            }
            """,
            selector,
        )

    async def _scoped_option_items() -> list[dict[str, object]]:
        return await page.evaluate(
            """
            ({selector, controlKind}) => {
              const el = document.querySelector(selector);
              if (!el) return [];
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              const visible = (node) => {
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const textOf = (node) => (
                node?.getAttribute('data-automation-label')
                || node?.getAttribute('aria-label')
                || node?.innerText
                || node?.textContent
                || ''
              ).trim();
              const normalizedControlKind = String(controlKind || '').toLowerCase();
              const roots = prompt
                ? [prompt]
                : (normalizedControlKind && !normalizedControlKind.startsWith('native_') ? [document] : []);
              const items = [];
              for (const root of roots) {
                for (const node of Array.from(root.querySelectorAll(
                '[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [role=listbox] li, .ant-select-item-option, select option'
                ))) {
                  const text = textOf(node);
                  if (visible(node) && text && text.length <= 160) {
                    items.push({text, index: items.length});
                  }
                }
              }
              return items;
            }
            """,
            {"selector": selector, "controlKind": control_kind},
        )

    async def _workday_select_state() -> dict[str, object]:
        return await page.evaluate(
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el) return {};
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              const container = el.closest('[data-automation-id="multiSelectContainer"]')
                || (widgetId ? document.getElementById(widgetId) : null)
                || el.closest('.ant-select')
                || el.closest('.select__container')
                || el.closest('[class*="-container"]')
                || el.closest('[data-automation-id*="formField" i], label, div, section, fieldset');
              const textOf = (node) => (node?.innerText || node?.textContent || node?.getAttribute?.('aria-label') || '').trim();
              const hiddenRequired = container?.querySelector('input[required][aria-hidden="true"], input[class*="requiredInput"]');
              return {
                directText: textOf(el),
                value: (el.value || '').trim(),
                hiddenRequiredValue: (hiddenRequired?.value || '').trim(),
                ariaValueText: (el.getAttribute('aria-valuetext') || '').trim(),
                selectedChip: textOf(container?.querySelector('[data-automation-id="selectedItem"], .ant-select-selection-item, .select__single-value, [class*="singleValue"], [class*="multiValue"]')),
                promptSelection: textOf(container?.querySelector('[data-automation-id="promptSelectionLabel"]')),
                promptOpen: Boolean(prompt),
                promptTitle: textOf(prompt?.querySelector('[data-automation-id="promptTitle"]')),
              };
            }
            """,
            selector,
        )

    async def _open_combobox_prompt() -> None:
        await page.evaluate(
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const root = el.closest('.select__container')
                || el.closest('[data-automation-id="multiSelectContainer"]')
                || el.closest('label, div, section, fieldset');
              const toggle = root?.querySelector('button[aria-label*="Toggle" i], button[aria-haspopup="true"], .select__indicators button');
              if (toggle) {
                toggle.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
                toggle.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
                toggle.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                return true;
              }
              return false;
            }
            """,
            selector,
        )
        await asyncio.sleep(0.4)

    async def _open_workday_search_prompt() -> None:
        await page.evaluate(
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              if (prompt) return true;
              const container = el.closest('[data-automation-id="multiSelectContainer"]')
                || (widgetId ? document.getElementById(widgetId) : null)
                || el.closest('[data-automation-id*="formField" i], label, div, section, fieldset');
              const promptIcon = container?.querySelector('[data-automation-id="promptIcon"], [data-uxi-selectinputicon-type="promptIcon"]');
              const target = promptIcon || el;
              target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
              target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
              target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
            """,
            selector,
        )
        await asyncio.sleep(0.7)

    async def _combobox_commit_fallback(value: str) -> bool:
        try:
            await _clear_input(page, selector)
        except Exception:
            pass
        try:
            await page.locator(selector).first.click(force=True, timeout=2000)
        except Exception:
            return False
        await asyncio.sleep(0.2)
        try:
            await page.keyboard.type(value, delay=55)
            await asyncio.sleep(0.5)
            for key in ("ArrowDown", "Enter", "Tab"):
                await page.keyboard.press(key)
                await asyncio.sleep(0.35)
        except Exception:
            return False
        return not await _selection_stuck()

    async def _prompt_snapshot() -> dict[str, object]:
        return await page.evaluate(
            """
            (selector) => {
              const textOf = (node) => (
                node?.getAttribute?.('data-automation-label')
                || node?.getAttribute?.('aria-label')
                || node?.innerText
                || node?.textContent
                || ''
              ).trim();
              const visible = (node) => {
                const r = node?.getBoundingClientRect?.();
                const style = node ? window.getComputedStyle(node) : null;
                return !!r && r.width > 0 && r.height > 0 && style && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const el = document.querySelector(selector);
              if (!el) return {};
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              if (!prompt) return {promptOpen: false, hasBackButton: false, firstText: '', texts: [], count: 0, value: String(el.value || '').trim()};
              const root = prompt;
              const items = Array.from(root.querySelectorAll(
                '[data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [role=option], [role=listbox] li'
              )).filter(visible).map((node) => textOf(node)).filter(Boolean);
              return {
                promptOpen: Boolean(prompt),
                hasBackButton: Boolean(prompt?.querySelector('[data-automation-id="backButton"]')),
                firstText: items[0] || '',
                texts: items.slice(0, 8),
                count: items.length,
                value: String(el.value || '').trim(),
              };
            }
            """,
            selector,
        )

    def _prompt_snapshot_changed(before: dict[str, object], after: dict[str, object]) -> bool:
        before_texts = [str(item).strip() for item in (before.get("texts") or []) if str(item).strip()]
        after_texts = [str(item).strip() for item in (after.get("texts") or []) if str(item).strip()]
        return (
            str(before.get("firstText") or "").strip() != str(after.get("firstText") or "").strip()
            or before_texts != after_texts
            or bool(before.get("hasBackButton")) != bool(after.get("hasBackButton"))
            or int(before.get("count") or 0) != int(after.get("count") or 0)
        )

    async def _selection_stuck() -> bool:
        state = await _workday_select_state()
        if trace_select:
            print(f"[TRACE_SELECT_REPLAY] selection_state selector={selector!r} answer={answer!r} state={state!r}", flush=True)
        combined = " ".join(
            _normalize_option_text(state.get(key))
            for key in ("directText", "value", "ariaValueText", "selectedChip", "promptSelection", "promptTitle")
        ).strip()
        combined_lower = combined.lower()
        matched_lower = _normalize_option_text(matched_text).lower()
        nested_lower = _normalize_option_text(nested_answer).lower()
        answer_lower = _normalize_option_text(answer).lower()
        label_lower = str(action.get("label") or "").lower()
        role_lower = str(element_meta.get("role") or "").lower()
        hidden_required_value = _normalize_option_text(state.get("hiddenRequiredValue"))
        if action.get("required", True) and role_lower == "combobox" and not hidden_required_value and not state.get("selectedChip"):
            return True
        prompt_selection = _normalize_option_text(state.get("promptSelection"))
        if (
            action.get("required", True)
            and is_searchable_prompt
            and not hidden_required_value
            and not state.get("selectedChip")
            and (not prompt_selection or prompt_selection.lower() in {"0 items selected", "0 item selected"})
        ):
            return True

        def has_decline_meaning(value: str) -> bool:
            normalized = " ".join(str(value or "").lower().replace("-", " ").split())
            return any(
                token in normalized
                for token in (
                    "choose not to disclose",
                    "choose not to respond",
                    "not to disclose",
                    "not to respond",
                    "do not wish",
                    "do not want",
                    "decline",
                    "prefer not",
                    "prefer not to say",
                    "not prefer to say",
                    "not disclose",
                    "not self identify",
                    "self identify",
                    "no answer",
                    "no response",
                )
            )

        if (
            any(token in label_lower for token in ("gender", "ethnicity", "race", "veteran"))
            and has_decline_meaning(answer_lower + " " + matched_lower)
            and has_decline_meaning(combined_lower)
        ):
            return False
        if nested_answer and not nested_lower:
            return True
        if nested_lower and nested_lower in combined_lower:
            return False
        if nested_answer and matched_lower and matched_lower in combined_lower:
            return True
        if matched_lower and matched_lower in combined_lower:
            return False
        if answer_lower and answer_lower in combined_lower:
            return False
        return True

    async def _click_prompt_option_outcome(option_text: str) -> str:
        before = await _prompt_snapshot()
        clicked = await _click_prompt_option(option_text)
        if not clicked:
            return "no_change"
        await asyncio.sleep(0.6)
        after = await _prompt_snapshot()
        if _prompt_snapshot_changed(before, after) or bool(after.get("hasBackButton")):
            return "submenu_opened"
        if not bool(after.get("promptOpen")):
            return "leaf_selected"
        before_value = _normalize_option_text(before.get("value"))
        after_value = _normalize_option_text(after.get("value"))
        if after_value and after_value != before_value:
            return "leaf_selected"
        return "no_change"

    async def _click_prompt_option(option_text: str) -> bool:
        option_text = " ".join(str(option_text or "").split()).strip()
        if not option_text:
            return False
        try:
            for _ in range(14):
                target_box = await page.evaluate(
                    """
                    ({selector, optionText}) => {
                      const el = document.querySelector(selector);
                      if (!el) return null;
                      const widgetId = el.getAttribute('data-uxi-multiselect-id')
                        || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                        || '';
                      const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                      if (!prompt) return null;
                      const root = prompt;
                      const visible = (node) => {
                        if (!node) return false;
                        const r = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                      };
                      const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const textOf = (node) => (
                        node?.getAttribute('data-automation-label')
                        || node?.getAttribute('aria-label')
                        || node?.innerText
                        || node?.textContent
                        || ''
                      ).trim();
                      const target = normalize(optionText);
                      const nodes = Array.from(root.querySelectorAll(
                        '[data-automation-id="menuItem"], [role=option], [role=listbox] li'
                      )).filter(visible);
                      const match = nodes.find((node) => normalize(textOf(node)) === target)
                        || nodes.find((node) => normalize(textOf(node)).includes(target))
                        || nodes.find((node) => target.includes(normalize(textOf(node))));
                      if (!match) return null;
                      const row = match.closest('[data-automation-id="menuItem"], [role=option], li') || match;
                      const rect = row.getBoundingClientRect();
                      return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
                    }
                    """,
                    {"selector": selector, "optionText": option_text},
                )
                if target_box and target_box.get("x") and target_box.get("y"):
                    await page.mouse.click(float(target_box["x"]), float(target_box["y"]))
                    return True
                scrolled = await page.evaluate(
                    """
                    (selector) => {
                      const el = document.querySelector(selector);
                      if (!el) return false;
                      const widgetId = el.getAttribute('data-uxi-multiselect-id')
                        || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                        || '';
                      const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                      const list = prompt?.querySelector('[data-automation-id="activeListContainer"], [role="listbox"]');
                      if (!list) return false;
                      const before = list.scrollTop;
                      list.scrollTop = before + Math.max(120, Math.floor(list.clientHeight * 0.8));
                      list.dispatchEvent(new Event('scroll', {bubbles:true}));
                      return list.scrollTop !== before;
                    }
                    """,
                    selector,
                )
                if not scrolled:
                    break
                await asyncio.sleep(0.25)
        except Exception:
            pass
        option_selectors = [
            '[data-automation-id="menuItem"]',
            '[data-automation-id="promptLeafNode"]',
            '[data-automation-id="promptOption"]',
            '[role="option"]',
            '[role="listbox"] li',
            '.ant-select-item-option',
        ]
        for option_selector in option_selectors:
            try:
                locator = page.locator(option_selector).filter(has_text=option_text)
                if await locator.count():
                    await locator.first.click(force=True, timeout=3000)
                    return True
            except Exception:
                continue
        try:
            await page.get_by_role("option", name=re.compile(re.escape(option_text), re.I)).first.click(
                force=True,
                timeout=3000,
            )
            return True
        except Exception:
            pass
        js_clicked = await page.evaluate(
            """
            ({selector, optionText}) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              if (!prompt) return false;
              const root = prompt;
              const visible = (node) => {
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const textOf = (node) => (
                node?.getAttribute('data-automation-label')
                || node?.getAttribute('aria-label')
                || node?.innerText
                || node?.textContent
                || ''
              ).trim();
              const target = normalize(optionText);
              const nodes = Array.from(root.querySelectorAll(
                '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"], [data-automation-id="menuItem"], [role=option], [role=listbox] li, .ant-select-item-option'
              )).filter(visible);
              const match = nodes.find((node) => normalize(textOf(node)) === target)
                || nodes.find((node) => normalize(textOf(node)).includes(target))
                || nodes.find((node) => target.includes(normalize(textOf(node))));
              if (!match) return false;
              const clickTarget = match.closest('[data-automation-id="menuItem"], [role=option], li, button') || match;
              clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
              clickTarget.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
              clickTarget.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
            """,
            {"selector": selector, "optionText": option_text},
        )
        return bool(js_clicked)

    async def _search_and_select_prompt_leaf(option_text: str) -> bool:
        option_text = " ".join(str(option_text or "").split()).strip()
        if not option_text:
            return False
        try:
            await _clear_input(page, selector)
            await page.locator(selector).first.click(force=True, timeout=2000)
            await page.keyboard.type(option_text, delay=55)
            await asyncio.sleep(0.9)
        except Exception:
            return False
        clicked = await _click_prompt_option(option_text)
        if not clicked:
            try:
                await page.keyboard.press("ArrowDown")
                await asyncio.sleep(0.25)
                await page.keyboard.press("Enter")
                clicked = True
            except Exception:
                clicked = False
        if clicked:
            await asyncio.sleep(0.8)
        return clicked

    async def _click_popup_button_option(option_text: str) -> bool:
        option_text = " ".join(str(option_text or "").split()).strip()
        if not option_text:
            return False
        # PRIMARY strategy: native HTMLElement.click() inside the page.
        # Workday's popup_select_button has React handlers that pick up the
        # synthetic event from a native click, but NOT from dispatchEvent or
        # coordinate-based mouse.click (which can land in the wrong portal or
        # be intercepted by an overlay). Verified live on FNB Country popup:
        # `li.click()` flips "Laos" → "India" and closes the popup.
        try:
            native_clicked = await page.evaluate(
                """
                ({selector, optionText}) => {
                  const button = document.querySelector(selector);
                  const controlledId = button?.getAttribute('aria-controls') || '';
                  const roots = [];
                  if (controlledId) {
                    const controlled = document.getElementById(controlledId);
                    if (controlled) roots.push(controlled);
                  }
                  roots.push(document);
                  const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const normalize = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const textOf = (node) => (
                    node?.getAttribute('data-automation-label')
                    || node?.getAttribute('aria-label')
                    || node?.innerText
                    || node?.textContent
                    || ''
                  ).replace(/\\s+not checked$/i, '').trim();
                  const target = normalize(optionText);
                  for (const root of roots) {
                    const candidates = Array.from(root.querySelectorAll(
                      '[role="option"], [role="listbox"] li, [role="listbox"] button, [data-automation-id="menuItem"], [data-automation-id="promptOption"], [data-automation-id="promptLeafNode"]'
                    )).filter((node) => {
                      const text = textOf(node);
                      return visible(node) && text && text.length <= 160;
                    });
                    const match = candidates.find((node) => normalize(textOf(node)) === target)
                      || candidates.find((node) => normalize(textOf(node)).includes(target))
                      || candidates.find((node) => target.includes(normalize(textOf(node))));
                    if (match) {
                      const row = match.closest('[role="option"], [role="listbox"] li, [role="listbox"] button, [data-automation-id="menuItem"], button, li') || match;
                      if (typeof row.click === 'function') {
                        row.click();
                        return true;
                      }
                    }
                  }
                  return false;
                }
                """,
                {"selector": selector, "optionText": option_text},
            )
            if native_clicked:
                await asyncio.sleep(0.7)
                return True
        except Exception:
            pass
        try:
            target_box = await page.evaluate(
                """
                ({selector, optionText}) => {
                  const button = document.querySelector(selector);
                  if (!button) return null;
                  const controlledId = button.getAttribute('aria-controls') || '';
                  const roots = [];
                  if (controlledId) {
                    const controlled = document.getElementById(controlledId);
                    if (controlled) roots.push(controlled);
                  }
                  roots.push(document);
                  const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const textOf = (node) => (
                    node?.getAttribute('data-automation-label')
                    || node?.getAttribute('aria-label')
                    || node?.innerText
                    || node?.textContent
                    || ''
                  ).replace(/\\s+not checked$/i, '').trim();
                  const target = normalize(optionText);
                  for (const root of roots) {
                    const candidates = Array.from(root.querySelectorAll(
                      '[role="option"], [role="listbox"] li, [role="listbox"] button, [data-automation-id="menuItem"], [data-automation-id="promptOption"]'
                    )).filter((node) => {
                      const text = textOf(node);
                      return visible(node) && text && text.length <= 160;
                    });
                    const match = candidates.find((node) => normalize(textOf(node)) === target)
                      || candidates.find((node) => normalize(textOf(node)).includes(target))
                      || candidates.find((node) => target.includes(normalize(textOf(node))));
                    if (match) {
                      const row = match.closest('[role="option"], [role="listbox"] li, [role="listbox"] button, [data-automation-id="menuItem"], button, li') || match;
                      const rect = row.getBoundingClientRect();
                      return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
                    }
                  }
                  return null;
                }
                """,
                {"selector": selector, "optionText": option_text},
            )
            if target_box and target_box.get("x") and target_box.get("y"):
                await page.mouse.click(float(target_box["x"]), float(target_box["y"]))
                await asyncio.sleep(0.7)
                return True
        except Exception:
            pass
        selectors = [
            '[role="option"]',
            '[role="listbox"] li',
            '[role="listbox"] button',
            '[data-automation-id="menuItem"]',
            '[data-automation-id="promptOption"]',
        ]
        for option_selector in selectors:
            try:
                locator = page.locator(option_selector).filter(has_text=option_text)
                if await locator.count():
                    await locator.first.click(force=True, timeout=3000)
                    await asyncio.sleep(0.6)
                    return True
            except Exception:
                continue
        try:
            await page.get_by_role("option", name=re.compile(re.escape(option_text), re.I)).first.click(
                force=True,
                timeout=3000,
            )
            await asyncio.sleep(0.6)
            return True
        except Exception:
            pass
        return bool(
            await page.evaluate(
                """
                ({selector, optionText}) => {
                  const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const textOf = (node) => (
                    node?.getAttribute('data-automation-label')
                    || node?.getAttribute('aria-label')
                    || node?.innerText
                    || node?.textContent
                    || ''
                  ).trim();
                  const target = normalize(optionText);
                  const candidates = Array.from(document.querySelectorAll(
                    '[role="option"], [role="listbox"] li, [role="listbox"] button, [data-automation-id="menuItem"], [data-automation-id="promptOption"]'
                  )).filter((node) => {
                    const text = textOf(node);
                    return visible(node) && text && text.length <= 160;
                  });
                  const match = candidates.find((node) => normalize(textOf(node)) === target)
                    || candidates.find((node) => normalize(textOf(node)).includes(target))
                    || candidates.find((node) => target.includes(normalize(textOf(node))));
                  if (!match) return false;
                  const clickTarget = match.closest('[role="option"], [role="listbox"] li, [role="listbox"] button, [data-automation-id="menuItem"], button, li') || match;
                  clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
                  clickTarget.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
                  clickTarget.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                  return true;
                }
                """,
                {"selector": selector, "optionText": option_text},
            )
        )

    async def _click_ant_combobox_option(option_text: str) -> bool:
        option_text = " ".join(str(option_text or "").split()).strip()
        if not option_text:
            return False
        try:
            clicked = await page.evaluate(
                """
                (optionText) => {
                  const visible = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const target = normalize(optionText);
                  const nodes = Array.from(document.querySelectorAll(
                    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option, .ant-select-item-option, [role="option"]'
                  )).filter(visible);
                  const textOf = (node) => node.getAttribute('title') || node.getAttribute('aria-label') || node.innerText || node.textContent || '';
                  const match = nodes.find((node) => normalize(textOf(node)) === target)
                    || nodes.find((node) => normalize(textOf(node)).includes(target))
                    || nodes.find((node) => target.includes(normalize(textOf(node))));
                  if (!match) return false;
                  match.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
                  match.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
                  match.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                  return true;
                }
                """,
                option_text,
            )
            if clicked:
                await asyncio.sleep(0.7)
                return True
        except Exception:
            pass
        return False

    await log(application_id, "info", f"Step {step}: selecting [{answer}] for [{action.get('label')}]", db)
    try:
        await page.locator(selector).first.click(force=True)
    except Exception:
        await _click_selector(page, selector)
    await asyncio.sleep(1)

    element_meta = await _element_meta()
    is_searchable_prompt = (
        str(element_meta.get("tagName") or "") in {"input", "textarea"}
        and (
            str(element_meta.get("placeholder") or "") == "search"
            or str(element_meta.get("automationId") or "") == "searchbox"
        )
    )
    if is_searchable_prompt:
        await _open_workday_search_prompt()
        for _ in range(2):
            backed = await page.evaluate(
                """
                (selector) => {
                  const el = document.querySelector(selector);
                  if (!el) return false;
                  const widgetId = el.getAttribute('data-uxi-multiselect-id')
                    || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                    || '';
                  const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                  const back = prompt?.querySelector('[data-automation-id="backButton"]');
                  if (!back) return false;
                  back.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                  return true;
                }
                """,
                selector,
            )
            if not backed:
                break
            await asyncio.sleep(0.4)
        await page.evaluate(
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const container = el.closest('[data-automation-id="multiSelectContainer"]')
                || el.closest('[data-automation-id*="formField" i], label, div, section, fieldset');
              const clear = container?.querySelector('[data-automation-id="clearSearchButton"]');
              if (!clear) return false;
              clear.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
            """,
            selector,
        )
        await asyncio.sleep(0.3)
        await _open_workday_search_prompt()

    if str(element_meta.get("tagName") or "") in {"input", "textarea"} and not nested_answer:
        try:
            await _clear_input(page, selector)
            await page.locator(selector).first.click(force=True, timeout=2000)
            await page.keyboard.type(answer, delay=65)
            await asyncio.sleep(0.8)
        except Exception:
            pass

    # Workday searchable-prompt fast path. The Workday multiselect widget's
    # search input indexes leaves across submenus, so pressing Enter after
    # typing the candidate's raw answer selects the correct leaf even when it
    # lives under a category that is not visible at the root menu level (e.g.
    # "LinkedIn" leaf nested inside "LinkedIn, Monster, Indeed or other Job
    # Board"). Verified via Playwright on the standard Workday widget: exact /
    # close match selects the leaf, gibberish leaves the chip unchanged. Only
    # fires for is_searchable_prompt controls — radios/checkboxes/native
    # selects unaffected. If chip does not change we fall through to the
    # existing alias_match + click flow with no side effects.
    if is_searchable_prompt and not nested_answer and answer:
        try:
            baseline_state = await _workday_select_state()
            baseline_chip = _normalize_option_text(baseline_state.get("selectedChip"))
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.8)
            after_state = await _workday_select_state()
            after_chip = _normalize_option_text(after_state.get("selectedChip"))
            if trace_select:
                print(
                    f"[TRACE_SELECT_REPLAY] type_enter_fastpath selector={selector!r} "
                    f"answer={answer!r} baseline_chip={baseline_chip!r} "
                    f"after_chip={after_chip!r}",
                    flush=True,
                )
            if after_chip and after_chip.lower() != baseline_chip.lower():
                action["options"] = action.get("options") or []
                return
        except Exception:
            pass

    option_items = await _scoped_option_items()
    if not option_items and str(element_meta.get("role") or "") == "combobox":
        await _open_combobox_prompt()
        option_items = await _scoped_option_items()
    option_texts: list[str] = []
    seen_option_texts: set[str] = set()
    for item in option_items:
        normalized_text = _normalize_option_text(item.get("text"))
        if not normalized_text:
            continue
        lowered = normalized_text.lower()
        if lowered in seen_option_texts:
            continue
        seen_option_texts.add(lowered)
        option_texts.append(normalized_text)
    alias_match = _alias_option_match(answer, option_texts, str(action.get("label") or ""))
    normalized_answer_for_strict = " ".join(str(answer or "").lower().replace("-", " ").split())
    strict_decline_select = any(
        token in str(action.get("label") or "").lower()
        for token in ("gender", "ethnicity", "race", "veteran")
    ) and any(
        token in normalized_answer_for_strict
        for token in (
            "do not wish",
            "do not want",
            "decline",
            "prefer not",
            "not disclose",
            "choose not",
            "not to respond",
        )
    )
    strict_select = any(
        token in str(action.get("label") or "").lower()
        for token in ("country", "phone code", "dialing code", "calling code")
    ) or strict_decline_select
    match = process.extractOne(top_level_answer, option_texts, scorer=fuzz.ratio) if option_texts and not alias_match and not strict_select else None
    matched_text = alias_match or (str(match[0]) if match and match[1] > 85 else top_level_answer)
    action["options"] = option_texts

    if is_searchable_prompt and alias_match and alias_match.lower() != top_level_answer.lower() and not nested_answer:
        try:
            await _clear_input(page, selector)
            await page.locator(selector).first.click(force=True, timeout=2000)
            await page.keyboard.type(matched_text, delay=65)
            await asyncio.sleep(0.8)
        except Exception:
            pass

    if str(element_meta.get("tagName") or "") == "select":
        changed = await page.evaluate(
            """
            ({selector, matchedText, answer}) => {
              const el = document.querySelector(selector);
              if (!el || el.tagName.toLowerCase() !== 'select') return false;
              const norm = (value) => String(value || '').trim().toLowerCase();
              const phoneCode = String(answer || matchedText || '').match(/\\+\\d{1,4}/)?.[0] || '';
              const options = Array.from(el.options || []);
              const optionCodes = (option) => String(option.text || option.label || option.value || option.title || '').match(/\\+\\d{1,4}/g) || [];
              const option = options.find(o => norm(o.text || o.label || o.value) === norm(matchedText))
                || options.find(o => norm(o.value) === norm(matchedText))
                || options.find(o => phoneCode && optionCodes(o).includes(phoneCode))
                || options.find(o => norm(o.text || o.label || o.value || o.title).includes(norm(matchedText)))
                || options.find(o => norm(matchedText).includes(norm(o.text || o.label || o.value || o.title)));
              if (!option) return false;
              el.value = option.value;
              option.selected = true;
              el.dispatchEvent(new Event('input', {bubbles:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              if (window.jQuery) {
                window.jQuery(el).trigger('change');
              }
              return true;
            }
            """,
            {"selector": selector, "matchedText": matched_text, "answer": top_level_answer},
        )
        if changed:
            return

    if option_texts and not nested_answer and matched_text == top_level_answer and (not match or match[1] <= 40):
        if control_kind != "popup_select_button":
            raise RuntimeError(f"No dropdown option matched answer {answer}")
        # popup_select_button: fall through to keyboard-type approach below

    clicked = False
    hierarchical_runtime_detected = False
    if control_kind == "popup_select_button":
        clicked = await _click_popup_button_option(matched_text)
        if clicked:
            await asyncio.sleep(0.7)
    elif str(element_meta.get("role") or "") == "combobox" or control_kind == "combobox_button":
        clicked = await _click_ant_combobox_option(matched_text)
    elif is_searchable_prompt:
        click_outcome = await _click_prompt_option_outcome(matched_text)
        hierarchical_runtime_detected = click_outcome == "submenu_opened"
        clicked = click_outcome != "no_change"
        if trace_select:
            print(
                f"[TRACE_SELECT_REPLAY] top_level selector={selector!r} matched_text={matched_text!r} "
                f"click_outcome={click_outcome!r} hierarchical_runtime_detected={hierarchical_runtime_detected!r}",
                flush=True,
            )
        if clicked:
            await asyncio.sleep(0.7)

    if not clicked:
        clicked = await page.evaluate(
        """
        ({selector, matchedText}) => {
          const el = document.querySelector(selector);
          if (!el) return false;
          const widgetId = el.getAttribute('data-uxi-multiselect-id')
            || el.closest('[data-automation-id="multiSelectContainer"]')?.id
            || '';
          const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
          if (!prompt) return false;
          const root = prompt;
          const candidates = Array.from(root.querySelectorAll(
            '[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [role=listbox] li'
          )).filter(node => {
            const r = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            const text = (node.getAttribute('data-automation-label') || node.getAttribute('aria-label') || node.innerText || node.textContent || '').trim();
            return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && text && text.length <= 160;
          });
          const normalize = (value) => {
            let text = (value || '').trim().toLowerCase();
            for (const prefix of ['0 items selected', '1 item selected', '1 items selected']) {
              if (text.startsWith(prefix)) {
                text = text.slice(prefix.length).trim().replace(/^[,:-]\\s*/, '');
              }
            }
            return text;
          };
          const target = normalize(matchedText);
          const textOf = (node) => node.getAttribute('data-automation-label') || node.getAttribute('aria-label') || node.innerText || node.textContent || '';
          const match = candidates.find(node => normalize(textOf(node)) === target)
            || candidates.find(node => normalize(textOf(node)).includes(target))
            || candidates.find(node => target.includes(normalize(textOf(node))));
          if (!match) return false;
          const clickTarget = match.closest('[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], button, li') || match;
          clickTarget.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
          return true;
        }
        """,
        {"selector": selector, "matchedText": matched_text},
        )
    if not clicked:
        try:
            await page.get_by_text(matched_text, exact=False).first.click(force=True, timeout=2000)
            clicked = True
        except Exception:
            pass
    if clicked and (nested_answer or hierarchical_runtime_detected):
        nested_clicked = False
        if nested_answer:
            nested_outcome = await _click_prompt_option_outcome(nested_answer)
            nested_clicked = nested_outcome != "no_change"
            if trace_select:
                print(
                    f"[TRACE_SELECT_REPLAY] nested selector={selector!r} nested_answer={nested_answer!r} "
                    f"nested_outcome={nested_outcome!r}",
                    flush=True,
                )
            if nested_clicked:
                await asyncio.sleep(0.7)
        if not nested_clicked and nested_answer:
            nested_clicked = await page.evaluate(
            """
            ({selector, answer}) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
              const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
              if (!prompt) return false;
              const visible = (node) => {
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const normalize = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9+]+/g, ' ').trim();
              const textOf = (node) => (
                node.getAttribute('data-automation-label')
                || node.getAttribute('aria-label')
                || node.innerText
                || node.textContent
                || ''
              ).trim();
              const nodes = Array.from(prompt.querySelectorAll(
                '[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [role=listbox] li, button'
              )).filter((node) => {
                const text = normalize(textOf(node));
                return visible(node) && text && !['back', 'select', 'search'].includes(text);
              });
              if (!nodes.length) return false;
              const wanted = normalize(answer);
              const linkedinPreferred = /linkedin|linked in/.test(wanted);
              const target = nodes.find((node) => normalize(textOf(node)) === wanted)
                || nodes.find((node) => wanted && normalize(textOf(node)).includes(wanted))
                || (linkedinPreferred ? nodes.find((node) => /linkedin|linked in/.test(normalize(textOf(node)))) : null)
                || nodes.find((node) => /linkedin|linked in/.test(normalize(textOf(node))))
                || nodes[0];
              const clickTarget = target.closest('[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], button, li') || target;
              clickTarget.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
            """,
            {"selector": selector, "answer": nested_answer},
            )
        if not nested_clicked and is_searchable_prompt and not nested_answer and top_level_answer.lower() != matched_text.lower():
            nested_outcome = await _click_prompt_option_outcome(top_level_answer)
            nested_clicked = nested_outcome != "no_change"
            if nested_clicked:
                await asyncio.sleep(0.7)
        if not nested_clicked and hierarchical_runtime_detected and not nested_answer:
            # Submenu opened but user only provided a parent option — auto-click first available child.
            nested_clicked = await page.evaluate(
                """
                ({selector}) => {
                  const el = document.querySelector(selector);
                  if (!el) return false;
                  const widgetId = el.getAttribute('data-uxi-multiselect-id')
                    || el.closest('[data-automation-id="multiSelectContainer"]')?.id || '';
                  const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                  if (!prompt) return false;
                  const visible = (node) => {
                    const r = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const textOf = (node) => (
                    node.getAttribute('data-automation-label')
                    || node.getAttribute('aria-label')
                    || node.innerText
                    || node.textContent
                    || ''
                  ).trim();
                  const skip = new Set(['back', 'select', 'search']);
                  const nodes = Array.from(prompt.querySelectorAll(
                    '[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], [data-automation-id="promptOption"], [role=listbox] li, button'
                  )).filter(node => {
                    const text = textOf(node).toLowerCase();
                    return visible(node) && text && !skip.has(text);
                  });
                  if (!nodes.length) return false;
                  const clickTarget = nodes[0].closest(
                    '[role=option], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"], button, li'
                  ) || nodes[0];
                  clickTarget.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                  return true;
                }
                """,
                {"selector": selector},
            )
            if nested_clicked:
                await asyncio.sleep(0.7)
        if nested_clicked:
            await asyncio.sleep(0.7)

    if await _selection_stuck():
        label_lower = str(action.get("label") or "").lower()
        if is_searchable_prompt and any(token in label_lower for token in ("how did you hear", "hear about us", "source")):
            source_fallbacks = [
                nested_answer,
                "LinkedIn",
                "Job Site or Social Media > LinkedIn",
                "Job Boards > LinkedIn",
                "Social Media > LinkedIn",
            ]
            seen_source_fallbacks: set[str] = set()
            for fallback_text in source_fallbacks:
                fallback_text = " ".join(str(fallback_text or "").split()).strip()
                fallback_key = fallback_text.lower()
                if not fallback_text or fallback_key in seen_source_fallbacks:
                    continue
                seen_source_fallbacks.add(fallback_key)
                try:
                    await _open_workday_search_prompt()
                    await page.evaluate(
                        """
                        (selector) => {
                          const el = document.querySelector(selector);
                          if (!el) return false;
                          const widgetId = el.getAttribute('data-uxi-multiselect-id')
                            || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                            || '';
                          const prompt = widgetId ? document.querySelector(`[data-associated-widget="${widgetId}"]`) : null;
                          const back = prompt?.querySelector('[data-automation-id="backButton"]');
                          if (back) back.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                          return true;
                        }
                        """,
                        selector,
                    )
                    await asyncio.sleep(0.2)
                    if await _search_and_select_prompt_leaf(fallback_text):
                        await asyncio.sleep(0.8)
                        if not await _selection_stuck():
                            return
                except Exception:
                    continue
        if str(element_meta.get("role") or "") == "combobox":
            if await _combobox_commit_fallback(matched_text):
                return
            if matched_text != answer and await _combobox_commit_fallback(answer):
                return
        if control_kind == "popup_select_button":
            # GENERIC popup_select_button retry: the requested answer (e.g. "BS"
            # for Degree, "No" for Yes/No questions) is in the option list, but
            # the primary _click_popup_button_option path's persistence check
            # failed. Re-open the popup deliberately, scan visible options, and
            # native-click the target inside the listbox. This matches the same
            # pattern that fixed HDYHAU and applies to any popup that fails its
            # initial click.
            try:
                # Ensure popup is OPEN. The button toggles, so check before clicking.
                for _ in range(3):
                    is_open = await page.evaluate(
                        """sel => {
                            const btn = document.querySelector(sel);
                            if (!btn) return false;
                            const expanded = btn.getAttribute('aria-expanded') === 'true';
                            const ctlId = btn.getAttribute('aria-controls') || '';
                            const lb = ctlId ? document.getElementById(ctlId) : null;
                            const lbVisible = lb ? (lb.offsetWidth > 0 && lb.offsetHeight > 0 && !!lb.offsetParent) : false;
                            return expanded || lbVisible;
                        }""",
                        selector,
                    )
                    if is_open:
                        break
                    try:
                        await page.locator(selector).first.click(force=True, timeout=2500)
                    except Exception:
                        await _click_selector(page, selector)
                    await asyncio.sleep(0.45)
                # Try each candidate text in turn — exact match in any visible listbox.
                candidates = [c for c in dict.fromkeys([matched_text, answer]) if c]
                for candidate in candidates:
                    candidate_text = " ".join(str(candidate).split()).strip()
                    if not candidate_text:
                        continue
                    clicked = await page.evaluate(
                        """({selector, target}) => {
                            const button = document.querySelector(selector);
                            const ariaCtl = button?.getAttribute('aria-controls') || '';
                            const roots = [];
                            if (ariaCtl) {
                                const ctl = document.getElementById(ariaCtl);
                                if (ctl) roots.push(ctl);
                            }
                            for (const lb of document.querySelectorAll('[role=listbox]')) {
                                const r = lb.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && lb.offsetParent) roots.push(lb);
                            }
                            const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                            const tgt = norm(target);
                            for (const root of roots) {
                                const opts = Array.from(root.querySelectorAll('[role=option], li, button'));
                                // Exact match preferred, then substring (e.g. "BS" inside "B.S. (Bachelor of Science)")
                                let hit = opts.find(el => norm(el.innerText || el.textContent) === tgt);
                                if (!hit) {
                                    hit = opts.find(el => {
                                        const t = norm(el.innerText || el.textContent);
                                        return t && (t.includes(tgt) || tgt.includes(t));
                                    });
                                }
                                if (hit && typeof hit.click === 'function') {
                                    hit.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        {"selector": selector, "target": candidate_text},
                    )
                    if not clicked:
                        continue
                    await asyncio.sleep(0.75)
                    target_norm = candidate_text.lower()
                    button_state = await page.evaluate(
                        """({selector, target}) => {
                            const btn = document.querySelector(selector);
                            if (!btn) return {ok: false};
                            const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                            const combined = `${txt} ${label}`;
                            return {ok: combined.includes(target), text: txt};
                        }""",
                        {"selector": selector, "target": target_norm},
                    )
                    if button_state and button_state.get("ok"):
                        await log(
                            application_id,
                            "info",
                            f"Step {step}: popup re-click picked [{candidate_text}] for [{action.get('label')}]",
                            db,
                        )
                        return
            except Exception:
                pass
            # DEGREE-specific equivalences: candidates often save abbreviations
            # (BS/MS/PhD/JD) from one Workday site, but other sites spell out
            # ("Bachelors", "Masters", "Doctorate"). Try the equivalence list
            # when the original answer didn't match any option.
            if "degree" in label_lower or "education level" in label_lower:
                await log(
                    application_id,
                    "info",
                    f"Step {step}: Degree fallback entering label=[{action.get('label')}] answer=[{answer}]",
                    db,
                )
                try:
                    degree_equiv: dict[str, list[str]] = {
                        "bs": ["Bachelors", "Bachelor", "Bachelor of Science", "B.S."],
                        "ba": ["Bachelors", "Bachelor", "Bachelor of Arts", "B.A."],
                        "ms": ["Masters", "Master", "Master of Science", "M.S."],
                        "ma": ["Masters", "Master", "Master of Arts", "M.A."],
                        "mba": ["Masters", "Master", "MBA", "Master of Business Administration"],
                        "phd": ["Doctorate", "Doctor", "Ph.D."],
                        "jd": ["Juris Doctor", "J.D.", "Doctorate"],
                        "aa": ["Associates", "Associate"],
                        "as": ["Associates", "Associate"],
                    }
                    key = " ".join(str(answer).split()).strip().lower().rstrip(".")
                    equivalents = degree_equiv.get(key, [])
                    # Also derive a list from the answer itself + matched_text
                    candidates_dgr = list(dict.fromkeys(
                        [c for c in [matched_text, answer, *equivalents] if c]
                    ))
                    # Ensure popup is open
                    for _ in range(3):
                        is_open = await page.evaluate(
                            """sel => {
                                const btn = document.querySelector(sel);
                                if (!btn) return false;
                                const expanded = btn.getAttribute('aria-expanded') === 'true';
                                const ctlId = btn.getAttribute('aria-controls') || '';
                                const lb = ctlId ? document.getElementById(ctlId) : null;
                                const lbVisible = lb ? (lb.offsetWidth > 0 && lb.offsetHeight > 0 && !!lb.offsetParent) : false;
                                return expanded || lbVisible;
                            }""",
                            selector,
                        )
                        if is_open:
                            break
                        try:
                            await page.locator(selector).first.click(force=True, timeout=2500)
                        except Exception:
                            await _click_selector(page, selector)
                        await asyncio.sleep(0.45)
                    for cand in candidates_dgr:
                        cand_text = " ".join(str(cand).split()).strip()
                        if not cand_text:
                            continue
                        clicked = await page.evaluate(
                            """({selector, target}) => {
                                const button = document.querySelector(selector);
                                const ariaCtl = button?.getAttribute('aria-controls') || '';
                                const roots = [];
                                if (ariaCtl) {
                                    const ctl = document.getElementById(ariaCtl);
                                    if (ctl) roots.push(ctl);
                                }
                                for (const lb of document.querySelectorAll('[role=listbox]')) {
                                    const r = lb.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0 && lb.offsetParent) roots.push(lb);
                                }
                                const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const tgt = norm(target);
                                for (const root of roots) {
                                    const opts = Array.from(root.querySelectorAll('[role=option], li, button'));
                                    let hit = opts.find(el => norm(el.innerText || el.textContent) === tgt);
                                    if (!hit) {
                                        hit = opts.find(el => {
                                            const t = norm(el.innerText || el.textContent);
                                            return t && (t.includes(tgt) || tgt.includes(t));
                                        });
                                    }
                                    if (hit && typeof hit.click === 'function') {
                                        hit.click();
                                        return {text: (hit.innerText || hit.textContent || '').trim()};
                                    }
                                }
                                return null;
                            }""",
                            {"selector": selector, "target": cand_text},
                        )
                        if not clicked:
                            continue
                        await asyncio.sleep(0.75)
                        target_norm = clicked.get("text", "").lower()
                        button_state = await page.evaluate(
                            """({selector, target}) => {
                                const btn = document.querySelector(selector);
                                if (!btn) return {ok: false};
                                const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                                return {ok: (`${txt} ${label}`).includes(target), text: txt};
                            }""",
                            {"selector": selector, "target": target_norm},
                        )
                        if button_state and button_state.get("ok"):
                            await log(
                                application_id,
                                "info",
                                f"Step {step}: Degree fallback picked [{clicked.get('text')}] (mapped from {answer})",
                                db,
                            )
                            return
                except Exception:
                    pass
            # HDYHAU-specific fallback: the answer "LinkedIn" often isn't in the
            # site's option list (e.g. Cigna offers Agency/Career Site/Recruiter/
            # Referred by Employee). Re-open the popup, read what's actually
            # available, and pick the closest neutral source.
            if any(token in label_lower for token in ("how did you hear", "hear about us", "source")):
                try:
                    # Ensure popup is OPEN before scanning. The button toggles open/closed,
                    # so check aria-expanded and only click to open if currently closed.
                    for attempt in range(3):
                        is_open = await page.evaluate(
                            """sel => {
                                const btn = document.querySelector(sel);
                                if (!btn) return false;
                                const expanded = btn.getAttribute('aria-expanded') === 'true';
                                const ctlId = btn.getAttribute('aria-controls') || '';
                                const lb = ctlId ? document.getElementById(ctlId) : null;
                                const lbVisible = lb ? (lb.offsetWidth > 0 && lb.offsetHeight > 0 && !!lb.offsetParent) : false;
                                return expanded || lbVisible;
                            }""",
                            selector,
                        )
                        if is_open:
                            break
                        try:
                            await page.locator(selector).first.click(force=True, timeout=2500)
                        except Exception:
                            await _click_selector(page, selector)
                        await asyncio.sleep(0.45)
                    await log(
                        application_id,
                        "info",
                        f"Step {step}: HDYHAU fallback opening priority-match search",
                        db,
                    )
                    available_opt = await page.evaluate(
                        """({selector, priority}) => {
                            const button = document.querySelector(selector);
                            const ariaCtl = button?.getAttribute('aria-controls') || '';
                            const roots = [];
                            if (ariaCtl) {
                                const ctl = document.getElementById(ariaCtl);
                                if (ctl) roots.push(ctl);
                            }
                            // Fall back to any visible listbox on screen
                            for (const lb of document.querySelectorAll('[role=listbox]')) {
                                const r = lb.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && lb.offsetParent) roots.push(lb);
                            }
                            const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                            const skip = new Set(['select one', 'select', '', 'back']);
                            for (const root of roots) {
                                const opts = Array.from(root.querySelectorAll('[role=option], li, button'))
                                    .map(el => ({el, text: (el.innerText || '').trim()}))
                                    .filter(o => o.text && !skip.has(norm(o.text)) && o.text.length <= 80);
                                if (!opts.length) continue;
                                const optsByNorm = new Map(opts.map(o => [norm(o.text), o]));
                                // Try priority list first (exact match)
                                for (const p of priority) {
                                    const hit = optsByNorm.get(norm(p));
                                    if (hit) return {text: hit.text, picked: p};
                                }
                                // Substring match next
                                for (const p of priority) {
                                    const np = norm(p);
                                    const sub = opts.find(o => norm(o.text).includes(np) || np.includes(norm(o.text)));
                                    if (sub) return {text: sub.text, picked: p};
                                }
                                // Fallback: first non-"select one" option
                                return {text: opts[0].text, picked: 'first'};
                            }
                            return null;
                        }""",
                        {
                            "selector": selector,
                            "priority": [
                                "LinkedIn",
                                "Indeed",
                                "Glassdoor",
                                "Job Site",
                                "Job Board",
                                "Career Site",
                                "Company Website",
                                "Corporate Website",
                                "Web Search",
                                "Search Engine",
                                "Internet",
                                "Social Media",
                                "Other",
                            ],
                        },
                    )
                    if available_opt and available_opt.get("text"):
                        target = available_opt["text"]
                        await log(
                            application_id,
                            "info",
                            f"Step {step}: HDYHAU fallback trying [{target}] (priority match: {available_opt.get('picked')})",
                            db,
                        )
                        # Native click directly via JS within the listbox — bypasses
                        # the more generic _click_popup_button_option which may pick
                        # the wrong target in a re-opened popup.
                        clicked = await page.evaluate(
                            """({selector, target}) => {
                                const button = document.querySelector(selector);
                                const ariaCtl = button?.getAttribute('aria-controls') || '';
                                const roots = [];
                                if (ariaCtl) {
                                    const ctl = document.getElementById(ariaCtl);
                                    if (ctl) roots.push(ctl);
                                }
                                for (const lb of document.querySelectorAll('[role=listbox]')) {
                                    const r = lb.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0 && lb.offsetParent) roots.push(lb);
                                }
                                const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const tgt = norm(target);
                                for (const root of roots) {
                                    const opts = Array.from(root.querySelectorAll('[role=option], li, button'));
                                    const hit = opts.find(el => norm(el.innerText || el.textContent) === tgt);
                                    if (hit && typeof hit.click === 'function') {
                                        hit.click();
                                        return true;
                                    }
                                }
                                return false;
                            }""",
                            {"selector": selector, "target": target},
                        )
                        if clicked:
                            await asyncio.sleep(0.8)
                            # Verify the BUTTON's displayed text now reflects the
                            # picked option. _selection_stuck() can't be used here
                            # because it looks for the original answer ("LinkedIn"),
                            # not our chosen substitute ("Career Site").
                            target_norm = " ".join(target.lower().split())
                            button_state = await page.evaluate(
                                """({selector, target}) => {
                                    const btn = document.querySelector(selector);
                                    if (!btn) return {ok: false};
                                    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                                    const combined = `${txt} ${label}`;
                                    return {ok: combined.includes(target), text: txt};
                                }""",
                                {"selector": selector, "target": target_norm},
                            )
                            if button_state and button_state.get("ok"):
                                await log(
                                    application_id,
                                    "info",
                                    f"Step {step}: HDYHAU fallback picked [{target}]",
                                    db,
                                )
                                return
                            else:
                                await log(
                                    application_id,
                                    "warn",
                                    f"Step {step}: HDYHAU clicked [{target}] but button still shows [{button_state.get('text') if button_state else '?'}]",
                                    db,
                                )
                except Exception as hd_err:
                    await log(
                        application_id,
                        "warn",
                        f"Step {step}: HDYHAU fallback raised {hd_err}",
                        db,
                    )
            for fallback_text in dict.fromkeys([matched_text, answer]):
                fallback_text = " ".join(str(fallback_text or "").split()).strip()
                if not fallback_text:
                    continue
                try:
                    await page.locator(selector).first.click(force=True, timeout=2500)
                except Exception:
                    await _click_selector(page, selector)
                await asyncio.sleep(0.3)
                try:
                    await page.keyboard.type(fallback_text, delay=55)
                    await asyncio.sleep(0.5)
                    for key in ("ArrowDown", "Enter", "Tab"):
                        await page.keyboard.press(key)
                        await asyncio.sleep(0.35)
                    if not await _selection_stuck():
                        return
                except Exception:
                    continue
        if control_kind == "popup_select_button" and any(
            token in label_lower for token in ("gender", "ethnicity", "race", "veteran")
        ):
            fallback_options = [
                matched_text,
                answer,
                "Choose Not to Disclose",
                "I choose not to disclose.",
                "I choose not to disclose",
                "I do not wish to answer",
                "I do not wish to self-identify",
                "I do not wish to disclose",
                "Decline to Answer",
                "Prefer not to answer",
            ]
            seen_fallbacks: set[str] = set()
            for fallback_text in fallback_options:
                fallback_text = " ".join(str(fallback_text or "").split()).strip()
                fallback_key = fallback_text.lower()
                if not fallback_text or fallback_key in seen_fallbacks:
                    continue
                seen_fallbacks.add(fallback_key)
                try:
                    await page.locator(selector).first.click(force=True, timeout=2500)
                except Exception:
                    await _click_selector(page, selector)
                await asyncio.sleep(0.4)
                runtime_options = [
                    _normalize_option_text(item.get("text"))
                    for item in await _scoped_option_items()
                    if _normalize_option_text(item.get("text"))
                ]
                runtime_match = _alias_option_match(fallback_text, runtime_options, str(action.get("label") or ""))
                clicked_fallback = await _click_popup_button_option(runtime_match or fallback_text)
                if clicked_fallback:
                    await asyncio.sleep(0.7)
                    if not await _selection_stuck():
                        return
        raise RuntimeError(f"Select did not persist for [{action.get('label')}] using [{answer}]")

    if is_searchable_prompt:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass
        return

    typed = await page.evaluate(
        """
        ({selector, matchedText}) => {
          const el = document.querySelector(selector);
          if (!el || !['input', 'textarea'].includes((el.tagName || '').toLowerCase())) return false;
          el.focus();
          el.value = matchedText;
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return true;
        }
        """,
        {"selector": selector, "matchedText": matched_text},
    )
    if typed:
        try:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    if str(element_meta.get("tagName") or "") in {"input", "textarea"}:
        for key in ("ArrowDown", "Enter", "Tab"):
            try:
                await page.keyboard.press(key)
                await asyncio.sleep(0.4)
            except Exception:
                pass

    selected_state = await _workday_select_state()
    if trace_select:
        print(
            f"[TRACE_SELECT_REPLAY] final_state selector={selector!r} answer={answer!r} "
            f"matched_text={matched_text!r} nested_answer={nested_answer!r} state={selected_state!r}",
            flush=True,
        )
    committed_text = " | ".join(
        _normalize_option_text(selected_state.get(key))
        for key in ("promptSelection", "selectedChip", "directText", "value", "ariaValueText")
        if _normalize_option_text(selected_state.get(key))
    ).lower()
    prompt_open = bool(selected_state.get("promptOpen"))
    if is_searchable_prompt:
        if not prompt_open and committed_text and nested_answer and nested_answer.lower() in committed_text:
            return
        if not prompt_open and committed_text and answer.lower() in committed_text:
            return
        if not prompt_open and committed_text and matched_text.lower() in committed_text:
            return
    elif committed_text and answer.lower() in committed_text:
        return
    if not is_searchable_prompt and committed_text and matched_text.lower() in committed_text:
        return
    if clicked and not is_searchable_prompt:
        return
    label_text = str(action.get("label") or "").lower()
    if is_searchable_prompt and "skill" in label_text and not str(action.get("required") or "").lower() == "true":
        await log(
            application_id,
            "info",
            f"Step {step}: optional skills prompt had no committed match for [{answer}], continuing",
            db,
        )
        return

    # Native <select> fallback without Playwright's CSS selector option path.
    changed = await page.evaluate(
        """
        ({selector, matchedText}) => {
          const el = document.querySelector(selector);
          if (!el || el.tagName.toLowerCase() !== 'select') return false;
          const option = Array.from(el.options).find(o => (o.text || o.label || o.value || '').trim() === matchedText)
            || Array.from(el.options).find(o => (o.text || o.label || o.value || '').toLowerCase().includes(matchedText.toLowerCase()));
          if (!option) return false;
          el.value = option.value;
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return true;
        }
        """,
        {"selector": selector, "matchedText": matched_text},
    )
    if not changed:
        raise RuntimeError(f"Could not select option {matched_text}")


async def _execute_radio_action(
    page: object,
    action: dict[str, object],
    application_id: UUID,
    db: AsyncSession,
    step: int,
) -> None:
    answer = str(action.get("answer") or action.get("value") or "Yes")
    field_label = str(action.get("label") or "")
    await log(application_id, "info", f"Step {step}: selecting radio [{answer}] for [{field_label}]", db)
    selector = str(action.get("selector") or "")

    async def radio_selection_persisted() -> bool:
        if not selector:
            return True
        await asyncio.sleep(1.0)
        return await _radio_is_selected(page, selector, answer)

    if selector:
        try:
            resolved_selector = await _resolve_radio_answer_selector(page, selector, answer)
            if resolved_selector and resolved_selector != selector:
                await log(
                    application_id,
                    "warn",
                    f"Step {step}: radio selector corrected from [{selector}] to [{resolved_selector}] for [{answer}]",
                    db,
                )
                selector = resolved_selector
        except Exception:
            pass

        # Universal strategy ladder. Each strategy is tried only if the previous
        # one did NOT commit the selection. Firing extra synthetic events after a
        # successful click contaminates framework form state (Workday in
        # particular: extra clicks can transiently re-mark the other radio as
        # selected, which the validator then complains about on submit).
        try:
            label_selector = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el?.id) return '';
                    return `label[for="${CSS.escape(el.id)}"]`;
                }""",
                selector,
            )
            if label_selector:
                label_locator = page.locator(str(label_selector)).first
                try:
                    await label_locator.click(timeout=2500)
                except Exception:
                    await label_locator.click(force=True, timeout=2500)
                if await radio_selection_persisted():
                    return
        except Exception:
            pass
        try:
            locator = page.locator(selector).first
            await locator.check(force=True, timeout=2500)
            if await radio_selection_persisted():
                return
        except Exception:
            pass
        try:
            if await _click_workday_radio_row(page, selector):
                with suppress(Exception):
                    await page.keyboard.press("Tab")
                if await radio_selection_persisted():
                    return
        except Exception:
            pass
        try:
            if await _keyboard_select_radio(page, selector):
                with suppress(Exception):
                    await page.keyboard.press("Tab")
                if await radio_selection_persisted():
                    return
        except Exception:
            pass
        try:
            if await _click_radio_selector(page, selector, answer):
                if await radio_selection_persisted():
                    return
        except Exception:
            pass
        try:
            if await _force_native_radio_selection(page, selector):
                if await radio_selection_persisted():
                    return
        except Exception:
            pass
        try:
            await page.locator(selector).first.click(force=True, timeout=2000)
            if await radio_selection_persisted():
                return
        except Exception:
            pass
        await log(application_id, "warning", f"Step {step}: radio selector [{selector}] did not persist for [{answer}], trying label fallback", db)

    radios = await page.evaluate(
        """
        (groupLabel) => {
          const labels = Array.from(document.querySelectorAll('label, [role=radio], [class*=radio]'));
          return labels.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          }).map(el => ({
            text: (el.innerText || el.textContent || '').trim(),
            ariaLabel: el.getAttribute('aria-label') || ''
          }));
        }
        """,
        field_label,
    )
    option_texts = [
        (str(item.get("text") or item.get("ariaLabel") or "").strip())
        for item in radios
        if str(item.get("text") or item.get("ariaLabel") or "").strip()
    ]
    match = process.extractOne(answer, option_texts, scorer=fuzz.partial_ratio) if option_texts else None
    matched_text = str(match[0]) if match and match[1] > 40 else answer

    try:
        await page.locator("label").filter(has_text=matched_text).first.click(force=True, timeout=2000)
        if selector and await radio_selection_persisted():
            return
    except Exception:
        pass
    clicked_in_group = await page.evaluate(
        """
        ({groupLabel, matchedText}) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const normalize = (value) => (value || '').trim().toLowerCase();
          const targetGroup = normalize(groupLabel);
          const targetOption = normalize(matchedText);
          const textOf = (el) => (el?.innerText || el?.textContent || '').trim();

          const labelNodes = Array.from(document.querySelectorAll('label, legend, span, p, div'))
            .filter(visible)
            .filter(el => {
              const text = normalize(textOf(el));
              return text && text.length <= 220 && text.includes(targetGroup);
            });

          for (const labelNode of labelNodes) {
            const container = labelNode.closest('fieldset, section, div, li') || labelNode.parentElement;
            if (!container) continue;
            const candidates = Array.from(container.querySelectorAll('label, button, [role=radio], [data-automation-id], div, span'))
              .filter(visible)
              .filter(el => {
                const text = normalize(textOf(el));
                return text && text.length <= 40;
              });
            const match = candidates.find(el => normalize(textOf(el)) === targetOption)
              || candidates.find(el => normalize(textOf(el)).endsWith(targetOption))
              || candidates.find(el => normalize(textOf(el)).includes(targetOption));
            if (match) {
              match.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
          }
          return false;
        }
        """,
        {"groupLabel": field_label, "matchedText": matched_text},
    )
    if clicked_in_group:
        if not selector or await radio_selection_persisted():
            return
    try:
        await page.get_by_text(matched_text, exact=False).first.click(force=True, timeout=2000)
        if not selector or await radio_selection_persisted():
            return
        raise RuntimeError(f"Radio option {matched_text} did not persist")
    except Exception as exc:
        raise RuntimeError(f"Could not click radio option {matched_text}") from exc


async def _execute_upload_action(
    page: object,
    action: dict[str, object],
    application_id: UUID,
    db: AsyncSession,
    step: int,
) -> bool:
    selector = str(action.get("selector") or "")
    raw_path = str(action.get("path") or "")
    if not selector or not raw_path:
        return False
    resume_path = str(Path(raw_path).resolve())
    if not Path(resume_path).exists():
        await log(application_id, "error", f"Resume file not found: {resume_path}", db)
        return False
    await page.evaluate(
        """(selector) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const fileInput = document.querySelector(selector);
            const roots = [
                fileInput?.closest('fieldset, section, form, div'),
                document,
            ].filter(Boolean);
            const textOf = (node) => (
                node?.innerText
                || node?.textContent
                || node?.getAttribute?.('aria-label')
                || node?.value
                || ''
            );
            for (const root of roots) {
                const radios = Array.from(root.querySelectorAll('input[type="radio"], [role="radio"]'));
                const preferred = radios.find((radio) => {
                    const id = radio.id || '';
                    const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                    const text = normalize([id, radio.name, radio.value, textOf(label), textOf(radio.closest('label'))].join(' '));
                    return (
                        (text.includes('resumeuploadradio') || text.includes('resume') || text.includes('cv file') || text.includes('file to upload'))
                        && !text.includes('indeed')
                        && !text.includes('linkedin')
                        && !text.includes('no thanks')
                        && !text.includes('manually')
                    );
                });
                if (preferred) {
                    const label = preferred.id ? document.querySelector(`label[for="${CSS.escape(preferred.id)}"]`) : null;
                    const target = label || preferred.closest('label') || preferred;
                    target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
                    target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
                    target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                    preferred.checked = true;
                    preferred.dispatchEvent(new Event('input', {bubbles:true}));
                    preferred.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }
            }
            return false;
        }""",
        selector,
    )
    await asyncio.sleep(0.3)
    already_uploaded = await page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            const text = (document.body?.innerText || '').toLowerCase();
            return Boolean(
              (el && el.files && el.files.length > 0)
              || text.includes('successfully uploaded')
              || document.querySelector('[data-automation-id="file-upload-successful"], [data-automation-id="file-upload-item-name"]')
            );
        }""",
        selector,
    )
    if already_uploaded:
        await log(application_id, "info", f"Step {step}: resume already uploaded for [{selector}]", db)
        return True
    await log(application_id, "info", f"Step {step}: uploading resume from [{resume_path}]", db)
    try:
        file_selectors = [selector, 'input[type="file"]', '[data-automation-id="file-upload-input-ref"]']
        seen_file_selectors: set[str] = set()
        for file_selector in file_selectors:
            if not file_selector or file_selector in seen_file_selectors:
                continue
            seen_file_selectors.add(file_selector)
            try:
                inputs = page.locator(file_selector)
                count = await inputs.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    await inputs.nth(index).set_input_files(resume_path, timeout=5000)
                    await asyncio.sleep(0.5)
                    uploaded = await page.evaluate(
                        """() => {
                            const text = (document.body?.innerText || '').toLowerCase();
                            const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                            return Boolean(
                              inputs.some((el) => el.files && el.files.length > 0)
                              || text.includes('successfully uploaded')
                              || document.querySelector('[data-automation-id="file-upload-successful"], [data-automation-id="file-upload-item-name"]')
                            );
                        }"""
                    )
                    if uploaded:
                        return True
                except Exception:
                    continue
        await page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (el) {
                    el.style.display = 'block';
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                    el.style.width = el.style.width || '1px';
                    el.style.height = el.style.height || '1px';
                }
            }""",
            selector,
        )
        await page.set_input_files(selector, resume_path)
        await asyncio.sleep(0.5)
        uploaded = await page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return Boolean(el.files && el.files.length > 0);
            }""",
            selector,
        )
        if uploaded:
            return True
        raise RuntimeError("direct set_input_files left file input empty")
    except Exception as direct_error:
        try:
            drop_zone = page.locator(
                '#resumeAttachments--attachments, [data-automation-id="select-files"], [data-automation-id="file-upload-drop-zone"], label[for="resume"], button:has-text("Attach")'
            ).first
            async with page.expect_file_chooser() as file_chooser_info:
                await drop_zone.click(force=True, timeout=5000)
            chooser = await file_chooser_info.value
            await chooser.set_files(resume_path)
            await asyncio.sleep(0.5)
            uploaded = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    return Boolean(el && el.files && el.files.length > 0);
                }""",
                selector,
            )
            if uploaded:
                return True
        except Exception as chooser_error:
            await log(
                application_id,
                "error",
                f"Resume upload failed: {chooser_error}; direct set_input_files failed: {direct_error}",
                db,
            )
            return False
    await log(application_id, "error", f"Resume upload did not attach a file for [{selector}]", db)
    return False


async def _execute_field_action(
    page: object,
    action: dict[str, object],
    candidate: Candidate,
    application_id: UUID,
    db: AsyncSession,
    step: int,
) -> bool | None:
    action_type = action.get("type")
    if action_type == "clear":
        selector = str(action.get("selector") or "")
        if not selector:
            return None
        await log(application_id, "info", f"Step {step}: clearing skipped field [{action.get('label')}]", db)
        await _clear_input(page, selector)
        await _dispatch_change(page, selector)
    elif action_type == "fill":
        selector = str(action.get("selector") or "")
        if not selector:
            return None
        field_type = str(action.get("field_type") or "").lower()
        label_text = str(action.get("label") or "").lower()
        raw_value = str(action.get("value") or "").strip()
        page_url = str(getattr(page, "url", "") or "").lower()
        is_workday_page = "myworkdayjobs.com" in page_url or "workdayjobs.com" in page_url
        if "phone extension" in label_text or ("extension" in label_text and "phone" in label_text):
            if is_workday_page or re.search(r"\+\d{1,4}", raw_value):
                await log(application_id, "info", f"Step {step}: skipping phone extension [{action.get('label')}]", db)
                return None
        if (
            (field_type == "tel" or any(token in label_text for token in ("phone", "mobile", "telephone")))
            and not any(token in label_text for token in ("country phone code", "phone code", "dialing code", "calling code", "phone type", "phone device"))
            and re.search(r"\+\d{1,4}", raw_value)
            and len(re.sub(r"\D+", "", raw_value)) <= 4
        ):
            action = {**action, "value": _local_phone_number_value(candidate, raw_value)}
        await log(application_id, "info", f"Step {step}: filling [{action.get('label')}] with [{action.get('value')}]", db)
        if "-datesection" in selector.lower():
            raw_value = str(action.get("value") or "").strip()
            parsed_month = parsed_day = parsed_year = ""
            iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw_value)
            us_match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw_value)
            # Partial-date formats — common for "Graduation Date", "Start Date",
            # "Work Experience End Date" on Workday widgets that render only
            # Month + Year sections (no Day). The section-existence probe below
            # drops parsed parts the widget doesn't render, so it is safe to
            # parse a month here even when the widget lacks a day section.
            month_year_match = re.fullmatch(r"(\d{1,2})[/-](\d{4})", raw_value)
            year_month_match = re.fullmatch(r"(\d{4})[/-](\d{1,2})", raw_value)
            year_only_match = re.fullmatch(r"(\d{4})", raw_value)
            if iso_match:
                parsed_year, parsed_month, parsed_day = iso_match.groups()
            elif us_match:
                parsed_month, parsed_day, parsed_year = us_match.groups()
            elif month_year_match:
                parsed_month, parsed_year = month_year_match.groups()
            elif year_month_match:
                parsed_year, parsed_month = year_month_match.groups()
            elif year_only_match:
                parsed_year = year_only_match.group(1)
            if parsed_year:
                prefix = re.sub(r"-dateSection(?:Month|Day|Year)-input$", "", selector, flags=re.I)
                # Workday's date widget hides each <input role="spinbutton"> behind a
                # styled wrapper. The input is ~0px wide — Playwright .click() reports
                # "outside viewport" and any keyboard typed there goes to <body>.
                # Verified live: clicking the parent dateInputWrapper focuses the
                # rightmost section (Year), then ArrowLeft walks back through sections
                # (Year → Day → Month), and each digit auto-advances after the section
                # is full. Sequence below: click wrapper, walk to leftmost present
                # section, type each section's digits in order, Tab to commit.
                try:
                    # Detect which sections exist on this widget
                    section_existence = await page.evaluate(
                        """(prefix) => ({
                            month: !!document.getElementById(`${prefix}-dateSectionMonth-input`.replace(/^#/, '')),
                            day: !!document.getElementById(`${prefix}-dateSectionDay-input`.replace(/^#/, '')),
                            year: !!document.getElementById(`${prefix}-dateSectionYear-input`.replace(/^#/, '')),
                        })""",
                        prefix.lstrip("#"),
                    )
                    has_month = bool(section_existence.get("month")) and parsed_month
                    has_day = bool(section_existence.get("day")) and parsed_day
                    has_year = bool(section_existence.get("year")) and parsed_year
                    if not (has_month or has_day or has_year):
                        return None

                    # Click the wrapper (the parent div, which IS visible)
                    wrapper_sel = prefix
                    try:
                        await page.locator(wrapper_sel).first.click(timeout=2500)
                    except Exception:
                        # Fallback: dispatch click via JS on the wrapper element
                        await page.evaluate(
                            "sel => { const el = document.querySelector(sel); if (el) el.click(); }",
                            wrapper_sel,
                        )
                    await asyncio.sleep(0.15)

                    # Clear whatever section currently has focus (typically Year by default).
                    # Backspace works inside spinbutton — 4 covers max year digits.
                    for _ in range(4):
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(0.02)

                    # Walk left to the leftmost section we need to fill.
                    # Default focus lands on Year. ArrowLeft Year → Day (if exists) → Month.
                    if has_month:
                        steps_left = 2 if has_day else 1
                    elif has_day:
                        steps_left = 1
                    else:
                        steps_left = 0
                    for _ in range(steps_left):
                        await page.keyboard.press("ArrowLeft")
                        await asyncio.sleep(0.05)

                    async def _press_digits(value: str, *, clear_first: bool) -> None:
                        # Backspace only on the FIRST section. For subsequent sections
                        # Workday has already auto-advanced focus and the section is
                        # empty — extra Backspace presses would navigate BACKWARDS
                        # and erase what we just typed (caused 2026-05-14 → 2/2/2006).
                        if clear_first:
                            for _ in range(4):
                                await page.keyboard.press("Backspace")
                                await asyncio.sleep(0.02)
                        for ch in value:
                            await page.keyboard.press(ch)
                            await asyncio.sleep(0.05)

                    is_first = True
                    if has_month:
                        await _press_digits(str(int(parsed_month)).zfill(2), clear_first=is_first)
                        is_first = False
                        # Auto-advance happens after 2 digits — focus moves right
                    if has_day:
                        await _press_digits(str(int(parsed_day)).zfill(2), clear_first=is_first)
                        is_first = False
                    if has_year:
                        await _press_digits(parsed_year, clear_first=is_first)

                    # Tab commits and blurs the widget so Workday persists + validates
                    await page.keyboard.press("Tab")
                    await asyncio.sleep(0.25)
                except Exception:
                    pass
                return None
        if (field_type == "tel" or "phone" in label_text) and not any(
            token in label_text for token in ("country phone code", "phone code", "dialing code", "calling code")
        ):
            phone_value = _local_phone_number_value(candidate, action.get("value"))
            phone_digits = re.sub(r"\D+", "", phone_value)
            extra_answers = getattr(candidate, "extra_answers", None) or {}
            country_code = _candidate_country_dial_code(candidate, extra_answers)
            stripped_phone = phone_value.strip()
            if stripped_phone.startswith("+"):
                # Trust an already-formatted E.164 value; never overwrite its prefix.
                e164_value = stripped_phone
            elif country_code and phone_digits:
                if country_code.startswith("+") and phone_digits.startswith(country_code.lstrip("+")):
                    # Avoid stacking the code on top of digits that already contain it.
                    e164_value = "+" + phone_digits
                else:
                    e164_value = f"{country_code}{phone_digits}"
            else:
                e164_value = phone_value
            await page.evaluate(
                """({selector, value, e164Value}) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    const setNativeValue = (node, nextValue) => {
                        const setter = Object.getOwnPropertyDescriptor(node.constructor.prototype, 'value')?.set;
                        if (setter) {
                            setter.call(node, nextValue);
                        } else {
                            node.value = nextValue;
                        }
                    };
                    el.focus();
                    setNativeValue(el, '');
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));

                    const hasE164Prefix = /^\\+\\d/.test(String(e164Value || ''));
                    const intlInstance = window.intlTelInputGlobals?.getInstance?.(el);
                    if (intlInstance?.setNumber && hasE164Prefix) {
                        // Only let intl-tel-input parse when we have a real "+code" prefix,
                        // otherwise it silently picks its initialCountry default (often +1).
                        intlInstance.setNumber(e164Value);
                    } else {
                        setNativeValue(el, value);
                    }

                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    el.blur();
                    return true;
                }""",
                {"selector": selector, "value": phone_value, "e164Value": e164_value},
            )
            await asyncio.sleep(0.5)
            expected_digits = re.sub(r"\D+", "", str(action.get("value") or ""))
            actual_digits = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    return String(el?.value || '').replace(/\\D+/g, '');
                }""",
                selector,
            )
            if expected_digits and expected_digits not in str(actual_digits or ""):
                await page.evaluate(
                    """({selector, value}) => {
                        const el = document.querySelector(selector);
                        if (!el) return;
                        el.focus();
                        const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set;
                        if (setter) {
                            setter.call(el, value);
                        } else {
                            el.value = value;
                        }
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.blur();
                    }""",
                    {"selector": selector, "value": str(action.get("value") or "")},
                )
        else:
            # Detect chip-picker inputs (placeholder=Search) before typing.
            is_chip_picker = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    return (el.placeholder || '').trim().toLowerCase() === 'search';
                }""",
                selector,
            )
            try:
                await _clear_input(page, selector)
                await asyncio.sleep(0.3)
                await page.locator(selector).first.click(force=True, timeout=3000)
                await page.keyboard.type(str(action.get("value") or ""), delay=65)
                if is_chip_picker:
                    await asyncio.sleep(0.8)
                    # Wait for dropdown option to appear and click it
                    _opt_sel = '[data-automation-id="promptOption"], li[role="option"]'
                    chip_added = False
                    try:
                        await page.wait_for_selector(_opt_sel, state="visible", timeout=3000)
                        await page.locator(_opt_sel).first.click(timeout=2000)
                        await asyncio.sleep(0.5)
                        chip_added = True
                    except Exception:
                        pass
                    if not chip_added:
                        # Fallback: ArrowDown + Enter (free-text chip entry)
                        await page.keyboard.press("ArrowDown")
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.5)
                    return None
                await _dispatch_change(page, selector)
            except Exception as exc:
                await log(
                    application_id,
                    "warn",
                    f"Step {step}: direct fill failed for [{action.get('label')}], using DOM fallback: {exc}",
                    db,
                )
            if is_chip_picker:
                return None
            desired_value = str(action.get("value") or "")
            actual_value = await page.evaluate(
                """(selector) => {
                    const el = document.querySelector(selector);
                    return String(el?.value || '');
                }""",
                selector,
            )
            if desired_value:
                await page.evaluate(
                    """({selector, value}) => {
                        const el = document.querySelector(selector);
                        if (!el) return;
                        el.focus();
                        const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set;
                        if (setter) {
                            setter.call(el, value);
                        } else {
                            el.value = value;
                        }
                        el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        el.blur();
                    }""",
                    {"selector": selector, "value": desired_value},
                )
    elif action_type == "select":
        await _execute_select_action(page, action, application_id, db, step)
    elif action_type == "radio":
        await _execute_radio_action(page, action, application_id, db, step)
    elif action_type == "check":
        await _execute_checkbox_action(page, action, application_id, db, step)
    elif action_type == "upload":
        return await _execute_upload_action(page, action, application_id, db, step)
    elif action_type == "unanswered":
        await log(application_id, "info", f"Step {step}: unanswered field [{action.get('label')}] saved", db)
        await _save_unanswered(db, page, candidate, application_id, action)
    return None


async def _click_button_action(
    page: object,
    action: dict[str, object],
    application_id: UUID,
    db: AsyncSession,
    step: int,
) -> bool:
    await log(application_id, "info", f"Step {step}: clicking button [{action.get('text')}]", db)
    # [DEBUG-EEOC] temporary trace — remove once redirect is solved
    await log(
        application_id,
        "info",
        (
            "[CLICK_BUTTON] "
            f"text={action.get('text')!r} "
            f"selector={action.get('selector')!r} "
            f"href={action.get('href')!r} "
            f"automationId={action.get('automationId')!r} "
            f"tagName={action.get('tagName')!r}"
        ),
        db,
    )
    href = str(action.get("href") or "").strip()
    surface = _find_matching_frame(page, str(action.get("frame_url") or "")) or page
    if href.lower().startswith("mailto:"):
        await log(application_id, "warn", f"Step {step}: apply link opens email client [{href}]", db)
        return True
    if (
        href.lower().startswith(("http://", "https://"))
        and "workdayjobs.com" in href.lower()
        and "/apply" in href.lower()
        and not is_third_party_apply_button({"href": href, "text": action.get("text")})
    ):
        await log(application_id, "info", f"Step {step}: navigating directly to Workday apply href [{href}]", db)
        await page.goto(href, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)
        return True

    async def _close_extra_pages() -> None:
        """Close every page/popup in the context except the main page.

        Never navigate the main page to a popup URL. Workday opens informational
        popups (eeoc.gov/poster, privacy notices, etc.) on Save and Continue —
        following them was the EEOC redirect bug. After any button click we just
        close anything extra; the main page continues naturally from its own
        navigation triggered by the click.
        """
        try:
            context = page.context
            for extra in list(context.pages):
                if extra is page or extra.is_closed():
                    continue
                try:
                    popup_url = extra.url or ""
                    await log(
                        application_id,
                        "info",
                        f"Step {step}: closing popup/tab [{popup_url}] (main page stays put)",
                        db,
                    )
                    await extra.close()
                except Exception:
                    pass
        except Exception:
            pass

    async def click_locator(selector: str) -> bool:
        locator = surface.locator(selector).first
        await locator.scroll_into_view_if_needed(timeout=3000)
        clicked = False
        try:
            async with page.expect_popup(timeout=5000) as popup_info:
                await locator.click(force=True, timeout=3000)
                clicked = True
            popup = await popup_info.value
            popup_url = ""
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=5000)
                popup_url = popup.url or ""
            except Exception:
                pass
            await log(
                application_id,
                "info",
                f"Step {step}: closing popup [{popup_url}] after button click (main page stays put)",
                db,
            )
            try:
                await popup.close()
            except Exception:
                pass
            await _close_extra_pages()
            return True
        except Exception:
            if not clicked:
                await locator.click(force=True, timeout=3000)
            await asyncio.sleep(1.5)
            await _close_extra_pages()
            return True

    selector = str(action.get("selector") or "")
    if selector:
        try:
            await click_locator(selector)
            return True
        except Exception:
            pass
        try:
            if await _click_selector(surface, selector):
                return True
        except Exception:
            pass

    automation_id = str(action.get("automationId") or "")
    if automation_id:
        try:
            if await _click_selector(page, f'[data-automation-id="{automation_id}"]'):
                return True
        except Exception:
            pass

    text = str(action.get("text") or "")
    if text:
        try:
            clicked_visible = await surface.evaluate(
                """
                (targetText) => {
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const visible = (node) => {
                    if (!node) return false;
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0
                      && rect.height > 0
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && style.opacity !== '0'
                      && !node.disabled;
                  };
                  const textOf = (node) => (
                    node?.innerText
                    || node?.textContent
                    || node?.getAttribute?.('aria-label')
                    || node?.getAttribute?.('title')
                    || node?.value
                    || ''
                  ).trim();
                  const target = normalize(targetText);
                  // Structural filter: skip any <a> without data-automation-id
                  // whose closest ancestor's text length exceeds 300 chars —
                  // those are inline hyperlinks inside body copy, never action buttons.
                  const isActionButton = (node) => {
                    if ((node.tagName || '').toLowerCase() !== 'a') return true;
                    if (node.hasAttribute('data-automation-id')) return true;
                    let cur = node.parentElement;
                    while (cur && cur !== document.body) {
                      if ((cur.innerText || cur.textContent || '').trim().length > 300) return false;
                      cur = cur.parentElement;
                    }
                    return true;
                  };
                  const nodes = Array.from(document.querySelectorAll('button, a[href], [role="button"], input[type="submit"], [type="submit"]'))
                    .filter(visible)
                    .filter(isActionButton);
                  const match = nodes.find((node) => normalize(textOf(node)) === target)
                    || nodes.find((node) => normalize(textOf(node)).includes(target))
                    || nodes.find((node) => target.includes(normalize(textOf(node))));
                  if (!match) return false;
                  // Prefer direct .click() so React/Vue synthetic handlers fire;
                  // fall back to dispatchEvent if .click is not available.
                  if (typeof match.click === 'function') {
                    match.click();
                  } else {
                    match.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                  }
                  return true;
                }
                """,
                text,
            )
            if clicked_visible:
                return True
        except Exception:
            pass
    if href and href.lower().startswith(("http://", "https://")):
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            return True
        except Exception:
            pass
    return False


async def _visible_error_text(page: object) -> str:
    try:
        text = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const isReddish = (el) => {
                const c = window.getComputedStyle(el).color;
                const m = c.match(/rgba?\\(([^)]+)\\)/);
                if (!m) return false;
                const parts = m[1].split(',').map(s => parseFloat(s.trim()));
                if (parts.length < 3) return false;
                const [r, g, b] = parts;
                return r > 140 && r > g + 40 && r > b + 40;
              };
              const labelLike = (text) => {
                if (/\\(\\s*required\\s*\\)\\s*$/i.test(text)) return true;
                if (/^\\*?\\s*required\\s*$/i.test(text)) return true;
                if (/^required\\s*field$/i.test(text)) return true;
                if (text.split(/\\s+/).length <= 4 && /required/i.test(text)) return true;
                return false;
              };
              const errorSentence = /(invalid|please\\s+(?:enter|fill|select|provide|choose|complete|correct|fix)|incorrect|must\\s+(?:be|contain|include|match)|should\\s+be|fix|missing|not\\s+(?:valid|allowed)|format|wrong|cannot\\s+be|isn't valid|isn't a|errors\\s+found|page\\s+error|error\\s+code|^error\\s*-)/i;
              const errorSelector = '[role="alert"], [aria-invalid="true"], .error, .errors, .invalid, .validation, .validation-error, .field-error, .form-error, .form__error, .help-block.error, .help-block-error, [class*="error"], [class*="invalid"], [class*="validation"], [class*="danger"], [data-automation-id*="error"], [data-automation-id*="Error"]';
              const errorNodes = Array.from(document.querySelectorAll(errorSelector)).filter(el => {
                if (!visible(el)) return false;
                const cls = String(el.getAttribute('class') || '').toLowerCase();
                if (/(no-error|no_error|error-free|valid-input|success)/.test(cls)) return false;
                return true;
              });
              const reddish = Array.from(document.querySelectorAll('span, div, p, small, li, strong, em'))
                .filter(el => visible(el) && isReddish(el) && (el.innerText || '').length < 300);
              const merged = Array.from(new Set([...errorNodes, ...reddish]));
              const messages = merged
                .map(el => (el.innerText || el.textContent || '').trim())
                .filter(text => text && text.length >= 4 && text.length < 300)
                .filter(text => !labelLike(text))
                .filter(text => errorSentence.test(text));
              return Array.from(new Set(messages)).slice(0, 5).join(' | ');
            }
            """
        )
        return str(text or "").strip()
    except Exception:
        return ""


_ERROR_FIELD_LABELS_JS = """
            () => {
              const visible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const fieldLabel = (input) => {
                if (!input) return '';
                if (input.getAttribute && input.getAttribute('aria-label')) {
                  return input.getAttribute('aria-label').trim();
                }
                const id = input.id;
                if (id) {
                  try {
                    const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
                    if (lbl && lbl.innerText) return lbl.innerText.trim();
                  } catch (e) {}
                }
                const labelEl = input.closest && input.closest('label');
                if (labelEl && labelEl.innerText) {
                  return labelEl.innerText.trim();
                }
                const wrapper = input.closest && input.closest('[data-automation-id], [data-uxi-element-id], fieldset, .css-fieldGroup');
                if (wrapper) {
                  const headingEl = wrapper.querySelector('label, h3, h4, [role="heading"], legend');
                  if (headingEl && headingEl.innerText) return headingEl.innerText.trim();
                }
                return '';
              };
              const cleanLabel = (text) => {
                if (!text) return '';
                return text.replace(/\\s*\\*\\s*$/, '').replace(/\\s*\\(required\\)\\s*$/i, '').replace(/\\s+/g, ' ').trim();
              };
              const out = [];
              const seen = new Set();
              const push = (label, message) => {
                const cleanedLabel = cleanLabel(label);
                if (!cleanedLabel || cleanedLabel.length > 200) return;
                const cleanedMessage = (message || '').slice(0, 200).trim();
                const key = cleanedLabel.toLowerCase() + '||' + cleanedMessage.toLowerCase();
                if (seen.has(key)) return;
                seen.add(key);
                out.push({label: cleanedLabel, message: cleanedMessage});
              };
              // Strategy 1: any control marked aria-invalid=true.
              document.querySelectorAll('[aria-invalid="true"]').forEach(node => {
                if (!visible(node)) return;
                let input = node;
                if (node.tagName === 'DIV' || node.tagName === 'SPAN') {
                  const inner = node.querySelector('input, select, textarea, [role="combobox"], [role="textbox"], [role="radiogroup"], button');
                  if (inner) input = inner;
                }
                let message = '';
                const describedBy = input.getAttribute && input.getAttribute('aria-describedby');
                if (describedBy) {
                  describedBy.split(/\\s+/).forEach(id => {
                    const m = document.getElementById(id);
                    if (m && visible(m) && m.innerText) {
                      message = (message + ' ' + m.innerText).trim();
                    }
                  });
                }
                push(fieldLabel(input), message);
              });
              // Strategy 2: visible error nodes -> nearest input within the same widget.
              const errSel = '[role="alert"], [class*="error"], [class*="validation"], [data-automation-id*="error"], [data-automation-id*="Error"]';
              document.querySelectorAll(errSel).forEach(errEl => {
                if (!visible(errEl)) return;
                const cls = String(errEl.getAttribute('class') || '').toLowerCase();
                if (/(no-error|no_error|error-free|valid-input|success)/.test(cls)) return;
                const message = (errEl.innerText || errEl.textContent || '').trim();
                if (!message || message.length < 4 || message.length > 300) return;
                if (/^errors?\\s*found$/i.test(message)) return;
                const wrapper = errEl.closest('[data-automation-id], [data-uxi-element-id], fieldset, .form-group, .css-fieldGroup, [data-automation-widget]');
                if (!wrapper) return;
                const input = wrapper.querySelector('input, select, textarea, [role="combobox"], [role="textbox"], [role="radiogroup"], button[aria-haspopup]');
                if (!input) return;
                push(fieldLabel(input), message);
              });
              return out.slice(0, 25);
            }
            """


def _normalise_error_field_labels(result: Any) -> list[dict[str, str]]:
    if not isinstance(result, list):
        return []
    cleaned: list[dict[str, str]] = []
    for entry in result:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        if not label:
            continue
        cleaned.append({"label": label, "message": str(entry.get("message") or "").strip()})
    return cleaned


async def _visible_error_field_labels(page: object) -> list[dict[str, str]]:
    """Return validation errors linked to specific fields.

    Walks from each error/aria-invalid node UP to the surrounding form widget
    and back DOWN to the labelling element, so we know which question to
    re-prompt the user for. Returns ``[{"label": str, "message": str}]``.

    On Workday tenants the inline error nodes sometimes flash briefly and the
    surviving signal is the aggregator ``button[data-automation-id="errorBanner"]``
    which opens a modal listing every error. When the inline scan returns
    nothing but the banner is present, we click it and re-scan.
    """
    try:
        cleaned = _normalise_error_field_labels(await page.evaluate(_ERROR_FIELD_LABELS_JS))
        if cleaned:
            return cleaned
        try:
            banner = page.locator('button[data-automation-id="errorBanner"]').first
            if await banner.count() > 0 and await banner.is_visible():
                await banner.click(timeout=1500)
                await page.wait_for_timeout(400)
                cleaned = _normalise_error_field_labels(await page.evaluate(_ERROR_FIELD_LABELS_JS))
        except Exception as banner_exc:
            logger.debug("errorBanner expansion skipped: %s", banner_exc)
        return cleaned
    except Exception as exc:
        logger.warning("_visible_error_field_labels failed: %s", exc)
        return []


async def _click_consent_fallback(page: object, application_id: UUID, db: AsyncSession, step: int) -> bool:
    clicked = await page.evaluate(
        """
        () => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const checked = (el) => Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
          const direct = Array.from(document.querySelectorAll('input[type=checkbox], [role=checkbox]'))
            .filter(visible)
            .find(el => /privacy|consent|terms|policy/i.test(el.closest('label, div, section, fieldset')?.innerText || el.getAttribute('aria-label') || ''));
          if (direct) {
            if (!checked(direct)) {
              if ('checked' in direct) {
                direct.checked = true;
              }
              direct.setAttribute('aria-checked', 'true');
              direct.dispatchEvent(new Event('input', {bubbles:true}));
              direct.dispatchEvent(new Event('change', {bubbles:true}));
            }
            return checked(direct);
          }

          const textNode = Array.from(document.querySelectorAll('label, div, span, p'))
            .filter(visible)
            .find(el => /i understand and consent|privacy policy|consent to the terms/i.test(el.innerText || el.textContent || ''));
          if (!textNode) return false;

          const container = textNode.closest('label, div, section, fieldset') || textNode;
          const candidates = Array.from(container.querySelectorAll('*')).filter(visible);
          const checkish = candidates.find(el => {
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const text = (el.innerText || el.textContent || '').trim();
            const cls = String(el.getAttribute('class') || '');
            return text.length < 12 && r.width >= 8 && r.width <= 40 && r.height >= 8 && r.height <= 40
              && (/(box|check|control|input)/i.test(cls) || style.borderStyle !== 'none' || el.tagName.toLowerCase() === 'input');
          });
          if (checkish) {
            checkish.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
            return Boolean(
              container.querySelector('input[type=checkbox]:checked')
              || container.querySelector('[role=checkbox][aria-checked="true"]')
            );
          }

          const rect = textNode.getBoundingClientRect();
          const target = document.elementFromPoint(Math.max(1, rect.left - 18), rect.top + Math.min(14, rect.height / 2));
          if (target) {
            target.click();
            return Boolean(
              container.querySelector('input[type=checkbox]:checked')
              || container.querySelector('[role=checkbox][aria-checked="true"]')
            );
          }
          return false;
        }
        """
    )
    if clicked:
        await log(application_id, "info", f"Step {step}: clicked privacy/consent fallback", db)
        await asyncio.sleep(0.5)
        return True
    return False


async def _dismiss_blocking_modal(page: object, application_id: UUID, db: AsyncSession, step: int) -> bool:
    clicked = False
    try:
        clicked = bool(
            await page.evaluate(
                """
                () => {
                  const visible = (node) => {
                    if (!node) return false;
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0
                      && rect.height > 0
                      && style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && style.opacity !== '0'
                      && !node.disabled;
                  };
                  const textOf = (node) => (
                    node.getAttribute('aria-label')
                    || node.getAttribute('title')
                    || node.innerText
                    || node.textContent
                    || ''
                  ).replace(/\\s+/g, ' ').trim().toLowerCase();
                  const candidates = Array.from(document.querySelectorAll(
                    'button, [role="button"], a, [aria-label], [title], .close, [class*="close" i]'
                  )).filter(visible);
                  const close = candidates.find((node) => {
                    const text = textOf(node);
                    return text === 'close'
                      || text === 'close the popup'
                      || text === 'close dialog'
                      || text === 'x'
                      || text === '×'
                      || text.includes('close the popup')
                      || text.includes('close popup')
                      || text.includes('close modal');
                  });
                  if (!close) return false;
                  close.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
                  close.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
                  close.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        await log(application_id, "info", f"Step {step}: dismissed blocking modal", db)
        await asyncio.sleep(1)
        return True
    try:
        await page.keyboard.press("Escape")
        await log(application_id, "info", f"Step {step}: pressed Escape for blocking modal", db)
        await asyncio.sleep(1)
        return True
    except Exception:
        return False


async def _promote_dom_apply_link(page: object, application_id: UUID, db: AsyncSession, step: int) -> bool:
    try:
        href = str(
            await page.evaluate(
                """
                () => {
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const links = Array.from(document.querySelectorAll('a[href]'));
                  const candidates = links.map((link) => {
                    const href = link.href || '';
                    const text = normalize(
                      link.innerText
                      || link.textContent
                      || link.getAttribute('title')
                      || link.getAttribute('aria-label')
                    );
                    const attrText = normalize([
                      link.getAttribute('title'),
                      link.getAttribute('aria-label'),
                      link.getAttribute('data-ph-at-id'),
                      link.getAttribute('ph-tevent')
                    ].join(' '));
                    return {href, text, attrText};
                  }).filter((item) => {
                    if (!/^https?:\\/\\//i.test(item.href)) return false;
                    if (/mailto:/i.test(item.href)) return false;
                    if (/applywithlinkedin|\\/awli\\/|linkedin\\.com\\/jobs\\/apply|ziprecruiter\\.com/i.test(item.href)) return false;
                    if (/\\bapply\\s+(with|via|using|through)\\s+(linkedin|linked in|indeed|zip\\s*recruiter|ziprecruiter|glassdoor|seek)\\b/i.test(item.text)) return false;
                    if (!/\\/apply(?:$|[/?#])/i.test(item.href)) return false;
                    return /apply/.test(item.text) || /apply/.test(item.attrText);
                  });
                  return candidates[0]?.href || '';
                }
                """
            )
        ).strip()
    except Exception:
        href = ""
    if not href:
        return False
    await log(application_id, "info", f"Step {step}: promoting DOM apply link [{href}]", db)
    await page.goto(href, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(2)
    return True


# ── Tkinter popup helpers ─────────────────────────────────────────────────────

def _ask_user_tkinter_sync(field_label: str, field_type: str, options: list[str]) -> str:
    """Blocking tkinter dialog — run in executor so it doesn't block the event loop."""
    import tkinter as tk
    from tkinter import ttk

    result: list[str] = []

    def on_submit() -> None:
        val = combo.get() if options else entry.get()
        result.append(val.strip())
        root.destroy()

    root = tk.Tk()
    root.title("Manual Answer Required")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.minsize(400, 1)

    tk.Label(
        root,
        text=field_label,
        wraplength=380,
        justify="left",
        font=("Helvetica", 11, "bold"),
    ).pack(anchor="w", padx=12, pady=(12, 4))

    if field_type:
        tk.Label(
            root,
            text=f"Type: {field_type}",
            wraplength=380,
            justify="left",
            fg="#666666",
            font=("Helvetica", 9),
        ).pack(anchor="w", padx=12, pady=(0, 6))

    if options:
        combo = ttk.Combobox(root, values=options, state="readonly", width=52)
        combo.set(options[0])
        combo.pack(padx=12, pady=(0, 8))
    else:
        # Show a date hint and pre-fill with today when the label/type suggests a date.
        _is_date = (
            field_type in ("date",)
            or any(tok in field_label.lower() for tok in ("date", "start date", "availability"))
        )
        if _is_date:
            import time as _time
            _hint_text = _time.strftime("MM/DD/YYYY  (today: %m/%d/%Y)")
            tk.Label(root, text=_hint_text, fg="#888888", font=("Helvetica", 9)).pack(anchor="w", padx=12)
        entry = tk.Entry(root, width=54, font=("Helvetica", 11))
        if _is_date:
            import time as _time2
            entry.insert(0, _time2.strftime("%m/%d/%Y"))
        entry.pack(padx=12, pady=(0, 8))
        entry.focus_set()
        entry.selection_range(0, "end")
        entry.bind("<Return>", lambda _e: on_submit())

    tk.Button(
        root,
        text="Submit Answer",
        command=on_submit,
        bg="#2563eb",
        fg="white",
        font=("Helvetica", 10, "bold"),
        padx=12,
        pady=4,
        relief="flat",
        cursor="hand2",
    ).pack(pady=(0, 12))

    root.update_idletasks()
    root.mainloop()
    return result[0] if result else ""


async def ask_user_tkinter(field_label: str, field_type: str, options: list[str]) -> str:
    """Show a tkinter prompt in a thread executor and await the answer."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _ask_user_tkinter_sync,
        field_label,
        field_type,
        list(options),
    )


def _show_step_summary_sync(step: int, url: str, num_fields: int, num_buttons: int) -> None:
    """Auto-closing 2-second status popup so the user can follow progress."""
    import tkinter as tk

    root = tk.Tk()
    root.title(f"Step {step}")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.minsize(380, 1)

    short_url = url if len(url) <= 68 else url[:65] + "…"
    body = f"Step {step}  •  {num_fields} fields  •  {num_buttons} buttons\n{short_url}"
    tk.Label(
        root,
        text=body,
        justify="left",
        anchor="w",
        font=("Helvetica", 10),
        bg="#1e293b",
        fg="#f1f5f9",
        padx=14,
        pady=10,
    ).pack(fill="both", expand=True)
    root.configure(bg="#1e293b")

    root.update_idletasks()
    root.after(2000, root.destroy)
    root.mainloop()


async def _prompt_and_execute_unanswered(
    actions: list[dict],
    page: object,
    candidate: Candidate,
    db: AsyncSession,
    application_id: UUID,
    step: int,
    domain: str,
) -> tuple[bool, list[dict]]:
    """Show a tkinter prompt for each action, save the answer, and execute immediately.

    Only call this for fields the user has NOT already been prompted about this
    run — filtering by ``tkinter_answered_labels`` is the caller's responsibility.
    Returns (any_answered, still_unanswered).
    """
    from backend.models import FormAnswer as _FA
    from sqlalchemy import select as _sa_select

    still_unanswered: list[dict] = []
    any_answered = False

    for action in actions:
        label = str(action.get("label") or "").strip()
        field_type = str(action.get("field_type") or "").strip()
        options = [str(o).strip() for o in (action.get("options") or []) if str(o).strip()]
        # Checkbox-group questions are conceptually "pick one of these" — show as a
        # dropdown in the popup regardless of how the field_type was originally tagged.
        if str(action.get("control_kind") or "").lower() == "checkbox_group" and options:
            field_type = "select-one"

        answer = await ask_user_tkinter(label, field_type, options)
        if not answer:
            still_unanswered.append(action)
            continue

        any_answered = True
        await log(application_id, "info",
                  f"Step {step}: user answered [{label}] = [{answer}] via tkinter popup", db)

        # ── File upload field: save as resume_path and execute upload directly ──
        # FormAnswer stores text answers; file uploads need set_input_files.
        # Saving to candidate.resume_path lets page_inspector generate the
        # correct "upload" action on re-inspect so _execute_upload_action fires.
        if field_type == "file" and answer:
            try:
                from pathlib import Path as _Path
                _resolved = str(_Path(answer).resolve())
                if not _Path(_resolved).exists():
                    await log(application_id, "warn",
                              f"Step {step}: resume file not found: {_resolved}", db)
                    still_unanswered.append(action)
                    continue
                candidate.resume_path = _resolved
                await db.flush()
                _fresh_data = await _inspect_page_with_retry(page, application_id, db, step,
                                                              f"post-tkinter-upload [{label}]")
                _fresh_learned = await _saved_answers(db, candidate, getattr(page, "url", ""))
                _fresh_actions = await decide_action(_fresh_data, _candidate_dict(candidate, _fresh_learned))
                _upload_done = False
                for _fa in _fresh_actions:
                    if _fa.get("type") == "upload" and _fa.get("selector"):
                        await _execute_upload_action(page, _fa, application_id, db, step)
                        _upload_done = True
                        break
                if not _upload_done:
                    await log(application_id, "warn",
                              f"Step {step}: upload action not found after saving resume_path", db)
                    still_unanswered.append(action)
            except Exception as _up_exc:
                await log(application_id, "warn",
                          f"Step {step}: file upload from tkinter failed: {_up_exc}", db)
                still_unanswered.append(action)
            continue  # skip the normal FormAnswer + re-inspect flow for this field

        # Persist to FormAnswer so the engine picks it up on future iterations.
        try:
            _existing = await db.execute(
                _sa_select(_FA).where(
                    _FA.candidate_id == candidate.id,
                    _FA.domain == domain,
                    _FA.question_text == label,
                )
            )
            _existing_row = _existing.scalars().first()
            if _existing_row:
                _existing_row.answer = answer
            else:
                db.add(_FA(candidate_id=candidate.id, domain=domain,
                           question_text=label, answer=answer))
            await db.commit()
        except Exception as fa_exc:
            logger.warning("save tkinter FormAnswer failed: %s", fa_exc)

        # Re-inspect the live page (real selectors) and execute the field action now
        # so the user sees the effect immediately without waiting for the next loop.
        try:
            _fresh_data = await _inspect_page_with_retry(page, application_id, db, step,
                                                          f"post-tkinter [{label}]")
            _fresh_learned = await _saved_answers(db, candidate, getattr(page, "url", ""))
            _fresh_actions = await decide_action(_fresh_data, _candidate_dict(candidate, _fresh_learned))
            for _fa in _fresh_actions:
                _fl = " ".join(str(_fa.get("label") or "").lower().split())
                _wl = " ".join(label.lower().split())
                if _fl == _wl and _fa.get("type") not in ("unanswered", None) and _fa.get("selector"):
                    await _execute_field_action(page, _fa, candidate, application_id, db, step)
                    await asyncio.sleep(0.5)
                    break
        except Exception as exec_exc:
            await log(application_id, "warn",
                      f"Step {step}: immediate tkinter field execution failed for [{label}]: {exec_exc}", db)

    return any_answered, still_unanswered


async def run_page_loop(page: object, candidate: Candidate, db: AsyncSession, application_id: UUID) -> str:
    # One-time cleanup: remove stale FormAnswer rows that could override the
    # hardcoded "No" answer for previous-worker / worked-here-before questions.
    await _purge_previous_worker_form_answers(db, candidate.id)

    target_job_title = ""
    target_job_url = ""
    application = await db.get(Application, application_id)
    if application is not None and application.job_id:
        target_job = await db.get(Job, application.job_id)
        if target_job is not None:
            target_job_title = str(target_job.title or "")
            target_job_url = str(target_job.url or "")

    try:
        from backend.engine.external_applier import run_vertex_vision_loop

        vision_result = await run_vertex_vision_loop(
            page,
            candidate,
            db,
            application_id,
            log_cb=lambda level, message: log(application_id, level, message, db),
            done_detector=_done_detected,
            followup_detector=_post_application_followup_detected,
        )
        if vision_result:
            return vision_result
    except Exception as exc:
        await log(
            application_id,
            "warn",
            f"Vertex vision loop failed, falling back to deterministic loop: {exc}",
            db,
        )

    stuck_counter = 0
    last_fingerprint = ""
    same_after_click = 0
    same_page_recovery_count = 0
    last_validation_signature = ""
    repeated_validation_errors = 0
    repeated_validation_text_counts: dict[str, int] = {}
    previous_button_clicked = False
    last_clicked_button_text = ""
    avoided_buttons_by_fingerprint: dict[str, set[str]] = {}
    uploaded_resume_fingerprints: set[str] = set()
    failed_resume_fingerprints: set[str] = set()
    upload_attempts_by_fingerprint: dict[str, int] = {}
    clicked_detail_add_buttons: set[str] = set()
    submitted_application = False
    failed_required_selects_by_fingerprint: dict[str, set[str]] = {}
    attempted_required_fields_by_fingerprint: dict[str, set[str]] = {}
    attempted_embedded_urls: set[str] = set()
    no_submit_stuck_by_fingerprint: dict[str, int] = {}
    conditional_radio_reinspected_fingerprints: set[str] = set()
    previous_worker_block_counts: dict[str, int] = {}
    # Labels the user has already answered via tkinter this run. We never
    # re-prompt these. If they're still showing as unanswered after the user
    # answered, the engine tries one more fill cycle; if still stuck → needs_manual.
    tkinter_answered_labels: set[str] = set()
    last_platform_handler = ""
    last_platform_page_state = ""
    # Cap how many times the bot will click the same button on the same page fingerprint.
    # When a button click does not change anything (e.g. AppOne pages with a non-functional
    # Apply, or sites where Apply opens an unhandled popup), we'd otherwise burn every step
    # re-clicking it. After this many attempts, escalate to needs_manual instead.
    button_click_attempts: dict[tuple[str, str], int] = {}
    MAX_SAME_BUTTON_CLICKS = 3

    # CHECKPOINT RESUME: load any persisted progress so subsequent iterations
    # can fast-forward through Workday step containers that were already
    # completed (e.g. contactInformationPage, myExperiencePage). On a fresh
    # run this is just None and the loop behaves identically.
    resume_checkpoint = load_checkpoint(candidate.id, application_id)
    resume_step_key: str | None = None
    resume_step_index: int = -1
    fast_forwarded_step_keys: set[str] = set()
    if resume_checkpoint:
        resume_step_key, _ckpt_idx = resume_checkpoint
        resume_step_index = step_index_for_key(resume_step_key)
        await log(
            application_id,
            "info",
            f"Resuming with checkpoint: last completed step = {resume_step_key}",
            db,
        )

    target_hostname = ""
    try:
        from urllib.parse import urlparse as _urlparse_local
        target_hostname = (_urlparse_local(target_job_url).hostname or "").lower()
    except Exception:
        target_hostname = ""

    def _is_workday_or_target(url_str: str) -> bool:
        try:
            from urllib.parse import urlparse as _up
            host = (_up(url_str).hostname or "").lower()
            if not host:
                return True  # about:blank / data: — don't block engine startup
            if target_hostname and (host == target_hostname or host.endswith("." + target_hostname.split(".", 1)[-1])):
                return True
            return "myworkdayjobs.com" in host or "workdayjobs.com" in host or "workday.com" in host
        except Exception:
            return True

    for step in range(1, MAX_STEPS + 1):
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)

        current_page_url = str(getattr(page, "url", "") or "")
        # Domain guard: only active when we have a valid target URL to return to.
        # Skipping when target_job_url is empty prevents navigating to about:blank.
        if current_page_url and target_job_url and target_job_url.startswith(("http://", "https://")) and not _is_workday_or_target(current_page_url):
            await log(
                application_id,
                "warn",
                (
                    f"Step {step}: domain guard fired — "
                    f"current={current_page_url!r} target_hostname={target_hostname!r} "
                    f"target_url={target_job_url!r}; navigating back to job URL"
                ),
                db,
            )
            try:
                await page.goto(target_job_url)
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as nav_exc:
                await log(application_id, "warn", f"Step {step}: domain-guard recovery navigation failed: {nav_exc}", db)
                async with SessionLocal() as _s:
                    _app = await _s.get(Application, application_id)
                    if _app is not None:
                        _app.status = "needs_manual"
                        _app.last_error = f"Navigated off Workday to {current_page_url}; could not recover"
                        await _s.commit()
                return "needs_manual"
            await asyncio.sleep(2)

        page_data = await _inspect_page_with_retry(page, application_id, db, step, "step start")
        await _screenshot(page, application_id, step, db)
        html_path, json_path = await _save_step_context(page, application_id, step, page_data)
        if html_path or json_path:
            context_parts = [part for part in (html_path, json_path) if part]
            await log(application_id, "info", f"Step {step}: saved page context [{' | '.join(context_parts)}]", db)
        platform_handler = handler_name_for_page(page_data)
        platform_page_state = page_state_for_page(page_data)
        if platform_handler != last_platform_handler or platform_page_state != last_platform_page_state:
            await log(
                application_id,
                "info",
                f"Step {step}: using {platform_handler} platform handler; page_state={platform_page_state}",
                db,
            )
            last_platform_handler = platform_handler
            last_platform_page_state = platform_page_state

        if platform_page_state == "dead_listing":
            async with SessionLocal() as _s:
                _app = await _s.get(Application, application_id)
                if _app is not None:
                    _app.last_error = "Job posting is no longer available"
                    await _s.commit()
            await log(application_id, "warn", f"Step {step}: dead listing — job no longer available; marking failed", db)
            return "failed"

        # CHECKPOINT FAST-FORWARD: if we have a saved checkpoint and the current
        # Workday page-step container is strictly EARLIER than the one we last
        # completed, we've landed on a page we already filled — click Next/
        # Save-and-Continue without re-filling. The next iteration re-inspects
        # the page and either fast-forwards another step or resumes normal
        # processing once we reach the post-checkpoint page.
        if resume_step_index >= 0:
            current_step_key = detect_step_key(page_data)
            if current_step_key and current_step_key != resume_step_key:
                current_idx = step_index_for_key(current_step_key)
                if 0 <= current_idx < resume_step_index and current_step_key not in fast_forwarded_step_keys:
                    fast_forwarded_step_keys.add(current_step_key)
                    await log(
                        application_id,
                        "info",
                        (
                            f"Step {step}: checkpoint fast-forward — landed on "
                            f"{current_step_key} which is before checkpoint "
                            f"{resume_step_key}; clicking Next without re-filling"
                        ),
                        db,
                    )
                    # Find the Save-and-Continue / Next button and click it
                    # WITHOUT executing any field actions. The next iteration
                    # re-inspects the page after navigation.
                    try:
                        next_button = None
                        for button in page_data.get("buttons", []):
                            if not isinstance(button, dict):
                                continue
                            text = str(button.get("text") or "").strip().lower()
                            aid = str(button.get("automationId") or "").strip().lower()
                            if (
                                "save and continue" in text
                                or "save & continue" in text
                                or text == "next"
                                or text == "continue"
                                or aid == "bottom-navigation-next-button"
                            ):
                                next_button = button
                                break
                        if next_button:
                            await _click_button_action(
                                page,
                                {
                                    "type": "click_button",
                                    "selector": next_button.get("selector"),
                                    "text": next_button.get("text"),
                                    "automationId": next_button.get("automationId"),
                                    "href": next_button.get("href"),
                                },
                                application_id,
                                db,
                                step,
                            )
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                            continue
                    except Exception as ff_exc:
                        logger.debug("checkpoint fast-forward click failed: %s", ff_exc)

        # Recovery: if a stale resume URL drops us on a userHome/job_search/unknown
        # page that doesn't contain a link to the target job, navigate directly to
        # the job URL. Otherwise the engine spins clicking [next] on a candidate
        # home page forever.
        if target_job_url and platform_page_state in {"job_search", "unknown"}:
            current_url = str(getattr(page, "url", "") or "")
            normalized_current = _canonical_action_url(current_url)
            normalized_target = _canonical_action_url(target_job_url)
            if normalized_current and normalized_target and normalized_current != normalized_target:
                # Does the page expose a link to the target job? If yes,
                # _target_job_action handles it on the listing surface path.
                has_target_link = False
                if is_listing_surface(page_data):
                    has_target_link = _target_job_action(
                        page_data, target_job_title, target_job_url, set()
                    ) is not None
                if not has_target_link:
                    await log(
                        application_id,
                        "info",
                        f"Step {step}: on {platform_page_state} page without target job link — navigating directly to {target_job_url}",
                        db,
                    )
                    try:
                        await page.goto(target_job_url, wait_until="domcontentloaded", timeout=30000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                        continue
                    except Exception as nav_err:
                        await log(
                            application_id,
                            "warn",
                            f"Step {step}: direct navigation to target job failed: {nav_err}",
                            db,
                        )

        if await _done_detected(page):
            await log(application_id, "info", f"Step {step}: APPLICATION DONE", db)
            return "completed"
        if submitted_application and await _post_application_followup_detected(page):
            await log(application_id, "info", f"Step {step}: APPLICATION DONE - post-submit follow-up page detected", db)
            return "completed"
        platform_blocker = page_blocker(page_data)
        if platform_blocker and platform_blocker.kind in {"captcha", "otp", "auth", "dead_listing", "video_interview"}:
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = platform_blocker.message
                await db.commit()
            await log(
                application_id,
                "warn",
                f"Step {step}: {platform_blocker.message}; stopping before unsafe navigation/fill",
                db,
            )
            return "needs_manual"
        if platform_blocker and platform_blocker.kind == "email_verification" and platform_handler != "workday":
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = platform_blocker.message
                await db.commit()
            await log(application_id, "warn", f"Step {step}: {platform_blocker.message}", db)
            return "needs_manual"
        domain = domain_from_url(str(page_data.get("url") or getattr(page, "url", "")))
        workday_auth_page = "myworkdayjobs.com" in domain and _workday_auth_page_detected(page_data)
        workday_auth_has_fields = bool(page_data.get("fields"))
        if workday_auth_page:
            auth_blocker = await _workday_auth_blocker_message(page)
            email_verification_blocker = "email" in auth_blocker.lower() or "account verification" in auth_blocker.lower()
            if auth_blocker != "Workday login/signup did not complete; manual sign-in is required" and not email_verification_blocker:
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = auth_blocker
                    await db.commit()
                await log(application_id, "warn", f"Step {step}: {auth_blocker}", db)
                return "needs_manual"
            if email_verification_blocker:
                await log(application_id, "info", f"Step {step}: {auth_blocker}; routing through auth/email handler", db)
        if workday_auth_page:
            await log(application_id, "info", f"Step {step}: Workday auth page detected; using controlled auth handler", db)
            try:
                page_kind = await handle_login(
                    page,
                    candidate,
                    db,
                    log_cb=lambda level, message: log(application_id, level if level != "warning" else "warn", message, db),
                )
                await log(application_id, "info", f"Step {step}: Workday auth handler returned {page_kind}", db)
                if page_kind == "login":
                    message = await _workday_auth_blocker_message(page)
                    application = await db.get(Application, application_id)
                    if application is not None:
                        application.last_error = message
                        await db.commit()
                    manual_result = await _wait_for_manual_auth_completion(
                        page,
                        application_id,
                        db,
                        step,
                        message,
                    )
                    if manual_result == "completed":
                        with suppress(Exception):
                            await anti_ban.save_storage_state(page.context, candidate.id, domain)
                        return "completed"
                    if manual_result == "continue":
                        with suppress(Exception):
                            await anti_ban.save_storage_state(page.context, candidate.id, domain)
                        await _wait_for_page_stability(page)
                        await asyncio.sleep(2)
                        continue
                    return "needs_manual"
                await _wait_for_page_stability(page)
                await asyncio.sleep(2)
                continue
            except ManualLoginRequired as exc:
                await log(application_id, "warn", f"Step {step}: Workday manual login required: {exc}", db)
                return "needs_manual"

        fingerprint = _page_fingerprint(page_data)
        if previous_button_clicked and fingerprint == last_fingerprint:
            same_after_click += 1
            await log(application_id, "warn", f"Step {step}: same page fingerprint after click ({same_after_click})", db)
        else:
            if fingerprint != last_fingerprint:
                same_page_recovery_count = 0
            same_after_click = 0
        last_fingerprint = fingerprint
        previous_button_clicked = False

        await log(
            application_id,
            "info",
            (
                f"Step {step}: found {len(page_data['fields'])} fields, "
                f"{len(page_data['buttons'])} buttons, {len(page_data['modals'])} modals"
            ),
            db,
        )
        # Fire-and-forget 2-second step summary popup (auto-closes; does not block automation).
        asyncio.ensure_future(
            asyncio.get_event_loop().run_in_executor(
                None,
                _show_step_summary_sync,
                step,
                getattr(page, "url", ""),
                len(page_data["fields"]),
                len(page_data["buttons"]),
            )
        )

        if page_data["modals"]:
            await log(application_id, "info", f"Step {step}: modal detected - handling modal first", db)
            # Only fall back to URL-string apply-link promotion when there is
            # NOTHING actionable in the modal (no fields AND no buttons).
            # Otherwise the engine should process the modal's buttons through
            # the normal action loop — "Apply Manually" etc. are real buttons
            # that route correctly via React; string-constructed /apply URLs
            # on newer Workday URL shapes (e.g. /jobs/jobs/details/<slug>)
            # redirect back to job_search and trap the engine in a loop.
            if not page_data["fields"] and not page_data["buttons"]:
                if await _promote_dom_apply_link(page, application_id, db, step):
                    continue
                if await _dismiss_blocking_modal(page, application_id, db, step):
                    continue

        if _security_challenge_surface(page_data):
            challenge_actions = [
                _score_button(page_data, button)
                for button in page_data.get("buttons", [])
                if isinstance(button, dict)
            ]
            if not any(score > 0 for score in challenge_actions):
                await log(application_id, "warn", f"Step {step}: security challenge requires manual completion", db)
                return "needs_manual"

        embedded_surface = await _embedded_frame_surface(page, page_data)
        embedded_url = embedded_application_url(page_data)
        if embedded_surface is not None:
            _frame, frame_data, frame_url = embedded_surface
            if frame_data.get("fields") and page.url != frame_url:
                await log(application_id, "info", f"Step {step}: navigating into embedded application frame [{frame_url}]", db)
                try:
                    await page.goto(frame_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    continue
                except Exception:
                    await log(application_id, "warn", f"Step {step}: could not promote embedded frame to top page [{frame_url}]", db)
        if (
            embedded_url
            and not embedded_surface
            and not page_data["fields"]
            and page.url != embedded_url
            and embedded_url not in attempted_embedded_urls
        ):
            attempted_embedded_urls.add(embedded_url)
            await log(application_id, "info", f"Step {step}: promoting embedded application url [{embedded_url}]", db)
            try:
                await page.goto(embedded_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                continue
            except Exception:
                await log(application_id, "warn", f"Step {step}: embedded application url promotion failed [{embedded_url}]", db)
        if embedded_url and not page_data["fields"]:
            frame_button_action = await _embedded_frame_button_action(page, page_data)
            if frame_button_action:
                await log(application_id, "info", f"Step {step}: acting inside embedded application frame [{frame_button_action.get('frame_url') or embedded_url}]", db)
                before_url = page.url
                clicked = await _click_button_action(page, frame_button_action, application_id, db, step)
                previous_button_clicked = clicked
                last_clicked_button_text = str(frame_button_action.get("text") or "")
                if clicked:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    if page.url != before_url:
                        await log(application_id, "info", f"Step {step}: embedded frame action navigated to new page", db)
                    continue

        blocker_reason = page_blocker_reason(page_data)
        if blocker_reason:
            await log(application_id, "warn", f"Step {step}: {blocker_reason}", db)
            return "needs_manual"

        learned_answers = await _saved_answers(db, candidate, getattr(page, "url", ""))
        learned_answers.update(await _workday_generic_auth_answers(db, candidate, page_data))
        learned_answers.update(await _applicantstack_signup_answers(db, candidate, page_data))
        previous_worker_recovery_page = _has_previous_worker_detail_fields(page_data)
        actions = await decide_action(page_data, _candidate_dict(candidate, learned_answers))
        field_action_types = {"clear", "fill", "select", "radio", "check", "upload"}
        field_actions = [action for action in actions if action.get("type") in field_action_types]
        unanswered_actions = [action for action in actions if action.get("type") == "unanswered"]
        target_listing_action = (
            _target_job_action(page_data, target_job_title, target_job_url, clicked_detail_add_buttons)
            if is_listing_surface(page_data)
            else None
        )
        if target_listing_action and is_listing_surface(page_data):
            await log(
                application_id,
                "info",
                f"Step {step}: opening matching job listing before search/filter fields [{target_listing_action.get('text')}]",
                db,
            )
            clicked = await _click_button_action(page, target_listing_action, application_id, db, step)
            previous_button_clicked = clicked
            last_clicked_button_text = str(target_listing_action.get("text") or "")
            if clicked:
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
        _required_unanswered_phase1 = [a for a in unanswered_actions if a.get("required", True)]
        for action in _required_unanswered_phase1:
            await _save_unanswered(db, page, candidate, application_id, action)
        if _required_unanswered_phase1:
            _unanswered_labels = [str(a.get("label") or "") for a in _required_unanswered_phase1]
            # Split into new (needs popup) vs already answered this run (FormAnswer saved, engine will fill).
            _to_prompt = [a for a in _required_unanswered_phase1
                          if str(a.get("label") or "").strip() not in tkinter_answered_labels]
            _already_tkinter = [a for a in _required_unanswered_phase1
                                 if str(a.get("label") or "").strip() in tkinter_answered_labels]
            if _to_prompt:
                await log(application_id, "warn",
                          f"Step {step}: prompting user via tkinter for: {[str(a.get('label') or '') for a in _to_prompt]}", db)
                _any_answered, _still_unanswered = await _prompt_and_execute_unanswered(
                    _to_prompt, page, candidate, db, application_id, step, domain,
                )
                for _a in _to_prompt:
                    _lbl = str(_a.get("label") or "").strip()
                    if not any(str(_s.get("label") or "").strip() == _lbl for _s in _still_unanswered):
                        tkinter_answered_labels.add(_lbl)
                if _any_answered or _already_tkinter:
                    await asyncio.sleep(0.5)
                    continue
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = f"Required questions need manual answer: {[str(a.get('label') or '') for a in _still_unanswered]}"
                    await db.commit()
                return "needs_manual"
            else:
                # All fields already answered this run but still unresolved — engine
                # will try to fill via saved FormAnswers; if it fails again, give up.
                _stuck_labels = [str(a.get("label") or "") for a in _already_tkinter]
                await log(application_id, "warn",
                          f"Step {step}: fields still unresolved after tkinter answers, giving up: {_stuck_labels}", db)
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = f"Fields unresolvable despite user input: {_stuck_labels}"
                    await db.commit()
                return "needs_manual"
        upload_actions = [action for action in field_actions if action.get("type") == "upload"]
        if upload_actions:
            non_upload_actions = [action for action in field_actions if action.get("type") != "upload"]
            if fingerprint in uploaded_resume_fingerprints:
                await log(application_id, "warn", f"Step {step}: resume upload already attempted on this page; skipping repeat upload controls", db)
                field_actions = non_upload_actions
            elif fingerprint in failed_resume_fingerprints:
                await log(application_id, "warn", f"Step {step}: resume upload already failed on this page; continuing without blocking submit", db)
                field_actions = non_upload_actions
            elif upload_attempts_by_fingerprint.get(fingerprint, 0) >= 2:
                await log(application_id, "warn", f"Step {step}: resume upload failed twice on this page; continuing without repeat upload", db)
                failed_resume_fingerprints.add(fingerprint)
                field_actions = non_upload_actions
            else:
                upload_attempts_by_fingerprint[fingerprint] = upload_attempts_by_fingerprint.get(fingerprint, 0) + 1
                field_actions = upload_actions[:1] + non_upload_actions

        avoid_texts = avoided_buttons_by_fingerprint.setdefault(fingerprint, set())
        if same_after_click >= 2 and last_clicked_button_text:
            avoid_texts.add(last_clicked_button_text.lower())
            await log(
                application_id,
                "warn",
                f"Step {step}: avoiding repeated button [{last_clicked_button_text}] for this page",
                db,
            )

        effective_avoid_texts = avoid_texts | clicked_detail_add_buttons
        fallback_button_action = (
            (
                _target_job_action(page_data, target_job_title, target_job_url, clicked_detail_add_buttons)
                if is_listing_surface(page_data)
                else None
            )
            or (_best_button_action(page_data, clicked_detail_add_buttons) if avoid_texts else None)
        )
        if not field_actions and not _best_button_action(page_data, effective_avoid_texts) and not fallback_button_action:
            stuck_counter += 1
            await log(application_id, "warn", f"Step {step}: no executable actions found, stuck={stuck_counter}", db)
            for action in unanswered_actions:
                await _execute_field_action(page, action, candidate, application_id, db, step)
            if stuck_counter >= 3:
                return "needs_manual"
            await _safe_scroll(page, 500)
            await asyncio.sleep(2)
            continue

        stuck_counter = 0
        failed_required_selects = failed_required_selects_by_fingerprint.setdefault(fingerprint, set())
        attempted_required_fields = attempted_required_fields_by_fingerprint.setdefault(fingerprint, set())

        # PHASE 1 - fill all fields first. Do not click submit/navigation buttons here.
        reinspect_after_conditional_radio = False
        for action in field_actions + unanswered_actions:
            action_type = str(action.get("type") or "")
            selector = str(action.get("selector") or "")
            if action.get("required", True) and selector and action_type in {"fill", "select", "radio", "check", "upload"}:
                attempted_required_fields.add(selector)
            try:
                action_result = await _execute_field_action(page, action, candidate, application_id, db, step)
                if action.get("type") == "upload" and action_result:
                    uploaded_resume_fingerprints.add(fingerprint)
                if action.get("type") == "upload" and not action_result and upload_attempts_by_fingerprint.get(fingerprint, 0) >= 2:
                    failed_resume_fingerprints.add(fingerprint)
                await _save_learned_rule(
                    page,
                    page_data,
                    action,
                    application_id,
                    db,
                    step,
                    "field action executed",
                )
                await asyncio.sleep(0.5)
                if action_type == "radio":
                    radio_label = str(action.get("label") or "").lower()
                    is_previous_worker_radio = (
                        "candidateispreviousworker" in selector.lower()
                        or ("worked" in radio_label and any(token in radio_label for token in ("previous", "previously", "before")))
                        or ("employed" in radio_label and any(token in radio_label for token in ("previous", "previously", "before")))
                    )
                    if previous_worker_recovery_page and _is_previous_worker_no_action(action):
                        if fingerprint not in conditional_radio_reinspected_fingerprints:
                            reinspect_after_conditional_radio = True
                            conditional_radio_reinspected_fingerprints.add(fingerprint)
                            await log(
                                application_id,
                                "info",
                                (
                                    f"Step {step}: Workday previous-worker draft was active; "
                                    "selected No and re-inspecting before Save and Continue"
                                ),
                                db,
                            )
                            break
                        application = await db.get(Application, application_id)
                        if application is not None:
                            application.last_error = (
                                "Workday restored previous-worker fields again after No; "
                                "stopped before Save and Continue"
                            )
                            await db.commit()
                        await log(
                            application_id,
                            "warn",
                            (
                                f"Step {step}: Workday restored previous-worker fields again; "
                                "selected No and stopped before Save and Continue"
                            ),
                            db,
                        )
                        return "needs_manual"
                    if (
                        fingerprint not in conditional_radio_reinspected_fingerprints
                        and is_previous_worker_radio
                    ):
                        reinspect_after_conditional_radio = True
                        conditional_radio_reinspected_fingerprints.add(fingerprint)
                        break
            except Exception as exc:
                await log(application_id, "error", f"Step {step}: action failed: {str(exc)}", db)
                if _is_previous_worker_no_action(action):
                    application = await db.get(Application, application_id)
                    if application is not None:
                        application.last_error = (
                            "Previous-worker No did not commit in Workday; stopped before Save and Continue"
                        )
                        await db.commit()
                    return "needs_manual"
                # Surface every failed field action (required or optional) to
                # the manual-review UI. Previously only selects were captured —
                # a failed fill/radio/check/upload would disappear silently and
                # the engine would loop on Save and Continue forever.
                failed_action_type = str(action.get("type") or "")
                if selector and failed_action_type in {"select", "fill", "radio", "check", "upload"}:
                    try:
                        await _save_unanswered(db, page, candidate, application_id, action)
                    except Exception as save_exc:
                        logger.warning(
                            "save_unanswered after %s failure failed: %s",
                            failed_action_type,
                            save_exc,
                        )
                    if failed_action_type == "select" and action.get("required", True):
                        failed_required_selects.add(selector)
                    if failed_action_type == "select":
                        # Close any open dropdown/overlay left by the failed select action.
                        try:
                            await page.keyboard.press("Escape")
                            await asyncio.sleep(0.3)
                        except Exception:
                            pass
                continue

        if reinspect_after_conditional_radio:
            await log(application_id, "info", f"Step {step}: conditional radio changed; re-inspecting before filling dependent fields", db)
            await asyncio.sleep(1.0)
            continue

        # PHASE 2 - verify required fields before submitting.
        if not any(action.get("type") == "check" for action in field_actions):
            await _click_consent_fallback(page, application_id, db, step)
        after_fill_data = await _inspect_page_with_retry(page, application_id, db, step, "post-fill verification")
        # Previous-worker server-draft restoration guard: after we have selected
        # No (or after a reinspect cycle following a previous-worker radio toggle),
        # if the controller is still on the page and No is NOT selected, Workday
        # restored the saved draft from the server. Clear storage and bail so the
        # next attempt starts from a clean slate.
        if _has_previous_worker_controller(after_fill_data) and not _previous_worker_no_selected(after_fill_data):
            # Workday restored Yes from its server draft. Try clicking No directly
            # up to 3 times before giving up.
            _pw_no_stuck = True
            for _pw_attempt in range(1, 4):
                # Find the No radio option from the live page data.
                _no_action: dict[str, object] | None = None
                for _rf in after_fill_data.get("fields", []):
                    if not isinstance(_rf, dict) or str(_rf.get("type") or "").lower() != "radio":
                        continue
                    _text = " ".join(str(_rf.get(k) or "") for k in ("label", "selector", "automationId")).lower()
                    if not _is_previous_worker_label_text(_text):
                        continue
                    for _opt in (_rf.get("radioOptions") or []):
                        if not isinstance(_opt, dict):
                            continue
                        _ol = " ".join(str(_opt.get(k) or "") for k in ("label", "value")).strip().lower()
                        if "no" in _ol or _ol == "false":
                            _no_action = {
                                "type": "radio",
                                "selector": _opt.get("selector"),
                                "answer": "No",
                                "label": _rf.get("label") or "",
                            }
                            break
                    if _no_action:
                        break
                if not _no_action or not _no_action.get("selector"):
                    await log(application_id, "warn",
                              f"Step {step}: previous-worker No radio selector not found (attempt {_pw_attempt})", db)
                    break
                await log(application_id, "warn",
                          f"Step {step}: Workday restored Yes from draft; clicking No (attempt {_pw_attempt}/3)", db)
                try:
                    await _execute_radio_action(page, _no_action, application_id, db, step)
                except Exception as _pw_exc:
                    await log(application_id, "warn",
                              f"Step {step}: No click failed on attempt {_pw_attempt}: {_pw_exc}", db)
                await asyncio.sleep(1.0)
                after_fill_data = await _inspect_page_with_retry(page, application_id, db, step,
                                                                  f"previous-worker verify attempt {_pw_attempt}")
                if _previous_worker_no_selected(after_fill_data):
                    await log(application_id, "info",
                              f"Step {step}: previous-worker No confirmed after attempt {_pw_attempt}", db)
                    _pw_no_stuck = False
                    break
            if _pw_no_stuck:
                anti_ban.clear_storage_state(candidate.id, domain)
                await log(application_id, "warn",
                          f"Step {step}: previous-worker radio reverts to Yes after 3 attempts; "
                          "cleared browser storage, please reapply", db)
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = (
                        "Previous worker radio reverts to Yes after Save and Continue"
                    )
                    await db.commit()
                return "needs_manual"
        if _verification_code_blocker_detected(after_fill_data):
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = "Verification or CAPTCHA challenge requires manual completion"
                await db.commit()
            await log(
                application_id,
                "warn",
                f"Step {step}: verification/security code appeared after filling; stopping before guessing",
                db,
            )
            return "needs_manual"
        empty_required = _empty_required_fields(after_fill_data)
        await log(
            application_id,
            "info",
            f"Step {step}: After filling: {len(empty_required)} fields still empty: {empty_required}",
            db,
        )
        previous_worker_detail_empty = _empty_previous_worker_detail_fields(after_fill_data)
        if previous_worker_detail_empty and _has_previous_worker_controller(after_fill_data):
            no_selected = _previous_worker_no_selected(after_fill_data)
            block_key = _page_fingerprint(after_fill_data)
            previous_worker_block_counts[block_key] = previous_worker_block_counts.get(block_key, 0) + 1
            await log(
                application_id,
                "warn",
                (
                    f"Step {step}: Workday previous-worker detail fields are still required "
                    f"after selecting {'No' if no_selected else 'a previous-worker radio value'}: "
                    f"{previous_worker_detail_empty}; not clicking Save and Continue"
                ),
                db,
            )
            if previous_worker_block_counts[block_key] >= 3:
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = (
                        "Previous-worker radio did not persist safely; stopped before "
                        "submitting required previous-employment fields"
                    )
                    await db.commit()
                return "needs_manual"
            await asyncio.sleep(2)
            continue
        if previous_worker_recovery_page and _has_previous_worker_detail_fields(after_fill_data):
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = (
                    "Workday restored a previous-worker draft after Save and Continue. "
                    "Stopped before submitting the application with prior-employment fields."
                )
                await db.commit()
            await log(
                application_id,
                "warn",
                (
                    f"Step {step}: Workday previous-worker draft was already active on this page; "
                    "not clicking Save and Continue after selecting No"
                ),
                db,
            )
            return "needs_manual"

        learned_answers = await _saved_answers(db, candidate, getattr(page, "url", ""))
        learned_answers.update(await _workday_generic_auth_answers(db, candidate, after_fill_data))
        learned_answers.update(await _applicantstack_signup_answers(db, candidate, after_fill_data))
        remaining_actions = await decide_action(after_fill_data, _candidate_dict(candidate, learned_answers))
        remaining_blocking_types = {"fill", "select", "radio", "check", "upload"}
        remaining_field_actions = [
            action
            for action in remaining_actions
            if action.get("type") in remaining_blocking_types and action.get("required", True)
        ]
        if fingerprint in uploaded_resume_fingerprints or fingerprint in failed_resume_fingerprints:
            remaining_field_actions = [
                action for action in remaining_field_actions if action.get("type") != "upload"
            ]
        remaining_required_unanswered = [
            action
            for action in remaining_actions
            if action.get("type") == "unanswered" and action.get("required", True)
        ]
        if remaining_required_unanswered:
            unanswered_labels = [str(action.get("label") or "") for action in remaining_required_unanswered]
            validation_unanswered = [
                action
                for action in remaining_required_unanswered
                if str(action.get("blocker_kind") or "").lower() == "validation_error"
            ]
            manual_unanswered = [
                action
                for action in remaining_required_unanswered
                if str(action.get("blocker_kind") or "").lower() != "validation_error"
            ]
            if validation_unanswered and not manual_unanswered:
                validation_labels = [str(action.get("label") or "") for action in validation_unanswered]
                await log(
                    application_id,
                    "warn",
                    f"Step {step}: field validation errors remain: {validation_labels}",
                    db,
                )
            elif manual_unanswered:
                await log(
                    application_id,
                    "warn",
                    f"Step {step}: required questions need manual answer: {unanswered_labels}",
                    db,
                )
            for action in remaining_required_unanswered:
                await _save_unanswered(db, page, candidate, application_id, action)
            if validation_unanswered and not manual_unanswered:
                primary_validation = validation_unanswered[0]
                validation_label = str(primary_validation.get("label") or "").strip()
                validation_options = [str(option or "").strip() for option in (primary_validation.get("options") or []) if str(option or "").strip()]
                validation_message = validation_options[0] if validation_options else "Field validation error"
                if validation_label:
                    await log(
                        application_id,
                        "error",
                        f"Step {step}: stopping on validation error [{validation_label}] {validation_message}",
                        db,
                    )
                return "validation_error"
            if manual_unanswered:
                _to_prompt2 = [a for a in manual_unanswered
                               if str(a.get("label") or "").strip() not in tkinter_answered_labels]
                _already_tkinter2 = [a for a in manual_unanswered
                                     if str(a.get("label") or "").strip() in tkinter_answered_labels]
                if _to_prompt2:
                    _any_answered2, _still2 = await _prompt_and_execute_unanswered(
                        _to_prompt2, page, candidate, db, application_id, step, domain,
                    )
                    for _a2 in _to_prompt2:
                        _lbl2 = str(_a2.get("label") or "").strip()
                        if not any(str(_s2.get("label") or "").strip() == _lbl2 for _s2 in _still2):
                            tkinter_answered_labels.add(_lbl2)
                    if _any_answered2 or _already_tkinter2:
                        await asyncio.sleep(0.5)
                        continue
                    unanswered_labels2 = [str(a.get("label") or "") for a in _still2]
                    application = await db.get(Application, application_id)
                    if application is not None:
                        application.last_error = f"Required questions need manual answer: {unanswered_labels2}"
                        await db.commit()
                    return "needs_manual"
                else:
                    unanswered_labels2 = [str(a.get("label") or "") for a in manual_unanswered]
                    application = await db.get(Application, application_id)
                    if application is not None:
                        application.last_error = f"Required questions need manual answer: {unanswered_labels2}"
                        await db.commit()
                    return "needs_manual"
        failed_blocking_selects = [
            action
            for action in remaining_field_actions
            if action.get("type") == "select" and str(action.get("selector") or "") in failed_required_selects
        ]
        if failed_blocking_selects:
            labels = [str(action.get("label") or action.get("selector") or "") for action in failed_blocking_selects]
            message = f"Required select actions still unresolved after execution: {labels}"
            await log(
                application_id,
                "warn",
                f"Step {step}: {message}",
                db,
            )
            for action in failed_blocking_selects:
                await _save_unanswered(db, page, candidate, application_id, action)
            _to_prompt3 = [a for a in failed_blocking_selects
                           if str(a.get("label") or "").strip() not in tkinter_answered_labels]
            _already_tkinter3 = [a for a in failed_blocking_selects
                                  if str(a.get("label") or "").strip() in tkinter_answered_labels]
            if _to_prompt3:
                _any_answered3, _still3 = await _prompt_and_execute_unanswered(
                    _to_prompt3, page, candidate, db, application_id, step, domain,
                )
                for _a3 in _to_prompt3:
                    _lbl3 = str(_a3.get("label") or "").strip()
                    if not any(str(_s3.get("label") or "").strip() == _lbl3 for _s3 in _still3):
                        tkinter_answered_labels.add(_lbl3)
                if _any_answered3 or _already_tkinter3:
                    await asyncio.sleep(0.5)
                    continue
            elif _already_tkinter3:
                await asyncio.sleep(0.5)
                continue
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = message
                await db.commit()
            return "needs_manual"
        unresolved_attempted_required_fields = [
            action
            for action in remaining_field_actions
            if str(action.get("selector") or "") in attempted_required_fields
        ]
        if unresolved_attempted_required_fields:
            labels = [str(action.get("label") or action.get("selector") or "") for action in unresolved_attempted_required_fields]
            message = f"Required fields were attempted but still unresolved: {labels}"
            await log(
                application_id,
                "warn",
                f"Step {step}: {message}",
                db,
            )
            for action in unresolved_attempted_required_fields:
                await _save_unanswered(db, page, candidate, application_id, action)
            _to_prompt4 = [a for a in unresolved_attempted_required_fields
                           if str(a.get("label") or "").strip() not in tkinter_answered_labels]
            _already_tkinter4 = [a for a in unresolved_attempted_required_fields
                                  if str(a.get("label") or "").strip() in tkinter_answered_labels]
            if _to_prompt4:
                _any_answered4, _still4 = await _prompt_and_execute_unanswered(
                    _to_prompt4, page, candidate, db, application_id, step, domain,
                )
                for _a4 in _to_prompt4:
                    _lbl4 = str(_a4.get("label") or "").strip()
                    if not any(str(_s4.get("label") or "").strip() == _lbl4 for _s4 in _still4):
                        tkinter_answered_labels.add(_lbl4)
                if _any_answered4 or _already_tkinter4:
                    await asyncio.sleep(0.5)
                    continue
            elif _already_tkinter4:
                await asyncio.sleep(0.5)
                continue
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = message
                await db.commit()
            return "needs_manual"
        if remaining_field_actions:
            await log(
                application_id,
                "info",
                f"Step {step}: remaining field actions found, delaying submit: {[a.get('type') for a in remaining_field_actions]}",
                db,
            )
            continue
        if remaining_required_unanswered:
            unanswered_labels = [str(action.get("label") or "") for action in remaining_required_unanswered]
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = f"Required questions need manual answer: {unanswered_labels}"
                await db.commit()
            return "needs_manual"

        if same_after_click >= 2 and empty_required:
            await log(
                application_id,
                "warn",
                f"Step {step}: same page after click but required fields remain, retrying learned recovery",
                db,
            )
            same_after_click = 0
            continue
        if same_after_click >= 3:
            same_page_recovery_count += 1
            await log(
                application_id,
                "warn",
                f"Step {step}: same page repeated after click; recovery={same_page_recovery_count}",
                db,
            )
            if same_page_recovery_count >= 2:
                await log(application_id, "warn", f"Step {step}: same page did not progress after repeated recovery", db)
                return "needs_manual"
            same_after_click = 0
            await _safe_scroll(page, 700)
            await asyncio.sleep(2)
            continue
        # PHASE 3 - submit only after field work is exhausted.
        if not empty_required and last_clicked_button_text:
            avoid_texts.discard(last_clicked_button_text.lower())
        effective_avoid_texts = avoid_texts | clicked_detail_add_buttons
        button_action = (
            _target_job_action(after_fill_data, target_job_title, target_job_url, effective_avoid_texts)
            if is_listing_surface(after_fill_data)
            else None
        )
        if button_action:
            await log(
                application_id,
                "info",
                f"Step {step}: opening matching job listing [{button_action.get('text')}]",
                db,
            )
        else:
            button_action = _best_button_action(after_fill_data, effective_avoid_texts)
            # If avoid_texts is blocking all buttons but the form looks filled, relax
            # the avoid set (keep only add-detail buttons excluded) and retry once.
            if not button_action and avoid_texts:
                button_action = _best_button_action(after_fill_data, clicked_detail_add_buttons)
                if button_action:
                    await log(
                        application_id,
                        "info",
                        f"Step {step}: relaxed avoid-set to find button [{button_action.get('text')}]",
                        db,
                    )
        if not button_action:
            stuck_counter += 1
            no_submit_stuck_by_fingerprint[fingerprint] = no_submit_stuck_by_fingerprint.get(fingerprint, 0) + 1
            fingerprint_stuck = no_submit_stuck_by_fingerprint[fingerprint]
            await log(
                application_id,
                "warn",
                f"Step {step}: no submit/next/apply button found, stuck={stuck_counter}, page_stuck={fingerprint_stuck}",
                db,
            )
            if stuck_counter >= 3 or fingerprint_stuck >= 3:
                return "needs_manual"
            await _safe_scroll(page, 500)
            await asyncio.sleep(2)
            continue

        before_url = page.url
        last_clicked_button_text = str(button_action.get("text") or "")
        if _safe_live_test_stop_before_submit() and _is_final_submit_button_text(last_clicked_button_text):
            message = f"Safe live test stopped before final submit button [{last_clicked_button_text}]"
            await log(application_id, "warn", f"Step {step}: {message}", db)
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = message
                await db.commit()
            return "needs_manual"
        click_key = (fingerprint, last_clicked_button_text.strip().lower())
        if click_key[1]:
            attempts_for_button = button_click_attempts.get(click_key, 0)
            if attempts_for_button >= MAX_SAME_BUTTON_CLICKS:
                await log(
                    application_id,
                    "warn",
                    (
                        f"Step {step}: button [{last_clicked_button_text}] already clicked "
                        f"{attempts_for_button} times on this page without progress; stopping"
                    ),
                    db,
                )
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = (
                        f"Stuck on button [{last_clicked_button_text}]: clicked "
                        f"{attempts_for_button} times without page progress"
                    )
                    await db.commit()
                return "needs_manual"
            button_click_attempts[click_key] = attempts_for_button + 1
        clicked = await _click_button_action(page, button_action, application_id, db, step)
        if clicked and _is_final_submit_button_text(last_clicked_button_text):
            submitted_application = True
        if _is_add_detail_button_text(last_clicked_button_text):
            clicked_detail_add_buttons.add(last_clicked_button_text.lower())
        previous_button_clicked = clicked
        if clicked:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            # On Workday tenants, networkidle fires before the React content
            # mounts (auth form / apply-choice picker / next form step). Without
            # this wait the next inspect_page sees only header chrome and the
            # engine concludes 'unknown' → bounces back → loops.
            page_url = str(getattr(page, "url", "") or "").lower()
            if "myworkdayjobs.com" in page_url or "workdayjobs.com" in page_url:
                await _wait_for_workday_content_ready(page, timeout_ms=15000)
            await asyncio.sleep(2)
            if submitted_application and await _post_application_followup_detected(page):
                await log(application_id, "info", f"Step {step}: APPLICATION DONE - submit led to confirmation/follow-up page", db)
                return "completed"

        # PHASE 4 - detect validation errors or progress.
        error_text = await _visible_error_text(page)
        if error_text:
            await log(application_id, "error", f"Step {step}: page validation error: {error_text}", db)
            application = await db.get(Application, application_id)
            if application is not None:
                application.last_error = error_text[:1000]
                await db.commit()
            latest_data = await _inspect_page_with_retry(page, application_id, db, step, "validation recovery")
            validation_signature = f"{_page_fingerprint(latest_data)}|{error_text[:500]}"
            if validation_signature == last_validation_signature:
                repeated_validation_errors += 1
            else:
                last_validation_signature = validation_signature
                repeated_validation_errors = 1
            normalized_error_text = " ".join(error_text.lower().split())[:500]
            # Strip Workday error-code GUIDs ("Error Code: I|abc-123-...") so each
            # retry's unique correlation ID doesn't make this look like a new error.
            normalized_error_text = re.sub(
                r"error\s+code:?\s*[a-z0-9|_\-]+",
                "error code: <id>",
                normalized_error_text,
                flags=re.I,
            )
            repeated_validation_text_counts[normalized_error_text] = repeated_validation_text_counts.get(normalized_error_text, 0) + 1
            repeated_error_text_count = repeated_validation_text_counts[normalized_error_text]
            # Workday "Page Error / Error Code: ..." is a generic system error that
            # won't resolve by retrying with the same data. Fail fast on first hit
            # instead of waiting for the same-button-3-times counter.
            if "page error" in normalized_error_text or "error code: <id>" in normalized_error_text:
                await log(
                    application_id,
                    "warn",
                    f"Step {step}: Workday page error detected — stopping for manual review",
                    db,
                )
                return "validation_error"
            if repeated_validation_errors >= MAX_REPEATED_VALIDATION_ERRORS or repeated_error_text_count >= MAX_REPEATED_VALIDATION_ERRORS:
                await log(
                    application_id,
                    "warn",
                    (
                        f"Step {step}: same validation error repeated "
                        f"{max(repeated_validation_errors, repeated_error_text_count)} times; stopping for manual review"
                    ),
                    db,
                )
                return "validation_error"
            if _verification_code_blocker_detected(latest_data):
                application = await db.get(Application, application_id)
                if application is not None:
                    application.last_error = "Verification or CAPTCHA challenge requires manual completion"
                    await db.commit()
                await log(
                    application_id,
                    "warn",
                    f"Step {step}: verification/security code found during validation recovery; stopping before guessing",
                    db,
                )
                return "needs_manual"
            learned_answers = await _saved_answers(db, candidate, getattr(page, "url", ""))
            recovery_actions = await decide_action(latest_data, _candidate_dict(candidate, learned_answers))
            # Surface banner-linked fields as unanswered IMMEDIATELY on the first
            # failure — don't wait for MAX_REPEATED_VALIDATION_ERRORS retries.
            # Workday often shows a global "Errors Found" banner whose only useful
            # information is which field(s) it points to via aria-invalid /
            # aria-describedby. Marking those fields as unanswered now lets the
            # frontend prompt the user instead of silently re-clicking Save.
            try:
                error_field_labels = await _visible_error_field_labels(page)
            except Exception:
                error_field_labels = []
            if error_field_labels:
                existing_unanswered = {
                    " ".join(str(action.get("label") or "").lower().split())
                    for action in recovery_actions
                    if action.get("type") == "unanswered"
                }
                for entry in error_field_labels:
                    label = entry.get("label") or ""
                    normalized = " ".join(label.lower().split())
                    if not normalized or normalized in existing_unanswered:
                        continue
                    # Negative-feedback loop: penalise / delete the rule that
                    # produced the bad answer for this field so we don't pick
                    # the same broken value on the next run. Manual rules are
                    # never deleted here — they get needs_user_reconfirm=True
                    # so the UI can prompt the user to re-confirm.
                    try:
                        feedback_result = record_rule_failure(
                            label,
                            entry.get("automation_id"),
                            entry.get("message") or "",
                        )
                        if feedback_result.get("deleted"):
                            await log(
                                application_id,
                                "info",
                                f"Step {step}: deleted broken rule for field [{label}] after validation failure",
                                db,
                            )
                        elif feedback_result.get("flagged_manual"):
                            await log(
                                application_id,
                                "info",
                                f"Step {step}: manual rule for field [{label}] flagged for user reconfirm after validation failure",
                                db,
                            )
                    except Exception as feedback_exc:
                        logger.debug("record_rule_failure failed: %s", feedback_exc)
                    await log(
                        application_id,
                        "warn",
                        f"Step {step}: validation error linked to field [{label}]: {entry.get('message') or ''}",
                        db,
                    )
                    # Try to enrich the unanswered row with the real field's
                    # selector + type + controlKind by matching the validation
                    # error's label against the inspector's captured fields.
                    # Workday's error labels often have suffixes like
                    # "State Select One Required" while the inspector captured
                    # "State"; match by substring/prefix so we can hand the
                    # real selector to _save_unanswered, enabling it to
                    # recapture options via _capture_select_options instead of
                    # writing a row with no options for the manual reviewer.
                    matched_field: dict[str, Any] | None = None
                    try:
                        normalized_err = " ".join(label.lower().split())
                        for field in (latest_data.get("fields") or []):
                            if not isinstance(field, dict):
                                continue
                            field_label_str = " ".join(str(field.get("label") or "").lower().split())
                            if not field_label_str:
                                continue
                            if (
                                field_label_str == normalized_err
                                or normalized_err.startswith(field_label_str + " ")
                                or normalized_err.startswith(field_label_str)
                                or field_label_str in normalized_err
                            ):
                                matched_field = field
                                break
                    except Exception:
                        matched_field = None
                    enriched_action: dict[str, Any] = {
                        "type": "unanswered",
                        "label": label,
                        "field_type": "unknown",
                        "options": [],
                        "required": True,
                        "blocker_kind": "validation_error",
                        "validation_message": entry.get("message") or "",
                    }
                    if matched_field is not None:
                        # Carry through every signal _capture_select_options /
                        # _save_unanswered need; never widen field_type to
                        # 'unknown' if we have a better one from the inspector.
                        field_type_real = str(matched_field.get("type") or "").strip()
                        if field_type_real:
                            enriched_action["field_type"] = field_type_real
                        ck = matched_field.get("controlKind") or matched_field.get("control_kind")
                        if ck:
                            enriched_action["control_kind"] = ck
                        sel = matched_field.get("selector")
                        if sel:
                            enriched_action["selector"] = sel
                    await _save_unanswered(
                        db,
                        page,
                        candidate,
                        application_id,
                        enriched_action,
                    )
                    existing_unanswered.add(normalized)
            for action in recovery_actions:
                if action.get("type") == "unanswered" and action.get("required", True):
                    await _save_unanswered(db, page, candidate, application_id, action)
                if action.get("type") in field_action_types:
                    await _save_learned_rule(
                        page,
                        latest_data,
                        action,
                        application_id,
                        db,
                        step,
                        f"validation error: {error_text[:120]}",
                    )
                    # Execute fill/select recovery immediately so the corrected value
                    # is in place before the next button click rather than waiting
                    # an extra round-trip through the loop.
                    try:
                        await _execute_field_action(page, action, candidate, application_id, db, step)
                    except Exception:
                        pass
            continue

        current_url = page.url
        if current_url != before_url:
            await log(application_id, "info", f"Step {step}: navigated to new page", db)
        elif clicked:
            await log(application_id, "info", f"Step {step}: button clicked; no validation errors detected", db)

        # CHECKPOINT: after PHASE 4 confirms no validation errors AND a button
        # was clicked (i.e. a real Save-and-Continue happened), persist the
        # current page-step container so a future re-run can fast-forward.
        # The container is detected from the page_data captured at the TOP of
        # this iteration — that's the page we just finished filling.
        if clicked and not error_text:
            try:
                completed_step_key = detect_step_key(page_data)
                if completed_step_key:
                    save_checkpoint(candidate.id, application_id, completed_step_key, step)
            except Exception as ckpt_exc:
                logger.debug("save_checkpoint failed: %s", ckpt_exc)

    await log(
        application_id,
        "warn",
        f"Reached {MAX_STEPS} learning steps without success text; marking needs_manual instead of failed",
        db,
    )
    return "needs_manual"


async def run_job_application(application_id: str | UUID) -> None:
    app_id = UUID(str(application_id))
    async with SessionLocal() as session:
        application, candidate, job = await _load_application(session, app_id)
        # Race-condition guard: another worker already picked this application up.
        if str(application.status or "").lower() == "running":
            await log(
                app_id,
                "warn",
                f"run_job_application: application {app_id} is already running; skipping duplicate launch",
                session,
            )
            return
        domain = anti_ban.domain_from_url(job.url)
        saved_previous_worker_draft = _has_saved_previous_worker_draft(app_id)
        start_url = None if saved_previous_worker_draft else _latest_saved_step_url(app_id)
        start_url = start_url or job.url
        application.attempt_count = int(application.attempt_count or 0) + 1
        await _set_status(session, application, "running")
        await log(app_id, "info", f"Starting application for {job.title}", session)
        if saved_previous_worker_draft:
            anti_ban.clear_storage_state(candidate.id, domain)
            await log(
                app_id,
                "warn",
                "Previous-worker Workday draft detected in saved context; starting fresh from job URL with cleared browser storage",
                session,
            )

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        await _needs_manual(app_id, "Playwright is not installed", None)
        raise RuntimeError("Playwright is not installed") from exc

    await anti_ban.wait_for_domain_cooldown(domain)

    async with anti_ban.browser_slot():
        async with async_playwright() as playwright:
            browser, context, page = await anti_ban.setup_browser(
                playwright,
                candidate.id,
                domain,
                use_storage_state=not saved_previous_worker_draft,
            )
            try:
                if start_url != job.url:
                    await _log(app_id, "info", f"Resuming application from {start_url}")
                else:
                    await _log(app_id, "info", f"Navigating to {job.url}")
                await page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(3)

                async with SessionLocal() as session:
                    application, fresh_candidate, _ = await _load_application(session, app_id)
                    result = await run_page_loop(page, fresh_candidate, session, app_id)

                if result == "completed":
                    await _complete(app_id, None)
                elif result == "failed":
                    async with SessionLocal() as _fs:
                        _fapp = await _fs.get(Application, app_id)
                        if _fapp is not None:
                            _fapp.status = "failed"
                            _fapp.completed_at = datetime.now(timezone.utc)
                            await _fs.commit()
                elif result == "validation_error":
                    await _validation_error(
                        app_id,
                        "Browser stopped because a validation error remained",
                        None,
                    )
                else:
                    await _needs_manual(
                        app_id,
                        "Browser stopped because a manual answer or blocker remained",
                        None,
                    )

            except Exception as exc:
                await _needs_manual(app_id, str(exc), None)
            finally:
                if not saved_previous_worker_draft:
                    with suppress(Exception):
                        await anti_ban.save_storage_state(context, candidate.id, domain)
                await context.close()
                await browser.close()


async def run_application(application_id: str | UUID) -> None:
    """Compatibility entry point used by job_applier routers."""
    await run_job_application(application_id)
