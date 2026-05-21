from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.db import ROOT_DIR
from backend.engine.platform_adapters import (
    PLATFORM_RULE_SCOPES,
    is_sensitive_auth_field,
    platform_for_domain,
    platform_scope_for_domain,
)


logger = logging.getLogger(__name__)

RULES_PATH = ROOT_DIR / "rules" / "form_rules.json"
GLOBAL_RULE_MIN_DOMAINS = 3


def domain_from_url(url: str) -> str:
    try:
        hostname = urlparse(str(url or "")).hostname or ""
        return hostname.removeprefix("www.")
    except Exception as e:
        logger.warning("domain_from_url failed: %s", e)
        return ""


def normalize_label(value: object) -> str:
    try:
        text = str(value or "").lower().replace("_", " ").replace("-", " ")
        text = text.replace("*", " ")
        text = re.sub(r"\b(select one|required field|required|choose one)\b", " ", text)
        text = re.sub(r"\bplease\s+select\b", "please", text)
        return " ".join(text.split())
    except Exception as e:
        logger.warning("normalize_label failed: %s", e)
        return str(value or "").strip().lower()


def _phone_code(value: object) -> str:
    try:
        match = re.search(r"\+\d{1,4}", str(value or ""))
        return match.group(0) if match else ""
    except Exception as e:
        logger.warning("_phone_code failed: %s", e)
        return ""


def _option_phone_codes(value: object) -> set[str]:
    try:
        return set(re.findall(r"\+\d{1,4}", str(value or "")))
    except Exception as e:
        logger.warning("_option_phone_codes failed: %s", e)
        return set()


def _matching_option_value(value: object, options: list[str] | None) -> str:
    try:
        raw_value = str(value or "").strip()
        if not raw_value:
            return raw_value
        path_parts = [part.strip() for part in raw_value.split(" > ") if part.strip()]
        top_level_value = path_parts[0] if path_parts else raw_value
        normalized_value = normalize_label(raw_value)
        normalized_top_level = normalize_label(top_level_value)
        option_values = [str(option or "").strip() for option in options or [] if str(option or "").strip()]
        for option in option_values:
            if normalize_label(option) == normalized_value:
                return option
        for option in option_values:
            if normalize_label(option) == normalized_top_level:
                return raw_value
        phone_code = _phone_code(raw_value)
        if phone_code:
            for option in option_values:
                if phone_code in _option_phone_codes(option):
                    return option
        return raw_value
    except Exception as e:
        logger.warning("_matching_option_value failed: %s", e)
        return str(value or "").strip()


def _is_low_signal_label(value: object) -> bool:
    try:
        normalized = normalize_label(value)
        if not normalized:
            return True
        if "captcha" in normalized:
            return True
        if normalized in {
            "select",
            "select one",
            "select one required",
            "required",
            "field required",
            "choose one",
            "please select",
        }:
            return True
        words = normalized.split()
        return len(words) <= 3 and all(word in {"select", "one", "required", "field", "choose", "please"} for word in words)
    except Exception as e:
        logger.warning("_is_low_signal_label failed: %s", e)
        return True


def _top_level_rule_value(value: object) -> str:
    try:
        parts = [part.strip() for part in str(value or "").split(" > ") if part.strip()]
        return parts[0] if parts else str(value or "").strip()
    except Exception as e:
        logger.warning("_top_level_rule_value failed: %s", e)
        return str(value or "").strip()


