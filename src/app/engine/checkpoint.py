"""Per-application checkpoint persistence for resumable Workday/Greenhouse runs.

Workday's apply flow is broken into 4-6 step containers. When the engine
crashes or the user re-runs an application, we want to fast-forward past
the steps that have already been completed instead of re-filling every
field. This module persists a tiny JSON file per (candidate, application)
combination describing the last successfully-completed step.

Public API:

    save_checkpoint(candidate_id, application_id, step_key, step_index)
    load_checkpoint(candidate_id, application_id) -> (step_key, step_index) | None

Step keys come from the Workday cross-tenant page-step container catalog
in `reference_workday_platform_selectors.md`:

    contactInformationPage
    myExperiencePage
    voluntaryDisclosuresPage
    selfIdentificationPage

The ``step_index`` field is the engine's monotonic step counter (1-based)
at the moment the checkpoint was written — useful when the same Workday
page-step container is re-shown on a retry (e.g. validation bounce).

Files live under ``sessions/checkpoints/<candidate_id>_<application_id>.json``
relative to the project root. Writes are atomic (write-then-rename) so a
crash mid-write cannot corrupt an existing checkpoint.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.db import ROOT_DIR


logger = logging.getLogger(__name__)

CHECKPOINT_DIR: Path = ROOT_DIR / "sessions" / "checkpoints"

# Known Workday page-step containers — the engine emits these strings as
# step_key when calling save_checkpoint. Defined here so callers and tests
# can import a single source of truth instead of hard-coding strings.
KNOWN_STEP_KEYS: tuple[str, ...] = (
    "contactInformationPage",
    "myInformationPage",
    "myExperiencePage",
    "applicationQuestionsPage",
    "voluntaryDisclosuresPage",
    "selfIdentificationPage",
    "reviewPage",
    "previewPage",
    "equalEmploymentOpportunityPage",
    "jobApplicationPage",
)


def _checkpoint_path(candidate_id: object, application_id: object) -> Path:
    safe_candidate = str(candidate_id or "").strip().replace("/", "_").replace("\\", "_")
    safe_app = str(application_id or "").strip().replace("/", "_").replace("\\", "_")
    if not safe_candidate or not safe_app:
        raise ValueError("candidate_id and application_id are required for checkpoints")
    return CHECKPOINT_DIR / f"{safe_candidate}_{safe_app}.json"


def save_checkpoint(
    candidate_id: object,
    application_id: object,
    step_key: str,
    step_index: int,
) -> bool:
    """Persist the last successfully-completed step for this application.

    Returns ``True`` on success, ``False`` on any I/O / serialization error
    (callers should treat checkpointing as best-effort — losing a checkpoint
    is annoying but never fatal).
    """
    try:
        if not step_key:
            return False
        path = _checkpoint_path(candidate_id, application_id)
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "candidate_id": str(candidate_id),
            "application_id": str(application_id),
            "step_key": str(step_key),
            "step_index": int(step_index or 0),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        # Atomic write: serialise to a temp file in the same directory, then
        # os.replace() onto the final path so a crash mid-write cannot leave
        # a half-written JSON file on disk.
        fd, tmp_path = tempfile.mkstemp(prefix=".ckpt-", dir=str(CHECKPOINT_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        logger.warning("save_checkpoint failed: %s", e)
        return False


def load_checkpoint(
    candidate_id: object,
    application_id: object,
) -> tuple[str, int] | None:
    """Return ``(step_key, step_index)`` if a checkpoint exists, else ``None``.

    Returns ``None`` rather than raising for any error so callers can fall
    back to a fresh run on disk corruption.
    """
    try:
        path = _checkpoint_path(candidate_id, application_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        step_key = str(payload.get("step_key") or "").strip()
        step_index = int(payload.get("step_index") or 0)
        if not step_key:
            return None
        return step_key, step_index
    except Exception as e:
        logger.warning("load_checkpoint failed: %s", e)
        return None


def clear_checkpoint(candidate_id: object, application_id: object) -> bool:
    """Best-effort removal of a checkpoint file. Returns ``True`` if a file
    was removed (or never existed); ``False`` only on unexpected I/O errors."""
    try:
        path = _checkpoint_path(candidate_id, application_id)
        if path.exists():
            path.unlink()
        return True
    except Exception as e:
        logger.warning("clear_checkpoint failed: %s", e)
        return False


def detect_step_key(page_data: dict[str, Any] | None) -> str | None:
    """Identify the current Workday page-step container from a page_data dict.

    Walks fields/buttons/modals for any ``automationId`` that exactly matches
    a known step-key. Returns the matched key, or ``None`` when no recognised
    step container is present (which is the case on non-Workday pages and on
    Workday post-submit confirmation pages).
    """
    if not isinstance(page_data, dict):
        return None
    try:
        known_lower = {key.lower(): key for key in KNOWN_STEP_KEYS}
        # Check every plausible container source in page_data — fields,
        # buttons, modals, and the top-level text blob (some Workday tenants
        # expose the step container only as a data-automation-id on a
        # <section> wrapper that doesn't appear under the field walk).
        candidates: list[str] = []
        for collection_name in ("fields", "buttons", "modals"):
            for entry in page_data.get(collection_name) or []:
                if isinstance(entry, dict):
                    aid = str(entry.get("automationId") or "").strip()
                    if aid:
                        candidates.append(aid)
        # Bidirectional case-insensitive substring containment: tenants name
        # the step container slightly differently across releases (e.g. an
        # automationId of "contactInformation" should still match the known
        # key "contactInformationPage", and vice versa).
        for aid in candidates:
            aid_lower = aid.lower()
            if aid_lower in known_lower:
                return known_lower[aid_lower]
            for known_lc, canonical in known_lower.items():
                if known_lc in aid_lower or aid_lower in known_lc:
                    return canonical
        # Fall back to substring scan over the serialised page text — covers
        # tenants where the step container appears only in raw HTML/text.
        text_blob_lower = str(page_data.get("text") or "").lower()
        for known_lc, canonical in known_lower.items():
            if known_lc in text_blob_lower:
                return canonical
        return None
    except Exception as e:
        logger.warning("detect_step_key failed: %s", e)
        return None


def step_index_for_key(step_key: str) -> int:
    """Numeric ordering of the canonical Workday step keys.

    Returns ``-1`` for unknown keys so a fast-forward loop can decide to
    treat them as "earlier than every known checkpoint" (i.e. always run).
    """
    try:
        return KNOWN_STEP_KEYS.index(str(step_key or ""))
    except ValueError:
        return -1
