from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from rapidfuzz import process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import ROOT_DIR
from backend.models import Candidate, UnansweredQuestion

logger = logging.getLogger(__name__)


FIELD_ATTR = "data-cviance-field"
LogCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class FormField:
    selector: str
    label: str
    field_type: str
    options: list[dict[str, str]]


def _domain(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or ""
        return hostname.removeprefix("www.")
    except Exception as e:
        logger.warning("_domain failed: %s", e)
        return ""


def _norm(value: object) -> str:
    try:
        return str(value or "").strip().lower()
    except Exception as e:
        logger.warning("_norm failed: %s", e)
        return ""


def _candidate_name_part(candidate: Candidate, index: int) -> str:
    try:
        parts = candidate.name.split()
        if not parts:
            return candidate.name
        return parts[index]
    except Exception as e:
        logger.warning("_candidate_name_part failed: %s", e)
        return candidate.name


def _normalized_extra_answers(candidate: Candidate) -> dict[str, object]:
    try:
        extra_answers = candidate.extra_answers or {}
        if not isinstance(extra_answers, dict):
            return {}
        return {str(key).strip().lower(): value for key, value in extra_answers.items()}
    except Exception as e:
        logger.warning("_normalized_extra_answers failed: %s", e)
        return {}


def _extra_answer_value(candidate: Candidate, *keys: str) -> str | None:
    try:
        normalized_extra = _normalized_extra_answers(candidate)
        for key in keys:
            value = normalized_extra.get(key.strip().lower())
            if value is not None and str(value).strip():
                return str(value)
        return None
    except Exception as e:
        logger.warning("_extra_answer_value failed: %s", e)
        return None


_COUNTRY_DIAL_CODES = {
    "india": "+91",
    "bharat": "+91",
    "in": "+91",
    "united states": "+1",
    "usa": "+1",
    "us": "+1",
    "canada": "+1",
    "united kingdom": "+44",
    "uk": "+44",
    "australia": "+61",
    "pakistan": "+92",
    "bangladesh": "+880",
    "sri lanka": "+94",
    "nepal": "+977",
}


def _country_value(candidate: Candidate) -> str | None:
    try:
        return _extra_answer_value(candidate, "country", "address country", "current country", "country of residence")
    except Exception as e:
        logger.warning("_country_value failed: %s", e)
        return None


def _phone_country_code_value(candidate: Candidate) -> str | None:
    try:
        raw = _extra_answer_value(candidate, "phone country code", "country phone code", "dialing code", "calling code")
        match = re.search(r"\+\d{1,4}", str(raw or ""))
        if match:
            return match.group(0)
        country = str(_country_value(candidate) or candidate.location or "").lower()
        for key, code in _COUNTRY_DIAL_CODES.items():
            if key in country:
                return code
        return None
    except Exception as e:
        logger.warning("_phone_country_code_value failed: %s", e)
        return None


def _phone_number_value(candidate: Candidate) -> str | None:
    try:
        phone = str(candidate.phone or "").strip()
        if not phone:
            return None
        code = _phone_country_code_value(candidate) or ""
        text = re.sub(r"^[A-Za-z\s]+", "", phone).strip()
        if code and text.startswith(code):
            text = text[len(code):].strip()
        elif text.startswith("+"):
            text = re.sub(r"^\+\d{1,4}\s*", "", text).strip()
        digits = re.sub(r"\D+", "", text) or re.sub(r"\D+", "", phone)
        code_digits = re.sub(r"\D+", "", code)
        if code_digits and digits.startswith(code_digits) and len(digits) > len(code_digits) + 4:
            digits = digits[len(code_digits):]
        return digits or phone
    except Exception as e:
        logger.warning("_phone_number_value failed: %s", e)
        return None


def _first_name_value(candidate: Candidate) -> str:
    try:
        return _extra_answer_value(
            candidate,
            "legal given name",
            "legal first name",
            "given name",
            "first name",
        ) or _candidate_name_part(candidate, 0)
    except Exception as e:
        logger.warning("_first_name_value failed: %s", e)
        return candidate.name


def _last_name_value(candidate: Candidate) -> str:
    try:
        return _extra_answer_value(
            candidate,
            "legal family name",
            "legal surname",
            "legal last name",
            "family name",
            "surname",
            "last name",
        ) or _candidate_name_part(candidate, -1)
    except Exception as e:
        logger.warning("_last_name_value failed: %s", e)
        return candidate.name


def _full_name_value(candidate: Candidate) -> str:
    try:
        return _extra_answer_value(candidate, "legal name", "full legal name", "full name") or candidate.name
    except Exception as e:
        logger.warning("_full_name_value failed: %s", e)
        return candidate.name


def _local_given_name_value(candidate: Candidate) -> str:
    try:
        return _extra_answer_value(
            candidate,
            "local given name",
            "local given names",
            "local first name",
        ) or _first_name_value(candidate)
    except Exception as e:
        logger.warning("_local_given_name_value failed: %s", e)
        return candidate.name


def _local_family_name_value(candidate: Candidate) -> str:
    try:
        return _extra_answer_value(
            candidate,
            "local family name",
            "local surname",
            "local last name",
        ) or _last_name_value(candidate)
    except Exception as e:
        logger.warning("_local_family_name_value failed: %s", e)
        return candidate.name


def _local_full_name_value(candidate: Candidate) -> str:
    try:
        return _extra_answer_value(candidate, "local name", "full local name") or _full_name_value(candidate)
    except Exception as e:
        logger.warning("_local_full_name_value failed: %s", e)
        return candidate.name


def resolve_candidate_answer(candidate: Candidate, label: str) -> str | None:
    try:
        text = _norm(label)
        if any(key in text for key in ("country phone code", "phone country code", "phone code", "dialing code", "calling code")):
            return _phone_country_code_value(candidate)
        if "country" in text and "phone" not in text:
            return _country_value(candidate)
        normalized_extra = _normalized_extra_answers(candidate)
        for key, value in normalized_extra.items():
            if value is not None and key and (key == text or key in text):
                return str(value)
        mappings: tuple[tuple[tuple[str, ...], object | None], ...] = (
            (
                ("local given name", "local given names", "local first name"),
                _local_given_name_value(candidate),
            ),
            (
                ("local family name", "local surname", "local last name"),
                _local_family_name_value(candidate),
            ),
            (("local name", "full local name"), _local_full_name_value(candidate)),
            (
                ("legal given name", "legal first name", "first name", "fname", "given name"),
                _first_name_value(candidate),
            ),
            (
                ("legal family name", "legal surname", "legal last name", "last name", "lname", "surname", "family name"),
                _last_name_value(candidate),
            ),
            (("legal name", "full legal name", "full name"), _full_name_value(candidate)),
            (("email address", "email"), candidate.email),
            (("contact number", "telephone", "mobile", "phone"), _phone_number_value(candidate)),
            (("years of experience", "experience years", "how many years"), candidate.experience_years),
            (("linkedin profile", "linkedin url", "linkedin"), candidate.linkedin_url),
            (("personal website", "portfolio", "website"), candidate.portfolio_url),
        )
        for keywords, value in mappings:
            if value is not None and any(keyword in text for keyword in keywords):
                return str(value)
        return None
    except Exception as e:
        logger.warning("resolve_candidate_answer failed: %s", e)
        return None


async def safe_click(page: object, element: object) -> None:
    try:
        await page.evaluate(
            "el => el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}))",
            element,
        )
        await asyncio.sleep(0.5)
        return
    except Exception:
        pass

    try:
        box = await element.bounding_box()
        if box:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await asyncio.sleep(0.5)
            return
    except Exception:
        pass

    await element.click(force=True)
    await asyncio.sleep(0.5)


async def extract_form_fields(page: object, scope_selector: str | None = None) -> list[FormField]:
    script = f"""
    (scopeSelector) => {{
      const attr = "{FIELD_ATTR}";
      const root = scopeSelector ? document.querySelector(scopeSelector) : document;
      if (!root) return [];
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getClientRects().length > 0;
      }};
      const textOf = (node) => (node?.innerText || node?.textContent || "").trim();
      const isPlaceholderLabel = (t) => !t || /^(select\.?|choose\.?|type here|search)$/i.test(t.trim());
      const labelFor = (el) => {{
        const id = el.getAttribute("id");
        const byFor = id ? document.querySelector(`label[for="${{CSS.escape(id)}}"]`) : null;
        // Priority 1: unambiguous semantic sources — return first non-placeholder match
        const primary = [
          el.getAttribute("aria-label") || "",
          byFor ? textOf(byFor) : "",
          el.closest("label") ? textOf(el.closest("label")) : "",
        ];
        for (const s of primary) {{
          if (s && s.trim() && !isPlaceholderLabel(s.trim()))
            return s.trim().replace(/[_-]+/g, " ");
        }}
        // Priority 2: attribute-derived labels
        const attrs = [
          el.getAttribute("placeholder") || "",
          el.getAttribute("data-automation-id") || "",
          el.getAttribute("name") || "",
          el.getAttribute("id") || "",
        ];
        for (const s of attrs) {{
          if (s && s.trim() && !isPlaceholderLabel(s.trim()))
            return s.trim().replace(/[_-]+/g, " ");
        }}
        // Priority 3: closest fieldset/form-group label (not the element itself)
        const scope = el.closest("fieldset, .form-group");
        if (scope) {{
          const lbl = Array.from(scope.querySelectorAll("legend, label:not(.error)"))
            .find(l => !l.contains(el));
          if (lbl) {{
            const t = textOf(lbl).slice(0, 160).trim();
            if (t && !isPlaceholderLabel(t)) return t.replace(/[_-]+/g, " ");
          }}
        }}
        return "";
      }};
      return Array.from(root.querySelectorAll("input, textarea, select"))
        .filter(visible)
        .filter((el) => {{
          const type = (el.getAttribute("type") || el.tagName).toLowerCase();
          return !["hidden", "submit", "button", "image"].includes(type);
        }})
        .map((el, index) => {{
          const token = el.getAttribute(attr) || `field-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`;
          el.setAttribute(attr, token);
          const tag = el.tagName.toLowerCase();
          const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : tag;
          const optionsFromSelect = (sel) => Array.from(sel.options)
            .filter(o => o.value !== "" && o.value !== undefined)
            .map(o => ({{
              label: (o.label || o.innerText || o.textContent || o.value || "").trim(),
              value: o.value || "",
            }}));
          let options = [];
          if (tag === "select") {{
            options = optionsFromSelect(el);
          }} else {{
            // For custom comboboxes, look for a hidden native <select> in the same container
            const c = el.closest("fieldset, .form-group, label, [data-automation-id]");
            const ns = c && Array.from(c.querySelectorAll("select")).find(s => s !== el && (s.options || []).length > 1);
            if (ns) options = optionsFromSelect(ns);
          }}
          return {{
            selector: `[${{attr}}="${{token}}"]`,
            label: labelFor(el),
            field_type: type,
            options,
          }};
        }});
    }}
    """
    try:
        raw_fields = await page.evaluate(script, scope_selector)
        return [
            FormField(
                selector=str(item["selector"]),
                label=str(item.get("label") or ""),
                field_type=str(item.get("field_type") or "text").lower(),
                options=[
                    {"label": str(option.get("label") or ""), "value": str(option.get("value") or "")}
                    for option in item.get("options", [])
                ],
            )
            for item in raw_fields
        ]
    except Exception as e:
        logger.warning("extract_form_fields failed: %s", e)
        return []


async def _record_unanswered(
    db: AsyncSession,
    application_id: UUID,
    candidate_id: UUID,
    domain: str,
    field: FormField,
) -> None:
    try:
        existing = (await db.execute(
            select(UnansweredQuestion).where(
                UnansweredQuestion.application_id == application_id,
                UnansweredQuestion.field_label == field.label,
            )
        )).scalar_one_or_none()
        if existing:
            return
        db.add(
            UnansweredQuestion(
                application_id=application_id,
                candidate_id=candidate_id,
                domain=domain,
                field_label=field.label,
                field_type=field.field_type,
                options=[option["label"] for option in field.options] or None,
            )
        )
    except Exception as e:
        logger.warning("_record_unanswered failed: %s", e)


def _resume_path(candidate: Candidate) -> str | None:
    try:
        value = getattr(candidate, "resume_path", None)
        if not value:
            return None
        path = Path(str(value))
        if not path.is_absolute():
            path = ROOT_DIR / path
        return str(path.resolve())
    except Exception as e:
        logger.warning("_resume_path failed: %s", e)
        return None


async def _field_has_value(page: object, field: FormField) -> bool:
    try:
        current_value = await page.input_value(field.selector)
        if current_value.strip() != "":
            return True
    except Exception:
        pass
    return False


async def upload_resume_file(
    page: object,
    candidate: Candidate,
    log_cb: LogCallback | None = None,
) -> bool:
    resume_path = _resume_path(candidate)
    if not resume_path:
        return False
    if not Path(resume_path).exists():
        if log_cb:
            await log_cb(f"Resume file not found: {resume_path}")
        return False

    file_inputs = await page.query_selector_all("input[type=file]")
    for file_input in file_inputs:
        try:
            await file_input.set_input_files(resume_path)
            return True
        except Exception:
            continue

    drop_zone = await page.evaluate_handle(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            return style.visibility !== "hidden"
              && style.display !== "none"
              && el.getClientRects().length > 0;
          };
          return Array.from(document.querySelectorAll("div, section, label"))
            .filter(visible)
            .find((el) => /drop|upload|resume/i.test(el.innerText || el.textContent || el.getAttribute("aria-label") || ""))
            || null;
        }
        """
    )
    drop_element = drop_zone.as_element()
    if drop_element:
        hidden_input = await drop_element.evaluate_handle(
            """
            el => {
              let root = el;
              for (let i = 0; i < 5 && root; i += 1) {
                const input = root.querySelector?.("input[type=file]");
                if (input) return input;
                root = root.parentElement;
              }
              return document.querySelector("input[type=file]");
            }
            """
        )
        hidden_file_input = hidden_input.as_element()
        if hidden_file_input:
            try:
                await page.evaluate("el => { el.style.display='block'; }", hidden_file_input)
                await hidden_file_input.set_input_files(resume_path)
                return True
            except Exception:
                pass

        try:
            async with page.expect_file_chooser() as fc_info:
                await drop_element.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(resume_path)
            return True
        except Exception:
            return False

    return False


async def _select_option(locator: object, field: FormField, answer: str) -> None:
    try:
        element = await locator.element_handle()
        if element is None:
            return
        options = await element.evaluate("el => Array.from(el.options).map(o => o.text)")
        best_match = process.extractOne(answer, options or [])
        if best_match:
            await locator.select_option(label=best_match[0])
            return
        values = await element.evaluate("el => Array.from(el.options).map(o => o.value)")
        best_value = process.extractOne(answer, values or [])
        if best_value:
            await locator.select_option(value=best_value[0])
    except Exception as e:
        logger.warning("_select_option failed for [%s]: %s", field.label, e)


async def _fill_radio(page: object, field: FormField, answer: str) -> bool:
    try:
        element = await page.query_selector(field.selector)
        if element is None:
            return False
        name = await element.get_attribute("name")
        radios = await page.query_selector_all(f"input[type=radio][name='{name}']") if name else [element]
        for radio in radios:
            label = await radio.evaluate(
                "el => el.closest('label')?.innerText || document.querySelector(`label[for='${el.id}']`)?.innerText || ''"
            )
            if answer.lower() in label.lower() or label.lower() in answer.lower():
                await safe_click(page, radio)
                return True
        return False
    except Exception as e:
        logger.warning("_fill_radio failed for [%s]: %s", field.label, e)
        return False


async def fill_field(
    page: object,
    field: FormField,
    answer: str,
    candidate: Candidate,
) -> bool:
    try:
        locator = page.locator(field.selector).first
        if await locator.count() == 0:
            return False
        if await _field_has_value(page, field):
            return False
        if field.field_type in {"text", "email", "tel", "number", "url", "textarea", "search"}:
            await locator.fill("")
            await locator.type(answer, delay=70)
            return True
        if field.field_type == "select":
            await _select_option(locator, field, answer)
            return True
        if field.field_type == "radio":
            return await _fill_radio(page, field, answer)
        if field.field_type == "checkbox":
            element = await locator.element_handle()
            if element is None:
                return False
            is_checked = await element.is_checked()
            if not is_checked:
                await safe_click(page, element)
                return True
            return False
        if field.field_type == "file":
            return await upload_resume_file(page, candidate)
        return False
    except Exception as e:
        logger.warning("fill_field failed for [%s]: %s", field.label, e)
        return False


_MAX_FILL_RESCAN_ROUNDS = 4


async def fill_visible_fields(
    page: object,
    candidate: Candidate,
    db: AsyncSession,
    application_id: UUID | str,
    *,
    scope_selector: str | None = None,
    step: int | None = None,
    log_cb: LogCallback | None = None,
) -> int:
    app_id = UUID(str(application_id))
    domain = _domain(getattr(page, "url", ""))
    filled_count = 0
    if await upload_resume_file(page, candidate, log_cb):
        filled_count += 1

    seen_selectors: set[str] = set()

    for _round in range(_MAX_FILL_RESCAN_ROUNDS):
        fields = await extract_form_fields(page, scope_selector)
        new_fields = [f for f in fields if f.selector not in seen_selectors]
        if not new_fields:
            break

        needs_rescan = False
        for field in new_fields:
            seen_selectors.add(field.selector)
            answer = resolve_candidate_answer(candidate, field.label)
            if answer is None and field.field_type == "checkbox" and any(
                term in _norm(field.label) for term in ("agree", "consent", "privacy", "terms")
            ):
                answer = "Yes"
            if answer is None and field.field_type == "file":
                answer = _resume_path(candidate)
            if answer is None:
                await _record_unanswered(db, app_id, candidate.id, domain, field)
                continue
            if log_cb:
                prefix = f"Step {step}: " if step is not None else ""
                await log_cb(f"{prefix}filling field [{field.label}] with [{answer}]")
            try:
                if await fill_field(page, field, str(answer), candidate):
                    filled_count += 1
                    if field.field_type in {"select", "radio"}:
                        # Select/radio can reveal hidden conditional fields; rescan after a short wait
                        needs_rescan = True
                        await asyncio.sleep(0.8)
            except Exception:
                await _record_unanswered(db, app_id, candidate.id, domain, field)

        if not needs_rescan:
            break

    await db.commit()
    return filled_count


async def _submit_button(page: object, scope_selector: str | None = None) -> object | None:
    script = """
    (scopeSelector) => {
      const root = scopeSelector ? document.querySelector(scopeSelector) : document;
      if (!root) return null;
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getClientRects().length > 0;
      };
      const preferred = /(submit|next|continue|apply|send)/i;
      return Array.from(root.querySelectorAll('button, input[type="submit"], a, [role="button"]'))
        .filter(visible)
        .find((el) => preferred.test(el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || ""))
        || null;
    }
    """
    try:
        handle = await page.evaluate_handle(script, scope_selector)
        return handle.as_element()
    except Exception as e:
        logger.warning("_submit_button failed: %s", e)
        return None


async def fill_and_submit(
    page: object,
    candidate: Candidate,
    db: AsyncSession,
    application_id: UUID | str,
    *,
    step: int | None = None,
    log_cb: LogCallback | None = None,
) -> bool:
    try:
        await fill_visible_fields(page, candidate, db, application_id, step=step, log_cb=log_cb)
        button = await _submit_button(page)
        if not button:
            return False
        if log_cb:
            prefix = f"Step {step}: " if step is not None else ""
            await log_cb(f"{prefix}clicking [submit/next/continue]")
        await safe_click(page, button)
        await asyncio.sleep(3)
        text = (await page.locator("body").inner_text(timeout=3000)).lower()
        return any(
            phrase in text
            for phrase in (
                "thank you for applying",
                "application submitted",
                "successfully applied",
                "application received",
                "application complete",
                "you have applied",
            )
        )
    except Exception as e:
        logger.warning("fill_and_submit failed: %s", e)
        return False