def _is_bad_phone_rule(label: object, field_type: object, value: object) -> bool:
    try:
        normalized_label = normalize_label(label)
        normalized_type = str(field_type or "").lower()
        value_text = str(value or "").strip()
        normalized_value = normalize_label(value_text)
        has_dial_code = bool(re.search(r"\+\d{1,4}", value_text))
        digit_count = len(re.sub(r"\D+", "", value_text))

        if any(token in normalized_label for token in ("phone device", "phone type")):
            return has_dial_code or normalized_value in {"india 91", "pakistan 92"}
        if "phone extension" in normalized_label or ("extension" in normalized_label and "phone" in normalized_label):
            return has_dial_code or normalized_value in {"india 91", "pakistan 92"}
        if any(token in normalized_label for token in ("country phone code", "phone code", "dialing code", "calling code")):
            return normalized_value in {"mobile", "landline", "phone", "telephone"}
        if (
            normalized_type not in {"select-one", "select-multiple", "radio"}
            and any(token in normalized_label for token in ("phone", "mobile", "cell", "telephone"))
            and not any(token in normalized_label for token in ("country phone code", "phone code", "dialing code", "calling code"))
        ):
            return has_dial_code and digit_count <= 4
        return False
    except Exception as e:
        logger.warning("_is_bad_phone_rule failed: %s", e)
        return False


def _rule_scope(rule: dict[str, Any]) -> str:
    try:
        return str(rule.get("scope") or "domain")
    except Exception as e:
        logger.warning("_rule_scope failed: %s", e)
        return "domain"


def _rule_domains(rule: dict[str, Any]) -> list[str]:
    try:
        domains = rule.get("domains")
        if isinstance(domains, list):
            return [str(domain or "") for domain in domains if str(domain or "")]
        domain = str(rule.get("domain") or "")
        return [domain] if domain else []
    except Exception as e:
        logger.warning("_rule_domains failed: %s", e)
        return []


def _has_platform_domain(rule: dict[str, Any]) -> bool:
    try:
        return any(platform_for_domain(domain).rule_scope for domain in _rule_domains(rule))
    except Exception as e:
        logger.warning("_has_platform_domain failed: %s", e)
        return False


def _migrate_domain_to_platform_rules(payload: dict[str, Any]) -> bool:
    """One-time migration: copy useful domain-scoped rules on platform domains to platform scope."""
    try:
        if payload.get("platform_migrated_v1"):
            return False
        rules = payload.setdefault("rules", [])
        now = datetime.now(timezone.utc).isoformat()
        # Only migrate sources that carry real signal (not AI/fallback guesses)
        promoted_sources = {"manual", "required_checkbox", "form"}
        added = 0
        for rule in list(rules):
            if not isinstance(rule, dict):
                continue
            if _rule_scope(rule) not in {"domain", ""}:
                continue
            source = str(rule.get("source") or "").lower()
            if source not in promoted_sources:
                continue
            domain = str(rule.get("domain") or "")
            if not domain:
                continue
            ps = platform_scope_for_domain(domain)
            if not ps:
                continue
            label_key = normalize_label(rule.get("label") or rule.get("label_key"))
            field_type = str(rule.get("field_type") or "").lower()
            action = str(rule.get("action") or "")
            value = str(rule.get("value") or "")
            if not label_key or not action or value == "":
                continue
            if _is_low_signal_label(label_key):
                continue
            if is_sensitive_auth_field(label_key, field_type):
                continue
            already_exists = any(
                isinstance(r, dict)
                and _rule_scope(r) == ps
                and normalize_label(r.get("label") or r.get("label_key")) == label_key
                and str(r.get("field_type") or "").lower() == field_type
                and str(r.get("action") or "") == action
                and str(r.get("value") or "") == value
                for r in rules
            )
            if already_exists:
                continue
            rules.append({
                "scope": ps,
                "domain": ps,
                "label": rule.get("label") or label_key,
                "label_key": label_key,
                "field_type": field_type,
                "action": action,
                "value": value,
                "source": source,
                "reason": f"migrated from domain rule ({domain})",
                "options": rule.get("options") or [],
                "control_kind": rule.get("control_kind") or "",
                "success_count": int(rule.get("success_count") or 1),
                "created_at": now,
                "last_seen_at": now,
            })
            added += 1
        payload["platform_migrated_v1"] = True
        return added > 0
    except Exception as e:
        logger.warning("_migrate_domain_to_platform_rules failed: %s", e)
        payload["platform_migrated_v1"] = True
        return False


