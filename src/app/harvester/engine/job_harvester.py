from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.engine import anti_ban
from app.core.models import (
    Credential,
    Job,
    JobHarvesterConfig,
    JobHarvesterItem,
    JobHarvesterRun,
)


LINKEDIN_DOMAIN = "linkedin"   # normalized platform key (used for Credential.platform)
LINKEDIN_RAW_DOMAIN = "linkedin.com"  # raw hostname (used for anti-ban session storage)
LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs/search/"
HARVESTER_SESSION_ID = "global_harvester"
CHALLENGE_TEXT = ("captcha", "security verification", "verify it's you", "checkpoint")
FILTER_PARAM_MAP = {
    "date_posted": "f_TPR",
    "workplace": "f_WT",
    "job_type": "f_JT",
    "experience_level": "f_E",
    "sort_by": "sortBy",
}


@dataclass
class HarvestedListing:
    title: str | None = None
    company: str | None = None
    location: str | None = None
    experience: str | None = None
    source_url: str | None = None
    external_url: str | None = None
    status: str = "seen"
    reason: str | None = None


def _title_from_source_url(url: str | None) -> str:
    path = urlparse(url or "").path.strip("/")
    slug = path.split("/")[-1] if path else ""
    slug = re.sub(r"-?\d+$", "", slug)
    slug = re.sub(r"\bat\b.+$", "", slug).strip("-")
    title = " ".join(part for part in slug.split("-") if part)
    return _smart_title(title) if title else "LinkedIn job"


def _company_from_source_url(url: str | None) -> str:
    path = urlparse(url or "").path.strip("/")
    slug = path.split("/")[-1] if path else ""
    match = re.search(r"-at-(.+?)-\d+$", slug)
    if not match:
        return "Unknown company"
    company = " ".join(part for part in match.group(1).split("-") if part)
    return company.title() if company else "Unknown company"


def _smart_title(value: str) -> str:
    text = value.title()
    replacements = {
        "Qa": "QA",
        "Sdet": "SDET",
        "Ui": "UI",
        "Ux": "UX",
        "Api": "API",
        "Ai": "AI",
        "Ml": "ML",
        "Ii": "II",
        "Iii": "III",
        "Iv": "IV",
    }
    for source, target in replacements.items():
        text = re.sub(rf"\b{source}\b", target, text)
    return text


def _fallback_company_from_external_url(url: str | None) -> str:
    hostname = (urlparse(url or "").hostname or "").removeprefix("www.")
    if not hostname:
        return "Unknown company"
    name = hostname.split(".")[0]
    return name.replace("-", " ").title() or "Unknown company"


def _complete_listing_defaults(listing: HarvestedListing) -> HarvestedListing:
    listing.title = _clean_text(listing.title) or _title_from_source_url(listing.source_url)
    listing.company = (
        _clean_text(listing.company)
        or _company_from_source_url(listing.source_url)
        or _fallback_company_from_external_url(listing.external_url)
    )
    return listing


async def _log_item(
    session: AsyncSession,
    run_id: UUID,
    listing: HarvestedListing,
    *,
    job_id: object | None = None,
) -> None:
    if listing.status != "saved" or job_id is None:
        return
    listing = _complete_listing_defaults(listing)
    session.add(
        JobHarvesterItem(
            run_id=run_id,
            job_id=job_id,
            source_platform="linkedin",
            source_url=listing.source_url,
            external_url=listing.external_url,
            title=listing.title,
            company=listing.company,
            location=listing.location,
            experience=listing.experience,
            status=listing.status,
            reason=listing.reason,
        )
    )


def _clean_text(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith(("trk", "ref", "utm_"))
        ]
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def _is_linkedin_url(url: str | None) -> bool:
    hostname = urlparse(url or "").hostname or ""
    return hostname.endswith("linkedin.com")


_LOGGED_IN_URL_PATHS = ("/feed/", "/mynetwork/", "/jobs/", "/messaging/", "/notifications/")
_VERIFICATION_URL_TOKENS = ("login-challenge", "login-captcha", "checkpoint", "verify", "uas/login", "add-phone", "two-factor")


