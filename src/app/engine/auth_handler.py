from __future__ import annotations

import asyncio
import logging
import secrets
import string
from dataclasses import dataclass
from typing import Callable, Literal

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine import anti_ban
from app.engine.classifier import PageKind, classify_page
from app.engine.platform_adapters import platform_key_for_url
from app.core.models import Candidate, Credential


AuthKind = Literal["login", "signup", "unknown"]
LogCallback = Callable[[str, str], object]
AUTH_ATTR = "data-cviance-auth"


class ManualLoginRequired(RuntimeError):
    pass


@dataclass(slots=True)
class ScopedField:
    selector: str
    label: str
    field_type: str


def _strong_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(char.islower() for char in password)
            and any(char.isupper() for char in password)
            and any(char.isdigit() for char in password)
            and any(char in "!@#$%^&*" for char in password)
        ):
            return password


async def _emit(log_cb: LogCallback | None, level: str, message: str) -> None:
    if log_cb is None:
        return
    result = log_cb(level, message)
    if hasattr(result, "__await__"):
        await result


async def find_visible_modal(page: object) -> str | None:
    script = f"""
    () => {{
      const attr = "{AUTH_ATTR}";
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && el.getClientRects().length > 0;
      }};
      const dialogs = Array.from(document.querySelectorAll('[role="dialog"], dialog'))
        .filter(visible);
      const fixed = Array.from(document.querySelectorAll('div'))
        .filter((el) => visible(el) && window.getComputedStyle(el).position === "fixed")
        .filter((el) => {{
          const rect = el.getBoundingClientRect();
          return rect.width >= 240 && rect.height >= 120;
        }});
      const modal = dialogs[0] || fixed.sort((a, b) => {{
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (br.width * br.height) - (ar.width * ar.height);
      }})[0];
      if (!modal) return null;
      const token = modal.getAttribute(attr) || `modal-${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;
      modal.setAttribute(attr, token);
      return `[${{attr}}="${{token}}"]`;
    }}
    """
    try:
        return await page.evaluate(script)
    except Exception as e:
        logger.warning("find_visible_modal failed: %s", e)
        return None


async def classify_auth_surface(page: object, scope_selector: str | None = None) -> AuthKind:
    script = """
    (scopeSelector) => {
      const root = scopeSelector ? document.querySelector(scopeSelector) : document;
      if (!root) return "unknown";
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getClientRects().length > 0;
      };
      const labelFor = (el) => [
        el.getAttribute("aria-label"),
        el.getAttribute("placeholder"),
        el.getAttribute("name"),
        el.getAttribute("id"),
        el.closest("label")?.innerText,
      ].filter(Boolean).join(" ").toLowerCase();
      const fields = Array.from(root.querySelectorAll("input, textarea, select")).filter(visible);
      const labels = fields.map(labelFor).join(" ");
      const text = (root.innerText || root.textContent || "").toLowerCase();
      const hasPassword = fields.some((field) => (field.getAttribute("type") || "").toLowerCase() === "password");
      const hasVerify = /(verify|confirm|repeat|re-enter).{0,24}password|password.{0,24}(verify|confirm|repeat|re-enter)/.test(`${labels} ${text}`);
      if (hasVerify) return "signup";
      if (hasPassword) return "login";
      return "unknown";
    }
    """
    try:
        return await page.evaluate(script, scope_selector)
    except Exception as e:
        logger.warning("classify_auth_surface failed: %s", e)
        return "unknown"


async def _find_credentials(
    db: AsyncSession,
    candidate: Candidate,
    domain: str,
) -> Credential | None:
    try:
        result = await db.execute(
            select(Credential).where(
                Credential.candidate_id == candidate.id,
                Credential.platform == domain,
            )
        )
        return result.scalar_one_or_none()
    except Exception as e:
        logger.warning("_find_credentials failed: %s", e)
        return None