# --- Garbage-collection constants ---------------------------------------
# Values that the engine has historically captured as "answers" to fields
# that are NOT actually source/referral pickers. Example: a banner-leak
# event causing "Country=Linkedin" to be saved as an auto rule. When a
# non-manual rule has one of these values AND the label/automation-id
# does NOT look like a source/HDYHAU question, it is garbage and must
# be deleted on startup.
_GARBAGE_SOURCE_VALUES: frozenset[str] = frozenset(
    {"linkedin", "other", "social media", "linked in", "facebook"}
)
_SOURCE_LABEL_TOKENS: tuple[str, ...] = (
    "source",
    "hear",
    "referral",
    "how did you",
)
# Rules accumulating this many failures are considered toxic and removed
# wholesale (irrespective of value) by the startup GC.
_FAILED_COUNT_HARD_DELETE: int = 3


def _is_garbage_source_rule(rule: dict[str, Any]) -> bool:
    """A rule that auto-captured a HDYHAU-style value into a non-source field."""
    try:
        source = str(rule.get("source") or "").lower()
        if source == "manual":
            return False
        raw_value = str(rule.get("value") or "").strip().lower()
        if raw_value not in _GARBAGE_SOURCE_VALUES:
            return False
        label_text = " ".join(
            [
                str(rule.get("label") or "").lower(),
                str(rule.get("label_key") or "").lower(),
                str(rule.get("automation_id") or "").lower(),
                str(rule.get("data_automation_id") or "").lower(),
            ]
        )
        if any(token in label_text for token in _SOURCE_LABEL_TOKENS):
            return False
        return True
    except Exception as e:
        logger.warning("_is_garbage_source_rule failed: %s", e)
        return False


def _is_failure_saturated_rule(rule: dict[str, Any]) -> bool:
    try:
        return int(rule.get("failed_count") or 0) >= _FAILED_COUNT_HARD_DELETE
    except Exception as e:
        logger.warning("_is_failure_saturated_rule failed: %s", e)
        return False


def garbage_collect_rules(payload: dict[str, Any] | None = None, *, persist: bool = True) -> dict[str, Any]:
    """Conservative one-shot GC of corrupted/stale form rules.

    Drops two classes of rules:
      1. Non-manual rules whose value is a known source-picker garbage value
         (LinkedIn / Other / Social Media / Linked In / Facebook) AND whose
         label/automation-id does NOT look like a source/HDYHAU question.
      2. Any rule (manual or not) with ``failed_count >= 3`` — the negative-
         feedback loop has decided this rule is broken on every observed run.

    Returns a summary dict with the keys ``before``, ``deleted``, ``after``,
    and ``samples`` (up to 5 deleted rules) — useful for logging.

    Manual rules with a sub-threshold ``failed_count`` are never deleted by
    this function; they must be invalidated through the dedicated negative-
    feedback path (:func:`record_rule_failure`) which sets
    ``needs_user_reconfirm`` instead.
    """
    summary = {"before": 0, "deleted": 0, "after": 0, "samples": []}
    try:
        owns_payload = payload is None
        if owns_payload:
            if not RULES_PATH.exists():
                return summary
            try:
                payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("garbage_collect_rules: load failed: %s", e)
                return summary
        if not isinstance(payload, dict):
            return summary
        rules = payload.get("rules")
        if not isinstance(rules, list):
            return summary

        before = len(rules)
        kept: list[Any] = []
        deleted_samples: list[dict[str, Any]] = []
        deleted_count = 0
        for rule in rules:
            if isinstance(rule, dict) and (
                _is_garbage_source_rule(rule) or _is_failure_saturated_rule(rule)
            ):
                deleted_count += 1
                if len(deleted_samples) < 5:
                    deleted_samples.append(
                        {
                            "label": rule.get("label") or rule.get("label_key"),
                            "field_type": rule.get("field_type"),
                            "value": rule.get("value"),
                            "source": rule.get("source"),
                            "domain": rule.get("domain"),
                            "scope": rule.get("scope"),
                            "failed_count": rule.get("failed_count"),
                        }
                    )
                continue
            kept.append(rule)

        payload["rules"] = kept
        payload["garbage_collected_v1"] = True
        summary.update(
            {
                "before": before,
                "deleted": deleted_count,
                "after": len(kept),
                "samples": deleted_samples,
            }
        )
        if persist and owns_payload and deleted_count > 0:
            _write_payload(payload)
        if deleted_count:
            logger.info(
                "garbage_collect_rules: removed %d/%d rules (samples=%s)",
                deleted_count,
                before,
                deleted_samples,
            )
        return summary
    except Exception as e:
        logger.warning("garbage_collect_rules failed: %s", e)
        return summary