async def _is_logged_in(page: Any) -> bool:
    url = page.url
    if "linkedin.com/login" in url or any(t in url for t in _VERIFICATION_URL_TOKENS):
        return False
    # Fast-path: URL itself signals a logged-in LinkedIn page
    if any(path in url for path in _LOGGED_IN_URL_PATHS):
        return True
    try:
        return bool(
            await page.evaluate(
                """
                () => Boolean(
                  document.querySelector('a[href*="/feed/"], a[href*="/mynetwork/"], a[href*="/messaging/"], a[href*="/notifications/"]')
                  || /start a post|messaging|notifications/i.test(document.body?.innerText || '')
                )
                """
            )
        )
    except Exception:
        return False


async def _body_text(page: Any) -> str:
    try:
        return await page.locator("body").inner_text(timeout=2500)
    except Exception:
        return ""


async def _click_first(page: Any, selectors: list[str], *, timeout: int = 2500) -> bool:
    locator = await _visible_locator(page, selectors, timeout=timeout)
    if locator is None:
        return False
    try:
        await locator.click(timeout=timeout)
        return True
    except Exception:
        return False


async def _visible_locator(page: Any, selectors: list[str], *, timeout: int = 1200) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                await candidate.wait_for(state="visible", timeout=timeout)
                return candidate
            except Exception:
                continue
    return None


async def _visible_selector(page: Any, selectors: list[str], *, timeout: int = 1200) -> str | None:
    locator = await _visible_locator(page, selectors, timeout=timeout)
    if locator is None:
        return None
    return "__VISIBLE__"


async def _fill_human(page: Any, selector: str, value: str, selectors: list[str] | None = None) -> None:
    if selector == "__VISIBLE__":
        locator = await _visible_locator(page, selectors or [], timeout=3000)
    else:
        locator = page.locator(selector).first
    if locator is None:
        raise RuntimeError(f"Visible input was not found for selectors: {selectors or [selector]}")
    await locator.fill("")
    await locator.click()
    await asyncio.sleep(0.2)
    for char in value:
        await page.keyboard.type(char)
        await asyncio.sleep(0.05)


async def _ensure_linkedin_login(page: Any, credential: Credential) -> None:
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
    await anti_ban.random_delay(1000, 2500)
    text = (await _body_text(page)).lower()
    if any(token in text for token in CHALLENGE_TEXT):
        raise RuntimeError("LinkedIn security challenge detected; manual login is required")
    if await _is_logged_in(page):
        return

    # Navigate to login page if not already there
    if "linkedin.com/login" not in page.url:
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=45000)
        await anti_ban.random_delay(800, 1600)

    email_selectors = [
        "#username",
        "input[name='session_key']",
        "input[type='email']",
        "input[autocomplete='username']",
        "input[name='email-or-phone']",
    ]
    password_selectors = [
        "#password",
        "#session_password",
        "#verify-password",
        "input[name='session_password']",
        "input[name='password']",
        "input[name*='password']",
        "input[type='password']",
        "input[autocomplete='current-password']",
        "input[autocomplete*='password']",
        "input[aria-label*='password' i]",
        "input[placeholder*='password' i]",
    ]
    submit_selectors = [
        "button[type='submit']",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
    ]

    # Fill email field if visible
    email_selector = await _visible_selector(page, email_selectors, timeout=4000)
    if email_selector:
        await _fill_human(page, email_selector, credential.email, selectors=email_selectors)
        await asyncio.sleep(0.5)

    # Check if password is already visible (single-page flow)
    password_selector = await _visible_selector(page, password_selectors, timeout=3000)

    # LinkedIn two-step flow: email only on first page → click Continue → password on next page
    if not password_selector and email_selector:
        await _click_first(page, submit_selectors, timeout=4000)
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await anti_ban.random_delay(1000, 2000)
        password_selector = await _visible_selector(page, password_selectors, timeout=8000)

    if not password_selector:
        raise RuntimeError("LinkedIn password field was not found")

    # On the password-only page, email field may be hidden — only fill if visible
    email_selector_now = await _visible_selector(page, email_selectors, timeout=1000)
    if email_selector_now:
        await _fill_human(page, email_selector_now, credential.email, selectors=email_selectors)

    await _fill_human(page, password_selector, credential.password, selectors=password_selectors)
    await _click_first(page, submit_selectors, timeout=5000)
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await anti_ban.random_delay(2500, 5000)

    # Wait up to ~10 s for the auth redirect chain to settle at a final URL
    for _ in range(5):
        if await _is_logged_in(page):
            break
        current = page.url
        if any(t in current for t in _VERIFICATION_URL_TOKENS) or "linkedin.com/login" in current:
            break
        await asyncio.sleep(2)

    current_url = page.url
    text = (await _body_text(page)).lower()
    _extended_challenge = CHALLENGE_TEXT + ("verification", "two-factor", "two factor", "enter the code", "verify your", "confirm your", "add your phone")
    if any(token in text for token in _extended_challenge) or any(t in current_url for t in _VERIFICATION_URL_TOKENS):
        raise RuntimeError(
            f"LinkedIn requires verification before logging in (url={current_url!r}). "
            "Complete a manual login in the browser once, then retry."
        )
    if "linkedin.com/login" in current_url or not await _is_logged_in(page):
        raise RuntimeError(
            f"LinkedIn login did not complete (landed at {current_url!r}). "
            "Check that the stored LinkedIn credentials are correct."
        )
    await anti_ban.save_storage_state(page.context, HARVESTER_SESSION_ID, LINKEDIN_RAW_DOMAIN)