async def _fields(page: object, scope_selector: str | None = None) -> list[ScopedField]:
    script = f"""
    (scopeSelector) => {{
      const attr = "{AUTH_ATTR}";
      const root = scopeSelector ? document.querySelector(scopeSelector) : document;
      if (!root) return [];
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getAttribute("aria-hidden") !== "true"
          && el.getAttribute("tabindex") !== "-2"
          && el.getClientRects().length > 0;
      }};
      const textOf = (node) => (node?.innerText || node?.textContent || "").trim();
      const labelFor = (el) => {{
        const id = el.getAttribute("id");
        if (id) {{
          const label = document.querySelector(`label[for="${{CSS.escape(id)}}"]`);
          if (label && textOf(label)) return textOf(label);
        }}
        return [
          el.closest("label") ? textOf(el.closest("label")) : "",
          el.getAttribute("aria-label") || "",
          el.getAttribute("placeholder") || "",
          el.getAttribute("name") || "",
          el.getAttribute("id") || "",
        ].filter(Boolean).join(" ").replace(/[_-]+/g, " ").trim();
      }};
      return Array.from(root.querySelectorAll("input, textarea, select"))
        .filter(visible)
        .filter((field) => {{
          const type = (field.getAttribute("type") || field.tagName).toLowerCase();
          return !["hidden", "submit", "button", "image"].includes(type);
        }})
        .map((field, index) => {{
          const token = field.getAttribute(attr) || `field-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`;
          field.setAttribute(attr, token);
          return {{
            selector: `[${{attr}}="${{token}}"]`,
            label: labelFor(field),
            field_type: (field.getAttribute("type") || field.tagName).toLowerCase(),
          }};
        }});
    }}
    """
    try:
        values = await page.evaluate(script, scope_selector)
        return [
            ScopedField(
                selector=str(item["selector"]),
                label=str(item.get("label") or ""),
                field_type=str(item.get("field_type") or "text").lower(),
            )
            for item in values
        ]
    except Exception as e:
        logger.warning("_fields failed: %s", e)
        return []


def _text(field: ScopedField) -> str:
    return f"{field.label} {field.field_type}".lower()


def _email_field(fields: list[ScopedField]) -> ScopedField | None:
    return next((field for field in fields if "email" in _text(field)), None)


def _password_fields(fields: list[ScopedField]) -> list[ScopedField]:
    return [field for field in fields if field.field_type == "password" or "password" in _text(field)]


async def _page_text(page: object) -> str:
    try:
        return str(await page.evaluate("document.body ? document.body.innerText : ''")).lower()
    except Exception:
        return ""


async def _login_rejected(page: object) -> bool:
    _REJECTED_PHRASES = (
        "wrong email address or password",
        "account might be locked",
        # Additional Workday / generic ATS rejection messages
        "invalid email or password",
        "incorrect email or password",
        "invalid credentials",
        "password is incorrect",
        "we didn't recognize that email",
        "no account found",
        "couldn't find your account",
        "sign in failed",
        "authentication failed",
        "too many failed attempts",
    )
    try:
        text = await _page_text(page)
        return any(phrase in text for phrase in _REJECTED_PHRASES)
    except Exception as e:
        logger.warning("_login_rejected failed: %s", e)
        return False


async def _upsert_credential(
    db: AsyncSession,
    credential: Credential | None,
    candidate: Candidate,
    domain: str,
    password: str,
) -> None:
    try:
        if credential is None:
            credential = await _find_credentials(db, candidate, domain)
        if credential is not None:
            credential.email = candidate.email
            credential.password = password
        else:
            db.add(
                Credential(
                    candidate_id=candidate.id,
                    domain=domain,
                    email=candidate.email,
                    password=password,
                )
            )
        await db.commit()
    except Exception as e:
        logger.warning("_upsert_credential failed: %s", e)


async def _type_value(page: object, field: ScopedField, value: str) -> None:
    locator = page.locator(field.selector).first
    try:
        await locator.fill(value, timeout=5000)
        return
    except Exception:
        pass
    changed = await page.evaluate(
        """(args) => {
          const el = document.querySelector(args.selector);
          if (!el) return false;
          el.scrollIntoView({block: "center", inline: "nearest"});
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")?.set;
          if (setter) {
            setter.call(el, args.value);
          } else {
            el.value = args.value;
          }
          el.dispatchEvent(new Event("input", {bubbles: true}));
          el.dispatchEvent(new Event("change", {bubbles: true}));
          return true;
        }""",
        {"selector": field.selector, "value": value},
    )
    if not changed:
        raise ManualLoginRequired(f"Could not fill auth field {field.label or field.selector}")


