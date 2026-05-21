from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import UUID

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engine import anti_ban
from backend.engine.page_inspector import inspect_page as _inspector_inspect_page
from backend.engine.platform_adapters import is_third_party_apply_button
from backend.models import Application, ApplicationScreenshot, Candidate, FormAnswer, UnansweredQuestion


MAX_VISION_STEPS = 15
LogCallback = Callable[[str, str], Awaitable[None]]
DetectorCallback = Callable[[object], Awaitable[bool]]

VISION_PROMPT_TEMPLATE = """
You are a job application form assistant filling forms exactly like a human would.
Analyze this screenshot AND the HTML summary provided.
Return ONLY valid JSON, no markdown, no explanation.

Return this exact structure:
{
  "page_type": "form|confirmation|login|signup|captcha|otp|error|dead|upload|resume_parse|unknown",
  "page_description": "one sentence of what you see",
  "fields": [
    {
      "label": "visible label text",
      "field_type": "text|email|phone|select|multiselect|checkbox|radio|date|number|textarea|file|otp",
      "placeholder": "placeholder if visible",
      "input_name": "the name attribute of the input if visible in HTML",
      "input_id": "the id attribute of the input if visible in HTML",
      "current_value": "what is already filled in if anything",
      "value_to_fill": "what should be filled based on candidate profile",
      "options_available": ["list of visible dropdown options if select type"],
      "selector_hints": ["hint1", "hint2"],
      "should_skip": false,
      "skip_reason": "why skipped if should_skip is true"
    }
  ],
  "next_action": {
    "type": "click|select|upload|wait|stop|otp_wait",
    "target_text": "exact text of button/link to click",
    "is_final_submit": false
  },
  "confidence": "low|medium|high",
  "stop_reason": null
}

Candidate profile:
{candidate_profile_json}

HTML summary of current page:
{html_summary}

RULES - follow every rule exactly:

GENERAL:
- Only include fields that are visible, empty, or wrong
- Fill fields in the order they appear on screen top to bottom
- Use candidate_profile.saved_answers for manual answers the user has already taught you, especially reference fields
- Never fill fields that say "optional" unless you have the value
- If a field is already correctly filled set should_skip to true
- If a field's current_value already matches value_to_fill exactly, set should_skip to true

NAME FIELDS:
- Split candidate name into first and last for separate fields
- If single full name field use complete name

WORK AUTHORIZATION / SPONSORSHIP / RELOCATION:
- "Are you authorized to work?" -> yes
- "Do you require sponsorship?" -> no
- "Are you willing to relocate?" -> use candidate profile preference
- "Are you 18 or older?" -> yes
- All yes/no radio groups -> pick the radio button, not just type text

SALARY:
- If salary expectation field exists -> use candidate profile expected_salary
- If no expected_salary in profile -> skip the field
- Never enter 0 or leave as 0

DROPDOWNS:
- Always pick the closest matching option from options_available
- "How did you hear about us" -> LinkedIn
- Country dropdowns -> match candidate profile location country
- State/Province -> match candidate profile location

DATE FIELDS:
- Available start date -> use "Immediately" or 2 weeks from today
- Date format -> match what the placeholder shows (MM/DD/YYYY etc)

COVER LETTER / ADDITIONAL INFO:
- If cover letter textarea exists and is empty -> generate 2 sentence cover letter using candidate name, desired title, and top 3 skills
- "Additional information" or "Tell us about yourself" -> use candidate summary if available

RESUME UPLOAD:
- If file upload field exists -> set field_type to file
- If page is ONLY asking for resume upload -> set page_type to upload
- If page shows "parsing your resume" or spinner -> set page_type to resume_parse and next_action type to wait

OTP / EMAIL VERIFICATION:
- If page asks for verification code or OTP -> set page_type to otp
- Set next_action type to otp_wait
- The field_type for the code input should be otp

LOGIN / SIGNUP:
- If page asks to sign in or create account -> set page_type to login or signup
- Include email and password fields in fields array
- Set should_skip to false for both

CAPTCHA:
- Any CAPTCHA visible -> set page_type to captcha immediately
- Do not attempt to fill any fields on captcha pages
- "Let's confirm you are human" -> page_type captcha
- "Complete the security check" -> page_type captcha
- "Choose all the X" (image challenge) -> page_type captcha
- "I'm not a robot" checkbox -> page_type captcha
- hCaptcha, reCAPTCHA, Arkose, or any image selection challenge -> page_type captcha

CONFIRMATION:
- "Application submitted", "Thank you for applying", "We received your application" -> page_type confirmation
- "Application complete", "You have successfully applied" -> page_type confirmation

MANUAL APPLY VS AUTOMATIC:
- If page offers choice between manual apply and automatic/autofill -> always choose manual
- If page offers "Apply with LinkedIn" or "Apply with Indeed" -> skip those, look for manual form
- On Workday, never click header navigation such as "Search and Apply"; use the job Apply button or the "Apply Manually" choice.

CONFIDENCE:
- high -> you can clearly see the form and know what to fill
- medium -> some fields are ambiguous but you made best guess
- low -> page is unclear, something unexpected, or you are not sure what step this is
- If low -> explain exactly why in stop_reason

PHONE NUMBER RULES — follow exactly:
- There are always exactly 2 separate phone components: country code and local number
- Country code field: fill with +91 for India, +1 for USA, matching candidate country
- Local number field: fill with digits only, no + sign, no country code prefix
- If you already filled country code by selecting from dropdown, do NOT put +91 in the number field
- The number field should contain ONLY the 10-digit local number
- Never put the same value in both country code and phone number fields
- If you see a field already has +91 or a country code filled, mark it should_skip: true
- Return them as TWO separate field objects with different labels:
  Field 1: label="country_code", field_type="phone", value_to_fill="+91"
  Field 2: label="phone_number", field_type="phone", value_to_fill="9876543210" (digits only)

DAYFORCE / CERIDIAN RULES (jobs.dayforcehcm.com):
- "Country dialing code" is a combobox dropdown — field_type should be "select", value_to_fill should be just "+91" (for India) or "+1" (for USA)
- "Home Phone Number" and "Mobile Phone Number" are plain text inputs — fill with LOCAL digits only, no + prefix, no country code
- "Country" field is a combobox dropdown — type country name like "India" to search and select
- "State/Province" field is a combobox dropdown — type state name to search and select
- Return country dialing code and phone number as TWO separate fields following PHONE NUMBER RULES
- Privacy consent checkbox must be checked before the form loads — handled automatically

WORKDAY RULES:
- Workday forms always have multiple sections: My Information, My Experience, Application Questions
- Each section has a Save and Continue or Next button at the bottom — click it to advance
- Country field on Workday is a searchable dropdown — type the country name and select from results
- Phone on Workday has a separate country phone code dropdown and a number field — follow PHONE NUMBER RULES above
- Zip/Postal code on Workday must match the country selected — for India use 6-digit pincode from candidate profile, for USA use 5-digit zip
- Use candidate_profile.postal_code for postal/zip code fields
- If you see a "Please complete all required fields" banner — look for red-outlined fields and fill them
- Workday file upload uses data-automation-id="file-upload-input-ref"
- Submit button on the final Review page is labeled "Submit" — set is_final_submit: true only for that button
- Never set is_final_submit: true for "Save and Continue" or "Next" buttons

INSPECTOR FIELDS (when present below the HTML summary):
- The FORM FIELDS section lists every field the DOM inspector detected with exact id and selector
- ALWAYS use the "id" value from the inspector as input_id — never guess or invent an id
- For controlKind "combobox_button" or "select2_button": set field_type to "select"
- For controlKind "native_select": set field_type to "select"
- For controlKind "input" where label contains phone/mobile/cell: set field_type to "phone"
- For controlKind "input" where label contains email: set field_type to "email"
- For controlKind "input" otherwise: set field_type to "text"
- Only return fields that appear in the inspector list — do not invent fields not listed there"""