async def _relogin_if_needed(page: Any, credential: Credential, context: Any) -> bool:
    if await _is_logged_in(page):
        return True
    try:
        await _ensure_linkedin_login(page, credential)
    except Exception as exc:
        raise RuntimeError("LinkedIn login confirmation failed") from exc
    if not await _is_logged_in(page):
        raise RuntimeError("LinkedIn login confirmation failed")
    await anti_ban.save_storage_state(page.context, HARVESTER_SESSION_ID, LINKEDIN_RAW_DOMAIN)
    return True


def _keyword_terms(keyword: str) -> list[str]:
    terms = [term.strip() for term in re.split(r"[,;\n]+", keyword or "") if term.strip()]
    return terms or [keyword.strip()]


def _keyword_tokens(keyword: str) -> set[str]:
    generic_tokens = {
        "engineer",
        "developer",
        "software",
        "senior",
        "junior",
        "lead",
        "manager",
        "specialist",
        "analyst",
    }
    tokens = {
        token
        for token in re.findall(r"[a-z0-9+#.]+", (keyword or "").lower())
        if len(token) >= 2 and token not in generic_tokens
    }
    synonyms = {
        "qa": {"qa", "quality", "assurance", "test", "tester", "testing"},
        "sdet": {"sdet", "test", "automation", "quality"},
        "tester": {"tester", "test", "testing", "qa", "quality"},
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(synonyms.get(token, set()))
    return expanded


def _listing_matches_keyword(listing: HarvestedListing, keyword: str) -> bool:
    tokens = _keyword_tokens(keyword)
    if not tokens:
        return True
    title_text = f"{listing.title or ''} {listing.company or ''}".lower()
    title_tokens = set(re.findall(r"[a-z0-9+#.]+", title_text))
    if tokens & title_tokens:
        return True
    if "quality" in title_text and "assurance" in title_text and {"qa", "quality", "assurance"} & tokens:
        return True
    if "automation" in title_text and {"sdet", "automation", "qa", "test"} & tokens:
        return True
    return False


def _search_url(keyword: str, location: str | None, filters: dict[str, Any]) -> str:
    params: dict[str, str] = {"keywords": keyword}
    if location:
        params["location"] = location
    for key, value in (filters or {}).items():
        if value not in (None, ""):
            params[FILTER_PARAM_MAP.get(str(key), str(key))] = str(value)
    return f"{LINKEDIN_SEARCH_URL}?{urlencode(params)}"


async def _collect_job_urls(page: Any, max_jobs: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    last_count = -1
    for _ in range(8):
        anchors = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const resultRoots = Array.from(document.querySelectorAll(
                'li[data-occludable-job-id], li.jobs-search-results__list-item, .job-card-container, [data-job-id]'
              )).filter(visible);
              const links = [];
              for (const root of resultRoots) {
                const link = root.querySelector('a[href*="/jobs/view/"]');
                if (link?.href) links.push(link.href);
              }
              if (links.length) return links;
              return Array.from(document.querySelectorAll('main a[href*="/jobs/view/"], .jobs-search-results-list a[href*="/jobs/view/"]'))
                .filter(visible)
                .map((node) => node.href)
                .filter(Boolean);
            }
            """
        )
        for href in anchors:
            normalized = _normalize_url(str(href).split("?")[0])
            if normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
            if len(urls) >= max_jobs:
                return urls
        if len(urls) == last_count:
            break
        last_count = len(urls)
        await page.locator("body").press("PageDown")
        await anti_ban.random_delay(900, 1800)
    return urls


async def _visible_job_urls(page: Any) -> list[str]:
    anchors = await page.evaluate(
        """
        () => {
          const isAttached = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
          // Try specific job-card containers first (most precise)
          const roots = Array.from(document.querySelectorAll(
            'li[data-occludable-job-id], li[data-job-id], li.jobs-search-results__list-item, ' +
            '.job-card-container, [class*="job-card-list"], [class*="jobs-search-results__list-item"]'
          )).filter(isAttached);
          const links = [];
          for (const root of roots) {
            const link = root.querySelector('a[href*="/jobs/view/"]');
            if (link?.href) links.push(link.href);
          }
          if (links.length) return links;
          // Broad fallback: any visible job link anywhere on the page
          return Array.from(document.querySelectorAll('a[href*="/jobs/view/"]'))
            .filter(isAttached)
            .map((node) => node.href)
            .filter(Boolean);
        }
        """
    )
    urls: list[str] = []
    seen: set[str] = set()
    for href in anchors or []:
        normalized = _normalize_url(str(href).split("?")[0])
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


async def _scroll_job_results(page: Any) -> None:
    try:
        await page.locator("body").press("PageDown")
    except Exception:
        pass
    await anti_ban.random_delay(900, 1800)


async def _open_search_result(page: Any, source_url: str) -> bool:
    try:
        handle = await page.evaluate_handle(
            """
            (sourceUrl) => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const normalize = (value) => String(value || '').split('?')[0].trim();
              const target = normalize(sourceUrl);
              const links = Array.from(document.querySelectorAll('a[href*="/jobs/view/"]')).filter(visible);
              const match = links.find((node) => normalize(node.href) === target);
              if (!match) return null;
              match.scrollIntoView({block: 'center', behavior: 'instant'});
              return match;
            }
            """,
            source_url,
        )
        element = handle.as_element() if handle else None
        if element is None:
            return False
        await anti_ban.random_mouse_move(page)
        await anti_ban.random_delay(400, 900)
        await element.click(timeout=7000)
        await anti_ban.random_delay(1800, 3200)
        return True
    except Exception:
        return False


async def _listing_metadata(page: Any, source_url: str) -> HarvestedListing:
    data = await page.evaluate(
        """
        () => {
          const text = (selector) => {
            const el = document.querySelector(selector);
            return el ? (el.innerText || el.textContent || '').trim() : '';
          };
          const meta = (selector) => document.querySelector(selector)?.getAttribute('content') || '';
          const lines = (document.body?.innerText || '').split('\\n').map((line) => line.trim()).filter(Boolean);
          const experience = lines.find((line) => /\\b(\\d+\\+?\\s*(years?|yrs?)|senior|junior|entry level|mid[- ]level)\\b/i.test(line)) || '';
          const companyLink = Array.from(document.querySelectorAll('a[href*="/company/"]'))
            .map((el) => (el.innerText || el.textContent || '').trim())
            .find((value) => value && value.length <= 120) || '';
          return {
            title: text('h1')
              || text('.job-details-jobs-unified-top-card__job-title')
              || text('[class*="job-title" i]')
              || meta('meta[property="og:title"]')
              || document.title,
            company: companyLink
              || text('.job-details-jobs-unified-top-card__company-name')
              || text('.jobs-unified-top-card__company-name')
              || text('[data-job-detail-company-name]'),
            location: text('.job-details-jobs-unified-top-card__primary-description-container') || text('.jobs-unified-top-card__bullet'),
            experience,
          };
        }
        """
    )
    title = _clean_text(data.get("title"))
    if title:
        title = re.sub(r"\s*\|\s*LinkedIn.*$", "", title).strip()
    return _complete_listing_defaults(HarvestedListing(
        title=title,
        company=_clean_text(data.get("company")),
        location=_clean_text(data.get("location")),
        experience=_clean_text(data.get("experience")),
        source_url=source_url,
    ))


async def _has_easy_apply(page: Any) -> bool:
    try:
        return await page.get_by_role("button", name=re.compile("easy apply", re.I)).count() > 0
    except Exception:
        text = (await _body_text(page)).lower()
        return "easy apply" in text


async def _external_apply_url(page: Any) -> str | None:
    apply_info = await page.evaluate(
        """
        () => {
          const attr = 'data-cviance-apply-target';
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const textOf = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
          const scopes = [
            document.querySelector('.job-details-jobs-unified-top-card'),
            document.querySelector('.jobs-unified-top-card'),
            document.querySelector('[class*="jobs-s-apply"]')?.closest('section, div'),
            document.querySelector('main'),
          ].filter(Boolean);
          const scoped = scopes.flatMap(scope => Array.from(scope.querySelectorAll('a[href], button, [role="button"]')));
          const candidates = (scoped.length ? scoped : Array.from(document.querySelectorAll('a[href], button, [role="button"]')))
            .filter(visible)
            .map((el, index) => {
              const text = textOf(el);
              const href = el.href || el.getAttribute('href') || el.closest('a[href]')?.href || '';
              const topCard = el.closest('.job-details-jobs-unified-top-card, .jobs-unified-top-card, [class*="jobs-s-apply"]') ? -50 : 0;
              const externalHref = href && !/linkedin\\.com/i.test(new URL(href, location.href).hostname) ? -50 : 0;
              const primaryClass = /jobs-apply-button|apply-button/i.test(el.className || '') ? -25 : 0;
              const companyText = /company|website|site/i.test(text) ? -15 : 0;
              return {el, index, text, href, score: topCard + externalHref + primaryClass + companyText};
            })
            .filter((item) => /\\bapply\\b|company website|company site/i.test(item.text))
            .filter((item) => !/easy apply/i.test(item.text))
            .sort((a, b) => a.score - b.score);
          const picked = candidates[0];
          if (!picked) return null;
          picked.el.setAttribute(attr, '1');
          const href = picked.href ? new URL(picked.href, location.href).href : '';
          if (href && !/linkedin\\.com/i.test(new URL(href).hostname)) {
            return {selector: `[${attr}="1"]`, text: picked.text, href};
          }
          return {selector: `[${attr}="1"]`, text: picked.text, href};
        }
        """
    )
    if not apply_info:
        _log.debug("[harvester] no apply button found on %s", page.url)
        return None
    _log.debug("[harvester] apply_info text=%r href=%r url=%s", apply_info.get("text"), apply_info.get("href"), page.url)
    direct_url = str(apply_info.get("href") or "")
    if direct_url and not _is_linkedin_url(direct_url):
        _log.debug("[harvester] apply direct href: %s", direct_url)
        return _normalize_url(direct_url)

    selector = str(apply_info.get("selector") or "")
    if not selector:
        return None

    before_url = page.url
    before_pages = set(page.context.pages)
    await anti_ban.random_mouse_move(page)
    await anti_ban.random_delay(500, 1100)
    try:
        await page.locator(selector).first.click(force=True, timeout=7000)
    except Exception:
        clicked = await page.evaluate(
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
              return true;
            }
            """,
            selector,
        )
        if not clicked:
            return None

    # Fast-path: if the click directly navigated the page to an external site
    await asyncio.sleep(0.5)
    if not _is_linkedin_url(page.url) and page.url not in ("about:blank", before_url):
        _log.debug("[harvester] apply direct-nav external: %s", page.url)
        return _normalize_url(page.url)

    handled_external_modal = False
    for _ in range(8):
        handled_external_modal = await _continue_external_apply_modal(page)
        if handled_external_modal:
            break
        await asyncio.sleep(0.25)
    if handled_external_modal:
        await anti_ban.random_delay(1000, 1800)

    # Only bail out for LinkedIn-side modals/signin prompts — NOT for external site content
    if _is_linkedin_url(page.url):
        modal_visible = await page.locator('div[role="dialog"]').count() > 0
        body_text = (await _body_text(page)).lower()
        signin_text = ("sign in" in body_text or "join now" in body_text)
        if (modal_visible and not handled_external_modal) or signin_text:
            _log.debug("[harvester] LinkedIn-side bail: modal=%s signin=%s url=%s", modal_visible, signin_text, page.url)
            return None

    popup = None
    for _ in range(40):
        new_pages = [candidate for candidate in page.context.pages if candidate not in before_pages]
        if new_pages:
            popup = new_pages[-1]
            break
        await asyncio.sleep(0.25)

    target = popup or page
    try:
        await target.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    for _ in range(40):
        try:
            await target.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        url = _normalize_url(target.url)
        if url and url != "about:blank" and not _is_linkedin_url(url):
            if popup:
                await popup.close()
            return url
        if not popup and page.url != before_url and not _is_linkedin_url(page.url):
            return _normalize_url(page.url)
        nested_url = await target.evaluate(
            """
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const links = Array.from(document.querySelectorAll('a[href]'))
                .filter(visible)
                .map((el) => el.href)
                .filter((href) => href && !/linkedin\\.com/i.test(new URL(href, location.href).hostname));
              return links[0] || '';
            }
            """
        )
        if nested_url:
            if popup:
                await popup.close()
            return _normalize_url(str(nested_url))
        await asyncio.sleep(0.5)

    url = _normalize_url(target.url)
    if popup:
        await popup.close()
    if url and not _is_linkedin_url(url):
        return url
    _log.debug("[harvester] external url loop exhausted, final url=%s popup=%s", url, popup is not None)
    return None