# TODO(CVIAN-11 Item 5 - cross-tenant promotion): when a manual rule
# succeeds on >=N distinct platform-scoped tenants, promote it to a
# cross-tenant scope so a new Workday/Greenhouse/etc. domain inherits
# the answer on first visit. Out of scope for tonight per implementation
# brief 2026-05-15.


def _load_payload() -> dict[str, Any]:
    if not RULES_PATH.exists():
        return {"version": 1, "rules": []}
    try:
        payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "rules": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        return {"version": 1, "rules": []}
    dirty = False
    if not payload.get("platform_migrated_v1"):
        if _migrate_domain_to_platform_rules(payload):
            dirty = True
    if not payload.get("garbage_collected_v1"):
        result = garbage_collect_rules(payload, persist=False)
        if result.get("deleted"):
            dirty = True
        else:
            # Still set the marker so we don't re-scan every load.
            payload["garbage_collected_v1"] = True
            dirty = True
    if dirty:
        _write_payload(payload)
    return payload


def _write_payload(payload: dict[str, Any]) -> None:
    try:
        RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        RULES_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:
        logger.warning("_write_payload failed: %s", e)


def _rule_key(rule: dict[str, Any]) -> tuple[str, str, str, str]:
    try:
        return (
            normalize_label(rule.get("label") or rule.get("label_key")),
            str(rule.get("field_type") or "").lower(),
            str(rule.get("action") or ""),
            str(rule.get("value") or ""),
        )
    except Exception as e:
        logger.warning("_rule_key failed: %s", e)
        return ("", "", "", "")


def _promote_global_rules(payload: dict[str, Any], now: str) -> None:
    try:
        rules = [rule for rule in payload.get("rules", []) if isinstance(rule, dict)]
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for rule in rules:
            if _rule_scope(rule) != "domain":
                continue
            domain = str(rule.get("domain") or "")
            if not domain:
                continue
            if platform_scope_for_domain(domain):
                continue
            label_key, field_type, action, value = _rule_key(rule)
            if not label_key or not action or value == "":
                continue
            grouped.setdefault((label_key, field_type, action, value), []).append(rule)

        for key, members in grouped.items():
            distinct_domains = sorted({str(member.get("domain") or "") for member in members if str(member.get("domain") or "")})
            if len(distinct_domains) < GLOBAL_RULE_MIN_DOMAINS:
                continue

            best_member = max(members, key=lambda member: int(member.get("success_count") or 0))
            total_success = sum(int(member.get("success_count") or 0) for member in members)

            existing = next(
                (
                    rule
                    for rule in rules
                    if str(rule.get("scope") or "") == "global" and _rule_key(rule) == key
                ),
                None,
            )
            if existing is not None:
                existing["success_count"] = total_success
                existing["last_seen_at"] = now
                existing["domain_count"] = len(distinct_domains)
                existing["domains"] = distinct_domains
                continue

            rules.append(
                {
                    "scope": "global",
                    "domain": "",
                    "domains": distinct_domains,
                    "domain_count": len(distinct_domains),
                    "label": best_member.get("label") or best_member.get("label_key") or key[0],
                    "label_key": key[0],
                    "field_type": key[1],
                    "action": key[2],
                    "value": key[3],
                    "source": "promoted_global",
                    "reason": f"promoted after success on {len(distinct_domains)} domains",
                    "options": best_member.get("options") or [],
                    "control_kind": best_member.get("control_kind") or "",
                    "success_count": total_success,
                    "created_at": now,
                    "last_seen_at": now,
                }
            )

        payload["rules"] = rules
    except Exception as e:
        logger.warning("_promote_global_rules failed: %s", e)