def _build_inspector_field_summary(page_data: dict) -> str:
    """Compact JSON of inspector-detected fields to include in the Gemini prompt."""
    try:
        fields = page_data.get("fields", [])
        if not fields:
            return ""
        concise = []
        for f in fields:
            if f.get("isHidden"):
                continue
            selector = str(f.get("selector") or "")
            input_id = selector[1:] if selector.startswith("#") else ""
            entry: dict[str, Any] = {
                "label": f.get("label") or "",
                "id": input_id,
                "selector": selector,
                "type": f.get("type") or "",
                "controlKind": f.get("controlKind") or "",
                "current_value": str(f.get("value") or f.get("selectedText") or ""),
                "required": bool(f.get("required")),
            }
            opts = f.get("options") or []
            if opts:
                entry["options"] = opts[:8]
            concise.append(entry)
        return json.dumps(concise, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        logger.warning("_build_inspector_field_summary failed: %s", e)
        return ""


async def _extract_html_summary(page: object) -> str:
    try:
        return await page.evaluate("""
            () => {
                const elements = Array.from(document.querySelectorAll(
                    'input, select, textarea, button, label, [role="button"], [role="radio"], [role="checkbox"], [role="combobox"], [role="listbox"], h1, h2, h3'
                ));
                return elements.slice(0, 120).map(el => {
                    const tag = el.tagName.toLowerCase();
                    const attrs = ['id','name','type','placeholder','aria-label','role','value','checked']
                        .map(a => el.getAttribute(a) ? `${a}="${el.getAttribute(a)}"` : '')
                        .filter(Boolean).join(' ');
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 60);
                    return `<${tag} ${attrs}>${text}</${tag}>`;
                }).join('\\n');
            }
        """)
    except Exception:
        return ""


async def _safe_inspect_page(page: object) -> dict:
    try:
        return await _inspector_inspect_page(page)
    except Exception:
        return {}


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip() or default)
    except ValueError:
        return default


class ExternalApplierConfig(BaseModel):
    gcp_project_id: str
    gcp_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"
    apply_email: str = ""
    gmail_credentials_path: str = ""
    gmail_token_path: str = ""
    otp_wait_seconds: int = 120

    @classmethod
    def from_env(cls) -> ExternalApplierConfig | None:
        enabled = str(os.getenv("ENABLE_VERTEX_VISION_APPLIER") or "").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return None
        project_id = (
            os.getenv("GCP_PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GCLOUD_PROJECT")
            or ""
        ).strip()
        if not project_id:
            return None
        return cls(
            gcp_project_id=project_id,
            gcp_location=(os.getenv("GCP_LOCATION") or "us-central1").strip() or "us-central1",
            gemini_model=(os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip() or "gemini-2.5-flash",
            apply_email=(os.getenv("APPLY_EMAIL") or "").strip(),
            gmail_credentials_path=(os.getenv("GMAIL_CREDENTIALS_JSON") or "").strip(),
            gmail_token_path=(os.getenv("GMAIL_TOKEN_JSON") or "").strip(),
            otp_wait_seconds=_env_int("OTP_WAIT_SECONDS", 120),
        )


class VisionField(BaseModel):
    label: str = ""
    field_type: str = "text"
    placeholder: str = ""
    current_value: str = ""
    value_to_fill: str = ""
    options_available: list[str] = Field(default_factory=list)
    selector_hints: list[str] = Field(default_factory=list)
    should_skip: bool = False
    input_name: str = ""
    input_id: str = ""

    @field_validator(
        "label",
        "field_type",
        "placeholder",
        "current_value",
        "value_to_fill",
        "input_name",
        "input_id",
        mode="before",
    )
    @classmethod
    def _coerce_string(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value)

    @field_validator("options_available", "selector_hints", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if item is not None]
        return [str(value)]


class VisionNextAction(BaseModel):
    type: str = "wait"
    target_text: str = ""
    is_final_submit: bool = False

    @field_validator("type", "target_text", mode="before")
    @classmethod
    def _coerce_string(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value)


class VisionResult(BaseModel):
    page_type: str = "unknown"
    page_description: str = ""
    fields: list[VisionField] = Field(default_factory=list)
    next_action: VisionNextAction = Field(default_factory=VisionNextAction)
    confidence: str = "low"
    stop_reason: str | None = None

    @field_validator("page_type", "page_description", "confidence", mode="before")
    @classmethod
    def _coerce_string(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value)

    @field_validator("stop_reason", mode="before")
    @classmethod
    def _coerce_optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value)


def _candidate_profile(candidate: Candidate) -> dict[str, Any]:
    try:
        resume_path = str(getattr(candidate, "resume_path", "") or "")
        if resume_path:
            path = Path(resume_path)
            if path.exists():
                resume_path = str(path.resolve())
        extra = candidate.extra_answers or {}
        postal_code = (
            str(extra.get("postal_code") or extra.get("pincode") or extra.get("zip_code") or extra.get("zipcode") or "").strip()
        )
        country = str(extra.get("country") or "").strip()
        if not country and candidate.location:
            parts = [p.strip() for p in str(candidate.location).split(",")]
            if len(parts) > 1:
                country = parts[-1]
        return {
            "name": candidate.name,
            "email": candidate.email,
            "phone": candidate.phone or "",
            "location": candidate.location or "",
            "country": country,
            "postal_code": postal_code,
            "experience_years": candidate.experience_years or 0,
            "skills": candidate.skills or [],
            "desired_titles": candidate.desired_titles or [],
            "linkedin_url": candidate.linkedin_url or "",
            "portfolio_url": candidate.portfolio_url or "",
            "extra_answers": extra,
            "resume_path": resume_path,
        }
    except Exception as e:
        logger.warning("_candidate_profile failed: %s", e)
        return {"name": candidate.name, "email": candidate.email}


async def _saved_answers(db: AsyncSession, candidate: Candidate, page_url: str) -> dict[str, str]:
    try:
        domain = anti_ban.domain_from_url(page_url)
        result = await db.execute(
            select(FormAnswer)
            .where(FormAnswer.candidate_id == candidate.id)
            .order_by(FormAnswer.created_at.desc())
        )
        rows = list(result.scalars().all())
        priority_rows = sorted(
            rows,
            key=lambda row: 0 if domain and row.domain == domain else 1 if not row.domain else 2,
        )
        answers: dict[str, str] = {}
        domain_answers: dict[str, str] = {}
        reusable_answers: dict[str, str] = {}

        def is_code_only_generic_phone_answer(label: str, value: str) -> bool:
            normalized_label = " ".join(label.lower().replace("_", " ").replace("-", " ").split())
            if not any(token in normalized_label for token in ("phone", "mobile", "cell", "telephone")):
                return False
            if any(token in normalized_label for token in ("country phone code", "phone code", "dialing code", "calling code", "phone type", "phone device")):
                return False
            return bool(re.search(r"\+\d{1,4}", value)) and len(re.sub(r"\D+", "", value)) <= 4

        for row in priority_rows:
            label = str(row.question_text or "").strip()
            value = str(row.answer or "").strip()
            if not label or not value:
                continue
            if is_code_only_generic_phone_answer(label, value):
                continue
            answers.setdefault(label, value)
            if domain and row.domain == domain:
                domain_answers.setdefault(label, value)
            reusable_answers.setdefault(label, value)

        answers.update(reusable_answers)
        answers.update(domain_answers)
        return answers
    except Exception as e:
        logger.warning("_saved_answers failed: %s", e)
        return {}