async def _check_policy(page: object, scope_selector: str | None) -> None:
    script = f"""
    (scopeSelector) => {{
      const attr = "{AUTH_ATTR}";
      const root = scopeSelector ? document.querySelector(scopeSelector) : document;
      if (!root) return null;
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getAttribute("aria-hidden") !== "true"
          && el.getAttribute("tabindex") !== "-2"
          && el.getClientRects().length > 0;
      }};
      const textOf = (node) => (node?.innerText || node?.textContent || "").trim();
      const labelFor = (el) => {{
        const id = el.getAttribute("id");
        const byFor = id ? document.querySelector(`label[for="${{CSS.escape(id)}}"]`) : null;
        return [
          byFor ? textOf(byFor) : "",
          el.closest("label") ? textOf(el.closest("label")) : "",
          textOf(el.closest("div, p, li")),
        ].join(" ").toLowerCase();
      }};
      const checkbox = Array.from(root.querySelectorAll('input[type="checkbox"]'))
        .filter(visible)
        .map((field, index) => {{
          const label = labelFor(field);
          const token = field.getAttribute(attr) || `check-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`;
          field.setAttribute(attr, token);
          const score = /(privacy|policy|terms|agree|consent)/.test(label) ? 0 : 1;
          return {{ selector: `[${{attr}}="${{token}}"]`, score }};
        }})
        .sort((a, b) => a.score - b.score)[0];
      return checkbox?.selector || null;
    }}
    """
    try:
        selector = await page.evaluate(script, scope_selector)
        if selector:
            await page.locator(selector).first.check()
    except Exception as e:
        logger.warning("_check_policy failed: %s", e)


async def _closest_primary_button(
    page: object,
    field_selectors: list[str],
    scope_selector: str | None,
    action: AuthKind,
) -> str | None:
    script = f"""
    (args) => {{
      const attr = "{AUTH_ATTR}";
      const root = args.scopeSelector ? document.querySelector(args.scopeSelector) : document;
      if (!root) return null;
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getClientRects().length > 0;
      }};
      const fieldRects = args.fieldSelectors
        .map((selector) => document.querySelector(selector))
        .filter(Boolean)
        .map((field) => field.getBoundingClientRect());
      if (fieldRects.length === 0) return null;
      const centerX = fieldRects.reduce((sum, rect) => sum + rect.left + rect.width / 2, 0) / fieldRects.length;
      const centerY = fieldRects.reduce((sum, rect) => sum + rect.top + rect.height / 2, 0) / fieldRects.length;
      const preferred = args.action === "signup"
        ? /(create account|sign up|register|continue|submit)/i
        : /(sign in|log in|login|continue|submit)/i;
      const candidates = Array.from(root.querySelectorAll('button, input[type="submit"], [role="button"], a[role="button"], a'))
        .filter(visible)
        .map((button, index) => {{
          const rect = button.getBoundingClientRect();
          const text = (
            button.innerText
            || button.textContent
            || button.getAttribute("value")
            || button.getAttribute("aria-label")
            || button.getAttribute("data-automation-id")
            || ""
          ).trim();
          const token = button.getAttribute(attr) || `button-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`;
          button.setAttribute(attr, token);
          const distance = Math.hypot((rect.left + rect.width / 2) - centerX, (rect.top + rect.height / 2) - centerY);
          const textPenalty = preferred.test(text) ? 0 : 400;
          const tag = button.tagName.toLowerCase();
          const role = (button.getAttribute("role") || "").toLowerCase();
          const automationId = (button.getAttribute("data-automation-id") || "").toLowerCase();
          const tagPenalty = tag === "button" || role === "button" || (button.getAttribute("type") || "").toLowerCase() === "submit" ? 0 : 80;
          // Match Codex working version: click_filter is the React wrapper that
          // actually handles Workday form submission. Clicking the underlying
          // <button type="submit"> directly bypasses Workday's React handler
          // and the form never actually posts. Prefer click_filter via -60.
          const workdayFilterBonus = automationId === "click_filter" ? -60 : 0;
          return {{ selector: `[${{attr}}="${{token}}"]`, score: distance + textPenalty + tagPenalty + workdayFilterBonus }};
        }})
        .sort((a, b) => a.score - b.score);
      return candidates[0]?.selector || null;
    }}
    """
    try:
        return await page.evaluate(
            script,
            {"fieldSelectors": field_selectors, "scopeSelector": scope_selector, "action": action},
        )
    except Exception as e:
        logger.warning("_closest_primary_button failed: %s", e)
        return None