def find_field_rule(
    domain: str,
    label: str,
    field_type: str,
    *,
    options: list[str] | None = None,
) -> dict[str, Any] | None:
    try:
        normalized_domain = domain_from_url(domain) or str(domain or "")
        platform_scope = platform_scope_for_domain(normalized_domain)
        normalized_label = normalize_label(label)
        normalized_type = str(field_type or "").lower()
        if not normalized_domain or not normalized_label:
            return None
        if is_sensitive_auth_field(normalized_label, normalized_type):
            return None

        option_set = {normalize_label(option) for option in options or [] if normalize_label(option)}
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for rule in _load_payload().get("rules", []):
            if not isinstance(rule, dict):
                continue
            rule_source = str(rule.get("source") or "").lower()
            if rule_source in {"synthetic", "option_fallback", "radio_fallback"}:
                continue
            scope = _rule_scope(rule)
            is_global = scope == "global"
            is_platform = scope in PLATFORM_RULE_SCOPES
            rule_domain = str(rule.get("domain") or "")
            if platform_scope:
                if is_global:
                    continue
            elif is_global and _has_platform_domain(rule):
                continue
            if is_platform and scope != platform_scope:
                continue
            if not is_global and not is_platform and rule_domain and rule_domain != normalized_domain:
                continue
            if str(rule.get("field_type") or "").lower() not in {"", normalized_type}:
                continue
            rule_label = normalize_label(rule.get("label") or rule.get("label_key"))
            if not rule_label or _is_low_signal_label(rule_label):
                continue
            if is_sensitive_auth_field(rule_label, rule.get("field_type")):
                continue
            if rule_label == normalized_label:
                score = 100
            elif len(min(rule_label.split(), normalized_label.split(), key=len)) >= 5 and (
                rule_label in normalized_label or normalized_label in rule_label
            ):
                score = 80
            else:
                continue

            raw_rule_value = str(rule.get("value") or "")
            if _is_bad_phone_rule(normalized_label, normalized_type, raw_rule_value):
                continue
            value = normalize_label(_matching_option_value(raw_rule_value, options))
            top_level_value = normalize_label(_top_level_rule_value(raw_rule_value))
            if option_set and value and value not in option_set and top_level_value not in option_set:
                continue
            if scope == "domain" and rule_domain == normalized_domain:
                score += 30
            elif is_platform:
                score += 20
            elif is_global:
                score += 10
            # Manual rules (user-confirmed answers) win decisively over
            # auto-captured / option-fallback / promoted-global rules. This
            # prevents stale auto-saved guesses (e.g. "Country=Linkedin"
            # captured from an error banner) from outscoring a clean manual
            # answer the user has explicitly confirmed.
            if rule_source == "manual":
                score += 50
            candidates.append((
                score + int(rule.get("success_count") or 0),
                str(rule.get("last_seen_at") or rule.get("created_at") or ""),
                rule,
            ))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]
    except Exception as e:
        logger.warning("find_field_rule failed: %s", e)
        return None


