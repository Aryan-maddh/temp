from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any

LOGGER = logging.getLogger(__name__)

TWOCAPTCHA_BASE = "http://2captcha.com"
INITIAL_WAIT = 15       # seconds before first poll (captchas take ~15-30s)
POLL_INTERVAL = 5       # seconds between polls
MAX_SOLVE_WAIT = 120    # seconds total timeout


# ── Configuration ──────────────────────────────────────────────────────────────

def _api_key() -> str:
    return str(os.getenv("TWOCAPTCHA_API_KEY") or "").strip()


def _configured() -> bool:
    if not _api_key():
        LOGGER.warning("TWOCAPTCHA_API_KEY not set — CAPTCHA solving disabled")
        return False
    return True


# ── 2captcha API calls ─────────────────────────────────────────────────────────

async def _http_get(url: str) -> str:
    def _do() -> str:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    try:
        return await asyncio.to_thread(_do)
    except Exception as exc:
        LOGGER.warning("2captcha HTTP GET failed: %s", exc)
        return ""


async def _submit_task(params: dict[str, str]) -> str | None:
    params["key"] = _api_key()
    url = f"{TWOCAPTCHA_BASE}/in.php?{urllib.parse.urlencode(params)}"
    response = await _http_get(url)
    if response.startswith("OK|"):
        task_id = response.split("|", 1)[1].strip()
        LOGGER.info("2captcha task submitted: %s", task_id)
        return task_id
    LOGGER.warning("2captcha submit error: %s", response)
    return None


async def _poll_result(task_id: str) -> str | None:
    url = f"{TWOCAPTCHA_BASE}/res.php?key={_api_key()}&action=get&id={task_id}"
    await asyncio.sleep(INITIAL_WAIT)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_SOLVE_WAIT
    while loop.time() < deadline:
        response = await _http_get(url)
        if response.startswith("OK|"):
            token = response.split("|", 1)[1].strip()
            LOGGER.info("2captcha solved task %s", task_id)
            return token
        if response == "CAPCHA_NOT_READY":
            await asyncio.sleep(POLL_INTERVAL)
            continue
        LOGGER.warning("2captcha poll error for task %s: %s", task_id, response)
        return None

    LOGGER.warning("2captcha timed out after %s seconds", MAX_SOLVE_WAIT)
    return None


# ── Sitekey extraction ─────────────────────────────────────────────────────────

async def _extract_sitekey(page: Any, captcha_type: str) -> str | None:
    try:
        sitekey = await page.evaluate(
            """
            (type) => {
                // data-sitekey on container element (works for both)
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');

                // iframe src parameter
                const iframes = Array.from(document.querySelectorAll('iframe'));
                for (const f of iframes) {
                    const src = f.src || '';
                    if (type === 'recaptcha' && src.includes('recaptcha')) {
                        const m = src.match(/[?&]k=([A-Za-z0-9_-]+)/);
                        if (m) return m[1];
                    }
                    if (type === 'hcaptcha' && src.includes('hcaptcha')) {
                        const m = src.match(/[?&]sitekey=([A-Za-z0-9_-]+)/);
                        if (m) return m[1];
                    }
                }

                // grecaptcha internal config (reCAPTCHA v2/v3)
                if (type === 'recaptcha' && window.___grecaptcha_cfg) {
                    const clients = window.___grecaptcha_cfg.clients || {};
                    for (const key of Object.keys(clients)) {
                        const client = clients[key];
                        for (const field of Object.values(client)) {
                            if (typeof field === 'object' && field && field.sitekey)
                                return field.sitekey;
                        }
                    }
                }
                return null;
            }
            """,
            captcha_type,
        )
        return str(sitekey).strip() if sitekey else None
    except Exception as exc:
        LOGGER.warning("Could not extract %s sitekey: %s", captcha_type, exc)
        return None


# ── Token injection ────────────────────────────────────────────────────────────

async def _inject_recaptcha_token(page: Any, token: str) -> bool:
    try:
        await page.evaluate(
            """
            (token) => {
                // Set the hidden textarea that reCAPTCHA uses
                const ta = document.getElementById('g-recaptcha-response');
                if (ta) {
                    ta.style.display = 'block';
                    ta.value = token;
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }
                // Fire grecaptcha callback
                if (window.___grecaptcha_cfg) {
                    const clients = window.___grecaptcha_cfg.clients || {};
                    for (const key of Object.keys(clients)) {
                        const client = clients[key];
                        for (const field of Object.values(client)) {
                            if (typeof field === 'object' && field) {
                                if (typeof field.callback === 'function') {
                                    try { field.callback(token); } catch(e) {}
                                }
                                if (typeof field['expired-callback'] === 'function') {
                                    // reset if needed
                                }
                            }
                        }
                    }
                }
            }
            """,
            token,
        )
        return True
    except Exception as exc:
        LOGGER.warning("Could not inject reCAPTCHA token: %s", exc)
        return False


