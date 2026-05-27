"""
outlook_demo.py — Simple standalone Outlook email demo.

No candidate IDs, no database — just your Outlook email address.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ONE-TIME SETUP  (2 minutes)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 1. Azure Portal → App registrations → your app
    → Authentication → Redirect URIs → Add:
       http://localhost:8001/api/outlook-demo/callback

 2. Open in browser to connect:
    http://localhost:8001/api/outlook-demo/connect?email=you@outlook.com

 3. Sign in with Microsoft → you will see a ✅ Connected page.
    Token saved to: sessions/outlook_tokens/<your-email>.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ENDPOINTS  (all accept ?email=you@outlook.com)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  GET  /connect?email=           → start OAuth login
  GET  /status?email=            → check connection
  DELETE /disconnect?email=      → remove saved token

  GET  /inbox?email=             → list inbox (or any folder)
  GET  /message/{id}?email=      → read full message
  GET  /search?email=&q=         → search all mail

  POST /send?email=              → send new email
  POST /reply/{id}?email=        → reply to a message
  POST /forward/{id}?email=      → forward a message

  PATCH /message/{id}/read?email= → mark read / unread
  DELETE /message/{id}?email=    → delete (moves to Deleted Items)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

router = APIRouter()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = [
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
]

# In-memory store for pending OAuth flows: { state -> {flow, email} }
_pending_flows: dict[str, dict[str, Any]] = {}


# ── Config ─────────────────────────────────────────────────────────────────────

def _client_id() -> str:
    return str(os.getenv("OUTLOOK_CLIENT_ID") or "").strip()


def _tenant_id() -> str:
    return str(os.getenv("OUTLOOK_TENANT_ID") or "common").strip()


def _redirect_uri() -> str:
    # You can override this in .env with OUTLOOK_DEMO_REDIRECT_URI.
    # Default: http://localhost:8001/api/outlook-demo/callback
    return str(
        os.getenv("OUTLOOK_DEMO_REDIRECT_URI")
        or "http://localhost:8001/api/outlook-demo/callback"
    ).strip()


def _token_path(email: str) -> Path:
    """Token cache lives alongside the full Outlook integration's tokens."""
    safe = re.sub(r"[^a-zA-Z0-9._+-]", "_", email.strip().lower())
    return Path("sessions/outlook_tokens") / f"{safe}.json"


# ── MSAL helpers ────────────────────────────────────────────────────────────────

def _msal_app(cache_path: Path | None = None):
    try:
        import msal  # type: ignore[import]
    except ImportError:
        raise HTTPException(500, "msal is not installed — run: pip install msal")

    import msal  # type: ignore[import]

    cache = msal.SerializableTokenCache()
    if cache_path and cache_path.exists():
        try:
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    app = msal.PublicClientApplication(
        client_id=_client_id(),
        authority=f"https://login.microsoftonline.com/{_tenant_id()}",
        token_cache=cache,
    )
    return app, cache


def _save_cache(cache: Any, path: Path) -> None:
    if cache.has_state_changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize(), encoding="utf-8")


def _get_access_token(email: str) -> str:
    """Return a valid access token, refreshing silently if needed."""
    path = _token_path(email)
    if not path.exists():
        raise HTTPException(
            401,
            f"'{email}' is not connected yet. "
            f"Open http://localhost:8001/api/outlook-demo/connect?email={email} to connect.",
        )

    app, cache = _msal_app(path)
    accounts = app.get_accounts(username=email)
    if not accounts:
        accounts = app.get_accounts()  # fall back to first available account
    if not accounts:
        raise HTTPException(
            401,
            f"Session expired for '{email}'. "
            f"Reconnect at /api/outlook-demo/connect?email={email}",
        )

    result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise HTTPException(
            401,
            f"Could not refresh token for '{email}'. "
            f"Reconnect at /api/outlook-demo/connect?email={email}",
        )

    _save_cache(cache, path)
    return str(result["access_token"])


# ── Graph API helper ────────────────────────────────────────────────────────────

