from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

from app.engine.platform_adapters import (
    is_third_party_apply_button,
    is_optional_conditional_field,
    is_verification_or_security_label,
    platform_for_domain,
    score_action_button,
    verification_code_blocker_detected,
)
from app.engine.navigation_rules import classify_page_state, navigation_action_override
from app.engine.site_rules import domain_from_url, find_field_rule, normalize_label
from app.engine.platforms.workday_intent import classify as workday_classify


def _normalize_page_text(value: object) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


def _button_texts(page_data: dict[str, Any]) -> list[str]:
    return [
        _normalize_page_text(button.get("text"))
        for button in page_data.get("buttons", [])
        if isinstance(button, dict)
    ]


def _iframe_sources(page_data: dict[str, Any]) -> list[str]:
    return [
        str(frame.get("src") or "").strip()
        for frame in page_data.get("iframes", [])
        if isinstance(frame, dict) and str(frame.get("src") or "").strip()
    ]


def _is_workday_start_application_choice(page_data: dict[str, Any]) -> bool:
    if platform_for_domain(str(page_data.get("url") or "")).name != "workday":
        return False
    haystack = _normalize_page_text(
        " ".join(
            [
                str(page_data.get("title") or ""),
                str(page_data.get("text") or ""),
                " ".join(
                    str(modal.get("text") or "")
                    for modal in page_data.get("modals", [])
                    if isinstance(modal, dict)
                ),
            ]
        )
    )
    automation_ids = {
        str(button.get("automationId") or "").strip().lower()
        for button in page_data.get("buttons", [])
        if isinstance(button, dict)
    }
    button_texts = {
        _normalize_page_text(button.get("text"))
        for button in page_data.get("buttons", [])
        if isinstance(button, dict)
    }
    return (
        (
            "start your application" in haystack
            or any(
                is_third_party_apply_button(button)
                for button in page_data.get("buttons", [])
                if isinstance(button, dict)
            )
        )
        and (
            "applymanually" in automation_ids
            or "autofillwithresume" in automation_ids
            or "usemylastapplication" in automation_ids
            or "apply manually" in button_texts
            or "autofill with resume" in button_texts
            or "use my last application" in button_texts
        )
    )


def embedded_application_url(page_data: dict[str, Any]) -> str | None:
    try:
        for src in _iframe_sources(page_data):
            normalized = _normalize_page_text(src)
            if is_third_party_apply_button({"href": src, "text": src}):
                continue
            if any(
                marker in normalized
                for marker in (
                    "recruitingbypaycor.com/career/",
                    "jobintroduction.action",
                    "apply",
                    "jobs.",
                    "greenhouse.io",
                    "lever.co",
                    "ashbyhq.com",
                    "workdayjobs.com",
                    "myworkdayjobs.com",
                )
            ) and not any(
                skip in normalized
                for skip in (
                    "youtube.com",
                    "googleads.",
                    "doubleclick.net",
                    "pagead/ads",
                    "googlesyndication.com",
                    "googletagmanager.com",
                    "google-analytics.com",
                    "analytics.",
                    "facebook.com/tr",
                    "adsystem.",
                    "adservice.",
                    "adnxs.com",
                    "taboola.",
                    "outbrain.",
                    "google.com/recaptcha",
                    "simplebooklet.com",
                    "embed.php",
                )
            ):
                return src
        return None
    except Exception as e:
        logger.warning("embedded_application_url failed: %s", e)
        return None