def _reference_stop_detected(result: VisionResult, reason: str | None, html_summary: str) -> bool:
    try:
        combined = " ".join(
            [
                str(reason or ""),
                str(result.stop_reason or ""),
                str(result.page_description or ""),
                str(html_summary or "")[:4000],
            ]
        ).lower()
        return "reference" in combined
    except Exception as e:
        logger.warning("_reference_stop_detected failed: %s", e)
        return False


def _manual_reference_label(label: str) -> str:
    cleaned = " ".join(str(label or "").replace("*", " ").split()).strip()
    if not cleaned:
        return ""
    if "reference" in cleaned.lower():
        return cleaned
    return f"Reference {cleaned}"


def _manual_reference_field_type(field: VisionField) -> str:
    normalized = str(field.field_type or "").lower()
    label = str(field.label or "").lower()
    if "email" in normalized or "email" in label:
        return "email"
    if "phone" in normalized or "phone" in label:
        return "phone"
    if normalized in {"select", "multiselect", "radio", "checkbox", "date", "number", "textarea"}:
        return normalized
    return "text"


async def _save_reference_manual_questions(
    db: AsyncSession,
    page: object,
    candidate: Candidate,
    application_id: UUID,
    result: VisionResult,
    reason: str | None,
    html_summary: str,
) -> int:
    if not _reference_stop_detected(result, reason, html_summary):
        return 0

    skip_tokens = {
        "attachment",
        "cover letter",
        "additional documents",
        "prefix",
        "middle name",
        "suffix",
        "fax",
        "county",
        "preferred contact method",
        "address line 2",
    }
    questions: list[tuple[str, str, list[str] | None]] = []
    seen: set[str] = set()
    for field in result.fields:
        label = str(field.label or "").strip()
        if not label or str(field.value_to_fill or "").strip():
            continue
        normalized = " ".join(label.lower().replace("_", " ").replace("-", " ").split())
        if any(token in normalized for token in skip_tokens):
            continue
        if not ("reference" in normalized or normalized in {"first name", "last name", "email", "phone", "phone number", "mobile phone", "business phone", "relationship", "company", "title"}):
            continue
        manual_label = _manual_reference_label(label)
        key = manual_label.lower()
        if not manual_label or key in seen:
            continue
        seen.add(key)
        options = [str(option).strip() for option in field.options_available if str(option).strip()]
        questions.append((manual_label, _manual_reference_field_type(field), options or None))

    if not questions:
        questions = [
            ("Reference First Name", "text", None),
            ("Reference Last Name", "text", None),
            ("Reference Email", "email", None),
            ("Reference Phone", "phone", None),
            ("Reference Relationship", "text", None),
            ("Reference Company", "text", None),
        ]

    domain = anti_ban.domain_from_url(getattr(page, "url", ""))
    inserted = 0
    for label, field_type, options in questions:
        existing_result = await db.execute(
            select(UnansweredQuestion)
            .where(UnansweredQuestion.application_id == application_id)
            .where(UnansweredQuestion.candidate_id == candidate.id)
            .where(UnansweredQuestion.domain == domain)
            .where(UnansweredQuestion.field_label == label)
            .where(UnansweredQuestion.field_type == field_type)
            .where(UnansweredQuestion.answered_at.is_(None))
        )
        if existing_result.scalars().first() is not None:
            continue
        db.add(
            UnansweredQuestion(
                application_id=application_id,
                candidate_id=candidate.id,
                domain=domain,
                field_label=label,
                field_type=field_type,
                options=options,
            )
        )
        inserted += 1
    if inserted:
        await db.commit()
    return inserted


