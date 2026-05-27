from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

OTP_PATTERNS = [
    re.compile(r"\b\d{6}\b"),
    re.compile(r"\b\d{4}\b"),
    re.compile(r"\b[A-Z0-9]{8}\b"),
    re.compile(r"\b(?:code|otp|verification code)\s*[:\-]?\s*([A-Z0-9]{4,10})\b", re.I),
]
MAGIC_LINK_TERMS = ("verify", "confirm", "activate", "magic", "token", "auth", "login", "signin")
BLOCKED_LINK_TERMS = ("unsubscribe", "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com")

GRAPH_SCOPES = ["https://graph.microsoft.com/Mail.ReadWrite"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ── Configuration ──────────────────────────────────────────────────────────────

def _client_id() -> str:
    return str(os.getenv("OUTLOOK_CLIENT_ID") or "").strip()


def _tenant_id() -> str:
    return str(os.getenv("OUTLOOK_TENANT_ID") or "common").strip()


def _token_cache_path(email: str | None = None) -> Path:
    if email:
        safe = re.sub(r"[^a-zA-Z0-9._+-]", "_", email.strip().lower())
        return Path("sessions/outlook_tokens") / f"{safe}.json"
    # Legacy fallback: single-account cache from env
    cache = str(os.getenv("OUTLOOK_TOKEN_CACHE") or "sessions/outlook_token.json")
    return Path(cache)


def _outlook_configured(email: str | None = None) -> bool:
    if not _client_id():
        LOGGER.warning("Outlook verifier: OUTLOOK_CLIENT_ID not set — run scripts/setup_outlook_auth.py")
        return False
    cache_path = _token_cache_path(email)
    if not cache_path.exists():
        LOGGER.warning(
            "Outlook verifier: no token cache for %s — run: python scripts/setup_outlook_auth.py %s",
            email or "(default)",
            email or "",
        )
        return False
    return True


# ── Token management (MSAL) ────────────────────────────────────────────────────

def _get_access_token(email: str | None = None) -> str | None:
    if not _outlook_configured(email):
        return None
    try:
        import msal  # type: ignore[import]
    except ImportError:
        LOGGER.warning("msal not installed — run: pip install msal")
        return None

    cache = msal.SerializableTokenCache()
    cache_path = _token_cache_path(email)
    if cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    app = msal.PublicClientApplication(
        client_id=_client_id(),
        authority=f"https://login.microsoftonline.com/{_tenant_id()}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    # If a specific email is requested, prefer the matching account
    result = None
    if accounts:
        target_account = next(
            (a for a in accounts if email and str(a.get("username") or "").lower() == email.lower()),
            accounts[0],
        )
        result = app.acquire_token_silent(GRAPH_SCOPES, account=target_account)

    if not result or "access_token" not in result:
        LOGGER.warning(
            "No valid Outlook token for %s. Run: python scripts/setup_outlook_auth.py %s",
            email or "(default)",
            email or "",
        )
        return None

    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")

    return str(result["access_token"])


# ── Microsoft Graph API helpers ────────────────────────────────────────────────

def _graph_get_sync(path: str, token: str) -> dict[str, Any] | None:
    url = f"{GRAPH_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        LOGGER.warning("Graph GET failed %s: %s", path, exc)
        return None


def _graph_patch_sync(path: str, token: str, body: dict[str, Any]) -> None:
    url = f"{GRAPH_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception as exc:
        LOGGER.warning("Graph PATCH failed %s: %s", path, exc)


async def _list_unread_messages(token: str, sender_domain: str | None) -> list[dict[str, Any]]:
    # Filter isRead server-side; filter sender domain client-side
    # (Graph OData contains() on nested address fields is unreliable across tenants)
    params = urllib.parse.urlencode({
        "$filter": "isRead eq false",
        "$top": "20",
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,receivedDateTime,from,body",
    })
    result = await asyncio.to_thread(
        _graph_get_sync, f"/me/mailFolders/inbox/messages?{params}", token
    )
    messages: list[dict[str, Any]] = list((result or {}).get("value") or [])

    if sender_domain:
        domain = sender_domain.lower().lstrip("@").lstrip(".")
        messages = [
            m for m in messages
            if domain in str(
                ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            ).lower()
        ]
    return messages


async def _mark_as_read(token: str, message_id: str) -> None:
    await asyncio.to_thread(
        _graph_patch_sync, f"/me/messages/{message_id}", token, {"isRead": True}
    )


# ── Text extraction ────────────────────────────────────────────────────────────

def _message_body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    content = str(body.get("content") or "")
    if str(body.get("contentType") or "text").lower() == "html":
        content = re.sub(r"<[^>]+>", " ", content)
        content = html.unescape(content)
    return re.sub(r"\s+", " ", content).strip()


def _extract_otp(subject: str, body: str) -> str | None:
    text = f"{subject}\n{body}"
    for pattern in OTP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        return match.group(1) if match.lastindex else match.group(0)
    return None


def _extract_magic_link(body: str) -> str | None:
    urls = re.findall(r"https?://[^\s\"'<>]+", body)
    for raw_url in urls:
        url = html.unescape(raw_url).rstrip(").,;]")
        lowered = url.lower()
        if any(term in lowered for term in BLOCKED_LINK_TERMS):
            continue
        if any(term in lowered for term in MAGIC_LINK_TERMS):
            return url
    return None


# ── Public polling API ─────────────────────────────────────────────────────────

async def fetch_otp(
    sender_domain: str | None = None,
    max_wait_seconds: int = 120,
    poll_interval: int = 5,
    email: str | None = None,
) -> str | None:
    token = await asyncio.to_thread(_get_access_token, email)
    if not token:
        return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    while loop.time() < deadline:
        for message in await _list_unread_messages(token, sender_domain):
            msg_id = str(message.get("id") or "")
            subject = str(message.get("subject") or "")
            body_text = _message_body_text(message)
            code = _extract_otp(subject, body_text)
            if code:
                await _mark_as_read(token, msg_id)
                LOGGER.info("OTP found in subject: %s", subject)
                return code
        await asyncio.sleep(poll_interval)
    LOGGER.warning("OTP not received within %s seconds", max_wait_seconds)
    return None


async def fetch_magic_link(
    sender_domain: str | None = None,
    max_wait_seconds: int = 120,
    poll_interval: int = 5,
    email: str | None = None,
) -> str | None:
    token = await asyncio.to_thread(_get_access_token, email)
    if not token:
        return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds
    while loop.time() < deadline:
        for message in await _list_unread_messages(token, sender_domain):
            msg_id = str(message.get("id") or "")
            link = _extract_magic_link(_message_body_text(message))
            if link:
                await _mark_as_read(token, msg_id)
                LOGGER.info("Magic link found: %s", link[:80])
                return link
        await asyncio.sleep(poll_interval)
    LOGGER.warning("Magic link not received within %s seconds", max_wait_seconds)
    return None


# ── Page interaction (platform-agnostic) ───────────────────────────────────────

async def detect_verification_type(page: object) -> str:
    try:
        data = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                  && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
              };
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
              const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(visible).map((el) => ({
                type: String(el.getAttribute('type') || '').toLowerCase(),
                name: String(el.getAttribute('name') || ''),
                id: String(el.getAttribute('id') || ''),
                placeholder: String(el.getAttribute('placeholder') || ''),
                aria: String(el.getAttribute('aria-label') || ''),
                maxlength: String(el.getAttribute('maxlength') || ''),
              }));
              const buttons = Array.from(document.querySelectorAll(
                'button, a[href], [role="button"], input[type="submit"]'
              )).filter(visible).map((el) =>
                String(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').toLowerCase()
              );
              return { text, inputs, buttons, url: String(location.href || '').toLowerCase() };
            }
            """
        )
    except Exception:
        return "unknown"

    text = str(data.get("text") or "")
    url = str(data.get("url") or "")
    inputs = list(data.get("inputs") or [])
    buttons = " ".join(str(b) for b in data.get("buttons") or [])
    visible_input_count = len(inputs)
    input_blob = " ".join(
        " ".join(str(v) for v in item.values())
        for item in inputs
        if isinstance(item, dict)
    ).lower()

    if (
        any(t in text for t in ("reset your password", "create new password", "new password"))
        or ("forgot password" in text and not re.search(r"email|sign\s*in|log\s*in", buttons))
        or re.search(r"new password|confirm password|verify password", input_blob)
    ):
        return "password_reset"

    if any(t in text for t in ("already have an account", "email already registered", "account exists")) \
            and re.search(r"\bsign\s*in\b|\blog\s*in\b", buttons):
        return "account_exists"

    verified_url = any(t in url for t in ("/dashboard", "/home", "/feed", "/profile", "/account", "/my-jobs", "/myjobs"))
    if verified_url or any(t in text for t in ("welcome back", "you're in", "successfully verified", "email verified")):
        return "verified"

    otp_text = ("verification code", "enter code", "one-time", "otp", "6-digit", "security code")
    otp_input = any(
        isinstance(item, dict) and (
            str(item.get("maxlength") or "") in {"4", "5", "6", "7", "8"}
            or re.search(r"code|otp|verification", " ".join(str(v) for v in item.values()), re.I)
        )
        for item in inputs
    )
    if otp_input or any(t in text for t in otp_text):
        return "otp"

    magic_text = (
        "check your email", "sent you a link", "click the link",
        "verify your email", "verify your account", "verification email",
        "account may need verification", "resend account verification",
        # Workday-specific phrasing
        "verify your account before you sign in",
    )
    # Detect magic-link / "check your inbox" pages even when residual inputs
    # (e.g. a hidden CSRF token, a "Resend" button form, or the login form still
    # rendered in the background) are present — as long as no OTP input was found
    # above.  The `visible_input_count == 0` guard was too strict and caused
    # Workday's post-signup verification page to be misclassified as "unknown".
    if any(t in text for t in magic_text):
        return "magic_link"

    return "unknown"


async def _fill_otp_and_submit(page: object, code: str) -> bool:
    try:
        selector = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                  && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
              };
              const candidates = Array.from(document.querySelectorAll('input, textarea')).filter(visible);
              const field = candidates.find((el) => {
                const text = [
                  el.getAttribute('name'), el.getAttribute('id'), el.getAttribute('placeholder'),
                  el.getAttribute('aria-label'), el.getAttribute('maxlength'), el.getAttribute('type')
                ].join(' ').toLowerCase();
                return /code|otp|verification|security|one.?time/.test(text)
                  || ['4','5','6','7','8'].includes(String(el.getAttribute('maxlength') || ''));
              }) || candidates[0];
              if (!field) return null;
              const token = `cviance-otp-${Date.now()}`;
              field.setAttribute('data-cviance-otp', token);
              return `[data-cviance-otp="${token}"]`;
            }
            """
        )
        if not selector:
            return False
        await page.locator(selector).first.fill(code, timeout=5000)
        await asyncio.sleep(0.3)
        clicked = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                  && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
              };
              const nodes = Array.from(document.querySelectorAll(
                'button, a[href], [role="button"], input[type="submit"]'
              )).filter(visible);
              const target = nodes.find((el) => /verify|submit|continue|confirm|next|sign in|log in/i.test(
                String(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '')
              ));
              if (!target) return false;
              ['mousedown','mouseup','click'].forEach((e) =>
                target.dispatchEvent(new MouseEvent(e, {bubbles:true, cancelable:true}))
              );
              return true;
            }
            """
        )
        if not clicked:
            await page.keyboard.press("Enter")
        return True
    except Exception as exc:
        LOGGER.warning("Could not fill OTP: %s", exc)
        return False


async def _click_sign_in(page: object) -> bool:
    try:
        return bool(await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                  && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
              };
              const nodes = Array.from(document.querySelectorAll(
                'button, a[href], [role="button"], input[type="submit"]'
              )).filter(visible);
              const target = nodes.find((el) => /sign\\s*in|log\\s*in/i.test(
                String(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '')
              ));
              if (!target) return false;
              ['mousedown','mouseup','click'].forEach((e) =>
                target.dispatchEvent(new MouseEvent(e, {bubbles:true, cancelable:true}))
              );
              return true;
            }
            """
        ))
    except Exception:
        return False