async def _click_button_and_wait(page: object, button_selector: str) -> None:
    await anti_ban.random_mouse_move(page)
    try:
        await page.locator(button_selector).first.click(timeout=5000)
    except Exception:
        clicked = await page.evaluate(
            """(selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              el.scrollIntoView({block: "center", inline: "nearest"});
              el.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true}));
              el.dispatchEvent(new MouseEvent("mouseup", {bubbles: true, cancelable: true}));
              el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
              return true;
            }""",
            button_selector,
        )
        if not clicked:
            raise
    await page.wait_for_timeout(3000)


async def _switch_auth_surface(
    page: object,
    scope_selector: str | None,
    target: AuthKind,
) -> bool:
    script = f"""
    (args) => {{
      const attr = "{AUTH_ATTR}";
      const root = args.scopeSelector ? document.querySelector(args.scopeSelector) : document;
      if (!root) return null;
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getAttribute("aria-hidden") !== "true"
          && el.getClientRects().length > 0;
      }};
      const pattern = args.target === "signup"
        ? /(create account|sign up|register)/i
        : /^(sign in|log in|login)$/i;
      const candidates = Array.from(root.querySelectorAll('button, a, [role="button"]'))
        .filter(visible)
        .map((el, index) => {{
          const text = (
            el.innerText
            || el.textContent
            || el.getAttribute("aria-label")
            || el.getAttribute("data-automation-id")
            || ""
          ).trim();
          const automationId = (el.getAttribute("data-automation-id") || "").toLowerCase();
          const rect = el.getBoundingClientRect();
          const exactAutomation = args.target === "signup"
            ? automationId.includes("createaccount")
            : automationId === "signinlink";
          const textScore = pattern.test(text) ? 0 : 1000;
          const automationScore = exactAutomation ? -250 : 0;
          const headerPenalty = automationId.includes("utility") ? 200 : 0;
          const token = el.getAttribute(attr) || `auth-switch-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`;
          el.setAttribute(attr, token);
          return {{
            selector: `[${{attr}}="${{token}}"]`,
            score: textScore + automationScore + headerPenalty + rect.top
          }};
        }})
        .filter((row) => row.score < 1000)
        .sort((a, b) => a.score - b.score);
      return candidates[0]?.selector || null;
    }}
    """
    try:
        selector = await page.evaluate(
            script,
            {"scopeSelector": scope_selector, "target": target},
        )
        if not selector:
            return False
        await _click_button_and_wait(page, selector)
        return True
    except Exception as e:
        logger.warning("_switch_auth_surface failed: %s", e)
        return False


async def close_modal(page: object, modal_selector: str | None, log_cb: LogCallback | None = None) -> bool:
    if not modal_selector:
        return False
    script = f"""
    (modalSelector) => {{
      const attr = "{AUTH_ATTR}";
      const modal = document.querySelector(modalSelector);
      if (!modal) return null;
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden"
          && style.display !== "none"
          && !el.disabled
          && el.getClientRects().length > 0;
      }};
      const candidates = Array.from(modal.querySelectorAll('button, a, [role="button"]'))
        .filter(visible)
        .map((el, index) => {{
          const text = (el.innerText || el.textContent || el.getAttribute("aria-label") || "").trim();
          const rect = el.getBoundingClientRect();
          const token = el.getAttribute(attr) || `close-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`;
          el.setAttribute(attr, token);
          const closeText = /^(x|×|close|dismiss|cancel)$/i.test(text) ? 0 : 1000;
          const corner = rect.top + (window.innerWidth - rect.right);
          return {{ selector: `[${{attr}}="${{token}}"]`, score: closeText + corner }};
        }})
        .sort((a, b) => a.score - b.score);
      return candidates[0]?.selector || null;
    }}
    """
    try:
        selector = await page.evaluate(script, modal_selector)
        if not selector:
            return False
        await _click_button_and_wait(page, selector)
        await _emit(log_cb, "warning", "Closed auth modal because it could not be filled")
        return True
    except Exception as e:
        logger.warning("close_modal failed: %s", e)
        return False