def _contextual_button_score(page_data: dict[str, Any], button: dict[str, Any], platform: str) -> int:
    try:
        text = _normalize_page_text(button.get("text"))
        automation_id = str(button.get("automationId") or "").strip().lower()
        page_text = _normalize_page_text(page_data.get("text"))
        title = _normalize_page_text(page_data.get("title"))
        modal_text = " ".join(
            _normalize_page_text(modal.get("text"))
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
        if "chatbot" in page_text or "chatbot" in modal_text:
            if text == "close chatbot window":
                return 125
            if text in {"i'm interested", "i am interested", "im interested"}:
                return -1
        if "cookie" in page_text or "cookie" in modal_text:
            if text in {"accept cookies", "accept all cookies", "accept all", "accept", "allow", "allow all"}:
                return 120
            if text in {"reject cookies", "reject", "manage preferences", "customize", "cookie settings"}:
                return -1
        if "if you have not yet created an account" in page_text or "please create an account" in page_text:
            if text == "create an account":
                return 95
            if text in {"login", "log in", "sign in"}:
                return -1
        if "how to apply" in title or "how to apply" in page_text:
            if text in {"search jobs", "browse all jobs", "view all open opportunities"}:
                return 95
            if text == "submit resume":
                return -1
        if "verify your account before you sign in" in page_text and text == "resend account verification":
            return 100
        if "privacy agreement" in title or "privacy agreement" in page_text:
            if text in {"i accept", "accept"}:
                return 100
            if text in {"i decline", "decline"}:
                return -1
        return score_action_button(button, platform)
    except Exception as e:
        logger.warning("_contextual_button_score failed: %s", e)
        return 0


def _has_real_apply_control(page_data: dict[str, Any]) -> bool:
    try:
        for button in page_data.get("buttons", []):
            if not isinstance(button, dict):
                continue
            text = _normalize_page_text(button.get("text"))
            href = str(button.get("href") or "").lower()
            tag_name = str(button.get("tagName") or "").lower()
            button_type = str(button.get("type") or "").lower()
            if any(
                keyword in text
                for keyword in (
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
            ):
                return True
            if text == "apply" and (
                (href and "mailto:" not in href)
                or tag_name == "button"
                or button_type in {"button", "submit"}
            ):
                return True
        return False
    except Exception as e:
        logger.warning("_has_real_apply_control failed: %s", e)
        return False


def is_listing_surface(page_data: dict[str, Any]) -> bool:
    try:
        haystack = _normalize_page_text(
            " ".join(
                [
                    str(page_data.get("url") or ""),
                    str(page_data.get("title") or ""),
                    str(page_data.get("text") or ""),
                    " ".join(_button_texts(page_data)),
                ]
            )
        )
        application_flow_markers = (
            "save and continue",
            "submit application",
            "complete application",
            "completed step",
            "current step",
            "candidate home",
            "back to job posting",
            "my information",
            "my experience",
            "voluntary disclosures",
            "self identify",
            "review",
        )
        if any(marker in haystack for marker in application_flow_markers):
            return False
        current_url = str(page_data.get("url") or "").lower()
        # "/apply" pages (and their sub-paths like /apply/applyManually) are
        # application-flow surfaces — skip them. The previous code ALSO excluded
        # all Workday domains, which broke recovery from a Workday userHome or
        # job-search landing page after a session resume.
        if "/apply" in current_url:
            return False
        listing_markers = (
            "receive similar jobs",
            "similar jobs by email",
            "create alert",
            "job alert",
            "search for similar jobs",
            "similar jobs",
            "browse jobs",
            "jobseekers",
            "employers",
            "advanced",
            "show full description",
            "easy apply",
        )
        search_fields = {
            ("what?", "job, company, title"),
            ("where?", "city, state or zip code"),
        }
        has_listing_markers = any(marker in haystack for marker in listing_markers)
        has_search_fields = any(
            (
                _normalize_page_text(field.get("label")),
                _normalize_page_text(field.get("placeholder")),
            )
            in search_fields
            for field in page_data.get("fields", [])
            if isinstance(field, dict)
        )
        return has_listing_markers or has_search_fields
    except Exception as e:
        logger.warning("is_listing_surface failed: %s", e)
        return False


def page_blocker_reason(page_data: dict[str, Any]) -> str | None:
    try:
        page_state = classify_page_state(page_data)
        if page_state == "dead_listing":
            return "Job posting is unavailable or expired"

        if not is_listing_surface(page_data):
            return None
        if _has_real_apply_control(page_data):
            return None

        haystack = _normalize_page_text(
            " ".join(
                [
                    str(page_data.get("url") or ""),
                    str(page_data.get("title") or ""),
                    str(page_data.get("text") or ""),
                ]
            )
        )
        hard_stop_markers = (
            "job is not available in your region",
            "not available in your region",
            "the page you are looking for doesn't exist",
            "the page you are looking for does not exist",
            "this job is no longer available",
            "job is no longer available",
            "position has been filled",
            "job expired",
            "search for similar jobs in your region",
        )
        if any(marker in haystack for marker in hard_stop_markers):
            return "Listing page is not a real application form and the job is unavailable from this page"

        required_fields = [
            field
            for field in page_data.get("fields", [])
            if isinstance(field, dict) and bool(field.get("required"))
        ]
        if not required_fields:
            return "Listing page is not a real application form and no external apply control was found"
        return None
    except Exception as e:
        logger.warning("page_blocker_reason failed: %s", e)
        return None


async def inspect_page(page: object) -> dict[str, Any]:
    """Extract full actionable page structure from DOM."""
    try:
        _result = await page.evaluate(
        """() => {
        const INSPECT_ATTR = 'data-cviance-inspect';

        function cssString(value) {
            return String(value || '').replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
        }

        function textOf(el) {
            return (el?.innerText || el?.textContent || '').trim();
        }

        function cleanLabel(value) {
            return String(value || '').replace(/\\s+/g, ' ').trim();
        }

        function isPlaceholderLabel(value) {
            const text = cleanLabel(value).toLowerCase();
            return !text
                || text === 'select'
                || text === 'select...'
                || text === 'select one'
                || text === 'select one required'
                || text === 'choose'
                || text === 'choose...'
                || text === 'choose one'
                || text === 'choose one required'
                || text === 'search';
        }

        function meaningfulQuestionText(value) {
            const text = cleanLabel(value)
                .replace(/\bSelect One\b/ig, ' ')
                .replace(/\bRequired\b/ig, ' ')
                .replace(/\brequired field\b/ig, ' ')
                .replace(/[*]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            if (!text || isPlaceholderLabel(text)) return '';
            if (text.length > 260) return '';
            if (!/[?]/.test(text) && text.split(/\\s+/).length < 4) return '';
            return text;
        }

        function isMachineId(value) {
            const text = cleanLabel(value);
            return /^[A-Za-z0-9]+$/.test(text) && /[A-Za-z]/.test(text) && /\\d/.test(text) && text.length >= 8;
        }

        function usableAttr(value) {
            const text = cleanLabel(value);
            return text && !isPlaceholderLabel(text) && !isMachineId(text) ? text : '';
        }

        function attrLabel(value) {
            return cleanLabel(
                String(value || '')
                    .replace(/([a-z])([A-Z])/g, '$1 $2')
                    .replace(/[._\\[\\]-]+/g, ' ')
            );
        }

        function isRequiredField(el, label) {
            const ariaRequired = String(el.getAttribute('aria-required') || '').toLowerCase() === 'true';
            const text = cleanLabel(label || '');
            const rowHeader = el.closest('tr')?.querySelector('th');
            const rowHeaderText = cleanLabel(textOf(rowHeader));
            const rowHeaderRequired = /[*]/.test(rowHeaderText) || Boolean(rowHeader?.querySelector('font[color="red"], .required'));
            return Boolean(el.required || ariaRequired || rowHeaderRequired || /[*]/.test(text) || /\\brequired\\b/i.test(text));
        }

        function firstSmallText(root) {
            if (!root) return '';
            const directText = Array.from(root.childNodes || [])
                .filter(node => node.nodeType === Node.TEXT_NODE)
                .map(node => cleanLabel(node.textContent))
                .find(text => text && text.length <= 120 && !isPlaceholderLabel(text));
            if (directText) return directText;

            const labeled = root.querySelector?.('label, span, p');
            const labeledText = cleanLabel(textOf(labeled));
            if (labeledText && labeledText.length <= 160 && !isPlaceholderLabel(labeledText)) {
                return labeledText;
            }
            return '';
        }

        function labelText(label) {
            if (!label || label.classList?.contains('error')) return '';
            const dataContent = cleanLabel(label.getAttribute?.('data-content'));
            const text = cleanLabel(dataContent || textOf(label)).replace(/[*]/g, '').trim();
            return text && !isPlaceholderLabel(text) ? text : '';
        }

        function localFieldLabel(el) {
            const scopes = [
                el.closest('.form-group'),
                el.closest('.validation'),
                el.closest('[class*="form-group" i]'),
                el.parentElement,
            ].filter(Boolean);

            for (const scope of scopes) {
                const labels = Array.from(scope.querySelectorAll?.('label') || [])
                    .filter(label => !label.classList?.contains('error'));
                const exactLabel = labels.find(label => el.id && label.getAttribute('for') === el.id);
                const exactText = labelText(exactLabel);
                if (exactText) return exactText;

                const directLabels = labels.filter(label => {
                    const labelFor = label.getAttribute('for') || '';
                    return !labelFor || labelFor === 'field_Identifier' || labelFor === el.id;
                });
                for (const label of directLabels) {
                    const text = labelText(label);
                    if (text) return text;
                }
            }
            return '';
        }

        function tableRowHeaderLabel(el) {
            const rowHeader = el.closest('tr')?.querySelector('th');
            const text = cleanLabel(textOf(rowHeader)).replace(/[*]/g, '').trim();
            return text && text.length <= 160 && !isPlaceholderLabel(text) ? text : '';
        }

        function parentLabel(el) {
            const fieldIdParent = el.closest('[data-field-id]');
            const fieldId = fieldIdParent?.getAttribute('data-field-id');
            const usableFieldId = usableAttr(fieldId);

            // Stop climbing once we leave this field's formField wrapper —
            // otherwise we steal labels from sibling fields (e.g. "phone device
            // type" leaking onto the HDYHAU dropdown).
            const formFieldBoundary = el.closest('[data-automation-id*="formField" i]');

            const fieldBox = el.closest('div[class*="field" i]');
            if (!formFieldBoundary || (fieldBox && formFieldBoundary.contains(fieldBox))) {
                const fieldText = firstSmallText(fieldBox);
                if (fieldText) return fieldText;
            }

            const formBox = el.closest('div[class*="form" i]');
            if (!formFieldBoundary || (formBox && formFieldBoundary.contains(formBox))) {
                const formText = firstSmallText(formBox);
                if (formText) return formText;
            }

            let current = el.parentElement;
            for (let depth = 0; depth < 5 && current; depth += 1) {
                if (formFieldBoundary && !formFieldBoundary.contains(current)) break;
                const currentText = firstSmallText(current);
                if (currentText) return currentText;
                current = current.parentElement;
            }
            return usableFieldId || '';
        }

        function resolvePlaceholderLabel(el, label) {
            if (!isPlaceholderLabel(label)) return label;
            const parentText = parentLabel(el);
            if (parentText) return parentText;
            return label || '';
        }

        function getLabel(el) {
            const fieldsetLegend = cleanLabel(textOf(el.closest('fieldset')?.querySelector('legend')));
            if (fieldsetLegend && !isPlaceholderLabel(fieldsetLegend)) return fieldsetLegend;
            const formField = el.closest('[data-automation-id*="formField" i]');
            const formLegend = cleanLabel(textOf(formField?.querySelector('legend')));
            if (formLegend && !isPlaceholderLabel(formLegend)) return formLegend;

            const ariaLabel = cleanLabel(el.getAttribute('aria-label'));
            if (ariaLabel && !isPlaceholderLabel(ariaLabel)) return ariaLabel;

            // When we're inside a Workday formField wrapper, ONLY look at labels
            // scoped to that wrapper. Falling through to global ancestor scans
            // pulls labels from neighbouring fields (e.g. phone device type
            // bleeding into HDYHAU).
            if (formField) {
                const scopedLabelNode = formField.querySelector('label, legend');
                const scopedText = labelText(scopedLabelNode);
                if (scopedText && !isPlaceholderLabel(scopedText)) return scopedText;
                if (el.id) {
                    const forLabel = formField.querySelector(`label[for="${cssString(el.id)}"]`);
                    const forText = labelText(forLabel);
                    if (forText && !isPlaceholderLabel(forText)) return forText;
                }
                const dataFieldId = usableAttr(el.getAttribute('data-field-id'));
                const testId = usableAttr(el.getAttribute('data-testid'));
                const automationId = usableAttr(el.getAttribute('data-automation-id'));
                if (dataFieldId) return dataFieldId;
                if (testId) return testId;
                if (automationId) return automationId;
                return '';
            }
            if (el.id) {
                const label = document.querySelector(`label[for="${cssString(el.id)}"]`);
                const exactText = labelText(label);
                if (exactText) return exactText;
            }
            const parent = el.closest('label');
            const parentText = labelText(parent);
            if (parentText) return parentText;

            const localText = localFieldLabel(el);
            if (localText) return localText;

            const rowHeaderText = tableRowHeaderLabel(el);
            if (rowHeaderText) return rowHeaderText;

            const dataFieldId = usableAttr(el.getAttribute('data-field-id'));
            const testId = usableAttr(el.getAttribute('data-testid'));
            const automationId = usableAttr(el.getAttribute('data-automation-id'));

            if (dataFieldId) return dataFieldId;
            if (testId) return testId;
            if (automationId) return automationId;

            const placeholder = cleanLabel(el.placeholder);
            if (placeholder) return resolvePlaceholderLabel(el, placeholder);
            const name = attrLabel(el.getAttribute('name') || '');
            if (name) return resolvePlaceholderLabel(el, name);
            const id = attrLabel(el.id || '');
            if (id) return resolvePlaceholderLabel(el, id);

            let labelScope = el.parentElement;
            for (let depth = 0; depth < 4 && labelScope; depth += 1) {
                const previous = labelScope.previousElementSibling;
                const previousText = cleanLabel(textOf(previous));
                if (previousText && previousText.length <= 140 && !isPlaceholderLabel(previousText)) {
                    return previousText;
                }
                labelScope = labelScope.parentElement;
            }

            const containerText = parentLabel(el);
            if (containerText) return containerText;

            const questionText = nearbyQuestionText(el);
            if (questionText) return questionText;

            const prev = el.previousElementSibling;
            const prevText = cleanLabel(textOf(prev));
            if (prevText && !isPlaceholderLabel(prevText)) return prevText;
            return '';
        }

        function nearbyQuestionText(el) {
            const currentText = cleanLabel(textOf(el));
            const container = el.closest('[data-automation-id*="formField" i], [data-automation-id*="question" i], fieldset, section, li, div');
            let current = container || el.parentElement;
            for (let depth = 0; depth < 7 && current; depth += 1) {
                const nodes = Array.from(current.querySelectorAll('label, legend, p, span, div'))
                    .filter(node => node !== el && !node.contains(el));
                const candidates = nodes
                    .map(node => meaningfulQuestionText(textOf(node)))
                    .filter(Boolean)
                    .filter(text => text.toLowerCase() !== currentText.toLowerCase())
                    .filter(text => !/^(yes|no)$/i.test(text));
                const question = candidates.find(text => text.includes('?')) || candidates[0];
                if (question) return question;
                current = current.parentElement;
            }

            const lines = cleanLabel(document.body?.innerText || '')
                .split(/(?<=[?])\\s+|\\n+/)
                .map(line => meaningfulQuestionText(line))
                .filter(Boolean);
            const rect = el.getBoundingClientRect();
            if (Number.isFinite(rect.top)) {
                return lines.slice(-20).find(line => line.includes('?')) || '';
            }
            return '';
        }

        function getRadioGroupLabel(el) {
            const formGroupLabel = labelText(el.closest('.form-group')?.querySelector('label.control-label, label'));
            if (formGroupLabel) return formGroupLabel;
            const legendText = cleanLabel(textOf(el.closest('fieldset')?.querySelector('legend')));
            if (legendText && !isPlaceholderLabel(legendText)) return legendText;
            const group = el.closest('[role=radiogroup], [aria-labelledby]');
            const labelledBy = group?.getAttribute('aria-labelledby');
            if (labelledBy) {
                const labelledText = cleanLabel(textOf(document.getElementById(labelledBy)));
                if (labelledText && !isPlaceholderLabel(labelledText)) return labelledText;
            }
            return parentLabel(el) || getLabel(el);
        }

        function getRadioOptionLabel(el) {
            if (el.id) {
                const label = document.querySelector(`label[for="${cssString(el.id)}"]`);
                const labelText = cleanLabel(textOf(label));
                if (labelText && !isPlaceholderLabel(labelText)) return labelText;
            }
            const parent = el.closest('label');
            const parentText = cleanLabel(textOf(parent));
            if (parentText && !isPlaceholderLabel(parentText)) return parentText;
            const nextText = cleanLabel(textOf(el.nextElementSibling));
            if (nextText && !isPlaceholderLabel(nextText)) return nextText;
            const ariaLabel = cleanLabel(el.getAttribute('aria-label'));
            if (ariaLabel && !isPlaceholderLabel(ariaLabel)) return ariaLabel;
            return cleanLabel(el.value);
        }

        function selectedDisplayText(el, label, type) {
            function normalizeSelectedText(rawText) {
                let text = cleanLabel(rawText);
                if (!text) return '';
                text = text.replace(/\\b\\d+\\s+items?\\s+selected\\b[:,]?\\s*/ig, '');
                text = text.replace(/\\b\\d+\\s+item\\s+selected\\b[:,]?\\s*/ig, '');
                text = cleanLabel(text);
                if (!text) return '';
                if (/^(0\\s+items?|select one|select|choose one|choose|search)$/i.test(text)) return '';
                return text;
            }

            const tagName = (el.tagName || '').toLowerCase();
            const automationId = String(el.getAttribute('data-automation-id') || '').toLowerCase();
            const multiSelectId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
            const multiSelectContainer = el.closest('[data-automation-id="multiSelectContainer"]')
                || (multiSelectId ? document.getElementById(multiSelectId) : null)
                || el.closest('[data-automation-id*="formField" i], label, div, section, fieldset');
            const selectedChip = normalizeSelectedText(
                textOf(multiSelectContainer?.querySelector('[data-automation-id="selectedItem"]'))
            );
            if (selectedChip) return selectedChip;
            const promptSelection = normalizeSelectedText(
                textOf(multiSelectContainer?.querySelector('[data-automation-id="promptSelectionLabel"]'))
            );
            if (promptSelection) return promptSelection;
            const promptInstruction = normalizeSelectedText(
                textOf(multiSelectContainer?.querySelector('[data-automation-id="promptAriaInstruction"]'))
            );
            if (promptInstruction && !/^(expanded|collapsed|search)$/i.test(promptInstruction)) return promptInstruction;

            if (type === 'select-one' || type === 'select-multiple') {
                if (el.selectedOptions && el.selectedOptions.length) {
                    return normalizeSelectedText(
                        Array.from(el.selectedOptions)
                            .map(option => textOf(option) || option.label || option.value)
                            .join(' ')
                    );
                }
                const directText = normalizeSelectedText(textOf(el));
                if (directText) return directText;
                const ariaValueText = normalizeSelectedText(el.getAttribute('aria-valuetext'));
                if (ariaValueText) return ariaValueText;
                const ariaLabel = normalizeSelectedText(el.getAttribute('aria-label'));
                if (ariaLabel && ariaLabel.toLowerCase() !== cleanLabel(label).toLowerCase()) return ariaLabel;
                if (tagName === 'input' && automationId === 'searchbox') {
                    const promptOpen = Boolean(
                        multiSelectId && document.querySelector(`[data-associated-widget="${cssString(multiSelectId)}"]`)
                    );
                    const inputValue = normalizeSelectedText(el.value);
                    if (inputValue && !promptOpen) return inputValue;
                    return '';
                }
            }
            if (type !== 'select-one' && type !== 'select-multiple') return '';
            if (tagName === 'button' && el.getAttribute('aria-haspopup')) return '';

            const labelParts = cleanLabel(label).split(/\\s+/).filter(Boolean);
            let current = el.parentElement;
            for (let depth = 0; depth < 5 && current; depth += 1) {
                let text = cleanLabel(textOf(current));
                if (text && text.length <= 260) {
                    for (const part of labelParts) {
                        text = text.replace(new RegExp(part.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'ig'), ' ');
                    }
                    text = cleanLabel(
                        text
                            .replace(/\\*/g, ' ')
                            .replace(/\\bselect one\\b|\\bselect\\b|\\bchoose one\\b|\\bchoose\\b/ig, ' ')
                    );
                    text = normalizeSelectedText(text);
                    if (
                        text
                        && text.length <= 120
                        && !isPlaceholderLabel(text)
                        && !/^error\\b/i.test(text)
                    ) return text;
                }
                current = current.parentElement;
            }
            return '';
        }

        function cleanCapturedLabel(label, selectedText) {
            let text = cleanLabel(label || '');
            const selected = cleanLabel(selectedText || '');
            if (selected) {
                const escaped = selected.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                text = cleanLabel(text.replace(new RegExp(`\\\\b${escaped}\\\\b`, 'ig'), ' '));
            }
            text = cleanLabel(
                text
                    .replace(/[*]/g, ' ')
                    .replace(/\\bRequired\\b/ig, ' ')
                    .replace(/\\bSelect One\\b/ig, ' ')
                    .replace(/\\bSelect\\b/ig, ' ')
                    .replace(/\\bChoose One\\b/ig, ' ')
            );
            return text || cleanLabel(label || '');
        }

        function selectOptionsForField(el, label, type) {
            if (type !== 'select-one' && type !== 'select-multiple') return [];
            if (el.options && el.options.length) {
                return Array.from(el.options).map(option => ({
                    value: option.value,
                    text: cleanLabel(String(textOf(option) || option.label || option.value || '').replace(/,?\\s*press delete to clear value\\.?/ig, ' '))
                }));
            }

            const widgetId = el.getAttribute('data-uxi-multiselect-id')
                || el.closest('[data-automation-id="multiSelectContainer"]')?.id
                || '';
            const prompt = widgetId
                ? document.querySelector(`[data-associated-widget="${cssString(widgetId)}"]`)
                : null;
            // STRICT Workday boundaries only. Falling back to div/section/
            // fieldset pulls option nodes from other fields on the same form.
            const container = prompt
                || el.closest('[data-automation-id="multiSelectContainer"]')
                || el.closest('[data-automation-id*="formField" i]');
            const labelText = cleanLabel(label);
            const seen = new Set();
            const options = [];
            const candidateNodes = Array.from(
                container?.querySelectorAll(
                    '[role="option"], [data-automation-id="promptOption"], [data-automation-id="menuItem"], [data-automation-id="promptLeafNode"]'
                ) || []
            );

            for (const node of candidateNodes) {
                let text = cleanLabel(
                    node.getAttribute('data-automation-label')
                    || node.getAttribute('aria-label')
                    || textOf(node)
                );
                if (!text) continue;
                text = cleanLabel(
                    text
                        .replace(/\bnot checked\b/ig, ' ')
                        .replace(/\bchecked\b/ig, ' ')
                        .replace(/\bexpanded\b/ig, ' ')
                        .replace(/,?\\s*press delete to clear value\\.?/ig, ' ')
                );
                if (!text || isPlaceholderLabel(text)) continue;
                if (labelText && text === labelText) continue;
                if (text.length > 180) continue;
                const key = text.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                options.push({value: text, text});
            }
            return options;
        }

        function controlKindFor(el, type) {
            const tagName = (el.tagName || '').toLowerCase();
            const role = String(el.getAttribute('role') || '').toLowerCase();
            const className = String(el.getAttribute('class') || '').toLowerCase();
            const automationId = String(el.getAttribute('data-automation-id') || '').toLowerCase();
            const id = String(el.id || '').toLowerCase();
            const name = String(el.getAttribute('name') || '').toLowerCase();
            const text = `${id} ${name} ${className} ${automationId}`;
            if (type === 'select-one' || type === 'select-multiple') {
                if (tagName === 'select' && /salaryperiod/.test(text)) return 'native_salary_period_select';
                if (tagName === 'select' && /currency/.test(text)) return 'native_currency_select';
                if (tagName === 'select' && /countrycode|phone.*code|mobilephone/.test(text)) return 'native_phone_code_select';
                if (tagName === 'select') return 'native_select';
                if (/(select2|select2-selection)/.test(className)) return 'select2_button';
                if (role === 'combobox') return 'combobox_button';
                if (el.getAttribute('aria-haspopup')) return 'popup_select_button';
                return 'custom_select';
            }
            if (type === 'radio') return role === 'radio' ? 'custom_radio_button' : 'native_radio';
            if (type === 'checkbox') return role === 'checkbox' ? 'custom_checkbox_button' : 'native_checkbox';
            if (type === 'file') return 'file_upload';
            // Native date/month/time inputs — preserve the real type so decide_action
            // can route them to the correct date-filling logic instead of treating
            // them as plain text inputs.
            if (type === 'date' || type === 'month') return 'date_input';
            // Workday split-date sections (dateSectionDay / dateSectionMonth / dateSectionYear)
            // are plain text inputs but need date handling.
            if (/datesection(day|month|year)|datepicker(day|month|year)/.test(automationId)) return 'date_input';
            return tagName || type || '';
        }

        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0
                && style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !el.disabled;
        }

        function isFieldVisible(el) {
            if (isVisible(el)) return true;
            const type = String(el.type || '').toLowerCase();
            const role = String(el.getAttribute('role') || '').toLowerCase();
            if (type === 'radio' || role === 'radio' || type === 'checkbox' || role === 'checkbox') {
                if (el.id) {
                    const label = document.querySelector(`label[for="${cssString(el.id)}"]`);
                    if (label && isVisible(label)) return true;
                }
                const container = el.closest('label, fieldset, div, section');
                if (container && isVisible(container)) return true;
            }
            return false;
        }

        function generatedSelector(el) {
            let token = el.getAttribute(INSPECT_ATTR);
            if (!token) {
                token = `node-${Date.now()}-${Math.random().toString(36).slice(2)}`;
                el.setAttribute(INSPECT_ATTR, token);
            }
            return `[${INSPECT_ATTR}="${token}"]`;
        }

        function uniqueSelector(selector) {
            if (!selector) return false;
            try {
                return document.querySelectorAll(selector).length === 1;
            } catch (error) {
                return false;
            }
        }

        function getSelector(el) {
            if (el.id) {
                const selector = '#' + CSS.escape(el.id);
                if (uniqueSelector(selector)) return selector;
            }
            if (el.getAttribute('data-automation-id')) {
                const selector = `[data-automation-id="${cssString(el.getAttribute('data-automation-id'))}"]`;
                if (uniqueSelector(selector)) return selector;
            }
            if (el.getAttribute('name')) {
                const selector = `${el.tagName.toLowerCase()}[name="${cssString(el.getAttribute('name'))}"]`;
                if (uniqueSelector(selector)) return selector;
            }
            return generatedSelector(el);
        }

        function isLikelySelectControl(el) {
            const role = (el.getAttribute('role') || '').toLowerCase();
            const tagName = el.tagName.toLowerCase();
            const inputType = String(el.getAttribute('type') || el.type || '').toLowerCase();
            const className = String(el.getAttribute('class') || '').toLowerCase();
            const automationId = String(el.getAttribute('data-automation-id') || '').toLowerCase();
            const placeholder = cleanLabel(el.getAttribute('placeholder'));
            const text = cleanLabel(textOf(el));
            const hasPopup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
            const inputIdentity = [
                automationId,
                placeholder,
                text,
                cleanLabel(el.getAttribute('name')),
                cleanLabel(el.getAttribute('id'))
            ].join(' ').toLowerCase();

            if (tagName === 'input') {
                if (['tel', 'phone', 'email', 'number', 'url'].includes(inputType)) return false;
                if (/(^|\b)(phone|mobile|email|mail|first name|last name|full name|address|city|zip|postal)(\b|$)/.test(inputIdentity)
                    && !/(country phone code|phone code|dialing code|calling code|phone type|phone device)/.test(inputIdentity)) {
                    return false;
                }
            }

            if (role === 'combobox' || hasPopup === 'listbox') return true;
            if (tagName === 'select') return true;
            if (el.hasAttribute('aria-controls') && el.hasAttribute('aria-expanded')) return true;
            if (/(combobox|dropdown|select|prompt)/i.test(className) || /(dropdown|select|prompt)/i.test(automationId)) return true;
            if (tagName === 'button' && (isPlaceholderLabel(text) || /select|choose/i.test(text))) return true;
            if (placeholder && isPlaceholderLabel(placeholder) && role === 'combobox') return true;

            const parent = el.closest('label, div, section, fieldset');
            const siblingButton = parent
                ? Array.from(parent.querySelectorAll('button, [role=button], [aria-haspopup]')).find(node => node !== el)
                : null;
            if (siblingButton && tagName !== 'input' && tagName !== 'textarea') {
                const siblingPopup = String(siblingButton.getAttribute('aria-haspopup') || '').toLowerCase();
                const siblingText = cleanLabel(textOf(siblingButton));
                if (siblingPopup === 'listbox' || siblingPopup === 'menu' || /select|choose/i.test(siblingText)) {
                    return true;
                }
            }
            return false;
        }

        function activeModalRoot() {
            const selectors = [
                '[aria-modal="true"]',
                '[role="dialog"]',
                'dialog',
                '.modal',
                '[class*="modal" i]',
                '[class*="popup" i]',
                '[class*="overlay" i]',
                '[data-automation-id*="modal" i]'
            ].join(',');
            const candidates = Array.from(document.querySelectorAll(selectors))
                .filter(isVisible)
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const text = textOf(el);
                    const role = String(el.getAttribute('role') || '').toLowerCase();
                    const ariaModal = String(el.getAttribute('aria-modal') || '').toLowerCase() === 'true';
                    const modalish = ariaModal
                        || role === 'dialog'
                        || el.tagName.toLowerCase() === 'dialog'
                        || /modal|popup|overlay/i.test(String(el.getAttribute('class') || ''))
                        || /modal/i.test(String(el.getAttribute('data-automation-id') || ''));
                    const hasControls = Boolean(el.querySelector('input, textarea, select, button, [role=button], a[href]'));
                    const area = rect.width * rect.height;
                    const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
                    if (!modalish || area < 12000 || (!text && !hasControls)) return null;
                    return {el, area, zIndex, ariaModal, role};
                })
                .filter(Boolean)
                .sort((a, b) => Number(b.ariaModal) - Number(a.ariaModal) || b.zIndex - a.zIndex || b.area - a.area);
            return candidates[0]?.el || null;
        }

        const activeModal = activeModalRoot();
        const inActiveModal = (el) => !activeModal || el === activeModal || activeModal.contains(el);

        const result = {
            url: window.location.href,
            title: document.title,
            text: ((activeModal || document.body)?.innerText || '').substring(0, 2000),
            fields: [],
            buttons: [],
            iframes: [],
            modals: [],
            dropzones: [],
            activeModal: Boolean(activeModal)
        };

        document.querySelectorAll(
            'input, textarea, select, button[aria-haspopup], [role="combobox"], [role="radio"], [role="checkbox"], [aria-checked], [class*="checkbox" i], [data-automation-id*="checkbox" i]'
        ).forEach(el => {
            if (!inActiveModal(el)) return;
            if (!isFieldVisible(el)) return;
            if (el.closest('header, nav')) return;
            if (el.tagName.toLowerCase() === 'button' && isLikelySelectControl(el)) {
                const container = el.closest('label, div, section, fieldset');
                const relatedInput = container?.querySelector('input, textarea, select, [role="combobox"]');
                if (relatedInput && relatedInput !== el && isFieldVisible(relatedInput)) return;
            }
            let type = el.type || el.tagName.toLowerCase();
            const role = el.getAttribute('role') || '';
            const className = String(el.getAttribute('class') || '').toLowerCase();
            const automationId = String(el.getAttribute('data-automation-id') || '').toLowerCase();
            if (
                type !== 'radio'
                && (
                    role === 'checkbox'
                    || el.hasAttribute('aria-checked')
                    || className.includes('checkbox')
                    || automationId.includes('checkbox')
                )
            ) {
                type = 'checkbox';
            }
            if (
                type === 'radio'
                || role === 'radio'
                || className.includes('radio')
                || automationId.includes('radio')
            ) {
                type = 'radio';
            }
            const ariaAutocomplete = String(el.getAttribute('aria-autocomplete') || '').toLowerCase();
            const isFreeTextAutocompleteInput = (
                el.tagName.toLowerCase() === 'input'
                && (String(el.type || 'text').toLowerCase() === 'text' || el.type === '')
                && (ariaAutocomplete === 'list' || ariaAutocomplete === 'both' || ariaAutocomplete === 'inline')
                && !el.readOnly
                && !el.disabled
            );
            if (
                !isFreeTextAutocompleteInput
                && (
                    role === 'combobox'
                    || el.getAttribute('aria-haspopup') === 'listbox'
                    || (el.readOnly && isPlaceholderLabel(el.placeholder))
                    || isLikelySelectControl(el)
                )
            ) {
                type = 'select-one';
            }
            if (type === 'hidden' || type === 'submit' || type === 'image' || type === 'file') return;
            if (type === 'button' && !isLikelySelectControl(el)) return;

            const rawFieldLabel = type === 'radio' ? getRadioGroupLabel(el) : getLabel(el);
            const selectedText = selectedDisplayText(el, rawFieldLabel, type);
            const fieldLabel = cleanCapturedLabel(rawFieldLabel, selectedText);
            const options = selectOptionsForField(el, fieldLabel, type);

            // Workday CheckboxGroup: a single conceptual question rendered as
            // N <input type="checkbox"> children of a wrapper with
            // data-automation-id ending in "-CheckboxGroup" (e.g. disability
            // self-id has 3 mutually-exclusive options). Skip the wrapper element
            // and emit one field per member with the per-checkbox label text from
            // <label for="ID"> so the user can pick a specific option via the
            // manual-answer UI.
            const isCheckboxGroupWrapper = (
                type === 'checkbox'
                && /CheckboxGroup$/i.test(automationId)
            );
            if (isCheckboxGroupWrapper) {
                // Skip — actual options are emitted as their own field entries below,
                // and decide_action collapses them into a single answer surface.
                return;
            }
            const checkboxGroupContainer = type === 'checkbox'
                ? (
                    el.closest('[data-automation-id$="CheckboxGroup" i], [role="group"]')
                    || null
                )
                : null;
            const checkboxGroupAutomationId = checkboxGroupContainer
                ? (checkboxGroupContainer.getAttribute('data-automation-id') || '')
                : '';
            const radioOptions = type === 'radio'
                ? Array.from(
                    el.name
                        ? document.querySelectorAll(`input[type=radio][name="${cssString(el.name)}"]`)
                        : (el.closest('[role=radiogroup], fieldset, [data-automation-id*="radio" i], div')
                            ?.querySelectorAll('input[type=radio], [role=radio]') || [el])
                  )
                    .filter(isFieldVisible)
                    .map(r => ({
                        value: r.value,
                        label: getRadioOptionLabel(r),
                        checked: r.checked || r.getAttribute('aria-checked') === 'true',
                        selector: getSelector(r)
                    }))
                : (
                    type === 'checkbox' && checkboxGroupContainer
                        ? Array.from(checkboxGroupContainer.querySelectorAll('input[type=checkbox]'))
                            .filter(c => isFieldVisible(c))
                            .filter(c => !/CheckboxGroup$/i.test(String(c.getAttribute('data-automation-id') || '')))
                            .map(c => ({
                                value: c.value || '',
                                label: getRadioOptionLabel(c),
                                checked: c.checked || c.getAttribute('aria-checked') === 'true',
                                selector: getSelector(c),
                            }))
                        : []
                );
            const describedBy = String(el.getAttribute('aria-describedby') || '');
            const errorText = describedBy
                .split(/\\s+/)
                .map(id => textOf(document.getElementById(id)))
                .filter(Boolean)
                .join(' ');
            result.fields.push({
                type,
                label: fieldLabel,
                selector: getSelector(el),
                value: el.value || '',
                selectedText,
                checked: el.checked || el.getAttribute('aria-checked') === 'true' || false,
                required: isRequiredField(el, fieldLabel),
                options,
                radioOptions,
                placeholder: el.placeholder || '',
                automationId: el.getAttribute('data-automation-id') || '',
                controlKind: controlKindFor(el, type),
                invalid: el.getAttribute('aria-invalid') === 'true',
                errorText,
                tagName: el.tagName.toLowerCase(),
                role,
                ariaAutocomplete: String(el.getAttribute('aria-autocomplete') || '').toLowerCase(),
                readOnly: Boolean(el.readOnly)
            });
        });

        document.querySelectorAll('input[type=file]').forEach(el => {
            if (!inActiveModal(el)) return;
            const style = window.getComputedStyle(el);
            result.fields.push({
                type: 'file',
                label: getLabel(el) || 'resume',
                selector: getSelector(el),
                value: el.value || '',
                selectedText: '',
                checked: false,
                required: isRequiredField(el, getLabel(el) || 'resume'),
                options: [],
                radioOptions: [],
                placeholder: el.placeholder || '',
                automationId: el.getAttribute('data-automation-id') || '',
                isHidden: style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0' || el.type === 'hidden',
                fileCount: el.files ? el.files.length : 0
            });
        });

        document.querySelectorAll('button, [role=button], a[href], input[type=button], input[type=submit], [type=submit]').forEach(el => {
            if (!inActiveModal(el)) return;
            if (!isVisible(el)) return;
            const text = textOf(el) || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '';
            if (!text || text.length > 100) return;
            // Structural filter: never collect inline body-copy links as action buttons
            if ((el.tagName || '').toLowerCase() === 'a' && !el.getAttribute('data-automation-id')) {
              let cur = el.parentElement;
              while (cur && cur !== document.body) {
                if ((cur.innerText || cur.textContent || '').trim().length > 300) return;
                cur = cur.parentElement;
              }
            }
            result.buttons.push({
                text,
                selector: getSelector(el),
                type: el.getAttribute('type') || '',
                automationId: el.getAttribute('data-automation-id') || '',
                tagName: el.tagName.toLowerCase(),
                href: el.href || el.getAttribute('href') || ''
            });
        });

        if (activeModal) {
            result.modals.push({
                visible: true,
                text: textOf(activeModal).substring(0, 500),
                automationId: activeModal.getAttribute('data-automation-id') || '',
                selector: getSelector(activeModal),
                active: true
            });
        }

        document.querySelectorAll('[role=dialog], .modal, [aria-modal=true], dialog, [class*="popup" i], [class*="overlay" i], [data-automation-id*="modal" i]').forEach(el => {
            if (!isVisible(el)) return;
            if (activeModal && (el === activeModal || activeModal.contains(el))) return;
            result.modals.push({
                visible: true,
                text: textOf(el).substring(0, 500),
                automationId: el.getAttribute('data-automation-id') || '',
                selector: getSelector(el),
                active: false
            });
        });

        document.querySelectorAll('[class*=drop], [class*=upload], [class*=resume]').forEach(el => {
            if (!inActiveModal(el)) return;
            if (!isVisible(el)) return;
            const fileInput = el.querySelector('input[type=file]')
                || el.closest('div')?.querySelector('input[type=file]')
                || document.querySelector('input[type=file]');
            result.dropzones.push({
                text: textOf(el).substring(0, 100),
                hasFileInput: !!fileInput,
                fileInputSelector: fileInput ? getSelector(fileInput) : null
            });
        });

        document.querySelectorAll('iframe[src]').forEach(el => {
            if (!isVisible(el)) return;
            result.iframes.push({
                src: el.getAttribute('src') || '',
                title: el.getAttribute('title') || '',
                ariaLabel: el.getAttribute('aria-label') || ''
            });
        });

        return result;
    }"""
        )
        # [DEBUG-EEOC] Python-side trace — emit every button to stdout so we can
        # see exactly what entered result.buttons. Remove once redirect is solved.
        try:
            for _btn in (_result or {}).get("buttons", []) or []:
                if not isinstance(_btn, dict):
                    continue
                print(
                    "[INSPECT_PAGE_BUTTON]"
                    f" tag={_btn.get('tagName')!r}"
                    f" text={(_btn.get('text') or '')[:120]!r}"
                    f" href={_btn.get('href')!r}"
                    f" automationId={_btn.get('automationId')!r}",
                    flush=True,
                )
        except Exception as _trace_exc:
            logger.warning("inspect_page button trace failed: %s", _trace_exc)
        return _result
    except Exception as e:
        logger.warning("inspect_page failed: %s", e)
        return {}


async def probe_typeahead_popup(
    page: object,
    action: dict[str, Any],
    candidate_value: str | None,
) -> dict[str, Any] | None:
    """Handle Workday/FNB-style typeahead dropdowns whose static option DOM is empty.

    On some Workday tenants (FNB is the canonical example) clicking the
    dropdown trigger does NOT render a static list of ``[role=option]``
    children — it opens an inline ``input[type=text]`` and the user is
    expected to TYPE the answer; matching ``[role=option]`` rows are
    materialised by the React layer only after each keystroke.

    Our regular static-DOM probe (:func:`browser._capture_select_options`)
    therefore returns ``[]`` and the engine gives up. This helper runs the
    proper typeahead handshake:

      1. Click the trigger and wait 300ms for the popup to open.
      2. Locate an ``input[type=text]`` that has appeared inside the same
         widget container OR inside the element referenced by
         ``aria-controls``.
      3. If an input is found AND a candidate value was supplied, type the
         value (delay=80ms per char), wait 400ms, then look for
         ``ul[role='listbox'] li[role='option'], div[role='option']``
         entries whose text contains the typed value (case-insensitive).
         The first match is clicked.

    On success returns ``{"clicked_label": <option text>}``; otherwise
    returns ``None`` and the caller should keep the existing "mark as
    unanswered" behaviour. The function never raises.

    Pattern source: ``reference_workday_platform_selectors.md`` —
    "Popup option DOM" / "Typeahead/multiselect pattern". Don't rely on
    pressing Enter to commit — on FNB it commits an empty filter and
    closes the popup.
    """
    selector = str(action.get("selector") or "").strip()
    candidate_text = str(candidate_value or "").strip()
    if not selector:
        return None
    if not all(hasattr(page, name) for name in ("evaluate", "locator", "keyboard")):
        return None
    try:
        # Step 1: open the popup.
        try:
            await page.locator(selector).first.click(timeout=2000)
        except Exception as click_exc:
            logger.debug("probe_typeahead_popup: trigger click failed: %s", click_exc)
            return None
        await page.wait_for_timeout(300)

        # Step 2: locate the typeahead input that appeared.
        input_locator_selector = await page.evaluate(
            """
            (selector) => {
              const trigger = document.querySelector(selector);
              if (!trigger) return '';
              const visible = (node) => {
                if (!node) return false;
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const roots = [];
              const controlledId = trigger.getAttribute('aria-controls') || '';
              if (controlledId) {
                const ctl = document.getElementById(controlledId);
                if (ctl) roots.push(ctl);
              }
              const widget = trigger.closest('[data-automation-widget], [data-automation-id], fieldset, .css-fieldGroup, .form-group')
                || trigger.parentElement;
              if (widget && !roots.includes(widget)) roots.push(widget);
              for (const root of roots) {
                const inputs = Array.from(root.querySelectorAll('input[type=text], input:not([type])')).filter(visible);
                const fresh = inputs.find(inp => inp !== trigger && !inp.readOnly && !inp.disabled);
                if (!fresh) continue;
                // Build a stable selector for the located input.
                if (fresh.id) return '#' + CSS.escape(fresh.id);
                const aid = fresh.getAttribute('data-automation-id');
                if (aid) return `input[data-automation-id="${aid}"]`;
                const name = fresh.getAttribute('name');
                if (name) return `input[name="${name}"]`;
                return '';
              }
              return '';
            }
            """,
            selector,
        )
        input_selector = str(input_locator_selector or "").strip()
        if not input_selector:
            return None
        if not candidate_text:
            return None

        # Step 3: type the candidate value into the typeahead input.
        try:
            input_loc = page.locator(input_selector).first
            await input_loc.click(timeout=2000)
            await input_loc.fill("", timeout=2000)
        except Exception:
            # Some tenants don't allow .fill on the typeahead — fall through to type.
            pass
        try:
            await page.keyboard.type(candidate_text, delay=80)
        except Exception as type_exc:
            logger.debug("probe_typeahead_popup: keyboard.type failed: %s", type_exc)
            return None
        await page.wait_for_timeout(400)

        # Step 4: click the first option whose text contains the typed value.
        click_result = await page.evaluate(
            """
            (target) => {
              const visible = (node) => {
                if (!node) return false;
                const r = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const norm = (v) => String(v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const want = norm(target);
              if (!want) return null;
              const options = Array.from(document.querySelectorAll("ul[role='listbox'] li[role='option'], div[role='option']"))
                .filter(visible);
              const pick = options.find(opt => norm(opt.innerText || opt.textContent || '').includes(want));
              if (!pick) return null;
              const text = (pick.innerText || pick.textContent || '').trim();
              if (typeof pick.click === 'function') pick.click();
              return text;
            }
            """,
            candidate_text,
        )
        clicked_label = str(click_result or "").strip()
        if not clicked_label:
            return None
        return {"clicked_label": clicked_label}
    except Exception as exc:
        logger.warning("probe_typeahead_popup failed: %s", exc)
        return None


async def decide_action(page_data: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Pure rule-based decision. Returns list of actions to execute in order.
    Action format: {type: 'fill'|'click'|'select'|'check'|'upload'|'skip', ...}
    """
    actions: list[dict[str, Any]] = []
    listing_surface = is_listing_surface(page_data)
    platform = platform_for_domain(str(page_data.get("url") or "")).name
    is_workday = platform == "workday"
    if verification_code_blocker_detected(page_data):
        return actions
    navigation_action = navigation_action_override(page_data)
    if navigation_action and navigation_action.get("type") == "click_button":
        return [navigation_action]
    handled_radio_groups: set[str] = set()
    handled_checkbox_groups: set[str] = set()
    handled_date_groups: set[str] = set()

    def first_name(c: dict[str, Any]) -> str:
        return str(c["name"]).split()[0]

    def last_name(c: dict[str, Any]) -> str:
        parts = str(c["name"]).split()
        return parts[-1] if len(parts) > 1 else (parts[0] if parts else "")

    def cover_letter(c: dict[str, Any]) -> str:
        skills = c.get("skills") or []
        return (
            "I am excited about this opportunity. My skills in "
            f"{', '.join(skills[:3])} make me a strong fit."
        )

    def extra_value(c: dict[str, Any], *keys: str) -> str:
        answers = c.get("extra_answers") or {}
        if not isinstance(answers, dict):
            return ""
        normalized = {str(key).strip().lower(): value for key, value in answers.items()}
        for key in keys:
            value = normalized.get(key)
            if value is not None and str(value).strip():
                return str(value)
        return ""

    def normalize_place_value(value: object) -> str:
        text = " ".join(str(value or "").strip().split())
        aliases = {
            "ahmedabd": "Ahmedabad",
            "ahmadabad": "Ahmedabad",
        }
        return aliases.get(text.lower(), text)

    def city_value(c: dict[str, Any]) -> str:
        return normalize_place_value(extra_value(c, "city", "current city"))

    def address_line_value(c: dict[str, Any]) -> str:
        return normalize_place_value(extra_value(c, "address line 1", "address", "street address", "street"))

    def state_value(c: dict[str, Any]) -> str:
        return extra_value(c, "state", "province", "region")

    def postal_code_value(c: dict[str, Any]) -> str:
        return extra_value(c, "postal code", "postcode", "zip", "zip code")

    def country_value(c: dict[str, Any]) -> str:
        return extra_value(c, "country")

    def location_value(c: dict[str, Any]) -> str:
        explicit = normalize_place_value(c.get("location") or "")
        if explicit:
            return explicit
        parts = [city_value(c), state_value(c), country_value(c)]
        return ", ".join(part for part in parts if part)

    def legal_first_name_value(c: dict[str, Any]) -> str:
        return extra_value(c, "legal given name", "legal first name", "given name", "first name") or first_name(c)

    def legal_last_name_value(c: dict[str, Any]) -> str:
        return (
            extra_value(c, "legal family name", "legal surname", "legal last name", "family name", "surname", "last name")
            or last_name(c)
        )

    def legal_full_name_value(c: dict[str, Any]) -> str:
        return extra_value(c, "legal name", "full legal name", "full name") or str(c["name"])

    def local_given_name_value(c: dict[str, Any]) -> str:
        return extra_value(c, "local given name", "local given names", "local first name") or legal_first_name_value(c)

    def local_family_name_value(c: dict[str, Any]) -> str:
        return extra_value(c, "local family name", "local surname", "local last name") or legal_last_name_value(c)

    def local_full_name_value(c: dict[str, Any]) -> str:
        return extra_value(c, "local name", "full local name") or legal_full_name_value(c)

    def latest_job(c: dict[str, Any]) -> dict[str, Any]:
        jobs = c.get("work_experience") or []
        return jobs[0] if isinstance(jobs, list) and jobs else {}

    def latest_education(c: dict[str, Any]) -> dict[str, Any]:
        schools = c.get("education") or []
        return schools[0] if isinstance(schools, list) and schools else {}

    def normalize_profile_url(value: object) -> str:
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

    _COUNTRY_TO_DIAL: dict[str, str] = {
        "india": "+91", "bharat": "+91", "in": "+91",
        "united states": "+1", "usa": "+1", "us": "+1", "america": "+1", "u.s.": "+1", "u.s.a.": "+1",
        "canada": "+1", "ca": "+1",
        "united kingdom": "+44", "uk": "+44", "great britain": "+44", "england": "+44",
        "australia": "+61", "au": "+61",
        "germany": "+49", "france": "+33", "spain": "+34", "italy": "+39",
        "netherlands": "+31", "sweden": "+46", "norway": "+47", "denmark": "+45",
        "singapore": "+65", "malaysia": "+60", "indonesia": "+62", "philippines": "+63",
        "uae": "+971", "united arab emirates": "+971", "saudi arabia": "+966",
        "pakistan": "+92", "bangladesh": "+880", "sri lanka": "+94", "nepal": "+977",
        "china": "+86", "japan": "+81", "south korea": "+82", "hong kong": "+852",
        "brazil": "+55", "mexico": "+52", "ireland": "+353", "new zealand": "+64",
    }

    def _normalize_to_dial_code(raw: str) -> str:
        """Convert a raw dial-code answer (country name or +XX or mixed) to +XX format."""
        raw = raw.strip()
        if re.match(r"^\+\d{1,4}$", raw):
            return raw
        m = re.search(r"\+\d{1,4}", raw)
        if m:
            return m.group(0)
        return _COUNTRY_TO_DIAL.get(raw.lower(), "")

    def phone_country_code_value(c: dict[str, Any]) -> str:
        raw = extra_value(c, "phone country code", "country phone code", "dialing code", "calling code")
        if raw:
            normalized = _normalize_to_dial_code(raw)
            return normalized if normalized else raw
        # Fall back to country name → dial code
        country = extra_value(c, "country") or ""
        if country:
            code = _normalize_to_dial_code(country)
            if code:
                return code
        location = str(c.get("location") or "").strip().lower()
        for cname, code in _COUNTRY_TO_DIAL.items():
            if cname in location:
                return code
        return ""

    def phone_number_value(c: dict[str, Any]) -> str:
        phone = str(c.get("phone") or "").strip()
        if not phone:
            return ""
        # Strip leading country-name text like "India +91 9876543210" → "+91 9876543210"
        phone_clean = re.sub(r"^[A-Za-z\s]+", "", phone).strip()
        # Strip the specific known country code prefix to avoid greedy over-stripping.
        # e.g. "+919876543210" with code "+91" → "9876543210" (not "+9198..." → "76543210")
        code = phone_country_code_value(c)
        if code and phone_clean.startswith(code):
            phone_clean = phone_clean[len(code):].strip()
        digits = re.sub(r"\D+", "", phone_clean) or re.sub(r"\D+", "", phone)
        code_digits = re.sub(r"\D+", "", code)
        if code_digits and digits.startswith(code_digits) and len(digits) > len(code_digits) + 4:
            digits = digits[len(code_digits):]
        return digits or phone

    def local_phone_digits_from_value(value: object) -> str:
        text = re.sub(r"^[A-Za-z\s]+", "", str(value or "")).strip()
        code = phone_country_code_value(candidate)
        if code and text.startswith(code):
            text = text[len(code):].strip()
        elif text.startswith("+"):
            text = re.sub(r"^\+\d{1,4}\s*", "", text).strip()
        digits = re.sub(r"\D+", "", text)
        code_digits = re.sub(r"\D+", "", code)
        if code_digits and digits.startswith(code_digits) and len(digits) > len(code_digits) + 4:
            digits = digits[len(code_digits):]
        return digits

    field_map = {
        "first name": legal_first_name_value,
        "fname": first_name,
        "given name": legal_first_name_value,
        "legal first name": legal_first_name_value,
        "legal given name": legal_first_name_value,
        "last name": legal_last_name_value,
        "lname": last_name,
        "surname": legal_last_name_value,
        "family name": legal_last_name_value,
        "legal last name": legal_last_name_value,
        "legal family name": legal_last_name_value,
        "legal surname": legal_last_name_value,
        "local given name": local_given_name_value,
        "local given names": local_given_name_value,
        "local first name": local_given_name_value,
        "local family name": local_family_name_value,
        "local surname": local_family_name_value,
        "local last name": local_family_name_value,
        "local name": local_full_name_value,
        "full local name": local_full_name_value,
        "full name": legal_full_name_value,
        "name": legal_full_name_value,
        "legal name": legal_full_name_value,
        "full legal name": legal_full_name_value,
        "email": lambda c: c["email"],
        "email address": lambda c: c["email"],
        "username": lambda c: c["email"],
        "phone": phone_number_value,
        "mobile": phone_number_value,
        "telephone": phone_number_value,
        "country phone code": phone_country_code_value,
        "phone country code": phone_country_code_value,
        "dialing code": phone_country_code_value,
        "calling code": phone_country_code_value,
        "location": location_value,
        "city": city_value,
        "address line 1": address_line_value,
        "street address": address_line_value,
        "address": address_line_value,
        "state": state_value,
        "province": state_value,
        "region": state_value,
        "postal code": postal_code_value,
        "postcode": postal_code_value,
        "zip code": postal_code_value,
        "zip": postal_code_value,
        "country": country_value,
        "experience": lambda c: str(c["experience_years"]),
        "years of experience": lambda c: str(c["experience_years"]),
        "technology": lambda c: str((c.get("skills") or c.get("desired_titles") or [""])[0]),
        "tech stack": lambda c: str((c.get("skills") or c.get("desired_titles") or [""])[0]),
        "skills": lambda c: str((c.get("skills") or c.get("desired_titles") or [""])[0]).strip().title(),
        "linkedin": lambda c: normalize_profile_url(c.get("linkedin_url", "")),
        "portfolio": lambda c: c.get("portfolio_url", ""),
        "website": lambda c: c.get("portfolio_url", ""),
    }
    domain = domain_from_url(str(page_data.get("url") or ""))

    def label_has_field_key(label_text: str, key: str) -> bool:
        if " " in key:
            return key in label_text
        if key in {"email", "linkedin", "portfolio", "website", "telephone", "postcode"}:
            return key in label_text
        return re.search(rf"\b{re.escape(key)}\b", label_text) is not None

    def is_screening_question_label(label_text: str) -> bool:
        text = " ".join(str(label_text or "").lower().split())
        return bool(
            len(text) > 30
            and (
                "?" in text
                or re.match(r"^(are|is|do|does|did|have|has|will|would|can|could|should|what|when|where|why|how)\b", text)
            )
        )

    def is_placeholder(value: object) -> bool:
        text = str(value or "").strip().lower()
        return text in {
            "",
            "0 items selected",
            "no items",
            "no items.",
            "select",
            "select...",
            "select one",
            "select one required",
            "select option",
            "select an option",
            "choose",
            "choose...",
            "choose one",
            "choose one required",
            "please select",
            "please select an option",
            "please choose",
            "...",
            "…",
        }

    def fallback_options_for_label(label_text: str, field_type: str) -> list[str]:
        text = str(label_text or "").strip().lower()
        if field_type not in {"select-one", "radio"}:
            return []
        if re.match(r"^(are|is|do|does|did|have|has|will|would|can|could)\b", text):
            return ["Yes", "No"]
        if re.search(r"(?:[.:\n]\s*|\b)(are|is|do|does|did|have|has|will|would|can|could)\b", text):
            return ["Yes", "No"]
        return []

    def extra_answer_for(label: str) -> str | None:
        clean_label = normalize_label(label)
        source_like_label = any(
            token in clean_label
            for token in ("how did you hear", "where did you hear", "hear about us", "learn about this opportunity", "source")
        )
        # Workday source taxonomies vary by tenant. A saved source path from one
        # tenant, such as "Career Website > ...", often breaks another tenant.
        # Only explicit candidate extra answers should override the Workday
        # LinkedIn/job-site default; learned FormAnswer rows are ignored here.
        answer_sources = ("extra_answers",) if is_workday and source_like_label else ("saved_answers", "extra_answers")
        combined_answers: dict[str, Any] = {}
        for source_key in answer_sources:
            source_answers = candidate.get(source_key) or {}
            if isinstance(source_answers, dict):
                combined_answers.update(source_answers)
        if not combined_answers:
            return None

        if "phone extension" in clean_label or ("extension" in clean_label and "phone" in clean_label):
            return None
        normalized = {normalize_label(key): value for key, value in combined_answers.items()}
        generic_phone_labels = {
            "phone",
            "phone number",
            "mobile phone",
            "mobile number",
            "telephone",
            "telephone number",
            "contact number",
        }
        phone_code_keys = {"phone country code", "country phone code", "dialing code", "calling code"}
        is_dial_code_label = any(
            k in clean_label
            for k in ("dialing code", "calling code", "country phone code", "phone code", "country code")
        )
        is_phone_number_label = (
            any(k in clean_label for k in ("phone", "mobile", "cell", "telephone"))
            and not is_dial_code_label
        )
        is_plain_country_label = (
            "country" in clean_label
            and not any(k in clean_label for k in ("phone", "dialing", "calling", "code"))
        )
        is_phone_device_label = any(token in clean_label for token in ("phone device", "phone type"))

        if is_phone_device_label:
            for key in ("phone device type", "phone device", "phone type"):
                value = normalized.get(key)
                if value is None or not str(value).strip():
                    continue
                if re.search(r"\+\d{1,4}", str(value)):
                    continue
                return str(value)
            return None

        if "country phone code" in clean_label or "phone code" in clean_label:
            for key in phone_code_keys:
                value = normalized.get(key)
                if value is not None and str(value).strip():
                    return _normalize_to_dial_code(str(value)) or str(value)
            return None

        for key, value in normalized.items():
            if is_plain_country_label and (
                key in phone_code_keys or any(token in key for token in ("phone code", "dialing code", "calling code"))
            ):
                continue
            if clean_label in generic_phone_labels and key in phone_code_keys:
                continue
            if (
                re.match(r"^(are|is|do|does|did|have|has|will|would|can|could)\b", clean_label)
                and key in {"state", "province", "region", "country", "city", "location"}
            ):
                continue
            key_matches_label = key == clean_label
            if not key_matches_label and len(clean_label) >= 6:
                key_matches_label = clean_label in key
            if not key_matches_label and len(key) >= 5:
                key_matches_label = re.search(rf"\b{re.escape(key)}\b", clean_label) is not None
            if value is not None and key_matches_label:
                if "linkedin" in clean_label or "linkedin" in key:
                    return normalize_profile_url(value)
                if is_phone_device_label and re.search(r"\+\d{1,4}", str(value)):
                    continue
                val_str = str(value)
                # Dial code labels: convert country name to +XX
                if is_dial_code_label:
                    return _normalize_to_dial_code(val_str) or val_str
                # Phone number labels: strip country code prefix, keep local digits
                if is_phone_number_label:
                    digits = local_phone_digits_from_value(val_str)
                    if digits and len(digits) >= 7:
                        return digits
                    if (
                        (key in generic_phone_labels or clean_label in generic_phone_labels or is_phone_number_label)
                        and re.search(r"\+\d{1,4}", val_str)
                    ):
                        continue
                if any(token in clean_label for token in ("city", "location", "address")):
                    return normalize_place_value(val_str)
                return val_str

        return None

    def rule_for(label: str, field_type: str, options: list[str] | None = None) -> dict[str, Any] | None:
        return find_field_rule(domain, label, field_type, options=options) if domain else None

    def answer_for(label: str, field: dict[str, Any] | None = None) -> tuple[str | None, str]:
        # Hardcoded override — "worked here before" is always "No", universally,
        # before any saved-answer lookup or workday_classify.  Stale FormAnswer
        # rows with "Yes" must never reach this point and win.
        _lc = label.strip().lower()
        _compact = "".join(_lc.split())
        _sel_blob = " ".join(
            str((field or {}).get(k) or "")
            for k in ("selector", "automationId", "name", "id")
        ).lower()
        _PREV_TOKENS = (
            "candidateispreviousworker", "previousworker",
            "haspreviousworked", "isformeremployee",
        )
        if (
            any(tok in _compact for tok in _PREV_TOKENS)
            or any(tok in _sel_blob for tok in _PREV_TOKENS)
            or (
                any(v in _lc for v in ("worked", "employed", "employee"))
                and any(q in _lc for q in (
                    "before", "previously", "previous", "former", "past", "prior",
                ))
            )
        ):
            return "No", "hardcoded_previous_worker"

        # Universal Workday intent classifier runs first. If it recognizes
        # the question, its answer is authoritative — overrides extra_answers,
        # saved rules, and fuzzy matches. If it returns intent + None answer
        # (demographic question with no saved candidate value), short-circuit
        # so the caller marks the field unanswered instead of guessing.
        if is_workday:
            try:
                intent, intent_answer, intent_source = workday_classify(
                    label, field=field, candidate=candidate
                )
            except Exception as exc:
                logger.warning("workday_classify failed for label %r: %s", label, exc)
                intent, intent_answer, intent_source = None, None, ""
            if intent is not None:
                if intent_answer:
                    return intent_answer, intent_source or "workday_intent"
                # Known intent but no candidate profile answer.
                # Before giving up, honour any FormAnswer saved (e.g. via the
                # tkinter popup): the user explicitly told us what to put here.
                _saved_map = candidate.get("saved_answers") or {}
                # label is lowercase; saved_answers keys may be mixed-case raw DOM labels.
                # Use a case-insensitive linear scan so the keys always match.
                _saved_hit = next(
                    (v for k, v in _saved_map.items() if k.strip().lower() == label.strip()),
                    None,
                )
                if _saved_hit:
                    return str(_saved_hit), "saved_answer_intent_override"
                # Truly no answer → never guess.
                return None, ""
        label_text = label.strip().lower()
        compact_label = "".join(label_text.split())
        if any(
            token in label_text
            for token in (
                "how did you hear",
                "where did you hear",
                "hear about us",
                "learn about this opportunity",
                "source",
            )
        ):
            return "LinkedIn", "hardcoded_source"
        if "phone extension" in label_text or ("extension" in label_text and "phone" in label_text):
            return None, ""
        # "Today's date" signature fields (typically Self-Identify or
        # Voluntary Disclosure forms) must ALWAYS be the current date —
        # cached rules from prior runs go stale and Workday rejects with
        # "Enter today's date". Detect them by exact label match.
        # "Today's date" signature fields show as "Date" label, often combined
        # with the automationId tokens "datesection" / "datesignedon" / similar.
        # These MUST be the current date — cached rules go stale and Workday
        # rejects with "Enter today's date".
        if (
            label_text.strip() in {"date", "date*", "today's date", "today"}
            or any(token in label_text for token in (
                "today's date", "today", "current date", "signature date", "sign date",
                "datesection", "datesigned", "datesignedon", "signedon",
            ))
            or (label_text.startswith("date ") and "datesection" in label_text)
        ):
            # Use local system time, not UTC — Workday signature fields expect
            # the applicant's "today", which can be a different calendar day
            # from UTC during late-evening hours in IST/EU timezones.
            today = datetime.now().strftime("%m/%d/%Y")
            return today, "today_date"
        # Referrer-name-or-email fields are a Workday conditional that only
        # applies when HDYHAU = "Referred by Employee". If we auto-fill them
        # with candidate.email (which the fuzzy extra_answer_for below would
        # otherwise match because "email" is in the label), Workday throws:
        # "A source can be either a non-referral source, referral source with
        # accompanying referrer or a social share ID." Only fill these when
        # an explicit candidate referrer extra answer exists; otherwise leave
        # blank so the form doesn't conflict with non-referral HDYHAU choices.
        if (
            "name or email" in label_text
            or "referrer" in label_text
            or "referral contact" in label_text
            or ("their name" in label_text and "email" in label_text)
        ):
            referral = extra_value(
                candidate,
                "referrer",
                "referrer name",
                "referral",
                "referral contact",
                "employee referral",
            )
            return (referral, "extra_referral") if referral else (None, "")
        state_location_match = re.search(r"\bare you\b.*\blocated\b.*\bstate of ([a-z .'-]+)\??", label_text)
        if state_location_match:
            requested_state = normalize_label(state_location_match.group(1))
            current_state = normalize_label(state_value(candidate))
            return ("Yes" if requested_state and current_state == requested_state else "No"), "candidate"
        extra_answer = extra_answer_for(label)
        if extra_answer:
            return extra_answer, "extra"
        if is_verification_or_security_label(label_text):
            return None, ""
        generic_labels = {
            "application",
            "select one",
            "select one required",
            "choose one",
            "choose one required",
            "yes",
            "yes required",
            "no",
            "no required",
        }
        if label_text in generic_labels:
            return None, ""
        if is_workday and (
            ("worked" in label_text and any(token in label_text for token in ("previous", "previously", "before")))
            or ("employed" in label_text and any(token in label_text for token in ("previous", "previously", "before")))
        ):
            return "No", "workday_default"
        if is_optional_conditional_field(label_text, False):
            return None, ""
        if "middle name" in label_text:
            return None, ""
        if "preferred name" in label_text or "nickname" in label_text:
            preferred = extra_value(candidate, "preferred name", "nickname")
            return (preferred, "extra") if preferred else (None, "")
        if (
            "name or email" in label_text
            or "referrer" in label_text
            or "referral contact" in label_text
            or ("their name" in label_text and "email" in label_text)
        ):
            referral = extra_value(
                candidate,
                "referrer",
                "referrer name",
                "referral",
                "referral contact",
                "employee referral",
            )
            return (referral or "N/A"), "candidate_referral_fallback"
        if any(key in label_text for key in ("current salary", "expected salary", "salary", "compensation")):
            return None, ""
        if "relocat" in label_text:
            return None, ""
        if (
            "country phone code" in label
            or "phone code" in label
            or "dialing code" in label
            or "calling code" in label
            or ("country" in label and ("dialing" in label or "calling" in label))
            or ("phone" in compact_label and "countrycode" in compact_label)
        ):
            return phone_country_code_value(candidate) or None, "candidate"
        if "phone" in label and (
            "country" in compact_label
            or "dial" in label
            or "#country" in label
        ):
            return phone_country_code_value(candidate) or None, "candidate"
        if "phone device" in label or "phone type" in label:
            return ("Mobile", "candidate") if is_workday else (None, "")
        if any(key in label for key in ("postal code", "postcode", "zip code")) or re.search(r"\bzip\b", label):
            return postal_code_value(candidate) or None, "candidate"
        if any(key in label for key in ("state", "province", "region")):
            # Yes/No question forms ("Is the position within the state of X?")
            # must not return candidate's state — it's not a valid option there.
            # Leave for manual capture instead (see line 1522-1526 for the same guard).
            if re.match(r"^\s*(are|is|do|does|did|have|has|will|would|can|could)\b", label_text):
                return None, ""
            return state_value(candidate) or None, "candidate"
        if "nationality" in label:
            return country_value(candidate) or None, "candidate"
        if "country" in label and "phone" not in label:
            return country_value(candidate) or None, "candidate"
        # Sensitive demographic fields: only fill from candidate.extra_answers.
        # Never guess on behalf of the candidate — if not provided, return None
        # so the field is captured as an unanswered_question for manual answer
        # in the frontend. The user's saved answer flows back automatically.
        if "gender" in label:
            return extra_value(candidate, "gender") or None, "candidate"
        if "ethnicity" in label or "race" in label or "please your ethnicity" in label_text:
            return extra_value(candidate, "ethnicity", "race") or None, "candidate"
        if "veteran" in label or label_text.strip() in {"please a status", "please status"}:
            return extra_value(candidate, "veteran status", "veteran") or None, "candidate"
        if "hispanic" in label or "latino" in label:
            return extra_value(candidate, "hispanic", "latino") or None, "candidate"
        if "disability" in label or "disabled" in label:
            return extra_value(candidate, "disability") or None, "candidate"
        # Visa / work authorization questions
        if "visa" in label or ("authorized" in label and "work" in label) or "sponsorship" in label:
            return extra_value(candidate, "visa", "work authorization", "sponsorship") or None, "candidate"
        # Terms and conditions consent checkbox/select — safe to always accept,
        # the candidate already chose to apply.
        if (
            ("terms" in label and "conditions" in label)
            or "i have read and consent" in label_text
            or "i agree to the terms" in label_text
            or "accept the terms" in label_text
        ):
            return "Yes", "terms_consent"
        if re.search(r"\bcity\b", label):
            value = city_value(candidate)
            return (value, "candidate") if value else (None, "")
        if re.search(r"\baddress line\s*[23]\b", label):
            return None, ""
        if "address line" in label or "street" in label:
            value = address_line_value(candidate)
            return (value, "candidate") if value else (None, "")
        if is_screening_question_label(label):
            # Before bailing out, honour any FormAnswer the user saved manually
            # (e.g. via a tkinter popup in a previous loop iteration or run).
            # Without this check the tkinter answer is silently discarded and the
            # field loops forever as "unanswered".
            _saved_map = candidate.get("saved_answers") or {}
            _saved_hit = next(
                (v for k, v in _saved_map.items() if k.strip().lower() == label.strip()),
                None,
            )
            if _saved_hit:
                return str(_saved_hit), "saved_answer"
            return None, ""
        # Work experience fields — filled from parsed_resume.work_experience[0]
        job = latest_job(candidate)
        edu = latest_education(candidate)
        if any(tok in label_text for tok in ("current employer", "employer name", "company name", "organization name")) or (
            "company" in label_text and any(tok in label_text for tok in ("name", "current", "employer"))
        ):
            value = str(job.get("company") or "")
            return (value, "candidate_work_exp") if value else (None, "")
        if any(tok in label_text for tok in ("job title", "position title", "current title", "current position", "most recent title")):
            value = str(job.get("title") or "")
            return (value, "candidate_work_exp") if value else (None, "")
        if any(tok in label_text for tok in ("school", "university", "college", "institution")) and "employer" not in label_text:
            value = str(edu.get("school") or "")
            return (value, "candidate_education") if value else (None, "")
        if any(tok in label_text for tok in ("degree", "qualification")):
            value = str(edu.get("degree") or "")
            return (value, "candidate_education") if value else (None, "")
        if any(tok in label_text for tok in ("major", "field of study", "area of study", "discipline")):
            value = str(edu.get("major") or "")
            if not value:
                # Fall back to degree abbreviation (e.g. "BTech") so the engine can
                # match it against degree-type options rather than leaving the field
                # blank (which causes the first alphabetical option to stay selected).
                value = str(edu.get("degree") or "")
            return (value, "candidate_education") if value else (None, "")
        for key, fn in field_map.items():
            if label_has_field_key(label, key):
                value = str(fn(candidate) or "")
                return (value, "candidate") if value else (None, "")
        # Never-guess gate for Workday: the rapidfuzz partial-ratio fallback
        # below has been the source of cross-field bleed (e.g. "Country" matched
        # to "Country phone code"). For Workday tenants, if the classifier and
        # explicit field_map keys both missed, return None so the field is
        # captured as an unanswered_question instead of fuzzy-guessing.
        if not is_workday:
            match = process.extractOne(label, list(field_map.keys()), scorer=fuzz.partial_ratio)
            if match and match[1] > 85 and len(label.strip()) > 8 and len(match[0]) >= 6:
                value = str(field_map[match[0]](candidate) or "")
                return (value, "candidate") if value else (None, "")
        return None, ""

    def synthetic_answer_for(label: str, field_type: str) -> str:
        text = label.lower()
        compact_text = "".join(text.split())
        if is_verification_or_security_label(text, field_type):
            return ""
        if "middle name" in text or "relocat" in text:
            return ""
        if "phone extension" in text or ("extension" in text and "phone" in text):
            return ""
        if "preferred name" in text or "nickname" in text:
            return str(extra_value(candidate, "preferred name", "nickname") or "")
        if (
            "name or email" in text
            or "referrer" in text
            or "referral contact" in text
            or ("their name" in text and "email" in text)
        ):
            return str(
                extra_value(
                    candidate,
                    "referrer",
                    "referrer name",
                    "referral",
                    "referral contact",
                    "employee referral",
                )
                or "N/A"
            )
        if is_optional_conditional_field(text, False):
            return ""
        if "last name" in text or "family name" in text or "surname" in text:
            return ""
        if any(key in text for key in ("technology", "tech stack", "primary skill")):
            skills = candidate.get("skills") or []
            desired_titles = candidate.get("desired_titles") or []
            return str(skills[0] if skills else desired_titles[0] if desired_titles else "")
        if any(key in text for key in ("current salary", "expected salary", "salary", "compensation")):
            return ""
        if "email" in text or field_type == "email":
            return str(candidate.get("email") or "")
        if "linkedin" in text:
            return normalize_profile_url(candidate.get("linkedin_url") or "")
        if "veteran" in text:
            return ""
        if "gender" in text or "ethnicity" in text or "race" in text:
            return ""
        if "disability" in text:
            return ""
        if "phone device" in text or "phone type" in text:
            return ""
        if "country phone code" in text or "phone code" in text or ("phone" in compact_text and "countrycode" in compact_text):
            return phone_country_code_value(candidate)
        if any(key in text for key in ("phone", "mobile", "telephone")) or field_type == "tel":
            return phone_number_value(candidate)
        if any(key in text for key in ("portfolio", "website", "url")) or field_type == "url":
            return str(candidate.get("portfolio_url") or candidate.get("linkedin_url") or "")
        if any(key in text for key in ("postal code", "postcode", "zip code")) or re.search(r"\bzip\b", text):
            return postal_code_value(candidate)
        if any(key in text for key in ("state", "province", "region")):
            return state_value(candidate)
        if "nationality" in text:
            return country_value(candidate)
        if "country" in text and "phone" not in text:
            return country_value(candidate)
        if re.search(r"\bcity\b", text):
            return city_value(candidate)
        if "address line" in text or "street" in text:
            return address_line_value(candidate)
        if field_type == "number":
            return ""
        if "salary" in text or "compensation" in text:
            return ""
        if "notice" in text or "start" in text or "availability" in text:
            return ""
        # Work experience fields — filled from parsed_resume.work_experience[0]
        job = latest_job(candidate)
        edu = latest_education(candidate)
        if "school" in text or "university" in text or "college" in text or "institution" in text:
            return str(job.get("school") or edu.get("school") or "")
        if "degree" in text or "qualification" in text:
            return str(edu.get("degree") or "")
        if "major" in text or "field of study" in text or "area of study" in text or "discipline" in text:
            return str(edu.get("major") or "")
        if any(tok in text for tok in ("current employer", "employer name", "company name", "organization name")):
            return str(job.get("company") or "")
        if "company" in text and any(tok in text for tok in ("name", "current", "employer")):
            return str(job.get("company") or "")
        if any(tok in text for tok in ("job title", "position title", "current title", "current position", "most recent title")):
            return str(job.get("title") or "")
        if ("title" in text or "position" in text) and any(tok in text for tok in ("job", "work", "previous", "last", "current")):
            return str(job.get("title") or "")
        if "title" in text or "position" in text:
            # If we have work experience data, use that; otherwise fall back to desired title
            if job.get("title"):
                return str(job["title"])
            desired_titles = candidate.get("desired_titles") or []
            return str(desired_titles[0] if desired_titles else "")
        # Date fields: use actual work experience dates, not today's date
        if any(tok in text for tok in ("from date", "start date of employment", "employment start", "date from")):
            return str(job.get("from") or "")
        if any(tok in text for tok in ("to date", "end date of employment", "employment end", "date to")):
            val = job.get("to") or ""
            if not val and job.get("current"):
                val = "Present"
            return str(val)
        if text.strip() in {"from", "from*"} and job.get("from"):
            return str(job["from"])
        if text.strip() in {"to", "to*"} and job.get("to"):
            val = job.get("to") or ""
            if not val and job.get("current"):
                val = "Present"
            return str(val)
        if "graduation" in text or ("graduation" in text and "date" in text):
            return str(edu.get("to") or edu.get("graduation_year") or "")
        if field_type == "date":
            return ""
        if field_type == "month":
            return ""
        if field_type == "time":
            return ""
        if field_type == "password" or "password" in text:
            return ""
        if field_type == "textarea" or any(key in text for key in ("describe", "summary", "about", "why")):
            return ""
        return ""

    def option_text(option: dict[str, Any]) -> str:
        return str(option.get("text") or option.get("label") or option.get("value") or "").strip()

    def first_real_option(options: list[dict[str, Any]]) -> str | None:
        for option in options:
            text = option_text(option)
            if text and not is_placeholder(text):
                return text
        return None

    def preferred_choice(label: str, options: list[str], answer: str | None = None) -> str | None:
        choices = [choice.strip() for choice in options if choice and not is_placeholder(choice)]
        if not choices:
            return None

        label_text = label.lower()
        preferred = answer or ""
        phone_code = re.search(r"\+\d{1,4}", preferred)
        if phone_code and any(key in label_text for key in ("phone", "dialing", "calling", "country code")):
            code = phone_code.group(0)
            for choice in choices:
                if code in re.findall(r"\+\d{1,4}", choice):
                    return choice
            return None
        if "country" in label_text and "phone" not in label_text:
            normalized_preferred = normalize_label(preferred)
            for choice in choices:
                normalized_choice = normalize_label(choice)
                if normalized_choice == normalized_preferred or normalized_choice.startswith(f"{normalized_preferred} "):
                    return choice
            return None
        if any(token in label_text for token in ("gender", "ethnicity", "race", "veteran")):
            normalized_preferred = normalize_label(preferred)
            decline_terms = (
                "do not wish",
                "do not want",
                "decline",
                "prefer not",
                "not disclose",
                "not self identify",
                "choose not",
                "not to respond",
                "no response",
            )
            if any(term in normalized_preferred for term in decline_terms):
                for choice in choices:
                    normalized_choice = normalize_label(choice)
                    if any(term in normalized_choice for term in decline_terms):
                        return choice

        if preferred:
            preferred_norm = normalize_label(preferred)
            for choice in choices:
                if normalize_label(choice) == preferred_norm:
                    return choice
            if len(label) > 8:
                match = process.extractOne(preferred, choices, scorer=fuzz.partial_ratio)
                if match and match[1] > 85:
                    return str(match[0])
        return None

    def is_previous_worker_controller(label: str, field: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (label, field.get("label"), field.get("selector"), field.get("automationId"))
        ).lower()
        previous_terms = ("previous", "previously", "before", "past", "former", "prior")
        return (
            "candidateispreviousworker" in text
            or ("worked" in text and any(token in text for token in previous_terms))
            or ("employed" in text and any(token in text for token in previous_terms))
            or ("employee" in text and any(token in text for token in previous_terms))
        )

    previous_worker_no_cache: bool | None = None

    def desired_previous_worker_no() -> bool:
        nonlocal previous_worker_no_cache
        if previous_worker_no_cache is not None:
            return previous_worker_no_cache
        result = False
        for radio_field in page_data.get("fields", []):
            if not isinstance(radio_field, dict):
                continue
            if str(radio_field.get("type") or "").lower() != "radio":
                continue
            if not (radio_field.get("radioOptions") or []):
                continue
            controller_label = str(radio_field.get("label") or "")
            if not is_previous_worker_controller(controller_label, radio_field):
                continue
            # Always No for every portal — never fill previousWorker-- detail fields
            result = True
            break
        previous_worker_no_cache = result
        return result

    def is_previous_worker_detail_field(label: str, field: dict[str, Any]) -> bool:
        if str(field.get("type") or "").lower() == "radio" or field.get("radioOptions"):
            return False
        text = " ".join(
            str(value or "")
            for value in (label, field.get("label"), field.get("selector"), field.get("automationId"))
        ).lower()
        if "candidateispreviousworker" in text:
            return False
        if "previousworker--" in text:
            return True
        return any(
            token in text
            for token in (
                "old email address",
                "previous manager",
                "old manager",
                "previous employment",
                "prior employment",
                "employee id",
                "smid",
            )
        ) and any(context in str(page_data.get("text") or "").lower() for context in ("worked", "previous", "before"))

    def previous_worker_detail_answer(label: str, field: dict[str, Any], required: bool) -> tuple[str, str]:
        selector_text = str(field.get("selector") or "").lower()
        if "previousworker--" not in selector_text:
            return "", ""
        if "candidateispreviousworker" in selector_text:
            return "", ""
        if desired_previous_worker_no() and not required:
            return "", ""
        text = " ".join(
            str(value or "")
            for value in (label, field.get("label"), field.get("placeholder"), field.get("automationId"), selector_text)
        ).lower()
        if "email" in text:
            return str(candidate.get("email") or "").strip(), "candidate"
        if "manager" in text:
            return str(extra_value(candidate, "previous manager", "manager") or "N/A"), "workday_previous_worker_fallback"
        if "employeeid" in selector_text or "employee id" in text or "smid" in text:
            value = str(extra_value(candidate, "employee id", "employeeid", "smid") or "").strip()
            return (value or ("N/A" if required else "")), "workday_previous_worker_fallback"
        if "country" in text:
            value = country_value(candidate) or location_value(candidate) or state_value(candidate) or city_value(candidate)
            return str(value or "N/A").strip(), "candidate"
        if "location" in selector_text or "location" in text:
            value = location_value(candidate) or country_value(candidate) or state_value(candidate) or city_value(candidate)
            return str(value or "N/A").strip(), "candidate"
        return ("N/A" if required else ""), "workday_previous_worker_fallback"

    def matching_option_for_answer(answer: str, options: list[str], label: str) -> str | None:
        choices = [option for option in options if option and not is_placeholder(option)]
        if not choices:
            return None
        label_text = label.lower()
        answer_parts = [part.strip() for part in str(answer or "").split(" > ") if part.strip()]
        top_level_answer = answer_parts[0] if answer_parts else answer
        answer_text = top_level_answer.lower()
        full_answer_text = str(answer or "").lower()
        normalized_choices = {choice.lower(): choice for choice in choices}
        if any(token in label_text for token in ("how did you hear", "hear about us", "source", "learn about this opportunity")):
            if len(answer_parts) > 1:
                normalized_parts = [normalize_label(part) for part in answer_parts]
                for choice in choices:
                    normalized_choice = normalize_label(choice)
                    if normalized_choice == normalize_label(answer) or all(part and part in normalized_choice for part in normalized_parts):
                        return choice
                leaf = normalized_parts[-1]
                parent = normalized_parts[0]
                for choice in choices:
                    normalized_choice = normalize_label(choice)
                    if leaf and leaf in normalized_choice and (not parent or parent in normalized_choice):
                        return choice
            if "linkedin" in full_answer_text or "linked in" in full_answer_text:
                for key, choice in normalized_choices.items():
                    if ("linkedin" in key or "linked in" in key) and any(
                        source_token in key
                        for source_token in ("social media", "social", "job site", "job board", "job boards")
                    ):
                        return choice
                for key, choice in normalized_choices.items():
                    if "social media" in key or key == "social" or " social" in key:
                        return choice
                for key, choice in normalized_choices.items():
                    if "job site" in key:
                        return choice
                for key, choice in normalized_choices.items():
                    if "linkedin" in key or "linked in" in key:
                        return choice
                for key, choice in normalized_choices.items():
                    if "job board" in key or "job boards" in key:
                        return choice
            if any(token in full_answer_text for token in ("linkedin", "indeed", "glassdoor", "naukri", "monster", "job board", "job site", "social media")):
                for key, choice in normalized_choices.items():
                    if "job site" in key or "social media" in key or "job board" in key:
                        return choice
            if any(token in answer_text for token in ("website", "career site", "company site")):
                for key, choice in normalized_choices.items():
                    if "company website" in key or "career site" in key:
                        return choice
            if "referral" in answer_text:
                for key, choice in normalized_choices.items():
                    if "referral" in key:
                        return choice
        if any(token in label_text for token in ("phone device", "phone type")):
            for key, choice in normalized_choices.items():
                if any(device in key for device in ("mobile", "cell", "smartphone", "phone")) and "country" not in key:
                    return choice
            for key, choice in normalized_choices.items():
                if "fax" not in key and "landline" not in key:
                    return choice
        if any(token in label_text for token in ("gender", "ethnicity", "race", "veteran")):
            for key, choice in normalized_choices.items():
                if any(
                    token in key
                    for token in (
                        "do not wish",
                        "do not want",
                        "decline",
                        "prefer not",
                        "prefer not to say",
                        "not prefer to say",
                        "not disclose",
                        "not self-identify",
                        "choose not",
                        "not to respond",
                        "no response",
                    )
                ):
                    return choice
        phone_code = re.search(r"\+\d{1,4}", answer)
        if phone_code and any(key in label_text for key in ("phone", "dialing", "calling", "country code")):
            code = phone_code.group(0)
            for choice in choices:
                if code in re.findall(r"\+\d{1,4}", choice):
                    return choice
            return None
        if "country" in label_text and "phone" not in label_text:
            normalized_answer = normalize_label(top_level_answer)
            for choice in choices:
                normalized_choice = normalize_label(choice)
                if normalized_choice == normalized_answer or normalized_choice.startswith(f"{normalized_answer} "):
                    return choice
            return None
        # Degree/field-of-study: map common abbreviations to broader search keywords.
        # Fuzz.ratio("BTech", "Computer Science and Engineering") ≈ 20, which never
        # reaches the 85 threshold — so add a keyword-based fallback here.
        if any(tok in label_text for tok in ("field of study", "major", "area of study", "discipline", "concentration")):
            _DEGREE_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
                "btech": ("engineering", "technology"),
                "b.tech": ("engineering", "technology"),
                "btech.": ("engineering", "technology"),
                "be": ("engineering",),
                "b.e": ("engineering",),
                "b.e.": ("engineering",),
                "mtech": ("engineering", "technology"),
                "m.tech": ("engineering", "technology"),
                "me": ("engineering",),
                "m.e": ("engineering",),
                "bsc": ("science",),
                "b.sc": ("science",),
                "b.sc.": ("science",),
                "msc": ("science",),
                "m.sc": ("science",),
                "m.sc.": ("science",),
                "mba": ("business", "management"),
                "bba": ("business", "administration"),
                "bca": ("computer", "application"),
                "mca": ("computer", "application"),
                "bcom": ("commerce",),
                "b.com": ("commerce",),
                "mcom": ("commerce",),
                "m.com": ("commerce",),
                "phd": ("doctor", "doctorate", "research"),
                "ph.d": ("doctor", "doctorate", "research"),
                "ph.d.": ("doctor", "doctorate", "research"),
            }
            norm_ans = answer_text.strip().lower().replace(" ", "")
            degree_keywords = _DEGREE_KEYWORD_MAP.get(norm_ans) or _DEGREE_KEYWORD_MAP.get(answer_text.strip().lower())
            if degree_keywords:
                # Priority 1: option text contains any degree keyword
                for keyword in degree_keywords:
                    for key, choice in normalized_choices.items():
                        if keyword in key:
                            return choice
                # Priority 2: partial token match
                for keyword in degree_keywords:
                    for key, choice in normalized_choices.items():
                        if any(keyword in tok for tok in key.split()):
                            return choice
        match = process.extractOne(top_level_answer, choices, scorer=fuzz.ratio)
        if match and match[1] > 85:
            return str(match[0])
        return None

    def _normalized_select_text(value: object) -> str:
        text = str(value or "").strip()
        text = " ".join(text.split())
        text = text.replace("0 items ed", "0 items selected")
        text = text.replace("1 item ed", "1 item selected")
        text = text.replace("1 items ed", "1 items selected")
        lowered = text.lower()
        for prefix in ("0 items selected", "1 item selected", "1 items selected"):
            if lowered.startswith(prefix):
                text = text[len(prefix):].lstrip(" ,:-")
                lowered = text.lower()
        if lowered.startswith("error"):
            return ""
        return text

    def _is_placeholder_select_text(value: object) -> bool:
        normalized = " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())
        return normalized in {
            "",
            "search",
            "select",
            "select one",
            "select an option",
            "choose",
            "choose one",
            "required",
            "expanded",
            "collapsed",
            "no items",
            "no items.",
            "0 items selected",
            "0 item selected",
        }

    def field_has_value(field: dict[str, Any], field_type: str) -> bool:
        def normalize_display(value: object) -> str:
            return _normalized_select_text(value)

        if field_type == "file":
            return bool(field.get("fileCount")) or bool(str(field.get("value") or "").strip())
        if field_type == "checkbox":
            return bool(field.get("checked"))
        if field_type == "radio":
            return any(option.get("checked") for option in field.get("radioOptions") or [])
        if field_type in ("select-one", "select-multiple"):
            value = normalize_display(field.get("value"))
            selected_text = normalize_display(field.get("selectedText"))
            placeholder = str(field.get("placeholder") or "").strip().lower()
            automation_id = str(field.get("automationId") or "").strip().lower()
            if selected_text == "" and (placeholder == "search" or automation_id == "searchbox"):
                return False
            display_value = selected_text or value
            return bool(display_value) and not is_placeholder(display_value) and not _is_placeholder_select_text(display_value)
        selected_text = normalize_display(field.get("selectedText"))
        if selected_text and not is_placeholder(selected_text) and not _is_placeholder_select_text(selected_text):
            return True
        return bool(str(field.get("value") or "").strip())

    def split_embedded_validation_label(label_text: object) -> tuple[str, str]:
        raw = " ".join(str(label_text or "").split()).strip()
        if not raw:
            return "", ""

        def dedupe_repeated_label(value: str) -> str:
            words = value.split()
            if len(words) < 2 or len(words) % 2:
                return value.strip()
            midpoint = len(words) // 2
            if [word.lower() for word in words[:midpoint]] == [word.lower() for word in words[midpoint:]]:
                return " ".join(words[:midpoint]).strip()
            return value.strip()

        lower = raw.lower()
        for marker in (
            " is not in a valid format",
            " must be a valid ",
            " must be valid ",
            " is invalid",
        ):
            marker_index = lower.rfind(marker)
            if marker_index <= 0:
                continue
            subject_part = raw[:marker_index].strip()
            suffix = raw[marker_index:].strip()
            subject = dedupe_repeated_label(subject_part)
            return subject, f"{subject} {suffix}".strip()

        match = re.search(r"\b(?:please\s+)?(?:enter|provide)\s+(?:a\s+)?valid\b", raw, flags=re.I)
        if match and match.start() > 0:
            return raw[: match.start()].strip(), raw[match.start() :].strip()
        return raw, ""

    def split_date_group(selector: object) -> str:
        text = str(selector or "")
        match = re.search(r"(.+)-dateSection(?:Month|Day|Year)-input$", text, flags=re.I)
        return match.group(1) if match else ""

    def should_correct_prefilled_text(label: str, field_type: str, current_value: str, desired_value: str | None) -> bool:
        desired = str(desired_value or "").strip()
        current = str(current_value or "").strip()
        if not desired or not current:
            return False
        normalized_type = str(field_type or "").lower()
        if normalized_type in {"textarea", "password", "date", "month", "time", "number"}:
            return False

        text = " ".join(str(label or "").lower().replace("_", " ").replace("-", " ").split())
        if not text:
            return False
        if any(token in text for token in ("middle name", "preferred name", "nickname", "extension", "salary", "compensation")):
            return False
        if any(token in text for token in ("country phone code", "phone code", "dialing code", "calling code")):
            return False

        correctable = any(
            token in text
            for token in (
                "first name",
                "given name",
                "last name",
                "family name",
                "surname",
                "full name",
                "legal name",
                "email",
                "phone",
                "mobile",
                "telephone",
                "postal code",
                "postcode",
                "zip code",
                "city",
                "address line",
                "street",
                "linkedin",
                "portfolio",
                "website",
            )
        ) or normalized_type in {"email", "tel", "phone", "url"}
        if not correctable:
            return False

        if "phone" in text or "mobile" in text or "telephone" in text or normalized_type in {"tel", "phone"}:
            current_digits = re.sub(r"\D+", "", current)
            desired_digits = re.sub(r"\D+", "", desired)
            if not current_digits or not desired_digits:
                return current.lower() != desired.lower()
            compare_len = min(10, len(current_digits), len(desired_digits))
            return current_digits[-compare_len:] != desired_digits[-compare_len:]

        if "linkedin" in text or "portfolio" in text or "website" in text or normalized_type == "url":
            return normalize_profile_url(current).lower() != normalize_profile_url(desired).lower()

        return " ".join(current.lower().split()) != " ".join(desired.lower().split())

    def is_honeypot_field(label: str, field: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                label,
                field.get("label"),
                field.get("placeholder"),
                field.get("automationId"),
                field.get("selector"),
            )
        ).lower()
        honeypot_terms = (
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
        return any(term in text for term in honeypot_terms)

    def is_marketing_or_alert_field(label: str, field: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                label,
                field.get("label"),
                field.get("placeholder"),
                field.get("automationId"),
                field.get("selector"),
                field.get("name"),
            )
        ).lower()
        return any(
            term in text
            for term in (
                "subscribe",
                "newsletter",
                "job alert",
                "email alert",
                "create alert",
                "receive similar jobs",
                "similar jobs by email",
                "new jobs by email",
                "jobs delivered",
                "delivered to your inbox",
            )
        )

    def is_search_navigation_field(label: str, field: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                label,
                field.get("label"),
                field.get("placeholder"),
                field.get("automationId"),
                field.get("selector"),
                field.get("name"),
            )
        ).lower()
        return any(
            term in text
            for term in (
                "what?",
                "where?",
                "job, company, title",
                "city, state or zip code",
                "search jobs",
                "search for jobs",
                "search similar jobs",
            )
        )

    def is_optional_reference_field(label: str, field: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                label,
                field.get("label"),
                field.get("selector"),
                field.get("automationId"),
                field.get("name"),
                field.get("id"),
            )
        ).lower()
        return (
            "reference_" in text
            or "add reference" in page_text
            and any(token in text for token in ("relationship", "position title", "business phone", "reference_"))
        )

    def is_select_like_label(label: str, field: dict[str, Any]) -> bool:
        aria_autocomplete = str(field.get("ariaAutocomplete") or "").lower()
        role = str(field.get("role") or "").lower()
        tag_name = str(field.get("tagName") or "").lower()
        error_text = str(field.get("errorText") or "").strip().lower()
        selector = str(field.get("selector") or "").lower()
        placeholder = str(field.get("placeholder") or "").strip().lower()
        is_editable_input = tag_name == "input" and not field.get("readOnly")
        if is_editable_input and role == "combobox" and aria_autocomplete in {"list", "both", "inline"}:
            if error_text.startswith("select") or placeholder.startswith("select"):
                return True
            if selector in {"#country", "#candidate-location"} or selector.startswith("#question_"):
                return True
            return False
        text = " ".join(
            str(value or "")
            for value in (label, field.get("label"), field.get("placeholder"), field.get("automationId"))
        ).lower()
        return any(
            term in text
            for term in (
                "how did you hear",
                "how did you learn",
                "hear about us",
                "learn about this opportunity",
                "source",
                "phone device type",
                "phone type",
                "country phone code",
                "phone code",
                "country",
                "nationality",
            )
        )

    def is_phone_country_code_control(label: str, field: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                label,
                field.get("label"),
                field.get("placeholder"),
                field.get("automationId"),
                field.get("selector"),
                field.get("role"),
            )
        ).lower()
        compact = "".join(text.split())
        if any(token in text for token in ("country phone code", "phone code", "dialing code", "calling code")):
            return True
        return bool(
            "phone" in text
            and (
                "countrycode" in compact
                or "#country" in text
                or "country" in str(field.get("selector") or "").lower()
            )
        )

    page_text = str(page_data.get("text") or "").lower()
    upload_success_visible = "successfully uploaded" in page_text or "file-upload-successful" in page_text
    unfilled_required_fields = 0
    for field in page_data.get("fields", []):
        raw_label = " ".join(
            str(field.get(key) or "")
            for key in ("label", "placeholder", "automationId")
        ).strip()
        field_label, embedded_field_error = split_embedded_validation_label(raw_label)
        label = field_label.lower().strip()
        field_type = str(field.get("type") or "").lower()
        if (
            field_type in {"select-one", "select-multiple"}
            and str(field.get("tagName") or "").lower() == "span"
            and not field.get("options")
        ):
            continue
        # Workday phone-country-code popup renders every country as an isolated
        # <input type="radio"> with automationId="radioBtn" and no grouped radioOptions.
        # These are not real form questions — skip them entirely.
        if (
            field_type == "radio"
            and str(field.get("automationId") or "").strip().lower() == "radiobtn"
            and not (field.get("radioOptions") or [])
        ):
            continue
        non_text_types = {"checkbox", "radio", "file", "select-one", "select-multiple"}
        has_value = field_has_value(field, field_type)
        field_answer, field_answer_source = answer_for(label, field=field)
        field_invalid = bool(field.get("invalid")) or bool(embedded_field_error and has_value)
        field_error = str(field.get("errorText") or embedded_field_error or "")
        required_hint = " ".join(
            str(field.get(key) or "")
            for key in ("label", "placeholder", "automationId", "selectedText", "errorText")
        )
        is_required = bool(field.get("required")) or "*" in required_hint or " required" in required_hint.lower() or (
            field_invalid
            and (
                not has_value
                or any(term in field_error.lower() for term in ("required", "please enter", "please fill"))
            )
        )
        date_group = split_date_group(field.get("selector"))
        if date_group:
            date_label = str(field.get("label") or field.get("placeholder") or field_label or "").strip()
            if date_group in handled_date_groups:
                continue
            handled_date_groups.add(date_group)
            date_clean_key = date_label.lower().strip() or label
            saved_date_rule = rule_for(date_clean_key, "date") or rule_for(date_clean_key, field_type)
            date_rule_answer = str(saved_date_rule.get("value") or "") if saved_date_rule else ""
            date_answer = str(field_answer or date_rule_answer or "").strip()
            if date_answer and not has_value:
                actions.append(
                    {
                        "type": "fill",
                        "selector": field.get("selector"),
                        "label": date_label,
                        "field_type": "date",
                        "value": date_answer,
                        "source": field_answer_source or ("rule" if date_rule_answer else "manual_date"),
                        "required": True,
                    }
                )
            elif not has_value:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": date_label,
                        "field_type": "date",
                        "options": ["Use MM/DD/YYYY"],
                        "required": True,
                        "blocker_kind": "missing_required",
                    }
                )
            continue
        if is_verification_or_security_label(label, field_type) and not field_answer:
            continue
        if is_honeypot_field(label, field):
            if has_value:
                actions.append(
                    {
                        "type": "clear",
                        "selector": field.get("selector"),
                        "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                        "field_type": field_type,
                        "source": "honeypot",
                    }
                )
            continue

        if is_marketing_or_alert_field(label, field):
            continue
        # Clear stale referrer-name-or-email fields when there's no explicit
        # referrer extra to fill them with. Leaving "abhy@gmail.com" in this
        # conditional field while HDYHAU is "Career Site" triggers Workday's
        # "A source can be either a non-referral source, referral source with
        # accompanying referrer or a social share ID." page error.
        label_lower_for_referrer = label.lower() if isinstance(label, str) else ""
        if (
            has_value
            and not field_answer
            and (
                "name or email" in label_lower_for_referrer
                or "referrer" in label_lower_for_referrer
                or "referral contact" in label_lower_for_referrer
                or ("their name" in label_lower_for_referrer and "email" in label_lower_for_referrer)
            )
        ):
            actions.append(
                {
                    "type": "clear",
                    "selector": field.get("selector"),
                    "label": label,
                    "field_type": field_type,
                    "source": "referrer_conflict_clear",
                }
            )
            continue
        if is_search_navigation_field(label, field):
            continue
        if is_optional_reference_field(label, field) and not has_value:
            continue
        if desired_previous_worker_no() and is_previous_worker_detail_field(label, field):
            continue
        if is_required and not has_value:
            unfilled_required_fields += 1

        if (
            field_invalid
            and has_value
            and "phone" in label
            and "extension" not in label
            and any(term in field_error.lower() for term in ("valid", "too short", "too long"))
        ):
            desired_phone = phone_number_value(candidate)
            if desired_phone:
                actions.append(
                    {
                        "type": "fill",
                        "selector": field.get("selector"),
                        "label": field_label or "Phone Number",
                        "field_type": field_type,
                        "value": desired_phone,
                        "source": "candidate",
                        "required": True,
                    }
                )
            else:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field_label or "Phone Number",
                        "field_type": field_type,
                        "options": [field_error] if field_error else [],
                        "required": True,
                        "blocker_kind": "validation_error",
                    }
                )
            continue

        if field_invalid and has_value:
            desired_value = str(field_answer or "").strip()
            current_value_raw = str(field.get("value") or "").strip()
            if desired_value and desired_value != current_value_raw and field_type not in non_text_types:
                actions.append(
                    {
                        "type": "fill",
                        "selector": field.get("selector"),
                        "label": field_label,
                        "field_type": field_type,
                        "value": desired_value,
                        "source": field_answer_source or "candidate",
                        "required": True,
                    }
                )
            else:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field_label,
                        "field_type": field_type,
                        "options": [field_error] if field_error else [],
                        "required": True,
                        "blocker_kind": "validation_error",
                    }
                )
            continue

        if field_type not in non_text_types and has_value:
            current_value_raw = str(field.get("value") or "").strip()
            current_value = current_value_raw.lower()
            if should_correct_prefilled_text(label, field_type, current_value_raw, field_answer):
                actions.append(
                    {
                        "type": "fill",
                        "selector": field.get("selector"),
                        "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                        "field_type": field_type,
                        "value": str(field_answer or "").strip(),
                        "source": field_answer_source or "candidate_correction",
                        "required": is_required,
                    }
                )
                continue
            has_real_profile_url = bool(
                str(candidate.get("linkedin_url") or "").strip()
                or str(candidate.get("portfolio_url") or "").strip()
            )
            normalized_url = normalize_profile_url(
                candidate.get("linkedin_url") or candidate.get("portfolio_url") or current_value_raw
            )
            if (
                ("linkedin" in label or field_type == "url")
                and has_real_profile_url
                and current_value
                and not current_value.startswith(("http://", "https://"))
            ):
                if normalized_url and normalized_url.lower() != current_value:
                    actions.append(
                        {
                            "type": "fill",
                            "selector": field.get("selector"),
                            "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                            "field_type": field_type,
                            "value": normalized_url,
                            "source": "normalized_url",
                            "required": is_required,
                        }
                    )
                    continue
            if (
                current_value in {"https://example.com", "http://example.com", "example.com"}
                and ("linkedin" in label or field_type == "url")
                and not has_real_profile_url
            ):
                actions.append(
                    {
                        "type": "clear",
                        "selector": field.get("selector"),
                        "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                        "field_type": field_type,
                        "source": "invalid_placeholder_url",
                    }
                )
                continue
            continue
        if field_type in {"select-one", "select-multiple"} and has_value:
            current_display = _normalized_select_text(field.get("selectedText") or field.get("value") or "")
            option_texts = [
                text
                for text in (_normalized_select_text(option.get("text") or "") for option in field.get("options", []))
                if text and not _is_placeholder_select_text(text)
            ]
            if field_answer and option_texts:
                matched_option = matching_option_for_answer(str(field_answer), option_texts, label)
                if matched_option and _normalize_page_text(matched_option) != _normalize_page_text(current_display):
                    actions.append(
                        {
                            "type": "select",
                            "selector": field.get("selector"),
                            "value": matched_option,
                            "answer": field_answer,
                            "label": field.get("label") or "",
                            "field_type": field_type,
                            "source": field_answer_source,
                            "required": is_required,
                            "control_kind": field.get("controlKind"),
                        }
                    )
                    continue
            if field_answer and any(token in label for token in ("how did you hear", "hear about us", "source", "phone device", "phone type")):
                if _normalize_page_text(str(field_answer)) != _normalize_page_text(current_display):
                    actions.append(
                        {
                            "type": "select",
                            "selector": field.get("selector"),
                            "value": field_answer,
                            "answer": field_answer,
                            "label": field.get("label") or "",
                            "field_type": field_type,
                            "source": field_answer_source,
                            "required": is_required,
                            "control_kind": field.get("controlKind"),
                        }
                    )
                    continue
            # popup_select_button / combobox_button fields never expose static
            # option text — the list only appears in the DOM after clicking.
            # If we have a trusted answer and the current displayed value is
            # different, still emit a "select" action so the executor can
            # force-open the popup and pick the right value.
            _popup_ck = str(field.get("controlKind") or "").lower()
            if (
                field_answer
                and not option_texts
                and _popup_ck in {"popup_select_button", "combobox_button", "custom_select"}
            ):
                if _normalize_page_text(str(field_answer)) != _normalize_page_text(current_display):
                    actions.append(
                        {
                            "type": "select",
                            "selector": field.get("selector"),
                            "value": field_answer,
                            "answer": field_answer,
                            "label": field.get("label") or "",
                            "field_type": field_type,
                            "source": field_answer_source,
                            "required": is_required,
                            "control_kind": field.get("controlKind"),
                        }
                    )
                    continue
            continue
        if field_type == "checkbox" and has_value:
            continue
        if field_type == "radio" and has_value:
            radio_options = field.get("radioOptions") or []
            group_key = "|".join(opt.get("selector") or "" for opt in radio_options)
            if not radio_options or group_key in handled_radio_groups:
                continue
            option_labels = [opt.get("label") or opt.get("value") or "" for opt in radio_options]
            # Previous-worker controllers must always end up on "No" — never trust
            # whatever Workday restored from the server draft. If the currently
            # checked option is not "No", fall through to the main radio handler
            # which forces No.
            if is_previous_worker_controller(str(field.get("label") or ""), field):
                current_radio = next(
                    (normalize_label(opt.get("label") or opt.get("value") or "")
                     for opt in radio_options if opt.get("checked")),
                    "",
                )
                if current_radio not in {"no", "false"}:
                    pass  # fall through to main radio handler below (force No)
                else:
                    continue
            else:
                radio_rule = rule_for(label, field_type, option_labels)
                radio_rule_val = str(radio_rule.get("value") or "") if radio_rule else ""
                desired_radio = preferred_choice(label, option_labels, field_answer or radio_rule_val) if (radio_rule or field_answer) else None
                if desired_radio:
                    current_radio = next(
                        (normalize_label(opt.get("label") or opt.get("value") or "")
                         for opt in radio_options if opt.get("checked")),
                        "",
                    )
                    if current_radio and normalize_label(desired_radio) == current_radio:
                        continue
                else:
                    continue
        if field_type in {"select-one", "select-multiple", "file"} and has_value:
            continue

        previous_detail_value, previous_detail_source = previous_worker_detail_answer(label, field, is_required)
        if (
            previous_detail_value
            and field_type not in non_text_types
            and field.get("selector")
            and (is_required or field_invalid or not has_value)
        ):
            actions.append(
                {
                    "type": "fill",
                    "selector": field["selector"],
                    "value": previous_detail_value,
                    "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                    "field_type": field_type,
                    "source": previous_detail_source,
                    "required": is_required,
                }
            )
            continue

        if field_type == "file":
            if upload_success_visible:
                continue
            file_text = " ".join(
                str(field.get(key) or "")
                for key in ("label", "placeholder", "automationId", "selector")
            ).lower()
            if "cover" in file_text and not is_required:
                continue
            if candidate.get("resume_path"):
                actions.append(
                    {
                        "type": "upload",
                        "selector": field.get("selector"),
                        "path": candidate["resume_path"],
                    }
                )
            elif not has_value:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "resume",
                        "field_type": field_type,
                        "options": [],
                    }
                )
            continue

        if field_type == "checkbox":
            checkbox_key = normalize_label(field.get("label") or field.get("placeholder") or field.get("automationId") or "")
            # Workday CheckboxGroup-with-options (e.g. disability self-id):
            # multiple <input type=checkbox> sharing the same group label with
            # per-checkbox <label for> texts populated into radioOptions. Surface
            # the GROUP as a single unanswered_question with the option labels
            # so the user can pick a real option in the manual-answer UI.
            group_options = [
                str(o.get("label") or "").strip()
                for o in (field.get("radioOptions") or [])
                if isinstance(o, dict) and str(o.get("label") or "").strip()
            ]
            if checkbox_key and len(group_options) >= 2:
                if checkbox_key in handled_checkbox_groups:
                    continue
                handled_checkbox_groups.add(checkbox_key)
                # Have any of the group's options already been checked? If so,
                # the answer's already set — skip the question.
                any_checked = any(
                    bool(o.get("checked"))
                    for o in (field.get("radioOptions") or [])
                    if isinstance(o, dict)
                )
                if any_checked:
                    continue
                # Before surfacing as unanswered, try to apply a saved rule or
                # candidate answer. The UI saves checkbox-group answers as
                # field_type="select-one" (since that's how they were shown),
                # so look up rules with that type against the group options.
                group_rule = rule_for(label, "select-one", group_options)
                rule_value = str(group_rule.get("value") or "") if group_rule else ""
                chosen_value = str(field_answer or rule_value or "").strip()
                matched_option_record: dict[str, Any] | None = None
                if chosen_value:
                    matched_label = matching_option_for_answer(chosen_value, group_options, label)
                    if matched_label:
                        norm_target = normalize_label(matched_label)
                        for opt in (field.get("radioOptions") or []):
                            if isinstance(opt, dict) and normalize_label(opt.get("label") or "") == norm_target:
                                matched_option_record = opt
                                break
                if matched_option_record and matched_option_record.get("selector"):
                    actions.append(
                        {
                            "type": "check",
                            "selector": matched_option_record["selector"],
                            "label": field.get("label") or "",
                            "field_type": "checkbox",
                            "value": matched_option_record.get("label") or chosen_value,
                            "answer": chosen_value,
                            "source": field_answer_source if field_answer else "rule",
                            "required": True,
                            "control_kind": "checkbox_group",
                            "options": group_options,
                        }
                    )
                    continue
                # No saved answer — treat as sensitive demographic / required unanswered question.
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or field.get("placeholder") or "",
                        "field_type": "select-one",  # render as dropdown in UI
                        "options": group_options,
                        "required": True,
                        "control_kind": "checkbox_group",
                    }
                )
                continue
            if checkbox_key and checkbox_key in handled_checkbox_groups:
                continue
            if str(field.get("tagName") or "").lower() != "input":
                sibling_input = next(
                    (
                        other
                        for other in page_data.get("fields", [])
                        if isinstance(other, dict)
                        and str(other.get("type") or "").lower() == "checkbox"
                        and normalize_label(other.get("label") or other.get("placeholder") or other.get("automationId") or "") == checkbox_key
                        and str(other.get("tagName") or "").lower() == "input"
                    ),
                    None,
                )
                if sibling_input is not None:
                    continue
            if checkbox_key:
                # Skip entire checkbox group if any sibling is already checked
                # (e.g. disability/ethnicity radio-style checkbox groups).
                any_sibling_checked = any(
                    isinstance(other, dict)
                    and bool(other.get("checked"))
                    and str(other.get("type") or "").lower() == "checkbox"
                    and normalize_label(
                        other.get("label") or other.get("placeholder") or other.get("automationId") or ""
                    ) == checkbox_key
                    for other in page_data.get("fields", [])
                )
                if any_sibling_checked:
                    handled_checkbox_groups.add(checkbox_key)
                    continue
                handled_checkbox_groups.add(checkbox_key)
            consent_keywords = ["agree", "consent", "privacy", "terms", "policy", "authorize"]
            saved_rule = rule_for(label, field_type)
            if saved_rule or field.get("required") or any(keyword in label for keyword in consent_keywords):
                actions.append(
                    {
                        "type": "check",
                        "selector": field.get("selector"),
                        "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                        "field_type": field_type,
                        "value": "true",
                        "source": "rule" if saved_rule else "required_checkbox",
                        "required": is_required,
                    }
                )
            continue

        if field_type == "radio" and field.get("radioOptions"):
            group_key = "|".join(option.get("selector") or "" for option in field["radioOptions"])
            if group_key in handled_radio_groups:
                continue
            handled_radio_groups.add(group_key)
            option_labels = [option.get("label") or option.get("value") or "" for option in field["radioOptions"]]
            # Workday "Start Your Application" modal rendered as radio inputs.
            # Automatically pick "Apply Manually" — never follow OAuth apply options.
            _start_app_choice_labels = {"apply manually", "autofill with resume", "use my last application"}
            _option_labels_lower = [str(o or "").strip().lower() for o in option_labels]
            if any(lbl in _start_app_choice_labels for lbl in _option_labels_lower):
                _target_idx = next(
                    (i for i, lbl in enumerate(_option_labels_lower) if lbl == "apply manually"),
                    None,
                )
                if _target_idx is not None:
                    matched = field["radioOptions"][_target_idx]
                    actions.append({
                        "type": "radio",
                        "selector": matched.get("selector"),
                        "value": "Apply Manually",
                        "answer": "Apply Manually",
                        "label": "Start Your Application",
                        "field_type": "radio",
                        "options": option_labels,
                        "source": "workday_start_app_choice",
                    })
                continue

            if is_previous_worker_controller(str(field.get("label") or ""), field):
                no_idx = next(
                    (
                        i
                        for i, option_label in enumerate(option_labels)
                        if normalize_label(option_label) in {"no", "false"}
                    ),
                    None,
                )
                if no_idx is not None:
                    matched_option = field["radioOptions"][no_idx]
                    actions.append(
                        {
                            "type": "radio",
                            "selector": matched_option.get("selector"),
                            "value": option_labels[no_idx],
                            "answer": "No",
                            "label": field.get("label") or "",
                            "field_type": field_type,
                            "options": option_labels,
                            "source": "workday_previous_worker_default",
                            "required": is_required,
                        }
                    )
                    continue
            saved_rule = rule_for(label, field_type, option_labels)
            rule_value = str(saved_rule.get("value") or "") if saved_rule else ""
            selected_option = preferred_choice(label, option_labels, field_answer or rule_value) if (saved_rule or field_answer) else None
            if selected_option:
                matched_option = field["radioOptions"][option_labels.index(selected_option)]
                actions.append(
                    {
                        "type": "radio",
                        "selector": matched_option.get("selector"),
                        "value": selected_option,
                        "answer": field_answer or selected_option,
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": option_labels,
                        "source": field_answer_source if field_answer else "rule",
                        "required": is_required,
                    }
                )
            else:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": option_labels,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
            continue

        if field_type in ("select-one", "select-multiple"):
            option_texts = [
                text
                for text in (_normalized_select_text(option.get("text") or "") for option in field.get("options", []))
                if text and not _is_placeholder_select_text(text)
            ]
            unanswered_options = option_texts or fallback_options_for_label(label, field_type)
            saved_rule = rule_for(label, field_type, option_texts)
            rule_answer = _normalized_select_text(saved_rule.get("value") or "") if saved_rule else ""
            if _is_placeholder_select_text(rule_answer):
                rule_answer = ""
            best_answer = field_answer or rule_answer
            best_answer_source = field_answer_source if field_answer else ("rule" if saved_rule else "")
            is_screening_question = len(label) > 30 or "?" in label
            phone_code_options = any("(+" in option or re.fullmatch(r"\+\d[\d-]*(?:,\s*\+\d[\d-]*)?", option.strip()) for option in option_texts)
            compact_label = "".join(label.split())
            selector_text = str(field.get("selector") or "").lower()
            control_text = " ".join(
                str(field.get(key) or "")
                for key in ("automationId", "placeholder", "controlKind", "role")
            ).lower()
            if (
                any(phone_token in label for phone_token in ("mobile phone", "phone number", "phone"))
                and (
                    "countrycode" in compact_label
                    or "country" in selector_text
                    or "country" in control_text
                    or phone_code_options
                )
            ):
                phone_code = phone_country_code_value(candidate)
                if phone_code:
                    best_answer = phone_code
                    best_answer_source = "candidate"
            if (
                is_required
                and not option_texts
                and is_screening_question
                and not saved_rule
                and best_answer_source not in {
                    "extra", "saved_answer_intent_override", "saved", "saved_answer",
                    # Hardcoded authoritative sources — we already know the answer;
                    # do NOT surface as unanswered just because there are no static options.
                    "hardcoded_previous_worker", "candidate",
                }
                # workday_intent and all sub-variants (workday_intent_relatives,
                # workday_intent_previous_worker, workday_intent_terms, etc.) all
                # carry a definitive hardcoded answer — never surface as unanswered.
                and not (best_answer_source or "").startswith("workday_intent")
                and not any(token in label for token in ("gender", "ethnicity", "race", "veteran"))
            ):
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": unanswered_options,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
                continue
            if is_required and best_answer_source == "synthetic" and not option_texts:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": unanswered_options,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
                continue
            if is_required and not str(best_answer or "").strip():
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": unanswered_options,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
                continue
            # Sensitive demographic fields (gender / veteran / race / ethnicity /
            # hispanic / disability / visa-or-work-auth) are often marked OPTIONAL
            # by Workday, but the engine MUST surface them for the candidate to
            # answer (we don't guess on their behalf). Capture as unanswered even
            # when Workday says optional and the answer is blank.
            sensitive_demographic = any(
                token in label for token in (
                    "gender", "veteran", "ethnicity", "race", "hispanic", "latino",
                    "disability", "disabled",
                )
            ) or "please a status" in label or (
                ("sponsorship" in label or "visa" in label or ("authorized" in label and "work" in label))
            )
            if (
                sensitive_demographic
                and not str(best_answer or "").strip()
            ):
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": unanswered_options,
                        "required": True,  # treat as required so engine doesn't skip
                        "control_kind": field.get("controlKind"),
                    }
                )
                continue
            if not is_required and not option_texts and not field_invalid and not str(best_answer or "").strip():
                continue
            if (
                not is_required
                and any(token in label for token in ("type to add", "skills", "skill"))
                and not option_texts
            ):
                continue
            if not is_required and not str(best_answer or "").strip():
                continue
            if (
                not is_required
                and not option_texts
                and any(token in label for token in ("state", "province", "region"))
            ):
                continue
            matched_option = matching_option_for_answer(str(best_answer or ""), option_texts, label) if option_texts else None
            if matched_option:
                actions.append(
                    {
                        "type": "select",
                        "selector": field.get("selector"),
                        "value": matched_option,
                        "answer": best_answer,
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "source": best_answer_source,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
            elif option_texts and is_required:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": unanswered_options,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
            elif not option_texts and str(best_answer or "").strip():
                actions.append(
                    {
                        "type": "select",
                        "selector": field.get("selector"),
                        "value": best_answer,
                        "answer": best_answer,
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "source": best_answer_source,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
            else:
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or "",
                        "field_type": field_type,
                        "options": unanswered_options,
                        "required": is_required,
                        "control_kind": field.get("controlKind"),
                    }
                )
            continue

        if field_type not in non_text_types and is_select_like_label(label, field):
            saved_rule = rule_for(label, "select-one")
            rule_answer = str(saved_rule.get("value") or "") if saved_rule else ""
            best_answer = field_answer or rule_answer
            best_answer_source = field_answer_source if field_answer else ("rule" if saved_rule else "")
            if is_phone_country_code_control(label, field):
                phone_code = phone_country_code_value(candidate)
                if phone_code:
                    best_answer = phone_code
                    best_answer_source = "candidate"
            if not is_required and not str(best_answer or "").strip():
                continue
            if (
                not is_required
                and any(token in label for token in ("state", "province", "region"))
            ):
                continue
            if is_required and not str(best_answer or "").strip():
                actions.append(
                    {
                        "type": "unanswered",
                        "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                        "field_type": "select-one",
                        "options": [],
                        "required": is_required,
                    }
                )
                continue
            actions.append(
                {
                    "type": "select",
                    "selector": field.get("selector"),
                    "value": best_answer,
                    "answer": best_answer,
                    "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                    "field_type": "select-one",
                    "source": best_answer_source,
                    "required": is_required,
                }
            )
            continue

        saved_rule = rule_for(label, field_type)
        rule_answer = str(saved_rule.get("value") or "") if saved_rule else ""
        best_answer = field_answer or rule_answer
        best_answer_source = field_answer_source if field_answer else ("rule" if saved_rule else "")
        if is_optional_conditional_field(label, is_required):
            continue
        if not is_required and not str(best_answer or "").strip():
            continue
        if best_answer and field.get("selector"):
            actions.append(
                {
                    "type": "fill",
                    "selector": field["selector"],
                    "value": best_answer,
                    "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                    "field_type": field_type,
                    "source": best_answer_source,
                    "required": is_required,
                }
            )
        else:
            if not is_required:
                continue
            actions.append(
                {
                    "type": "unanswered",
                    "label": field.get("label") or field.get("placeholder") or field.get("automationId") or "",
                    "field_type": field_type,
                    "options": [option.get("text") or "" for option in field.get("options", [])],
                    "required": is_required,
                }
            )

    def score_button(button: dict[str, Any]) -> int:
        return _contextual_button_score(page_data, button, platform)

    try:
        scored_buttons = [(score_button(button), button) for button in page_data.get("buttons", [])]
        scored_buttons = [(score, button) for score, button in scored_buttons if score > 0]
        scored_buttons.sort(key=lambda item: item[0], reverse=True)
        if listing_surface:
            scored_buttons = [(score, button) for score, button in scored_buttons if score >= 90]

        field_work_actions = {"fill", "select", "radio", "check", "upload"}
        has_pending_field_work = any(action.get("type") in field_work_actions for action in actions)
        if scored_buttons and not has_pending_field_work and unfilled_required_fields == 0:
            best_button = scored_buttons[0][1]
            actions.append(
                {
                    "type": "click_button",
                    "selector": best_button.get("selector"),
                    "text": best_button.get("text"),
                    "automationId": best_button.get("automationId"),
                    "href": best_button.get("href"),
                }
            )
    except Exception as e:
        logger.warning("decide_action button scoring failed: %s", e)

    return actions