async def handle_email_verification(
    page: object,
    sender_domain: str | None = None,
    otp_fetcher: Callable[..., Any] | None = None,
    email: str | None = None,
) -> str:
    verification_type = await detect_verification_type(page)
    if verification_type == "verified":
        return "verified"
    if verification_type == "password_reset":
        return "needs_manual"
    if verification_type == "account_exists":
        await _click_sign_in(page)
        await asyncio.sleep(2)
        return "needs_manual"
    if verification_type == "otp":
        fetcher = otp_fetcher or fetch_otp
        try:
            code = await fetcher(sender_domain=sender_domain, email=email)
        except TypeError:
            code = await fetcher()
        except Exception as exc:
            LOGGER.warning("OTP fetcher failed: %s", exc)
            return "needs_manual"
        if not code:
            return "needs_manual"
        if not await _fill_otp_and_submit(page, str(code)):
            return "needs_manual"
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(3)
        return "verified" if await detect_verification_type(page) == "verified" else "needs_manual"
    if verification_type == "magic_link":
        link = await fetch_magic_link(sender_domain, email=email)
        if not link:
            return "needs_manual"
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception as exc:
            LOGGER.warning("Could not open magic link: %s", exc)
            return "failed"
        return "verified" if await detect_verification_type(page) == "verified" else "needs_manual"
    return "needs_manual"