async def _graph(
    method: str,
    path: str,
    email: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> Any:
    token = await asyncio.to_thread(_get_access_token, email)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method=method.upper(),
            url=f"{GRAPH_BASE}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params,
            json=json_body,
        )
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {}).get("message", resp.text[:400])
        except Exception:
            err = resp.text[:400]
        raise HTTPException(resp.status_code, f"Graph API error: {err}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


# ── HTML helpers ────────────────────────────────────────────────────────────────

_CSS = """
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; max-width: 680px;
         margin: 60px auto; padding: 0 20px; color: #111; }
  .card { background: #f0fdf4; border: 2px solid #22c55e;
          border-radius: 12px; padding: 32px 36px; }
  .card.err { background: #fef2f2; border-color: #ef4444; }
  h2 { margin: 0 0 12px; }
  hr { border: none; border-top: 1px solid #d1fae5; margin: 20px 0; }
  a { color: #16a34a; }
  ul { padding-left: 18px; line-height: 2; }
  code { background: #e5e7eb; padding: 2px 7px; border-radius: 4px;
         font-size: 13px; }
  .hint { color: #6b7280; font-size: 13px; margin-top: 20px; }
</style>
"""


def _success_page(email: str) -> str:
    base = f"/api/outlook-demo"
    return f"""<html><head><title>Outlook Demo — Connected</title>{_CSS}</head>
<body><div class="card">
  <h2>✅ Connected — {email}</h2>
  <p>Your Outlook account is linked. Try the endpoints below
     (or open <a href="/docs#/outlook-demo" target="_blank">Swagger UI</a>):</p>
  <hr>
  <ul>
    <li><a href="{base}/status?email={email}">GET /status</a> — connection check</li>
    <li><a href="{base}/inbox?email={email}">GET /inbox</a> — list inbox</li>
    <li><a href="{base}/inbox?email={email}&folder=sentitems">GET /inbox?folder=sentitems</a> — sent items</li>
    <li><a href="{base}/inbox?email={email}&unread_only=true">GET /inbox?unread_only=true</a> — unread only</li>
    <li><a href="{base}/search?email={email}&q=verification">GET /search?q=verification</a> — search mail</li>
  </ul>
  <hr>
  <p>Send via Swagger or curl:</p>
  <code>POST {base}/send?email={email}</code>
  <div class="hint">
    Token saved → sessions/outlook_tokens/ and persists between server restarts.
  </div>
</div></body></html>"""


def _error_page(title: str, detail: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<html><head><title>Outlook Demo — Error</title>{_CSS}</head>
<body><div class="card err"><h2>❌ {title}</h2><p>{detail}</p></div></body></html>""",
        status_code=400,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AUTH ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get(
    "/connect",
    summary="Connect Outlook account",
    description=(
        "Redirects your browser to Microsoft OAuth login. "
        "After signing in you will land on a **✅ Connected** page. "
        "Requires `http://localhost:8001/api/outlook-demo/callback` to be registered "
        "as a Redirect URI in your Azure app (Authentication tab)."
    ),
    response_class=RedirectResponse,
    tags=["auth"],
)
async def connect(
    email: str = Query(..., description="Your Outlook / Microsoft email address"),
):
    if not _client_id():
        raise HTTPException(400, "OUTLOOK_CLIENT_ID is not set in .env")

    app, _ = _msal_app()
    flow = app.initiate_auth_code_flow(
        scopes=GRAPH_SCOPES,
        redirect_uri=_redirect_uri(),
        login_hint=email,      # pre-fills the Microsoft login page with this email
    )
    state = str(flow.get("state") or "")
    _pending_flows[state] = {"flow": flow, "email": email}

    auth_url = str(flow.get("auth_uri") or "")
    if not auth_url:
        raise HTTPException(500, "Could not generate Microsoft login URL")
    return RedirectResponse(url=auth_url)


@router.get(
    "/callback",
    summary="OAuth callback (Microsoft redirects here)",
    include_in_schema=False,   # internal — hide from Swagger
)
async def callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    if error:
        return _error_page(
            f"OAuth Error: {error}",
            error_description or "Microsoft returned an error during sign-in.",
        )

    pending = _pending_flows.pop(state, None)
    if pending is None:
        return _error_page(
            "Unknown OAuth state",
            "The OAuth state is missing or expired. "
            "Please start the connect flow again.",
        )

    hint_email: str = str(pending.get("email") or "")
    app, cache = _msal_app()

    result = app.acquire_token_by_auth_code_flow(
        auth_code_flow=pending["flow"],
        auth_response={"code": code, "state": state},
    )

    if "error" in result:
        msg = str(result.get("error_description") or result.get("error") or "Token exchange failed")
        return _error_page("Token Exchange Failed", msg)

    accounts = app.get_accounts()
    email = str((accounts[0].get("username") or "") if accounts else "") or hint_email

    path = _token_path(email or hint_email)
    _save_cache(cache, path)

    return HTMLResponse(_success_page(email or hint_email))


@router.get(
    "/status",
    summary="Check connection status",
    tags=["auth"],
)
async def status(
    email: str = Query(..., description="Outlook email to check"),
):
    path = _token_path(email)
    if not path.exists():
        return {
            "connected": False,
            "email": email,
            "hint": f"Connect at: GET /api/outlook-demo/connect?email={email}",
        }
    try:
        await asyncio.to_thread(_get_access_token, email)
        return {"connected": True, "email": email}
    except HTTPException:
        return {
            "connected": False,
            "email": email,
            "hint": f"Session expired — reconnect at: GET /api/outlook-demo/connect?email={email}",
        }


@router.delete(
    "/disconnect",
    summary="Remove saved Outlook token",
    tags=["auth"],
)
async def disconnect(
    email: str = Query(..., description="Outlook email to disconnect"),
):
    path = _token_path(email)
    if path.exists():
        path.unlink()
        return {"disconnected": True, "email": email}
    return {"disconnected": False, "email": email, "reason": "No token found for this email"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  READ ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get(
    "/inbox",
    summary="List messages from a folder",
    tags=["messages"],
)
async def inbox(
    email: str = Query(..., description="Outlook email"),
    folder: str = Query(
        "inbox",
        description="Folder name: inbox | sentitems | drafts | deleteditems | junkemail",
    ),
    search: str | None = Query(None, description="Full-text search query"),
    limit: int = Query(20, ge=1, le=100, description="Max messages to return"),
    skip: int = Query(0, ge=0, description="Skip this many messages (for pagination)"),
    unread_only: bool = Query(False, description="Return only unread messages"),
):
    """
    List messages from any mail folder, newest first.

    - Use `folder=sentitems` for Sent, `folder=drafts` for Drafts, etc.
    - Use `search=` for full-text search (disables `$orderby`).
    - Use `unread_only=true` to filter unread messages.
    """
    params: dict[str, Any] = {
        "$top": limit,
        "$skip": skip,
        "$select": (
            "id,subject,from,toRecipients,receivedDateTime,"
            "isRead,isDraft,bodyPreview,importance,hasAttachments"
        ),
        "$orderby": "receivedDateTime desc",
    }
    if search:
        params["$search"] = f'"{search}"'
        params.pop("$orderby", None)   # Graph doesn't allow $orderby with $search
    elif unread_only:
        params["$filter"] = "isRead eq false"

    data = await _graph("GET", f"/me/mailFolders/{folder}/messages", email, params=params)
    messages = data.get("value", [])
    return {
        "email": email,
        "folder": folder,
        "count": len(messages),
        "skip": skip,
        "limit": limit,
        "messages": messages,
        "next_page": f"?email={email}&folder={folder}&limit={limit}&skip={skip + limit}"
        if len(messages) == limit
        else None,
    }


@router.get(
    "/message/{message_id}",
    summary="Read a single message (full body)",
    tags=["messages"],
)
async def read_message(
    message_id: str,
    email: str = Query(..., description="Outlook email"),
):
    """Fetch one message with its complete HTML or plain-text body."""
    data = await _graph(
        "GET",
        f"/me/messages/{message_id}",
        email,
        params={
            "$select": (
                "id,subject,from,toRecipients,ccRecipients,bccRecipients,"
                "receivedDateTime,sentDateTime,isRead,isDraft,"
                "body,bodyPreview,importance,hasAttachments,conversationId"
            )
        },
    )
    return {"email": email, "message": data}


@router.get(
    "/search",
    summary="Search all mail",
    tags=["messages"],
)
async def search_mail(
    email: str = Query(..., description="Outlook email"),
    q: str = Query(..., description="Search keywords"),
    limit: int = Query(10, ge=1, le=50),
):
    """Full-text search across all mail folders."""
    data = await _graph(
        "GET",
        "/me/messages",
        email,
        params={
            "$search": f'"{q}"',
            "$top": limit,
            "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview",
        },
    )
    messages = data.get("value", [])
    return {"email": email, "query": q, "count": len(messages), "messages": messages}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SEND / REPLY / FORWARD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SendPayload(BaseModel):
    to: list[str]
    subject: str
    body: str
    body_type: str = "HTML"    # "HTML" or "Text"
    cc: list[str] = []
    bcc: list[str] = []

    model_config = {"json_schema_extra": {
        "example": {
            "to": ["recipient@example.com"],
            "subject": "Hello from Outlook Demo",
            "body": "<b>This is a test email</b> sent via the demo API.",
            "body_type": "HTML",
            "cc": [],
            "bcc": [],
        }
    }}


class ReplyPayload(BaseModel):
    comment: str = ""

    model_config = {"json_schema_extra": {"example": {"comment": "Thanks for your email!"}}}


class ForwardPayload(BaseModel):
    to: list[str]
    comment: str = ""

    model_config = {"json_schema_extra": {
        "example": {"to": ["another@example.com"], "comment": "FYI — forwarding this to you."}
    }}


def _addr(email_str: str) -> dict:
    return {"emailAddress": {"address": email_str}}


@router.post(
    "/send",
    status_code=202,
    summary="Send a new email",
    tags=["send"],
)
async def send_email(
    payload: SendPayload,
    email: str = Query(..., description="Your Outlook email (sender)"),
):
    """
    Send a new email immediately.

    - `body_type`: `"HTML"` (default) or `"Text"`
    - Message is saved to Sent Items automatically.
    """
    msg: dict[str, Any] = {
        "subject": payload.subject,
        "body": {"contentType": payload.body_type, "content": payload.body},
        "toRecipients": [_addr(a) for a in payload.to],
    }
    if payload.cc:
        msg["ccRecipients"] = [_addr(a) for a in payload.cc]
    if payload.bcc:
        msg["bccRecipients"] = [_addr(a) for a in payload.bcc]

    await _graph("POST", "/me/sendMail", email, json_body={"message": msg, "saveToSentItems": True})
    return {
        "sent": True,
        "from": email,
        "to": payload.to,
        "cc": payload.cc,
        "subject": payload.subject,
    }


@router.post(
    "/reply/{message_id}",
    status_code=202,
    summary="Reply to a message",
    tags=["send"],
)
async def reply(
    message_id: str,
    payload: ReplyPayload,
    email: str = Query(..., description="Your Outlook email"),
):
    """Reply to the original sender (and all To/CC if reply-all)."""
    await _graph(
        "POST",
        f"/me/messages/{message_id}/reply",
        email,
        json_body={"comment": payload.comment},
    )
    return {"replied": True, "message_id": message_id}


@router.post(
    "/reply-all/{message_id}",
    status_code=202,
    summary="Reply to all recipients",
    tags=["send"],
)
async def reply_all(
    message_id: str,
    payload: ReplyPayload,
    email: str = Query(..., description="Your Outlook email"),
):
    await _graph(
        "POST",
        f"/me/messages/{message_id}/replyAll",
        email,
        json_body={"comment": payload.comment},
    )
    return {"replied_all": True, "message_id": message_id}


@router.post(
    "/forward/{message_id}",
    status_code=202,
    summary="Forward a message",
    tags=["send"],
)
async def forward(
    message_id: str,
    payload: ForwardPayload,
    email: str = Query(..., description="Your Outlook email"),
):
    await _graph(
        "POST",
        f"/me/messages/{message_id}/forward",
        email,
        json_body={
            "comment": payload.comment,
            "toRecipients": [_addr(a) for a in payload.to],
        },
    )
    return {"forwarded": True, "message_id": message_id, "to": payload.to}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MESSAGE MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MarkReadPayload(BaseModel):
    is_read: bool = True


@router.patch(
    "/message/{message_id}/read",
    summary="Mark a message as read or unread",
    tags=["manage"],
)
async def mark_read(
    message_id: str,
    payload: MarkReadPayload,
    email: str = Query(..., description="Your Outlook email"),
):
    data = await _graph(
        "PATCH",
        f"/me/messages/{message_id}",
        email,
        json_body={"isRead": payload.is_read},
    )
    return {
        "message_id": message_id,
        "is_read": (data or {}).get("isRead", payload.is_read),
    }


@router.delete(
    "/message/{message_id}",
    summary="Delete a message (moves to Deleted Items)",
    tags=["manage"],
)
async def delete_message(
    message_id: str,
    email: str = Query(..., description="Your Outlook email"),
):
    """Moves the message to Deleted Items. Not a permanent delete."""
    await _graph("DELETE", f"/me/messages/{message_id}", email)
    return {"deleted": True, "message_id": message_id, "note": "Moved to Deleted Items"}
