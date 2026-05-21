"""Universal Workday intent classifier.

One place to detect what a Workday form question is asking and return the
canonical answer. Pure regex/keyword tables — no API calls, no per-domain rules.

Public entry point: ``classify(label, field, candidate)`` returns
``(intent, answer, source)``. ``intent is None`` means "unknown — let the
caller fall back to saved rules or mark as unanswered".

Canonical answers approved by the user:
- previous_worker / relatives_at_company -> "No" (universal, every candidate)
- gender / race / hispanic / veteran / disability -> from candidate.extra_answers
  ONLY; never guess. Returns None if the candidate hasn't provided one so the
  field surfaces as a manual question.
- terms_consent -> "Yes" (candidate already chose to apply)
- hdyhau -> priority list LinkedIn > Social Media > Job Site/Board >
  Career Site > Other > first available option.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# --- Constants -------------------------------------------------------------

# Verbs that, combined with a "previous" qualifier, indicate the
# "have you ever worked here" question.
_PREVIOUS_VERBS = ("worked", "employed", "employee", "employment")
_PREVIOUS_QUALIFIERS = (
    "ever",
    "previous",
    "previously",
    "before",
    "past",
    "former",
    "formerly",
    "prior",
    "in the past",
    "currently or previously",
    "current or former",
    "current or previous",
)

# Workday's stable automation IDs / selector fragments for the
# previous-worker radio. These survive across tenants.
_PREVIOUS_WORKER_SELECTOR_TOKENS = (
    "candidateispreviousworker",
    "previousworker",
    "haspreviousworked",
    "isformeremployee",
)

# Words that introduce a "do you have any relatives at company X" question.
_RELATIVE_TOKENS = (
    "relative",
    "relatives",
    "family member",
    "family members",
    "immediate family",
    "spouse",
    "parent",
    "sibling",
    "related to anyone",
    "related to someone",
)
_RELATIVE_CONTEXT = (
    "work",
    "works",
    "worked",
    "employed",
    "employee",
    "company",
    "organization",
    "organisation",
    "firm",
    "us",
    "this employer",
    "for us",
)

# How-did-you-hear-about-us label triggers.
_HDYHAU_TRIGGERS = (
    "how did you hear",
    "where did you hear",
    "hear about us",
    "hear about this",
    "learn about this opportunity",
    "learn about us",
    "find out about",
    "referral source",
)
_HDYHAU_BARE_LABELS = {"source", "source required"}

# HDYHAU answer priority: each tier holds tokens ranked equal; tiers
# rank from highest priority (top) to lowest. Within a tier the first
# matching option wins.
HDYHAU_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("linkedin", "linked in"),
    ("social media", "social network", "social"),
    ("job site", "job board", "job boards", "indeed"),
    ("career site", "company website", "company site", "career page", "company career"),
    ("other",),
)


# --- Helpers ---------------------------------------------------------------

def _text(label: object) -> str:
    """Normalize a label to lowercase, single-spaced, stripped."""
    try:
        return " ".join(str(label or "").lower().split())
    except Exception as exc:
        logger.warning("workday_intent._text failed: %s", exc)
        return ""


def _selector_text(field: dict[str, Any] | None) -> str:
    """Concatenate identifying selectors / automation IDs for keyword scans."""
    if not isinstance(field, dict):
        return ""
    try:
        parts = [
            str(field.get("automationId") or ""),
            str(field.get("selector") or ""),
            str(field.get("name") or ""),
            str(field.get("id") or ""),
            str(field.get("dataAutomationId") or ""),
        ]
        return " ".join(parts).lower()
    except Exception as exc:
        logger.warning("workday_intent._selector_text failed: %s", exc)
        return ""


def _extra(candidate: dict[str, Any] | None, *keys: str) -> str | None:
    """Look up candidate.extra_answers by any of the given keys (case-insensitive)."""
    if not isinstance(candidate, dict):
        return None
    try:
        extras = candidate.get("extra_answers") or {}
        if not isinstance(extras, dict):
            return None
        normalized = {str(k).strip().lower(): v for k, v in extras.items()}
        for key in keys:
            value = normalized.get(key.strip().lower())
            if value is not None and str(value).strip():
                return str(value)
        return None
    except Exception as exc:
        logger.warning("workday_intent._extra failed: %s", exc)
        return None


def _field_options(field: dict[str, Any] | None) -> list[str]:
    """Extract option labels from a select / radio field, in original order."""
    if not isinstance(field, dict):
        return []
    try:
        options: list[str] = []
        raw = field.get("options")
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict):
                    text = str(entry.get("label") or entry.get("text") or entry.get("value") or "").strip()
                else:
                    text = str(entry or "").strip()
                if text:
                    options.append(text)
        if not options:
            radio = field.get("radioOptions")
            if isinstance(radio, list):
                for entry in radio:
                    if not isinstance(entry, dict):
                        continue
                    text = str(entry.get("label") or entry.get("value") or "").strip()
                    if text:
                        options.append(text)
        return options
    except Exception as exc:
        logger.warning("workday_intent._field_options failed: %s", exc)
        return []


# --- Intent matchers -------------------------------------------------------

def _is_previous_worker(text: str, field: dict[str, Any] | None) -> bool:
    selector = _selector_text(field)
    if any(token in selector for token in _PREVIOUS_WORKER_SELECTOR_TOKENS):
        return True
    if not text:
        return False
    has_verb = any(verb in text for verb in _PREVIOUS_VERBS)
    has_qualifier = any(q in text for q in _PREVIOUS_QUALIFIERS)
    if has_verb and has_qualifier:
        return True
    # Common phrasings without an explicit qualifier.
    if "former employee" in text or "previous employer" in text:
        return True
    if re.search(r"\bare you\b.*\b(current|former|previous)\b", text):
        return True
    if re.search(r"\bworked (for|at|with) (us|this|our)\b", text):
        return True
    if re.search(r"\bemployed (by|at|with) (us|this|our)\b", text):
        return True
    return False


def _is_relatives_at_company(text: str) -> bool:
    if not text:
        return False
    if not any(token in text for token in _RELATIVE_TOKENS):
        return False
    return any(ctx in text for ctx in _RELATIVE_CONTEXT)


def _is_terms_consent(text: str) -> bool:
    if not text:
        return False
    if "terms" in text and ("conditions" in text or "agreement" in text):
        return True
    return any(
        phrase in text
        for phrase in (
            "i have read and consent",
            "i have read and accept",
            "i have read and agree",
            "i agree to the terms",
            "i agree to the privacy",
            "accept the terms",
            "acknowledge the terms",
            "consent to the privacy",
        )
    )


def _is_sponsorship(text: str) -> bool:
    if not text:
        return False
    if "sponsorship" in text:
        return True
    return "sponsor" in text and ("visa" in text or "work" in text or "employment" in text)


def _is_auth_to_work(text: str) -> bool:
    if not text:
        return False
    if "sponsorship" in text:
        # Handled by _is_sponsorship — don't double-match.
        return False
    has_auth = any(token in text for token in ("authorized", "authorised", "eligible", "legally"))
    has_work = "work" in text or "employment" in text
    return has_auth and has_work


def _is_hdyhau(text: str) -> bool:
    if not text:
        return False
    if any(trigger in text for trigger in _HDYHAU_TRIGGERS):
        return True
    return text.strip() in _HDYHAU_BARE_LABELS


def _hdyhau_pick(options: list[str]) -> str | None:
    if not options:
        return None
    normalized: list[tuple[str, str]] = [(opt.lower(), opt) for opt in options]
    for tier in HDYHAU_PRIORITY:
        for token in tier:
            for low, original in normalized:
                if token in low:
                    return original
    return options[0]


# --- Public API ------------------------------------------------------------

def classify(
    label: str,
    field: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str]:
    """Classify a Workday form question.

    Returns ``(intent, answer, source)``.
    - ``intent`` is None when the label doesn't match any known intent —
      the caller should fall back to saved rules / extra_answers / unanswered.
    - ``answer`` may be None even when intent matches, for demographic
      questions where the candidate hasn't provided a value (the caller
      MUST treat this as unanswered, not guess).
    - ``source`` is a stable string suitable for logging or rule provenance.
    """
    try:
        text = _text(label)

        if _is_previous_worker(text, field):
            return "previous_worker", "No", "workday_intent_previous_worker"

        if _is_relatives_at_company(text):
            return "relatives_at_company", "No", "workday_intent_relatives"

        if _is_terms_consent(text):
            return "terms_consent", "Yes", "workday_intent_terms"

        if "gender" in text or text.strip() in {"sex", "your sex"}:
            return "gender", _extra(candidate, "gender"), "workday_intent_demographic"

        if "ethnicity" in text or "race" in text:
            return "race", _extra(candidate, "ethnicity", "race"), "workday_intent_demographic"

        if "hispanic" in text or "latino" in text:
            return "hispanic", _extra(candidate, "hispanic", "latino"), "workday_intent_demographic"

        if "veteran" in text or "military service" in text:
            return "veteran", _extra(candidate, "veteran status", "veteran"), "workday_intent_demographic"

        if "disability" in text or "disabled" in text:
            return "disability", _extra(candidate, "disability"), "workday_intent_demographic"

        if _is_sponsorship(text):
            return "sponsorship", _extra(candidate, "sponsorship", "visa sponsor", "require sponsorship"), "workday_intent_auth"

        if _is_auth_to_work(text):
            return "auth_to_work", _extra(candidate, "work authorization", "authorized to work", "visa", "employment authorization"), "workday_intent_auth"

        if _is_hdyhau(text):
            options = _field_options(field)
            answer = _hdyhau_pick(options) if options else "LinkedIn"
            return "hdyhau", answer, "workday_intent_hdyhau"

        return None, None, ""
    except Exception as exc:
        logger.warning("workday_intent.classify failed for label %r: %s", label, exc)
        return None, None, ""


def is_known_intent_label(label: str, field: dict[str, Any] | None = None) -> bool:
    """Return True if the label matches any intent (regardless of answer).

    Useful for the never-guess gate: even if the canonical answer is None
    (e.g. demographic question with no candidate value), the caller knows
    to mark the field as unanswered rather than fuzzy-guessing.
    """
    intent, _, _ = classify(label, field=field, candidate=None)
    return intent is not None