async def _inject_hcaptcha_token(page: Any, token: str) -> bool:
    try:
        await page.evaluate(
            """
            (token) => {
                // hCaptcha response textarea
                const ta = document.querySelector('textarea[name="h-captcha-response"]');
                if (ta) {
                    ta.value = token;
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }
                // Fire hcaptcha callback
                if (window.hcaptcha) {
                    try { window.hcaptcha.submit(); } catch(e) {}
                }
            }
            """,
            token,
        )
        return True
    except Exception as exc:
        LOGGER.warning("Could not inject hCaptcha token: %s", exc)
        return False


# ── Detection ──────────────────────────────────────────────────────────────────

async def detect_captcha_type(page: Any) -> str:
    """Returns 'recaptcha', 'hcaptcha', or 'none'."""
    try:
        result = await page.evaluate(
            """
            () => {
                const iframes = Array.from(document.querySelectorAll('iframe'));
                for (const f of iframes) {
                    if (f.src.includes('hcaptcha')) return 'hcaptcha';
                    if (f.src.includes('recaptcha')) return 'recaptcha';
                }
                if (document.querySelector('.h-captcha, #h-captcha, [data-hcaptcha-widget-id]'))
                    return 'hcaptcha';
                if (document.querySelector('.g-recaptcha, #g-recaptcha, [data-sitekey]'))
                    return 'recaptcha';
                const text = (document.body?.innerText || '').toLowerCase();
                if (text.includes('hcaptcha')) return 'hcaptcha';
                if (text.includes('recaptcha') || text.includes('captcha')) return 'recaptcha';
                return 'none';
            }
            """
        )
        return str(result or "none")
    except Exception:
        return "none"


# ── Public API ─────────────────────────────────────────────────────────────────

async def solve_recaptcha(page: Any, page_url: str) -> bool:
    if not _configured():
        return False
    sitekey = await _extract_sitekey(page, "recaptcha")
    if not sitekey:
        LOGGER.warning("reCAPTCHA sitekey not found on %s", page_url)
        return False
    LOGGER.info("Submitting reCAPTCHA to 2captcha (sitekey=%s)", sitekey)
    task_id = await _submit_task({
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
    })
    if not task_id:
        return False
    token = await _poll_result(task_id)
    if not token:
        return False
    return await _inject_recaptcha_token(page, token)


async def solve_hcaptcha(page: Any, page_url: str) -> bool:
    if not _configured():
        return False
    sitekey = await _extract_sitekey(page, "hcaptcha")
    if not sitekey:
        LOGGER.warning("hCaptcha sitekey not found on %s", page_url)
        return False
    LOGGER.info("Submitting hCaptcha to 2captcha (sitekey=%s)", sitekey)
    task_id = await _submit_task({
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": page_url,
    })
    if not task_id:
        return False
    token = await _poll_result(task_id)
    if not token:
        return False
    return await _inject_hcaptcha_token(page, token)


async def solve_captcha_if_present(page: Any) -> bool:
    """
    Auto-detects CAPTCHA type and attempts to solve via 2captcha.
    Returns True if no captcha found or solved successfully.
    Returns False if captcha found but could not solve.
    """
    captcha_type = await detect_captcha_type(page)
    if captcha_type == "none":
        return True

    page_url = str(page.url or "")
    LOGGER.info("CAPTCHA detected: %s on %s", captcha_type, page_url)

    if captcha_type == "recaptcha":
        return await solve_recaptcha(page, page_url)
    if captcha_type == "hcaptcha":
        return await solve_hcaptcha(page, page_url)
    return False


async def solve_or_flag(page: Any) -> str:
    """
    Returns 'solved', 'no_captcha', or 'needs_manual'.
    Use this from browser.py / adapters instead of calling solve_captcha_if_present directly.
    """
    captcha_type = await detect_captcha_type(page)
    if captcha_type == "none":
        return "no_captcha"
    if not _configured():
        return "needs_manual"
    solved = await solve_captcha_if_present(page)
    return "solved" if solved else "needs_manual"