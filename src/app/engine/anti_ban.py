from __future__ import annotations

import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.db import ROOT_DIR


def _browser_headless() -> bool:
    """Read BROWSER_HEADLESS from .env.  Default false (show window) for dev."""
    return str(os.getenv("BROWSER_HEADLESS") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _browser_channel() -> str | None:
    """BROWSER_CHANNEL=chrome runs the real Chrome binary (fixes TLS/JA3 + WebGL
    fingerprints that bundled Chromium leaks). Empty/unset falls back to Chromium."""
    value = (os.getenv("BROWSER_CHANNEL") or "").strip()
    return value or None


def _browser_persistent() -> bool:
    """BROWSER_PERSISTENT_CONTEXT=true uses a per-domain user-data-dir so cookies,
    localStorage, and IndexedDB survive across runs — makes the session look like
    a returning user to behavioural anti-bot ML."""
    return str(os.getenv("BROWSER_PERSISTENT_CONTEXT") or "false").strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}
MAX_BROWSER_INSTANCES = 3
DOMAIN_COOLDOWN_SECONDS = (10, 30)
SESSIONS_DIR = ROOT_DIR / "sessions"
SESSION_TTL = timedelta(days=3)

_browser_semaphore = asyncio.Semaphore(MAX_BROWSER_INSTANCES)
_domain_last_application_time: dict[str, float] = {}
_domain_lock = asyncio.Lock()
_domain_locks: dict[str, asyncio.Lock] = {}


def domain_from_url(url: str) -> str:
    hostname = urlparse(url).hostname or ""
    return hostname.removeprefix("www.")


def _user_agent() -> str:
    try:
        from fake_useragent import UserAgent

        return UserAgent().random
    except Exception:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )


def _viewport() -> dict[str, int]:
    return {
        "width": random.randint(1280, 1920),
        "height": random.randint(768, 1080),
    }


async def random_delay(min_ms: int = 800, max_ms: int = 3000) -> None:
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def human_type(page: Any, selector: str, text: str) -> None:
    await page.locator(selector).first.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(0.05, 0.18))


async def random_mouse_move(page: Any) -> None:
    viewport = page.viewport_size or _viewport()
    x = random.randint(40, max(41, viewport["width"] - 40))
    y = random.randint(40, max(41, viewport["height"] - 40))
    await page.mouse.move(x, y, steps=random.randint(8, 24))


async def apply_stealth(page: Any) -> None:
    try:
        from playwright_stealth import stealth_async
    except Exception:
        return
    await stealth_async(page)


async def apply_anti_ban_settings(context: Any, page: Any) -> None:
    await context.set_extra_http_headers(DEFAULT_HEADERS)
    await page.set_viewport_size(_viewport())
    await apply_stealth(page)


def session_state_path(candidate_id: object, domain: str) -> Path:
    safe_domain = domain.replace(":", "_").replace("/", "_").replace("\\", "_")
    return SESSIONS_DIR / f"{candidate_id}_{safe_domain}.json"


def session_metadata_path(candidate_id: object, domain: str) -> Path:
    return session_state_path(candidate_id, domain).with_suffix(".meta.json")


def session_state_is_valid(candidate_id: object, domain: str) -> bool:
    path = session_state_path(candidate_id, domain)
    if not path.exists():
        return False
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - updated_at <= SESSION_TTL


def clear_storage_state(candidate_id: object, domain: str) -> None:
    for path in (
        session_state_path(candidate_id, domain),
        session_metadata_path(candidate_id, domain),
    ):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


async def save_storage_state(context: Any, candidate_id: object, domain: str) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = session_state_path(candidate_id, domain)
    await context.storage_state(path=str(path))
    now = datetime.now(timezone.utc)
    metadata = {
        "candidate_id": str(candidate_id),
        "domain": domain,
        "saved_at": now.isoformat(),
        "expires_at": (now + SESSION_TTL).isoformat(),
    }
    session_metadata_path(candidate_id, domain).write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )
    return path


async def wait_for_domain_cooldown(domain: str) -> None:
    async with _domain_lock:
        domain_lock = _domain_locks.setdefault(domain, asyncio.Lock())

    async with domain_lock:
        last_time = _domain_last_application_time.get(domain)
        now = time.monotonic()
        if last_time is not None:
            elapsed = now - last_time
            cooldown = random.randint(*DOMAIN_COOLDOWN_SECONDS)
            if elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)
        _domain_last_application_time[domain] = time.monotonic()


@asynccontextmanager
async def browser_slot():
    async with _browser_semaphore:
        yield


async def detect_captcha(page: Any) -> bool:
    try:
        iframe_count = await page.locator(
            "iframe[src*='recaptcha'], iframe[src*='hcaptcha']"
        ).count()
        if iframe_count > 0:
            return True
    except Exception:
        pass

    try:
        text = (await page.locator("body").inner_text(timeout=2000)).lower()
    except Exception:
        return False
    return "recaptcha" in text or "hcaptcha" in text or "captcha" in text


async def handle_captcha(page: Any) -> str:
    """
    Attempt to auto-solve any CAPTCHA via 2captcha.
    Returns 'solved', 'no_captcha', or 'needs_manual'.
    Stop the application run on 'needs_manual'.
    """
    from app.engine.captcha_solver import solve_or_flag
    return await solve_or_flag(page)


async def setup_browser(
    playwright: Any,
    candidate_id: object | None = None,
    domain: str | None = None,
    use_storage_state: bool = True,
) -> tuple[Any, Any, Any]:
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-web-security",
        "--no-sandbox",
    ]
    channel = _browser_channel()

    context_options: dict[str, Any] = {
        "user_agent": _user_agent(),
        "viewport": _viewport(),
        "locale": "en-US",
        "timezone_id": "Asia/Kolkata",
        "permissions": ["geolocation"],
        "geolocation": {"longitude": 72.5714, "latitude": 23.0225},
    }

    if _browser_persistent() and domain:
        profile_dir = SESSIONS_DIR / "profiles" / domain.replace(":", "_").replace("/", "_")
        profile_dir.mkdir(parents=True, exist_ok=True)
        persistent_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": _browser_headless(),
            "args": launch_args,
            **context_options,
        }
        if channel:
            persistent_kwargs["channel"] = channel
        context = await playwright.chromium.launch_persistent_context(**persistent_kwargs)
        browser = context.browser
        page = context.pages[0] if context.pages else await context.new_page()
    else:
        launch_kwargs: dict[str, Any] = {"headless": _browser_headless(), "args": launch_args}
        if channel:
            launch_kwargs["channel"] = channel
        browser = await playwright.chromium.launch(**launch_kwargs)
        if use_storage_state and candidate_id is not None and domain:
            state_path = session_state_path(candidate_id, domain)
            if session_state_is_valid(candidate_id, domain):
                context_options["storage_state"] = str(state_path)
        context = await browser.new_context(**context_options)
        page = await context.new_page()

    await context.set_extra_http_headers(DEFAULT_HEADERS)
    await apply_stealth(page)
    return browser, context, page