def save_field_rule(
    *,
    domain: str,
    label: str,
    field_type: str,
    action: str,
    value: object,
    source: str,
    reason: str,
    options: list[str] | None = None,
    control_kind: str | None = None,
) -> bool:
    try:
        normalized_domain = domain_from_url(domain) or str(domain or "")
        platform_scope = platform_scope_for_domain(normalized_domain)
        normalized_label = normalize_label(label)
        normalized_type = str(field_type or "").lower()
        if not normalized_domain or not normalized_label or value in {None, ""}:
            return False
        if _is_low_signal_label(normalized_label):
            return False
        if is_sensitive_auth_field(normalized_label, normalized_type):
            return False
        if _is_bad_phone_rule(normalized_label, normalized_type, value):
            return False

        value = _matching_option_value(value, options)
        if _is_bad_phone_rule(normalized_label, normalized_type, value):
            return False
        option_set = {normalize_label(option) for option in options or [] if normalize_label(option)}
        normalized_value = normalize_label(value)
        normalized_top_level = normalize_label(_top_level_rule_value(value))
        if str(action or "").lower() == "select":
            if not option_set and str(source or "").lower() != "manual":
                return False
            if normalized_value and normalized_value not in option_set and normalized_top_level not in option_set:
                return False

        now = datetime.now(timezone.utc).isoformat()
        payload = _load_payload()
        rules = payload.setdefault("rules", [])

        def upsert_rule(scope: str, rule_domain: str, source_reason: str) -> bool:
            try:
                key = (scope, rule_domain, normalized_label, normalized_type, str(action or ""), str(value))
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    existing_key = (
                        _rule_scope(rule),
                        str(rule.get("domain") or ""),
                        normalize_label(rule.get("label") or rule.get("label_key")),
                        str(rule.get("field_type") or "").lower(),
                        str(rule.get("action") or ""),
                        str(rule.get("value") or ""),
                    )
                    if existing_key == key:
                        rule["success_count"] = int(rule.get("success_count") or 0) + 1
                        rule["last_seen_at"] = now
                        rule["reason"] = source_reason
                        if control_kind:
                            rule["control_kind"] = control_kind
                        return False

                rules.append(
                    {
                        "scope": scope,
                        "domain": rule_domain,
                        "label": label,
                        "label_key": normalized_label,
                        "field_type": normalized_type,
                        "action": str(action or ""),
                        "value": str(value),
                        "source": source,
                        "reason": source_reason,
                        "options": options or [],
                        "control_kind": control_kind or "",
                        "success_count": 1,
                        "created_at": now,
                        "last_seen_at": now,
                    }
                )
                return True
            except Exception as e:
                logger.warning("upsert_rule failed: %s", e)
                return False

        saved_domain = upsert_rule("domain", normalized_domain, reason)
        saved_platform = False
        if platform_scope:
            saved_platform = upsert_rule(platform_scope, platform_scope, f"{reason}; reusable {platform_scope} rule")
        _promote_global_rules(payload, now)
        _write_payload(payload)
        return saved_domain or saved_platform
    except Exception as e:
        logger.warning("save_field_rule failed: %s", e)
        return False


# --- Negative feedback loop --------------------------------------------------

# Substrings in a Workday/Greenhouse error message that prove the answer
# the engine just used is INCOMPATIBLE with the field's option list. When
# any of these appear, we delete the rule immediately rather than waiting
# for the failure counter to climb to 2.
_FATAL_FAILURE_PHRASES: tuple[str, ...] = (
    "not a valid",
    "invalid option",
    "select a valid",
    "select an option",
)

# How many cumulative failures (without an interleaved success) a rule
# may accumulate before we delete it.
_FAILED_COUNT_DELETE_THRESHOLD: int = 2


def _label_matches_rule(rule_label: str, normalized_label: str) -> bool:
    """Same fuzzy match used by find_field_rule — exact or substring (>=5 chars)."""
    if not rule_label or not normalized_label:
        return False
    if rule_label == normalized_label:
        return True
    shorter = min(rule_label.split(), normalized_label.split(), key=len) if rule_label.split() and normalized_label.split() else []
    if shorter and len(shorter) >= 5 and (rule_label in normalized_label or normalized_label in rule_label):
        return True
    return False