def _escape_css_fragment(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').strip()


def _normalize_hint(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truthy_value(value: str) -> bool:
    return str(value or "").strip().lower() in {"true", "yes", "y", "1", "checked", "select", "click"}


def _looks_like_resume_upload_field(field: VisionField) -> bool:
    field_type = str(field.field_type or "").strip().lower()
    if field_type == "file":
        return True
    text = " ".join(
        [
            str(field.label or ""),
            str(field.placeholder or ""),
            str(field.input_name or ""),
            str(field.input_id or ""),
            " ".join(str(hint or "") for hint in field.selector_hints),
        ]
    ).lower()
    return bool(re.search(r"\b(resume|cv|curriculum vitae|attachment|attach|upload|file)\b", text))


async def _set_application_error(db: AsyncSession, application_id: UUID, reason: str) -> None:
    application = await db.get(Application, application_id)
    if application is None:
        return
    application.last_error = reason
    await db.commit()


async def _check_field_validation_error(page: object, field_label: str) -> str | None:
    try:
        error_selectors = [
            '[aria-describedby*="error" i]',
            '.error-message',
            '.field-error',
            '[class*="error" i]',
            '[class*="invalid" i]',
            '[role="alert"]',
        ]
        for selector in error_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    error_text = str(await locator.inner_text()).strip()
                    if error_text:
                        return f"{field_label}: {error_text}"
            except Exception:
                continue
    except Exception:
        pass
    return None


def _extract_json_payload(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("Vertex AI returned an empty response")
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    if fenced.startswith("{") and fenced.endswith("}"):
        return fenced
    start = fenced.find("{")
    end = fenced.rfind("}")
    if start >= 0 and end > start:
        return fenced[start : end + 1]
    raise ValueError(f"Vertex AI did not return JSON: {raw[:200]}")


async def call_gemini_vision(
    screenshot_bytes: bytes,
    candidate_profile: dict[str, Any],
    config: ExternalApplierConfig,
    *,
    model_name: str | None = None,
    html_summary: str = "",
) -> VisionResult:
    try:
        import google.auth
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig, Part
    except Exception as exc:
        raise RuntimeError("Vertex AI SDK is not installed") from exc

    credentials, detected_project = google.auth.default()
    project_id = config.gcp_project_id or str(detected_project or "").strip()
    if not project_id:
        raise RuntimeError("GCP project id is required for Vertex AI vision")

    vertexai.init(project=project_id, location=config.gcp_location, credentials=credentials)
    prompt = (
        VISION_PROMPT_TEMPLATE
        .replace("{candidate_profile_json}", json.dumps(candidate_profile, ensure_ascii=False, indent=2))
        .replace("{html_summary}", html_summary or "Not available")
    )
    encoded = base64.b64encode(screenshot_bytes).decode("ascii")
    image_part = Part.from_data(data=base64.b64decode(encoded), mime_type="image/png")
    model = GenerativeModel(model_name or config.gemini_model)
    response = await asyncio.to_thread(
        model.generate_content,
        [prompt, image_part],
        generation_config=GenerationConfig(
            temperature=0.1,
            top_p=0.8,
            response_mime_type="application/json",
        ),
    )
    response_text = getattr(response, "text", "") or ""
    payload = json.loads(_extract_json_payload(response_text))
    return VisionResult.model_validate(payload)


async def _resolve_locator(page: object, hints: list[str]) -> tuple[object | None, str | None]:
    async def _search_context(context: object) -> tuple[object | None, str | None]:
        for raw_hint in hints:
            hint = _normalize_hint(raw_hint)
            if not hint:
                continue
            css_hint = _escape_css_fragment(hint)
            selectors = [
                f'input[placeholder*="{css_hint}" i]',
                f'textarea[placeholder*="{css_hint}" i]',
                f'input[name*="{css_hint}" i]',
                f'textarea[name*="{css_hint}" i]',
                f'select[name*="{css_hint}" i]',
                f'input[aria-label*="{css_hint}" i]',
                f'textarea[aria-label*="{css_hint}" i]',
                f'[id*="{css_hint}" i]',
            ]
            for selector in selectors:
                try:
                    locator = context.locator(selector).first
                    if await locator.count() and await locator.is_visible():
                        return locator, selector
                except Exception:
                    continue
            try:
                label = context.locator("label").filter(has_text=re.compile(re.escape(hint), re.IGNORECASE)).first
                if await label.count() and await label.is_visible():
                    # Try checkbox/radio first
                    control = label.locator("input[type='checkbox'], input[type='radio']").first
                    if await control.count():
                        return control, f"label:{hint}"
                    # Try any input/select/textarea inside the label
                    control = label.locator("input, select, textarea").first
                    if await control.count():
                        return control, f"label_input:{hint}"
                    # Try label[for] -> find by ID
                    try:
                        for_id = await label.get_attribute("for")
                        if for_id:
                            escaped = _escape_css_fragment(for_id)
                            control = context.locator(f'[id="{escaped}"]').first
                            if await control.count() and await control.is_visible():
                                return control, f"label_for:{for_id}"
                    except Exception:
                        pass
            except Exception:
                pass
        return None, None

    locator, selector = await _search_context(page)
    if locator is not None:
        return locator, selector

    # Try inside iframes after the top-level page.
    try:
        frames = page.frames
        for frame in frames[1:]:  # skip main frame
            locator, selector = await _search_context(frame)
            if locator is not None:
                return locator, selector
    except Exception:
        pass
    return None, None


async def _handle_blocking_agreement_modal(page: object, log_cb: LogCallback) -> bool:
    handled = await page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none'
              && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
          };
          const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
          const isChecked = (el) => Boolean(el.checked) || el.getAttribute('aria-checked') === 'true';
          const markChecked = (el) => {
            if ('checked' in el) el.checked = true;
            el.setAttribute('aria-checked', 'true');
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          };
          const agreement = /(agree|agreement|consent|terms|privacy|policy|acknowledge|certify|understand)/i;
          const modalCandidates = Array.from(document.querySelectorAll(
            '[role="dialog"], dialog, [aria-modal="true"], .modal, [class*="modal" i], [class*="dialog" i], [class*="overlay" i]'
          )).filter(visible);
          const fixedCandidates = Array.from(document.body.querySelectorAll('body > *')).filter((el) => {
            if (!visible(el)) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return ['fixed', 'sticky'].includes(style.position)
              && rect.width >= window.innerWidth * 0.35
              && rect.height >= window.innerHeight * 0.15;
          });
          const scopes = [...modalCandidates, ...fixedCandidates].filter((el, index, all) => all.indexOf(el) === index);

          for (const scope of scopes) {
            const scopeText = textOf(scope);
            const checkboxes = Array.from(scope.querySelectorAll('input[type="checkbox"], [role="checkbox"], [aria-checked]')).filter(visible);
            const agreementCheckbox = checkboxes.find((el) => {
              const label = textOf(el.closest('label'))
                || (el.id ? textOf(document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) : '')
                || textOf(el.closest('div, p, section, fieldset'))
                || el.getAttribute('aria-label')
                || scopeText;
              return agreement.test(label);
            });
            if (!agreementCheckbox) continue;
            if (!isChecked(agreementCheckbox)) {
              agreementCheckbox.click();
              if (!isChecked(agreementCheckbox)) markChecked(agreementCheckbox);
            }

            const buttons = Array.from(scope.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"], a'))
              .filter((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
            const primary = buttons
              .map((el) => {
                const text = textOf(el) || el.getAttribute('value') || el.getAttribute('aria-label') || '';
                const score =
                  /^(continue|next|ok|accept|agree|submit|save|done)$/i.test(text) ? 0 :
                  /(continue|next|ok|accept|agree|submit|save|done)/i.test(text) ? 10 :
                  /cancel|close|back|decline/i.test(text) ? 1000 :
                  100;
                return { el, score };
              })
              .sort((a, b) => a.score - b.score)[0];
            if (primary && primary.score < 1000) {
              primary.el.click();
            }
            return true;
          }
          return false;
        }
        """
    )
    if handled:
        await anti_ban.random_delay(500, 900)
        try:
            await page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                      && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
                  };
                  const textOf = (el) => (el?.innerText || el?.textContent || el?.value || '').replace(/\\s+/g, ' ').trim();
                  const scopes = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], .ant-modal'))
                    .filter(visible);
                  for (const scope of scopes) {
                    const buttons = Array.from(scope.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]'))
                      .filter((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                    const primary = buttons
                      .map((el) => {
                        const text = textOf(el);
                        const score =
                          /^(save|continue|next|ok|accept|agree|submit|done)$/i.test(text) ? 0 :
                          /(save|continue|next|ok|accept|agree|submit|done)/i.test(text) ? 10 :
                          /cancel|close|back|decline/i.test(text) ? 1000 :
                          100;
                        return { el, score };
                      })
                      .sort((a, b) => a.score - b.score)[0];
                    if (primary && primary.score < 1000) {
                      primary.el.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
        except Exception:
            pass
        await log_cb("info", "Vision handled blocking agreement modal before filling background form fields")
        await anti_ban.random_delay(800, 1500)
        return True
    return False

async def _clear_and_type(page: object, locator: object, selector: str, value: str) -> None:
    try:
        await locator.click(force=True, timeout=3000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        try:
            await locator.fill("")
        except Exception:
            pass
    await anti_ban.human_type(page, selector, value)


async def _fill_select(locator: object, value: str) -> bool:
    try:
        await locator.select_option(label=value, timeout=3000)
        return True
    except Exception:
        pass
    try:
        await locator.select_option(value=value, timeout=3000)
        return True
    except Exception:
        return False


async def _fill_custom_select(page: object, locator: object, value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    try:
        await locator.click(force=True, timeout=3000)
        await anti_ban.random_delay(300, 700)
    except Exception:
        return False
    try:
        tag_name = str(await locator.evaluate("(el) => (el.tagName || '').toLowerCase()"))
    except Exception:
        tag_name = ""
    if tag_name in {"input", "textarea"}:
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(value, delay=45)
            await anti_ban.random_delay(500, 900)
        except Exception:
            pass
    try:
        clicked = await page.evaluate(
            """
            (targetText) => {
              const visible = (node) => {
                if (!node) return false;
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const target = normalize(targetText);
              const nodes = Array.from(document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option, .ant-select-item-option,' +
                ' .rc-select-dropdown .rc-select-item-option, .rc-select-item-option, [class*="select-item-option"],' +
                ' [role="option"], [role="listbox"] li,' +
                ' [data-automation-id="menuItem"], [data-automation-id="promptOption"], [data-automation-id="promptLeafNode"]'
              )).filter(visible);
              const textOf = (node) => (
                node.getAttribute('title') || node.getAttribute('data-automation-label') ||
                node.getAttribute('aria-label') || node.getAttribute('data-value') ||
                node.querySelector('[class*="option-content"], [class*="item-content"]')?.innerText ||
                node.innerText || node.textContent || ''
              );
              const match = nodes.find((node) => normalize(textOf(node)) === target)
                || nodes.find((node) => normalize(textOf(node)).includes(target))
                || nodes.find((node) => target.includes(normalize(textOf(node))) && normalize(textOf(node)).length >= 2);
              if (!match) return false;
              const clickTarget = match.closest('[role="option"], .ant-select-item-option, .rc-select-item-option, [class*="select-item-option"], [data-automation-id="menuItem"], li, button') || match;
              clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
              clickTarget.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
              clickTarget.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
            """,
            value,
        )
        if clicked:
            await anti_ban.random_delay(500, 900)
            return True
    except Exception:
        pass
    try:
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")
        await anti_ban.random_delay(500, 900)
        return True
    except Exception:
        return False


async def _visible_click_contexts(page: object) -> list[object]:
    contexts = [page]
    try:
        contexts.extend(frame for frame in page.frames if frame is not page.main_frame)
    except Exception:
        pass
    return contexts


async def _visible_action_texts(page: object, limit: int = 20) -> list[str]:
    texts: list[str] = []
    script = """
        () => {
          const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none'
              && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
          };
          return Array.from(document.querySelectorAll(
            'button, a[href], [role="button"], input[type="button"], input[type="submit"]'
          ))
            .filter(visible)
            .map((el) => normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title')))
            .filter(Boolean)
            .slice(0, 40);
        }
    """
    for context in await _visible_click_contexts(page):
        try:
            for value in await context.evaluate(script):
                if value not in texts:
                    texts.append(value)
                if len(texts) >= limit:
                    return texts
        except Exception:
            continue
    return texts


async def _looks_like_application_form(page: object) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                      && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
                  };
                  const formFields = Array.from(document.querySelectorAll('input, textarea, select')).filter(visible);
                  const bodyText = String(document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
                  return formFields.length >= 3
                    || bodyText.includes('candidate info')
                    || bodyText.includes('personal information')
                    || bodyText.includes('import resume');
                }
                """
            )
        )
    except Exception:
        return False


async def _looks_like_email_verification_page(page: object) -> bool:
    try:
        text = str(
            await page.evaluate(
                "() => String(document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase()"
            )
        )
    except Exception:
        return False
    return any(
        token in text
        for token in (
            "verify your email",
            "verify your account",
            "account verification",
            "email verification",
            "verification email",
            "resend account verification",
        )
    )


async def _looks_like_workday_auth_page(page: object) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                  const host = String(location.hostname || '').toLowerCase();
                  if (!host.includes('myworkdayjobs.com')) return false;
                  const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                      && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
                  };
                  const fields = Array.from(document.querySelectorAll('input, textarea, select')).filter(visible);
                  const fieldText = fields.map((el) => [
                    el.getAttribute('type'), el.getAttribute('name'), el.getAttribute('id'),
                    el.getAttribute('placeholder'), el.getAttribute('aria-label')
                  ].join(' ')).join(' ').toLowerCase();
                  const hasAuthText = /create account|sign\\s*in|log\\s*in|already have an account/.test(text);
                  const hasAuthField = /email|password/.test(fieldText) || /email address|password/.test(text);
                  return hasAuthText && hasAuthField;
                }
                """
            )
        )
    except Exception:
        return False


async def _looks_like_captcha_page(page: object) -> bool:
    try:
        text = str(
            await page.evaluate(
                "() => String(document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase()"
            )
        )
    except Exception:
        return False
    captcha_phrases = (
        "let's confirm you are human",
        "lets confirm you are human",
        "complete the security check",
        "choose all the",
        "select all images",
        "i'm not a robot",
        "im not a robot",
        "verify you are human",
        "human verification",
        "press and hold",
        "solve the captcha",
        "hcaptcha",
        "please verify",
        "security challenge",
    )
    return any(phrase in text for phrase in captcha_phrases)


async def _click_by_text(page: object, text: str, log_cb: LogCallback | None = None) -> bool:
    target = _normalize_hint(text)
    if not target:
        return False
    target_norm = target.lower()
    if is_third_party_apply_button({"text": target_norm}):
        if log_cb is not None:
            await log_cb("warn", f"Vision refused third-party apply option [{target}]")
        return False
    page_url = str(getattr(page, "url", "") or "").lower()
    if "workdayjobs.com" in page_url and target_norm == "search and apply":
        if log_cb is not None:
            await log_cb("warn", "Vision refused Workday header navigation [Search and Apply]")
        return False
    target_pattern = re.compile(re.escape(target), re.IGNORECASE)
    target_words = [word for word in re.findall(r"[a-z0-9]+", target_norm) if len(word) >= 3]

    for context in await _visible_click_contexts(page):
        try:
            selector = await context.evaluate(
                """
                ({ target }) => {
                  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                      && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0
                      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                  };
                  const mark = (el) => {
                    if (!el || !visible(el)) return null;
                    const attr = 'data-cviance-vision-click';
                    const token = el.getAttribute(attr) || `vision-click-${Date.now()}-${Math.random().toString(36).slice(2)}`;
                    el.setAttribute(attr, token);
                    return `[${attr}="${token}"]`;
                  };
                  const host = window.location.hostname.toLowerCase();
                  const isWorkday = host.includes('workdayjobs.com');
                  if (!isWorkday) return null;

                  const bodyText = normalize(document.body?.innerText || '');
                  const thirdPartyApply = (text, href = '', automationId = '') => {
                    const haystack = normalize(`${text} ${href} ${automationId}`);
                    return /applywithlinkedin|\\/awli\\/|linkedin\\.com\\/jobs\\/apply/.test(haystack)
                      || /\\bapply\\s+(with|via|using|through)\\s+(linkedin|linked in|indeed|zip\\s*recruiter|ziprecruiter|glassdoor|seek)\\b/.test(haystack)
                      || /\\b(linkedin|linked in|indeed|zip\\s*recruiter|ziprecruiter|glassdoor|seek)\\s+(easy\\s+)?apply\\b/.test(haystack)
                      || /\\beasy\\s+apply\\b/.test(haystack)
                      || /\\buse\\s+(my\\s+)?(linkedin|linked in|indeed|zip\\s*recruiter|ziprecruiter|glassdoor|seek)\\b/.test(haystack)
                      || /\\bautofill\\s+with\\s+(linkedin|linked in|indeed|zip\\s*recruiter|ziprecruiter|glassdoor|seek)\\b/.test(haystack);
                  };
                  // Structural filter: drop inline-body <a> tags (no data-automation-id,
                  // ancestor text >300 chars).  These are textual hyperlinks like the EEOC
                  // "here" link inside long compliance paragraphs — never action buttons.
                  const isActionButton = (el) => {
                    if ((el.tagName || '').toLowerCase() !== 'a') return true;
                    if (el.hasAttribute('data-automation-id')) return true;
                    let cur = el.parentElement;
                    while (cur && cur !== document.body) {
                      if ((cur.innerText || cur.textContent || '').trim().length > 300) return false;
                      cur = cur.parentElement;
                    }
                    return true;
                  };
                  const nodes = Array.from(document.querySelectorAll(
                    'button, a[href], [role="button"], input[type="button"], input[type="submit"]'
                  )).filter((el) => visible(el) && isActionButton(el));
                  const applyNodeText = (el) => normalize(
                    el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title')
                  );
                  const workdayChoice = bodyText.includes('start your application')
                    || nodes.some((el) => {
                      const text = applyNodeText(el);
                      const automationId = normalize(el.getAttribute('data-automation-id'));
                      return automationId === 'applymanually'
                        || automationId === 'autofillwithresume'
                        || automationId === 'usemylastapplication'
                        || ['apply manually', 'autofill with resume', 'use my last application'].includes(text)
                        || thirdPartyApply(text, el.getAttribute('href') || '', automationId);
                    });
                  if (workdayChoice) {
                    const manual = document.querySelector('[data-automation-id="applyManually"]')
                      || nodes.find((el) => applyNodeText(el) === 'apply manually');
                    const autofill = document.querySelector('[data-automation-id="autofillWithResume"]')
                      || nodes.find((el) => applyNodeText(el) === 'autofill with resume');
                    if (/autofill|auto fill/.test(target) && autofill) {
                      return mark(autofill);
                    }
                    if (manual) return mark(manual);
                  }

                  if (/^(apply|apply now|start application|start your application)$/i.test(target)) {
                    const adventureButton = document.querySelector('[data-automation-id="adventureButton"]');
                    if (adventureButton && visible(adventureButton)) return mark(adventureButton);
                    const safeApplyNodes = nodes
                      .map((el, index) => {
                        const text = applyNodeText(el);
                        const href = el.getAttribute('href') || '';
                        const automationId = normalize(el.getAttribute('data-automation-id'));
                        if (!text || thirdPartyApply(text, href, automationId)) return null;
                        if (/^(search and apply|sign in|close)$/i.test(text)) return null;
                        let score = 0;
                        if (['apply', 'apply now', 'apply manually', 'start application', 'start your application'].includes(text)) score = 100;
                        else if (/\\bapply\\b|start application|start your application/.test(text)) score = 80;
                        if (!score) return null;
                        return { el, score, index };
                      })
                      .filter(Boolean)
                      .sort((a, b) => b.score - a.score || a.index - b.index);
                    if (safeApplyNodes.length) return mark(safeApplyNodes[0].el);
                    if (nodes.some((el) => thirdPartyApply(applyNodeText(el), el.getAttribute('href') || '', normalize(el.getAttribute('data-automation-id'))))) {
                      return '__CVIANCE_BLOCKED_THIRD_PARTY_APPLY__';
                    }
                  }
                  return null;
                }
                """,
                {"target": target_norm},
            )
            if selector:
                if selector == "__CVIANCE_BLOCKED_THIRD_PARTY_APPLY__":
                    if log_cb is not None:
                        await log_cb("warn", f"Vision found only third-party Workday apply options for [{target}]")
                    return False
                await context.locator(selector).first.click(force=True, timeout=3000)
                return True
        except Exception:
            pass
        candidates = [
            context.get_by_role("button", name=target_pattern),
            context.get_by_role("link", name=target_pattern),
            context.get_by_text(target_pattern).locator(
                "xpath=ancestor-or-self::*[self::button or self::a or @role='button' or @type='submit'][1]"
            ),
            context.locator("button, a[href], [role='button'], input[type='submit'], [type='submit']").filter(
                has_text=target_pattern
            ),
        ]
        # Structural sanity check applied to the candidate element before clicking.
        # Rejects inline-body <a> tags (no data-automation-id and inside a >300-char
        # ancestor) — the same rule we apply in inspect_page and _click_button_action.
        _IS_ACTION_BUTTON_JS = """
            (el) => {
              if (!el) return false;
              const tag = (el.tagName || '').toLowerCase();
              if (tag !== 'a') return true;
              if (el.hasAttribute('data-automation-id')) return true;
              let cur = el.parentElement;
              while (cur && cur !== document.body) {
                if ((cur.innerText || cur.textContent || '').trim().length > 300) return false;
                cur = cur.parentElement;
              }
              return true;
            }
        """
        for locator in candidates:
            try:
                if await locator.count():
                    handle = locator.first
                    if await handle.is_visible():
                        try:
                            structurally_safe = await handle.evaluate(_IS_ACTION_BUTTON_JS)
                        except Exception:
                            structurally_safe = True
                        if not structurally_safe:
                            if log_cb is not None:
                                await log_cb(
                                    "warn",
                                    f"Vision rejected inline-body <a> match for target [{target}] (structural filter)",
                                )
                            continue
                        await handle.click(force=True, timeout=3000)
                        return True
            except Exception:
                continue
        try:
            selector = await context.evaluate(
                """
                ({ target, targetWords }) => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                  && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
              };
              const skip = /^(skip to content|skip to main content|skip)$/i;
              // Structural filter: skip any <a> without data-automation-id whose
              // closest ancestor's text length exceeds 300 chars — those are inline
              // hyperlinks inside body copy (e.g. "here" links pointing to eeoc.gov),
              // never action buttons.
              const isActionButton = (el) => {
                if ((el.tagName || '').toLowerCase() !== 'a') return true;
                if (el.hasAttribute('data-automation-id')) return true;
                let cur = el.parentElement;
                while (cur && cur !== document.body) {
                  if ((cur.innerText || cur.textContent || '').trim().length > 300) return false;
                  cur = cur.parentElement;
                }
                return true;
              };
              const nodes = Array.from(document.querySelectorAll(
                'button, a[href], [role="button"], input[type="button"], input[type="submit"]'
              )).filter((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && isActionButton(el));
              const scored = nodes.map((el, index) => {
                const text = normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title'));
                const automationId = normalize(el.getAttribute('data-automation-id'));
                const nav = /^(search and apply|join our talent community!|gdit privacy notice and california privacy notice)$/i;
                if (!text || skip.test(text) || nav.test(text)) return null;
                if (automationId === 'navigationitem search and apply'
                    || automationId === 'navigationitem join our talent community!'
                    || automationId === 'privacylink'
                    || automationId === 'accessibilityskiptomaincontent') return null;
                let score = 0;
                if (text === target) score = 100;
                else if (text.includes(target)) score = 80;
                else if (target.includes(text) && text.length >= 4) score = 60;
                else if (targetWords.length && targetWords.every((word) => text.includes(word))) score = 70;
                if (!score) return null;
                const attr = 'data-cviance-vision-click';
                const token = el.getAttribute(attr) || `vision-click-${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`;
                el.setAttribute(attr, token);
                return { selector: `[${attr}="${token}"]`, score };
              }).filter(Boolean).sort((a, b) => b.score - a.score);
              return scored[0]?.selector || null;
            }
                """,
                {"target": target_norm, "targetWords": target_words},
            )
            if selector:
                await context.locator(selector).first.click(force=True, timeout=3000)
                return True
        except Exception:
            continue
    if log_cb is not None:
        visible_texts = await _visible_action_texts(page)
        if visible_texts:
            await log_cb("warn", f"Vision visible click candidates: {visible_texts}")
    return False


async def _upload_resume(page: object, candidate_profile: dict[str, Any]) -> bool:
    resume_path = str(candidate_profile.get("resume_path") or "").strip()
    if not resume_path or not Path(resume_path).exists():
        return False

    async def _uploaded(context: object) -> bool:
        try:
            return bool(
                await context.evaluate(
                    """
                    () => {
                      const text = String(document.body?.innerText || '').toLowerCase();
                      const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                      return Boolean(
                        inputs.some((el) => el.files && el.files.length > 0)
                        || text.includes('successfully uploaded')
                        || document.querySelector('[data-automation-id="file-upload-successful"], [data-automation-id="file-upload-item-name"]')
                      );
                    }
                    """
                )
            )
        except Exception:
            return False

    contexts = await _visible_click_contexts(page)
    selectors = [
        "input[type='file']",
        "[data-automation-id='file-upload-input-ref']",
        "input[type='file'][accept*='pdf' i]",
        "input[type='file'][name*='resume' i]",
        "input[type='file'][name*='cv' i]",
        "input[type='file'][name*='attach' i]",
        "input[type='file'][id*='resume' i]",
        "input[type='file'][id*='cv' i]",
        "input[type='file'][id*='attach' i]",
    ]
    for context in contexts:
        for selector in selectors:
            try:
                inputs = context.locator(selector)
                count = await inputs.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    await inputs.nth(index).set_input_files(resume_path, timeout=5000)
                    await asyncio.sleep(0.5)
                    if await _uploaded(context):
                        return True
                except Exception:
                    continue

    upload_control_selectors = [
        "#resumeAttachments--attachments",
        "[data-automation-id='select-files']",
        "[data-automation-id='file-upload-drop-zone']",
        "[data-automation-id*='upload' i]",
        "[data-automation-id*='attachment' i]",
        "button:has-text('Upload')",
        "button:has-text('Attach')",
        "button:has-text('Select files')",
        "button:has-text('Choose file')",
        "[role='button']:has-text('Upload')",
        "[role='button']:has-text('Attach')",
        "[role='button']:has-text('Select files')",
        "[role='button']:has-text('Choose file')",
        "text=/upload|attach|select files|choose file|resume|cv/i",
    ]
    for context in contexts:
        for selector in upload_control_selectors:
            try:
                control = context.locator(selector).first
                if not await control.count():
                    continue
                async with page.expect_file_chooser(timeout=5000) as chooser_info:
                    await control.click(force=True, timeout=5000)
                chooser = await chooser_info.value
                await chooser.set_files(resume_path)
                await asyncio.sleep(0.5)
                if await _uploaded(context):
                    return True
            except Exception:
                continue
    return False


async def _apply_field(page: object, field: VisionField, candidate_profile: dict[str, Any], log_cb: LogCallback) -> None:
    field_type = str(field.field_type or "text").lower()
    if _looks_like_resume_upload_field(field):
        uploaded = await _upload_resume(page, candidate_profile)
        if uploaded:
            return
        await log_cb("info", f"Vision skipped file field [{field.label or field.placeholder}] because resume upload was unavailable")
        return

    direct_hints = []
    if field.input_id:
        direct_hints.append(field.input_id)
    if field.input_name:
        direct_hints.append(field.input_name)
    hints = direct_hints + [hint for hint in field.selector_hints if _normalize_hint(hint)]
    if field.label:
        hints.append(field.label)
    if field.placeholder:
        hints.append(field.placeholder)
    locator, selector = await _resolve_locator(page, hints)
    if locator is None or selector is None:
        await log_cb("info", f"Vision skipped field [{field.label or field.placeholder}] because no selector hint matched")
        return

    value = str(field.value_to_fill or "").strip()
    if not value and field.field_type != "file":
        await log_cb("info", f"Vision skipped field [{field.label or field.placeholder}] because no fill value was provided")
        return

    if field_type in {"text", "email", "textarea"}:
        fill_value = value
        if field_type == "text":
            label_lower = (field.label or "").lower()
            id_lower = (field.input_id or "").lower()
            name_lower = (field.input_name or "").lower()
            meta = f"{label_lower} {id_lower} {name_lower}"
            is_phone_number_field = bool(
                re.search(r'\b(phone|mobile|cell|tel)\b', meta)
            ) and not re.search(
                r'dial(ing)?\s*(phone\s*)?code|country\s*(phone\s*|dialing?\s*)?code|phone\s*code|countrycode|dialcode|phonecode',
                meta, re.IGNORECASE,
            )
            if is_phone_number_field and re.match(r'^\+\d{1,4}\s*\d', fill_value):
                fill_value = re.sub(r'^\+\d{1,4}\s*', '', fill_value).strip()
                fill_value = re.sub(r'[^\d]', '', fill_value) or fill_value
        await _clear_and_type(page, locator, selector, fill_value)
        return
    if field_type == "phone":
        label_lower = (field.label or "").lower()
        id_lower = (field.input_id or "").lower()
        name_lower = (field.input_name or "").lower()
        hints_text = " ".join(str(h) for h in field.selector_hints).lower()
        combined_meta = f"{label_lower} {id_lower} {name_lower} {hints_text}"
        is_country_code_field = bool(
            re.search(
                r'dial(ing)?\s*(phone\s*)?code|country\s*(phone\s*|dialing?\s*)?code'
                r'|phone\s*code|countrycode|dialcode|phonecode|dialing\s*code',
                combined_meta, re.IGNORECASE,
            )
        ) or bool(re.match(r'^\+\d{1,4}$', value.strip()))
        if is_country_code_field:
            if await _fill_select(locator, value):
                await anti_ban.random_delay(500, 1000)
                return
            if await _fill_custom_select(page, locator, value):
                await anti_ban.random_delay(500, 1000)
                return
            await _clear_and_type(page, locator, selector, value)
            await anti_ban.random_delay(500, 1000)
            return
        # Number field: strip any country-code prefix, keep local digits only
        local_number = re.sub(r'^\+\d{1,4}\s*', '', value).strip()
        local_number = re.sub(r'[^\d]', '', local_number)
        if not local_number:
            local_number = re.sub(r'[^\d]', '', value)
        await _clear_and_type(page, locator, selector, local_number or value)
        return
    if field_type == "select":
        if await _fill_select(locator, value):
            return
        if await _fill_custom_select(page, locator, value):
            return
        try:
            await locator.click(force=True, timeout=3000)
            await page.get_by_text(value, exact=False).first.click(force=True, timeout=3000)
            return
        except Exception:
            await log_cb("info", f"Vision skipped select [{field.label}] because option [{value}] was not reachable")
            return
    if field_type in {"checkbox", "radio"}:
        if _truthy_value(value):
            try:
                await locator.click(force=True, timeout=3000)
            except Exception:
                await log_cb("info", f"Vision skipped toggle [{field.label}] because click failed")
        return
    if field_type == "file":
        uploaded = await _upload_resume(page, candidate_profile)
        if not uploaded:
            await log_cb("info", f"Vision skipped file field [{field.label}] because resume upload was unavailable")


async def run_vertex_vision_loop(
    page: object,
    candidate: Candidate,
    db: AsyncSession,
    application_id: UUID,
    *,
    otp_fetcher: Callable[[], Awaitable[str | None]] | None = None,
    log_cb: LogCallback,
    done_detector: DetectorCallback,
    followup_detector: DetectorCallback,
) -> str | None:
    config = ExternalApplierConfig.from_env()
    if config is None:
        await log_cb("info", "Vertex vision loop skipped because ENABLE_VERTEX_VISION_APPLIER is not enabled")
        return None

    candidate_profile = _candidate_profile(candidate)
    same_page_count = 0
    last_decision_fingerprint: str | None = None
    same_action_page_count = 0
    last_action_page_key: str | None = None
    same_progress_page_count = 0
    last_progress_page_key: str | None = None
    submitted_application = False

    for step in range(MAX_VISION_STEPS):
        if await done_detector(page):
            return "completed"
        if submitted_application and await followup_detector(page):
            return "completed"
        if await _handle_blocking_agreement_modal(page, log_cb):
            continue
        if await _looks_like_workday_auth_page(page):
            await log_cb("info", "Vertex vision loop deferred Workday auth page to controlled auth handler")
            return None

        # Bail immediately on any captcha — don't waste a Gemini call
        if await _looks_like_captcha_page(page):
            await log_cb("warn", "Vision detected CAPTCHA on page, stopping for manual intervention")
            await _set_application_error(db, application_id, "CAPTCHA detected — manual intervention required")
            return "needs_manual"

        screenshot, html_summary, inspector_page_data = await asyncio.gather(
            page.screenshot(type="png", full_page=False),
            _extract_html_summary(page),
            _safe_inspect_page(page),
        )
        inspector_summary = _build_inspector_field_summary(inspector_page_data)
        if inspector_summary:
            html_summary = f"{html_summary}\n\nFORM FIELDS (inspector, use id as input_id):\n{inspector_summary}"
        candidate_profile["saved_answers"] = await _saved_answers(db, candidate, getattr(page, "url", ""))
        result = await call_gemini_vision(screenshot, candidate_profile, config, html_summary=html_summary)
        await log_cb(
            "info",
            f"Vision step {step + 1}: page_type={result.page_type} confidence={result.confidence} action={result.next_action.type}:{result.next_action.target_text}",
        )

        # Save per-step screenshot to disk and DB for debugging
        try:
            screenshot_dir = Path("screenshots") / str(application_id)
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_file = screenshot_dir / f"vision_step_{step + 1}.png"
            screenshot_file.write_bytes(screenshot)
            db.add(ApplicationScreenshot(
                application_id=application_id,
                step_number=step + 1,
                page_type=result.page_type,
                confidence=result.confidence,
                action_taken=str(result.next_action.type or ""),
                action_target=str(result.next_action.target_text or ""),
                succeeded=None,
                screenshot_path=str(screenshot_file),
            ))
            await db.flush()
        except Exception:
            pass

        if result.confidence == "low" and step > 0:
            fallback_model = "gemini-2.5-pro"
            if config.gemini_model != fallback_model:
                await log_cb("info", f"Vision step {step + 1}: low confidence, retrying with {fallback_model}")
                result = await call_gemini_vision(
                    screenshot,
                    candidate_profile,
                    config,
                    model_name=fallback_model,
                    html_summary=html_summary,
                )
        if result.page_type == "confirmation":
            return "completed"
        if result.page_type in {"login", "signup"}:
            auth_host = urlparse(str(getattr(page, "url", ""))).hostname or ""
            if "myworkdayjobs.com" in auth_host.lower():
                await log_cb("info", "Vertex vision loop deferred Workday login/signup to controlled auth handler")
                return None
        if await _looks_like_email_verification_page(page) or result.page_type == "otp" or result.page_type in {"login", "signup"}:
            from backend.engine.email_verifier import detect_verification_type, fetch_otp, handle_email_verification

            verification_type = await detect_verification_type(page)
            if result.page_type in {"login", "signup"} and verification_type == "unknown":
                pass
            else:
                sender_domain = urlparse(str(getattr(page, "url", ""))).hostname or ""

                async def configured_otp_fetcher(sender_domain: str | None = None) -> str | None:
                    if otp_fetcher is not None:
                        try:
                            return await otp_fetcher(sender_domain=sender_domain)
                        except TypeError:
                            return await otp_fetcher()
                    return await fetch_otp(sender_domain=sender_domain, max_wait_seconds=config.otp_wait_seconds)

                await log_cb("info", f"Email verification handler detected type={verification_type}")
                verification_result = await handle_email_verification(
                    page,
                    sender_domain,
                    otp_fetcher=configured_otp_fetcher,
                )
                if verification_result == "verified":
                    await log_cb("info", "Email verification completed successfully")
                    continue
                if verification_result == "needs_manual":
                    await _set_application_error(db, application_id, "Email verification required manual intervention")
                    return "needs_manual"
                await _set_application_error(db, application_id, "Email verification failed")
                return "needs_manual"
        if result.page_type == "resume_parse":
            await log_cb("info", "Vision detected resume parsing, waiting 5 seconds...")
            await asyncio.sleep(5)
            continue  # re-screenshot and re-evaluate

        if result.page_type in {"captcha", "error", "dead"}:
            reason = result.stop_reason or f"Vision detected {result.page_type}"
            await _set_application_error(db, application_id, reason)
            return "needs_manual"

        if result.confidence == "low" and result.stop_reason:
            saved_count = await _save_reference_manual_questions(
                db,
                page,
                candidate,
                application_id,
                result,
                result.stop_reason,
                html_summary,
            )
            if saved_count:
                await log_cb("info", f"Vision saved {saved_count} missing reference questions for manual answer")
            await _set_application_error(db, application_id, result.stop_reason)
            return "needs_manual"

        action_type = str(result.next_action.type or "wait").lower()
        action_target = str(result.next_action.target_text or "")
        progress_action = action_type in {"click", "select"} and action_target.lower() in {
            "next",
            "save",
            "update",
            "continue",
        }
        progress_page_key = f"{getattr(page, 'url', '')}|{result.page_type}"
        if progress_action:
            if progress_page_key == last_progress_page_key:
                same_progress_page_count += 1
                if same_progress_page_count >= 6:
                    await _set_application_error(
                        db,
                        application_id,
                        "could not progress after repeated form navigation actions on the same page",
                    )
                    return "needs_manual"
            else:
                same_progress_page_count = 0
            last_progress_page_key = progress_page_key
        else:
            same_progress_page_count = 0
            last_progress_page_key = progress_page_key
        action_page_key = f"{getattr(page, 'url', '')}|{result.page_type}|{action_type}|{action_target.lower()}"
        if progress_action:
            if action_page_key == last_action_page_key:
                same_action_page_count += 1
                if same_action_page_count >= 5:
                    await _set_application_error(
                        db,
                        application_id,
                        f"could not progress after repeated [{action_target}] on the same form",
                    )
                    return "needs_manual"
            else:
                same_action_page_count = 0
            last_action_page_key = action_page_key
        else:
            same_action_page_count = 0
            last_action_page_key = action_page_key
        field_fingerprint = "|".join(
            f"{field.label}:{field.field_type}:{field.value_to_fill}:{field.should_skip}" for field in result.fields[:12]
        )
        decision_fingerprint = f"{result.page_type}|{action_type}|{action_target}|{field_fingerprint}"
        if decision_fingerprint == last_decision_fingerprint:
            same_page_count += 1
            if same_page_count >= 3:
                await _set_application_error(db, application_id, "stuck on same page")
                return "needs_manual"
        else:
            same_page_count = 0
        last_decision_fingerprint = decision_fingerprint

        for field in result.fields:
            if field.should_skip:
                continue
            current = str(field.current_value or "").strip()
            target = str(field.value_to_fill or "").strip()
            if current and target and current.lower() == target.lower():
                await log_cb("info", f"Vision auto-skipped [{field.label}] — current_value already matches value_to_fill")
                continue
            await _apply_field(page, field, candidate_profile, log_cb)
            validation_error = await _check_field_validation_error(page, field.label or field.placeholder)
            if validation_error:
                await log_cb("error", f"Field validation failed — {validation_error}")
                await _set_application_error(db, application_id, f"Invalid field: {validation_error}")
                return "needs_manual"

        if action_type == "upload":
            uploaded = await _upload_resume(page, candidate_profile)
            if not uploaded:
                await _set_application_error(db, application_id, "Vision requested upload but no resume input was available")
                return "needs_manual"
        elif action_type == "click":
            clicked = await _click_by_text(page, result.next_action.target_text, log_cb)
            if not clicked:
                if await _handle_blocking_agreement_modal(page, log_cb):
                    continue
                if await _looks_like_application_form(page):
                    await log_cb(
                        "warn",
                        f"Vision ignored stale click target [{result.next_action.target_text}] because an application form is already visible",
                    )
                    continue
                await _set_application_error(
                    db,
                    application_id,
                    f"Vision could not find visible click target [{result.next_action.target_text}]",
                )
                return "needs_manual"
            if result.next_action.is_final_submit:
                submitted_application = True
        elif action_type == "stop":
            saved_count = await _save_reference_manual_questions(
                db,
                page,
                candidate,
                application_id,
                result,
                result.stop_reason,
                html_summary,
            )
            if saved_count:
                await log_cb("info", f"Vision saved {saved_count} missing reference questions for manual answer")
            await _set_application_error(db, application_id, result.stop_reason or "Vision requested manual stop")
            return "needs_manual"
        elif action_type == "select":
            clicked = await _click_by_text(page, result.next_action.target_text, log_cb)
            if not clicked:
                if await _handle_blocking_agreement_modal(page, log_cb):
                    continue
                if await _looks_like_application_form(page):
                    await log_cb(
                        "warn",
                        f"Vision ignored stale select target [{result.next_action.target_text}] because an application form is already visible",
                    )
                    continue
                await _set_application_error(
                    db,
                    application_id,
                    f"Vision could not find visible select target [{result.next_action.target_text}]",
                )
                return "needs_manual"
        elif action_type == "wait":
            pass

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await anti_ban.random_delay(1500, 3000)

    await _set_application_error(db, application_id, "exceeded max steps")
    return "needs_manual"

async def otp_fetcher() -> str | None:
    from backend.engine.email_verifier import fetch_otp

    return await fetch_otp()