async def _continue_external_apply_modal(page: Any) -> bool:
    try:
        action = await page.evaluate(
            """
            () => {
              const attr = 'data-cviance-external-continue';
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const textOf = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
              const dialogs = Array.from(document.querySelectorAll(
                '[role="dialog"], [aria-modal="true"], .artdeco-modal, .modal, [class*="modal" i], [class*="dialog" i]'
              )).filter(visible);
              for (const dialog of dialogs) {
                const dialogText = textOf(dialog);
                const looksExternalApply = /continue applying|company website|company site|external site|external application|you.?re leaving linkedin|finish your application/i.test(dialogText);
                if (!looksExternalApply) continue;
                const buttons = Array.from(dialog.querySelectorAll('button, a[href], [role="button"]'))
                  .filter(visible)
                  .map((el, index) => {
                    const text = textOf(el);
                    const href = el.href || el.getAttribute('href') || '';
                    const preferred = /^continue applying$/i.test(text) ? 0
                      : /continue applying/i.test(text) ? 5
                      : /^continue$/i.test(text) ? 10
                      : /apply|company website|company site|external/i.test(text) ? 20
                      : href && !/linkedin\\.com/i.test(new URL(href, location.href).hostname) ? 25
                      : 1000;
                    const reject = /cancel|close|dismiss|not now|back/i.test(text) || /^x$/i.test(text) ? 1000 : 0;
                    return {el, index, text, href, score: preferred + reject};
                  })
                  .filter((item) => item.score < 1000)
                  .sort((a, b) => a.score - b.score);
                const picked = buttons[0];
                if (!picked) continue;
                picked.el.setAttribute(attr, '1');
                return {selector: `[${attr}="1"]`, text: picked.text};
              }
              return null;
            }
            """
        )
    except Exception:
        return False
    selector = str((action or {}).get("selector") or "")
    if not selector:
        return False
    try:
        await anti_ban.random_mouse_move(page)
        await anti_ban.random_delay(400, 900)
        await page.locator(selector).first.click(force=True, timeout=7000)
        return True
    except Exception:
        try:
            return bool(
                await page.evaluate(
                    """
                    (selector) => {
                      const el = document.querySelector(selector);
                      if (!el) return false;
                      el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}));
                      return true;
                    }
                    """,
                    selector,
                )
            )
        except Exception:
            return False