def record_rule_failure(
    field_label: str,
    field_automation_id: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Penalise (and possibly delete) the rule that produced the bad answer.

    Behaviour:
      * Finds the most recently used non-manual rule whose label matches
        ``field_label`` (or whose automation_id matches when provided).
      * Increments ``failed_count`` on that rule (initialising to 1 if
        missing).
      * If the new ``failed_count`` reaches :data:`_FAILED_COUNT_DELETE_THRESHOLD`
        OR ``error_message`` contains one of :data:`_FATAL_FAILURE_PHRASES`,
        the rule is deleted from the rules file.
      * Manual rules are NEVER deleted — they get a ``needs_user_reconfirm``
        flag set to ``True`` instead so the UI can ask the user to verify.

    Returns a small summary describing what happened, primarily for logs.
    """
    result: dict[str, Any] = {
        "matched": False,
        "deleted": False,
        "flagged_manual": False,
        "failed_count": 0,
        "label": field_label,
    }
    try:
        normalized_label = normalize_label(field_label)
        normalized_aid = str(field_automation_id or "").strip().lower()
        if not normalized_label and not normalized_aid:
            return result
        error_text = (error_message or "").lower()
        fatal = any(phrase in error_text for phrase in _FATAL_FAILURE_PHRASES)

        payload = _load_payload()
        rules = payload.get("rules")
        if not isinstance(rules, list):
            return result

        # Find every plausible match, then pick the most recently used.
        candidates: list[tuple[str, int, dict[str, Any]]] = []
        for idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            rule_label = normalize_label(rule.get("label") or rule.get("label_key"))
            rule_aid = str(
                rule.get("automation_id") or rule.get("data_automation_id") or ""
            ).strip().lower()
            label_hit = _label_matches_rule(rule_label, normalized_label) if normalized_label else False
            aid_hit = bool(normalized_aid) and bool(rule_aid) and (
                rule_aid == normalized_aid or rule_aid in normalized_aid or normalized_aid in rule_aid
            )
            if not (label_hit or aid_hit):
                continue
            last_seen = str(rule.get("last_seen_at") or rule.get("created_at") or "")
            candidates.append((last_seen, idx, rule))

        if not candidates:
            return result

        # Prefer non-manual rules first (those are the ones we suspect),
        # but fall back to manual if that's all we have so we can flag it.
        non_manual = [c for c in candidates if str(c[2].get("source") or "").lower() != "manual"]
        target_pool = non_manual or candidates
        target_pool.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, target_idx, target_rule = target_pool[0]
        result["matched"] = True

        source = str(target_rule.get("source") or "").lower()
        new_count = int(target_rule.get("failed_count") or 0) + 1
        target_rule["failed_count"] = new_count
        target_rule["last_failure_at"] = datetime.now(timezone.utc).isoformat()
        if error_message:
            target_rule["last_failure_message"] = str(error_message)[:500]
        result["failed_count"] = new_count

        if source == "manual":
            target_rule["needs_user_reconfirm"] = True
            result["flagged_manual"] = True
            logger.info(
                "record_rule_failure: manual rule flagged for reconfirm label=%r value=%r failed_count=%d",
                target_rule.get("label"),
                target_rule.get("value"),
                new_count,
            )
        elif fatal or new_count >= _FAILED_COUNT_DELETE_THRESHOLD:
            # Drop the rule entirely.
            try:
                del rules[target_idx]
                result["deleted"] = True
                logger.info(
                    "record_rule_failure: deleted rule label=%r value=%r source=%r reason=%s",
                    target_rule.get("label"),
                    target_rule.get("value"),
                    source,
                    "fatal_message" if fatal else f"failed_count>={_FAILED_COUNT_DELETE_THRESHOLD}",
                )
            except Exception as e:
                logger.warning("record_rule_failure: delete failed: %s", e)

        _write_payload(payload)
        return result
    except Exception as e:
        logger.warning("record_rule_failure failed: %s", e)
        return result