async def _submit_login(page: object, credential: Credential, scope_selector: str | None) -> None:
    try:
        fields = await _fields(page, scope_selector)
        email = _email_field(fields)
        passwords = _password_fields(fields)
        if not email or not passwords:
            raise ManualLoginRequired("Could not find login email/password fields")

        filled = [email.selector, passwords[0].selector]
        await _type_value(page, email, credential.email)
        await _type_value(page, passwords[0], credential.password)
        button = await _closest_primary_button(page, filled, scope_selector, "login")
        if not button:
            raise ManualLoginRequired("Could not find login submit button")
        await _click_button_and_wait(page, button)
    except ManualLoginRequired:
        raise
    except Exception as e:
        logger.warning("_submit_login failed unexpectedly: %s", e)
        raise ManualLoginRequired(f"Login attempt failed: {e}") from e


async def _wait_for_login_fields(page: object, scope_selector: str | None, timeout_ms: int = 12000) -> bool:
    try:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            fields = await _fields(page, scope_selector)
            if _email_field(fields) and _password_fields(fields):
                return True
            await asyncio.sleep(1)
        return False
    except Exception as e:
        logger.warning("_wait_for_login_fields failed: %s", e)
        return False


async def _submit_signup(
    page: object,
    candidate: Candidate,
    scope_selector: str | None,
) -> str:
    try:
        fields = await _fields(page, scope_selector)
        email = _email_field(fields)
        passwords = _password_fields(fields)
        if not email or len(passwords) < 2:
            raise ManualLoginRequired("Could not find signup email/password/verify-password fields")

        password = _strong_password()
        filled = [email.selector, passwords[0].selector, passwords[1].selector]
        await _type_value(page, email, candidate.email)
        await _type_value(page, passwords[0], password)
        await _type_value(page, passwords[1], password)
        await _check_policy(page, scope_selector)
        button = await _closest_primary_button(page, filled, scope_selector, "signup")
        if not button:
            raise ManualLoginRequired("Could not find signup submit button")
        await _click_button_and_wait(page, button)
        return password
    except ManualLoginRequired:
        raise
    except Exception as e:
        logger.warning("_submit_signup failed unexpectedly: %s", e)
        raise ManualLoginRequired(f"Signup attempt failed: {e}") from e


async def _dismiss_social_login_selector(
    page: object,
    log_cb: LogCallback | None = None,
) -> bool:
    """Click 'Continue with Email' on a Workday social-login-selector screen.

    Some Workday tenants display a page with social-provider buttons
    (Facebook, Google, LinkedIn) plus a 'Continue with Email' option
    immediately after the candidate clicks Apply Manually.  Password
    fields are NOT present on this screen — they only appear after the
    user chooses their auth method.

    This function detects that selector and clicks 'Continue with Email'
    so the normal email + password form is revealed and ``handle_login``
    can proceed as usual.

    Returns True if the button was clicked (selector was present),
    False if the social-login-selector screen was not detected.
    """
    try:
        selector = await page.evaluate(f"""
        () => {{
          const ATTR = "{AUTH_ATTR}";
          const visible = (el) => {{
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0
              && style.display !== "none" && style.visibility !== "hidden"
              && !el.disabled && el.getAttribute("aria-hidden") !== "true";
          }};
          const normalize = (val) => String(val || "").replace(/\\s+/g, " ").trim().toLowerCase();
          // GUARD: if a visible password field already exists, this is a normal
          // sign-in / sign-up page (possibly with social login as an extra option).
          // Do NOT click anything — leave the regular handle_login() flow intact.
          const hasPasswordField = Array.from(
            document.querySelectorAll('input[type="password"]')
          ).some(visible);
          if (hasPasswordField) return null;
          const buttons = Array.from(document.querySelectorAll(
            "button, a[href], [role=\\"button\\"], input[type=\\"button\\"]"
          )).filter(visible);
          const textOf = (el) => normalize(
            el.innerText || el.textContent ||
            el.getAttribute("aria-label") || el.getAttribute("value") || ""
          );
          // Confirm this is the social-login-selector by finding at least one social button.
          const hasSocial = buttons.some((el) => {{
            const t = textOf(el);
            return t.includes("facebook") || t.includes("google") || t.includes("linkedin");
          }});
          if (!hasSocial) return null;
          // Find the "Continue with Email" button (exact or partial match).
          const emailBtn = buttons.find((el) => {{
            const t = textOf(el);
            return (
              t === "continue with email" ||
              t === "sign in with email" ||
              t === "email" ||
              (t.includes("with email") && !t.includes("facebook") && !t.includes("google") && !t.includes("linkedin"))
            );
          }});
          if (!emailBtn) return null;
          const token = emailBtn.getAttribute(ATTR) ||
            `social-dismiss-${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;
          emailBtn.setAttribute(ATTR, token);
          return `[${{ATTR}}="${{token}}"]`;
        }}
        """)
        if not selector:
            return False
        await _emit(log_cb, "info", "Social login selector detected — clicking 'Continue with Email'")
        await page.locator(selector).first.click(timeout=5000)
        # Actively wait for the email+password form to appear (up to 12 s)
        # instead of a fixed sleep.  Some Workday tenants take longer than
        # 3 s to re-render the login form after the social-selector click,
        # and handle_login() needs the fields to be present before it can
        # proceed.  _wait_for_login_fields polls every second and returns
        # as soon as both an email field and a password field are visible.
        did_load = await _wait_for_login_fields(page, None, timeout_ms=12000)
        if not did_load:
            logger.warning(
                "_dismiss_social_login_selector: email+password form did not appear "
                "within 12 s after clicking 'Continue with Email' — handle_login will "
                "try anyway in case the page is still rendering"
            )
        return True
    except Exception as exc:
        logger.warning("_dismiss_social_login_selector failed: %s", exc)
        return False