async def _save_job(session: AsyncSession, listing: HarvestedListing) -> tuple[Job | None, bool]:
    listing = _complete_listing_defaults(listing)
    if not listing.external_url:
        return None, False
    if _is_linkedin_url(listing.external_url):
        return None, False
    existing = await session.execute(select(Job).where(Job.url == listing.external_url))
    job = existing.scalar_one_or_none()
    if job is not None:
        return job, False

    description_parts = []
    if listing.experience:
        description_parts.append(f"Experience: {listing.experience}")
    if listing.source_url:
        description_parts.append(f"Harvested from: {listing.source_url}")

    job = Job(
        title=listing.title or _title_from_source_url(listing.source_url),
        company=listing.company or _fallback_company_from_external_url(listing.external_url),
        url=listing.external_url,
        source_url=listing.source_url,
        location=listing.location,
        description="\n".join(description_parts) or None,
        status="active",
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(select(Job).where(Job.url == listing.external_url))
        return existing.scalar_one_or_none(), False
    return job, True


async def _load_run(session: AsyncSession, run_id: UUID) -> tuple[JobHarvesterRun, JobHarvesterConfig, Credential]:
    run = await session.get(JobHarvesterRun, run_id)
    if run is None:
        raise RuntimeError(f"Harvester run {run_id} not found")
    config = await session.get(JobHarvesterConfig, run.config_id)
    if config is None:
        raise RuntimeError("Harvester config not found")
    credential_result = await session.execute(
        select(Credential)
        .where(Credential.candidate_id.is_(None))
        .where(Credential.platform == LINKEDIN_DOMAIN)
    )
    credential = credential_result.scalars().first()
    if credential is None:
        raise RuntimeError("Global LinkedIn credential is missing")
    return run, config, credential


import logging as _logging
_log = _logging.getLogger(__name__)


async def run_job_harvester(run_id: UUID) -> None:
    stats = {"seen": 0, "saved": 0, "duplicates": 0, "skipped_easy_apply": 0, "skipped_unrelated": 0, "failed": 0}
    async with SessionLocal() as session:
        run, config, credential = await _load_run(session, run_id)
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        config.status = "running"
        await session.commit()

    _log.info("[harvester] run=%s keyword=%r location=%r max=%s", run_id, config.keyword, config.location, config.max_jobs_per_run)

    browser = context = page = None
    try:
        async with anti_ban.browser_slot():
            async with async_playwright() as playwright:
                browser, context, page = await anti_ban.setup_browser(
                    playwright,
                    HARVESTER_SESSION_ID,
                    LINKEDIN_RAW_DOMAIN,
                )
                _log.info("[harvester] browser launched, logging in")
                try:
                    await _relogin_if_needed(page, credential, context)
                except RuntimeError:
                    # Stale session — clear it and retry with a clean browser
                    _log.warning("[harvester] login failed with saved session, retrying fresh")
                    anti_ban.clear_storage_state(HARVESTER_SESSION_ID, LINKEDIN_RAW_DOMAIN)
                    with suppress(Exception):
                        await context.close()
                    with suppress(Exception):
                        await browser.close()
                    browser, context, page = await anti_ban.setup_browser(
                        playwright,
                        HARVESTER_SESSION_ID,
                        LINKEDIN_RAW_DOMAIN,
                        use_storage_state=False,
                    )
                    await _relogin_if_needed(page, credential, context)
                _log.info("[harvester] logged in, url=%s", page.url)
                await anti_ban.wait_for_domain_cooldown(LINKEDIN_RAW_DOMAIN)
                seen_urls: set[str] = set()
                successful_urls = 0
                for term in _keyword_terms(config.keyword):
                    if successful_urls >= config.max_jobs_per_run:
                        break
                    search_url = _search_url(term, config.location, config.filters)
                    _log.info("[harvester] navigating to search url: %s", search_url)
                    await page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    await anti_ban.random_delay(2500, 5000)
                    await _relogin_if_needed(page, credential, context)
                    # If LinkedIn redirected away from search, navigate again
                    if "/jobs/search/" not in page.url:
                        _log.warning("[harvester] redirected to %s, retrying search url", page.url)
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                        await anti_ban.random_delay(2000, 3500)
                        await _relogin_if_needed(page, credential, context)
                    # Wait for job cards to lazy-load before starting collection
                    with suppress(Exception):
                        await page.wait_for_selector(
                            'a[href*="/jobs/view/"]',
                            timeout=10000,
                        )
                    initial_urls = await _visible_job_urls(page)
                    _log.info("[harvester] term=%r page=%s visible_jobs=%d", term, page.url, len(initial_urls))
                    exhausted_scrolls = 0
                    while successful_urls < config.max_jobs_per_run and exhausted_scrolls < 12:
                        current_urls = await _visible_job_urls(page)
                        fresh_urls = [url for url in current_urls if url not in seen_urls]
                        if not fresh_urls:
                            exhausted_scrolls += 1
                            await _scroll_job_results(page)
                            continue
                        exhausted_scrolls = 0
                        for source_url in fresh_urls:
                            if successful_urls >= config.max_jobs_per_run:
                                break
                            seen_urls.add(source_url)
                            stats["seen"] += 1
                            listing = HarvestedListing(source_url=source_url)
                            try:
                                if page.is_closed():
                                    page = await context.new_page()
                                    await anti_ban.apply_stealth(page)
                                    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                                    await anti_ban.random_delay(1800, 3200)
                                if not _is_linkedin_url(page.url) or "/jobs/search/" not in page.url:
                                    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                                    await anti_ban.random_delay(1800, 3200)
                                    await _relogin_if_needed(page, credential, context)
                                opened = await _open_search_result(page, source_url)
                                if not opened:
                                    listing.status = "failed"
                                    listing.reason = "Could not open job card from search results"
                                    stats["failed"] += 1
                                    continue
                                if not await _is_logged_in(page):
                                    await _relogin_if_needed(page, credential, context)
                                    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                                    await anti_ban.random_delay(1800, 3200)
                                    opened = await _open_search_result(page, source_url)
                                    if not opened:
                                        listing.status = "failed"
                                        listing.reason = "Could not reopen job card after LinkedIn relogin"
                                        stats["failed"] += 1
                                        continue
                                listing = await _listing_metadata(page, source_url)
                                if not _listing_matches_keyword(listing, term):
                                    listing.status = "skipped"
                                    listing.reason = "Job title did not match this search"
                                    stats["skipped_unrelated"] += 1
                                    continue
                                if await _has_easy_apply(page):
                                    _log.debug("[harvester] easy apply: %s", source_url)
                                    listing.status = "skipped"
                                    listing.reason = "LinkedIn Easy Apply job skipped"
                                    stats["skipped_easy_apply"] += 1
                                    continue
                                _log.info("[harvester] non-easy-apply job: %s | %s", listing.title, source_url)
                                listing.external_url = await _external_apply_url(page)
                                if listing.external_url is None and not await _is_logged_in(page):
                                    await _relogin_if_needed(page, credential, context)
                                    await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                                    await anti_ban.random_delay(1800, 3200)
                                    opened = await _open_search_result(page, source_url)
                                    if opened:
                                        listing.external_url = await _external_apply_url(page)
                                if listing.external_url:
                                    if _is_linkedin_url(listing.external_url):
                                        listing.status = "failed"
                                        listing.reason = "LinkedIn URL rejected; only company application links are saved"
                                        stats["failed"] += 1
                                        continue
                                    async with SessionLocal() as session:
                                        job, created = await _save_job(session, listing)
                                        if job and created:
                                            listing.status = "saved"
                                            listing.reason = "External application link saved"
                                            stats["saved"] += 1
                                            successful_urls += 1
                                        elif job:
                                            listing.status = "duplicate"
                                            listing.reason = "External application link already exists"
                                            stats["duplicates"] += 1
                                        else:
                                            listing.status = "failed"
                                            listing.reason = "Could not save job"
                                            stats["failed"] += 1
                                        if listing.status == "saved":
                                            await _log_item(session, run_id, listing, job_id=job.id)
                                        await session.commit()
                                    continue
                                _log.warning("[harvester] no external url: %s | %s", listing.title, source_url)
                                listing.status = "failed"
                                listing.reason = "No external apply URL detected"
                                stats["failed"] += 1
                                continue
                            except PlaywrightTimeoutError as exc:
                                listing.status = "failed"
                                listing.reason = f"Timed out: {exc}"
                                stats["failed"] += 1
                                continue
                            except Exception as exc:
                                listing.status = "failed"
                                listing.reason = str(exc)
                                stats["failed"] += 1
                                continue
                        await _scroll_job_results(page)

                await anti_ban.save_storage_state(context, HARVESTER_SESSION_ID, LINKEDIN_RAW_DOMAIN)

        _log.info("[harvester] run=%s COMPLETED stats=%s", run_id, stats)
        async with SessionLocal() as session:
            run = await session.get(JobHarvesterRun, run_id)
            config = await session.get(JobHarvesterConfig, run.config_id)
            run.status = "completed"
            run.stats = stats
            run.completed_at = datetime.now(timezone.utc)
            config.status = "idle"
            config.last_run_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception as exc:
        _log.exception("[harvester] run=%s FAILED: %s", run_id, exc)
        async with SessionLocal() as session:
            run = await session.get(JobHarvesterRun, run_id)
            if run is not None:
                config = await session.get(JobHarvesterConfig, run.config_id)
                run.status = "needs_manual"
                run.error = str(exc)
                run.stats = stats
                run.completed_at = datetime.now(timezone.utc)
                if config is not None:
                    config.status = "needs_manual"
                await session.commit()
    finally:
        if context is not None:
            with suppress(Exception):
                await context.close()
        if browser is not None:
            with suppress(Exception):
                await browser.close()