async def handle_login(
    page: object,
    candidate: Candidate,
    db: AsyncSession,
    *,
    scope_selector: str | None = None,
    log_cb: LogCallback | None = None,
) -> PageKind:
    # ── Handle social-login-selector BEFORE credential lookup ─────────────────
    # Some Workday tenants show "Continue with Facebook / Google / LinkedIn /
    # Email" immediately after Apply Manually.  Dismiss it by clicking
    # "Continue with Email" so the normal email+password form appears.
    await _dismiss_social_login_selector(page, log_cb)

    raw_domain = anti_ban.domain_from_url(getattr(page, "url", ""))
    if not raw_domain:
        raise ManualLoginRequired("Could not determine auth domain")
    # Use platform name for known platforms so all tenants share one credential row.
    platform = platform_key_for_url(getattr(page, "url", ""))

    credential = await _find_credentials(db, candidate, platform)
    if credential is None:
        master_pw = str(getattr(candidate, "master_password", None) or "").strip()
        master_user = str(getattr(candidate, "master_username", None) or candidate.email or "").strip()
        if master_pw:
            credential = Credential(
                candidate_id=candidate.id,
                platform=platform,
                email=master_user,
                password=master_pw,
            )
            db.add(credential)
            try:
                await db.commit()
            except Exception as e:
                logger.warning("Failed to persist master credentials: %s", e)
                await db.rollback()

    pending_signup_password: str | None = None
    try:
        if credential is not None:
            active_scope = scope_selector
            fields = await _fields(page, active_scope)
            signup_surface = len(_password_fields(fields)) >= 2
            await _emit(log_cb, "info", "Credentials found; attempting login first")
            if signup_surface:
                await _emit(log_cb, "info", "Signup form shown with saved credentials; switching to sign in")
                if not await _switch_auth_surface(page, scope_selector, "login"):
                    raise ManualLoginRequired("Could not switch Workday signup form to sign in")
                active_scope = await find_visible_modal(page) or scope_selector
            await _submit_login(page, credential, active_scope)
            if await classify_page(page) == "login" and await _login_rejected(page):
                raise ManualLoginRequired("Saved Workday login was rejected; manual sign-in is required")
        else:
            await _emit(log_cb, "info", "No credentials found; attempting signup")
            fields = await _fields(page, scope_selector)
            if len(_password_fields(fields)) < 2:
                await _emit(log_cb, "info", "Login form shown without credentials; switching to create account")
                if not await _switch_auth_surface(page, scope_selector, "signup"):
                    raise ManualLoginRequired("Could not switch Workday sign-in form to create account")
            pending_signup_password = await _submit_signup(page, candidate, scope_selector)
    except ManualLoginRequired:
        if scope_selector and await close_modal(page, scope_selector, log_cb):
            return await classify_page(page)
        raise

    # Poll for up to 10 s for the page to leave the login screen.
    # A hard 2 s sleep was too short — some Workday tenants take 4-8 s
    # after credential submit before redirecting to the application form.
    # We check every 2 s and break as soon as we're off "login".
    page_kind = "login"
    for _post_login_poll in range(5):
        await asyncio.sleep(2)
        page_kind = await classify_page(page)
        if page_kind != "login":
            break

    # ── Case A: signup redirected to a "check your email" verification page ───────
    # Happens when the platform sends a verification email right after account
    # creation and shows "Check your inbox" / "Verify your account" rather than
    # redirecting to a login form.  page_kind is "unknown" here (no password
    # fields, no application fields) so the normal pending_signup_password path
    # below would be skipped.
    if pending_signup_password and page_kind not in {"login"}:
        from app.engine.email_verifier import detect_verification_type, handle_email_verification

        _vtype_a = await detect_verification_type(page)
        if _vtype_a in {"otp", "magic_link"}:
            await _emit(log_cb, "info", f"Post-signup verification page (type={_vtype_a}); trying Outlook auto-handle")
            # Persist the generated password now so it survives even if
            # verification requires manual steps.
            await _upsert_credential(db, None, candidate, platform, pending_signup_password)
            pending_signup_password = None  # prevent double-save in the block below
            _vresult_a = await handle_email_verification(page, raw_domain, email=candidate.email)
            await _emit(log_cb, "info", f"Post-signup email verification result: {_vresult_a}")
            if _vresult_a == "failed":
                raise ManualLoginRequired("Post-signup email verification failed; credentials have been saved")
            if _vresult_a == "needs_manual":
                raise ManualLoginRequired(
                    "Post-signup email verification requires manual completion; credentials have been saved"
                )
            await asyncio.sleep(1)
            page_kind = await classify_page(page)

    # ── Case B: existing-credential login landed on a verification gate ───────────
    # e.g. "verify your account before you sign in" or an OTP code page that
    # appears after a successful password-based login.
    if credential is not None and not pending_signup_password and page_kind == "login":
        from app.engine.email_verifier import detect_verification_type, handle_email_verification

        _vtype_b = await detect_verification_type(page)
        if _vtype_b in {"otp", "magic_link"}:
            await _emit(
                log_cb, "info",
                f"Login-triggered email verification (type={_vtype_b}); trying Outlook auto-handle"
            )
            _vresult_b = await handle_email_verification(page, raw_domain, email=candidate.email)
            await _emit(log_cb, "info", f"Login email verification result: {_vresult_b}")
            if _vresult_b == "verified":
                await anti_ban.save_storage_state(page.context, candidate.id, raw_domain)
                page_kind = await classify_page(page)
            else:
                raise ManualLoginRequired(
                    f"Login-triggered email verification could not be completed automatically"
                    f" (result={_vresult_b})"
                )

    if pending_signup_password and page_kind == "login":
        await _emit(log_cb, "info", "Signup returned to login; saving generated credentials for follow-up login")
        await _upsert_credential(db, None, candidate, platform, pending_signup_password)
        credential = await _find_credentials(db, candidate, platform)
        if credential is None:
            raise ManualLoginRequired("Signup password was generated but saved credentials could not be reloaded")
        active_scope = await find_visible_modal(page) or scope_selector
        if not await _wait_for_login_fields(page, active_scope):
            from app.engine.email_verifier import detect_verification_type, handle_email_verification

            verification_type = await detect_verification_type(page)
            if verification_type in {"otp", "magic_link", "account_exists", "verified"}:
                await _emit(log_cb, "info", f"Handling post-signup email verification type={verification_type}")
                verification_result = await handle_email_verification(page, raw_domain, email=candidate.email)
                if verification_result == "failed":
                    raise ManualLoginRequired("Post-signup email verification failed")
                active_scope = await find_visible_modal(page) or scope_selector
            if not await _wait_for_login_fields(page, active_scope):
                page_kind = await classify_page(page)
                if page_kind != "login":
                    if pending_signup_password:
                        await _upsert_credential(db, None, candidate, platform, pending_signup_password)
                    await anti_ban.save_storage_state(page.context, candidate.id, raw_domain)
                    return page_kind
                raise ManualLoginRequired("Signup returned to sign-in but login email/password fields were not available")
        await _emit(log_cb, "info", "Using generated signup password on returned sign-in page")
        await _submit_login(page, credential, active_scope)
        if await classify_page(page) == "login" and await _login_rejected(page):
            raise ManualLoginRequired("Generated signup password was rejected by sign-in page")
        page_kind = await classify_page(page)
    if page_kind != "login":
        if pending_signup_password:
            await _upsert_credential(db, None, candidate, platform, pending_signup_password)
        await anti_ban.save_storage_state(page.context, candidate.id, raw_domain)
    return page_kind