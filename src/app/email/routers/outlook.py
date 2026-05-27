"""
outlook.py — Outlook / Microsoft Graph email API.

Auth:
  GET  /api/outlook/auth/login?candidate_id=<uuid>   → OAuth redirect (links token to candidate)
  GET  /api/outlook/auth/callback                    → Exchange code for token
  GET  /api/outlook/status?candidate_id=<uuid>       → Check connection status
  DELETE /api/outlook/auth/logout                    → Remove token(s)

Per-candidate email:
  GET  /api/outlook/{candidate_id}/accounts          → Connected Outlook accounts
  GET  /api/outlook/{candidate_id}/folders           → Mail folders list
  GET  /api/outlook/{candidate_id}/messages          → List messages (?folder=inbox&search=&skip=&limit=)
  GET  /api/outlook/{candidate_id}/messages/{id}     → Full message with body
  POST /api/outlook/{candidate_id}/messages/send     → Send new email immediately
  POST /api/outlook/{candidate_id}/messages/draft    → Create draft
  PATCH /api/outlook/{candidate_id}/messages/{id}    → Update draft
  POST /api/outlook/{candidate_id}/messages/{id}/send     → Send draft
  POST /api/outlook/{candidate_id}/messages/{id}/reply    → Reply
  POST /api/outlook/{candidate_id}/messages/{id}/reply-all → Reply all
  POST /api/outlook/{candidate_id}/messages/{id}/forward  → Forward
  PATCH /api/outlook/{candidate_id}/messages/{id}/read    → Mark read/unread
  POST /api/outlook/{candidate_id}/messages/{id}/move     → Move to folder
  DELETE /api/outlook/{candidate_id}/messages/{id}        → Delete message
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

router = APIRouter()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = [
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
]


# ── Config helpers ─────────────────────────────────────────────────────────────

def _client_id() -> str:
    return str(os.getenv("OUTLOOK_CLIENT_ID") or "").strip()


def _tenant_id() -> str:
    return str(os.getenv("OUTLOOK_TENANT_ID") or "common").strip()


def _redirect_uri() -> str:
    return str(
        os.getenv("OUTLOOK_REDIRECT_URI") or "http://localhost:8001/api/outlook/auth/callback"
    ).strip()


def _token_cache_path(email: str | None = None) -> Path:
    if email:
        safe = re.sub(r"[^a-zA-Z0-9._+-]", "_", email.strip().lower())
        return Path("sessions/outlook_tokens") / f"{safe}.json"
    cache = str(os.getenv("OUTLOOK_TOKEN_CACHE") or "sessions/outlook_token.json")
    return Path(cache)


def _candidate_map_path() -> Path:
    return Path("sessions/outlook_tokens/candidate_map.json")


# ── Candidate ↔ Outlook email mapping ─────────────────────────────────────────
# Stored as sessions/outlook_tokens/candidate_map.json
# Format: {"<candidate_id>": ["email1@outlook.com", "email2@live.com"], ...}

def _load_candidate_map() -> dict[str, list[str]]:
    p = _candidate_map_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_candidate_map(data: dict[str, list[str]]) -> None:
    p = _candidate_map_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _link_candidate_email(candidate_id: str, email: str) -> None:
    data = _load_candidate_map()
    emails = data.get(candidate_id, [])
    if email not in emails:
        emails.append(email)
    data[candidate_id] = emails
    _save_candidate_map(data)


def _emails_for_candidate(candidate_id: str) -> list[str]:
    return _load_candidate_map().get(candidate_id, [])


# ── MSAL helpers ───────────────────────────────────────────────────────────────

def _get_msal_app(cache_path: Path | None = None):
    try:
        import msal  # type: ignore[import]
    except ImportError:
        raise HTTPException(500, "msal not installed — run: pip install msal")

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


def _save_cache(cache, cache_path: Path) -> None:
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")


def _get_access_token(email: str) -> str:
    """Get a fresh (or silently-refreshed) access token for the given email."""
    cache_path = _token_cache_path(email)
    if not cache_path.exists():
        raise HTTPException(
            401,
            f"No Outlook session for {email}. "
            "Login at /api/outlook/auth/login?candidate_id=<id>",
        )

    app, cache = _get_msal_app(cache_path)
    accounts = app.get_accounts(username=email)
    if not accounts:
        raise HTTPException(
            401,
            f"Outlook session expired for {email}. "
            "Re-login at /api/outlook/auth/login?candidate_id=<id>",
        )

    result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise HTTPException(
            401,
            f"Could not refresh Outlook token for {email}. "
            "Re-login at /api/outlook/auth/login?candidate_id=<id>",
        )

    _save_cache(cache, cache_path)
    return str(result["access_token"])


# ── Graph API helper ───────────────────────────────────────────────────────────

async def _graph(
    method: str,
    path: str,
    email: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    expected_status: int | None = None,
) -> Any:
    """Execute a Microsoft Graph API request and return the parsed JSON (or None for 204)."""
    token = _get_access_token(email)
    url = f"{GRAPH_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            json=json_body,
        )

    if expected_status and resp.status_code != expected_status:
        _raise_graph_error(resp)
    if resp.status_code >= 400:
        _raise_graph_error(resp)
    if resp.status_code == 204:
        return None
    return resp.json()


def _raise_graph_error(resp: httpx.Response) -> None:
    try:
        err = resp.json().get("error", {}).get("message", resp.text[:400])
    except Exception:
        err = resp.text[:400]
    raise HTTPException(resp.status_code, f"Graph API error: {err}")


# In-memory store for active MSAL auth code flows {state → {flow, candidate_id}}
_pending_flows: dict[str, dict[str, Any]] = {}


# ── Auth routes ────────────────────────────────────────────────────────────────

@router.get("/auth/login")
async def outlook_login(candidate_id: str | None = Query(None)):
    """
    Redirect the browser to Microsoft OAuth login.

    Pass **?candidate_id=<uuid>** to automatically link the Outlook account
    to a candidate after the user authenticates.
    """
    if not _client_id():
        raise HTTPException(400, "OUTLOOK_CLIENT_ID is not configured")

    app, _ = _get_msal_app()
    flow = app.initiate_auth_code_flow(
        scopes=GRAPH_SCOPES,
        redirect_uri=_redirect_uri(),
    )
    state = str(flow.get("state") or "")
    _pending_flows[state] = {"flow": flow, "candidate_id": candidate_id}
    auth_url = flow.get("auth_uri") or ""
    if not auth_url:
        raise HTTPException(500, "Could not generate Microsoft login URL")
    return RedirectResponse(url=auth_url)


@router.get("/auth/callback")
async def outlook_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Handle OAuth callback from Microsoft, exchange code for tokens."""
    if error:
        raise HTTPException(400, f"OAuth error: {error} — {error_description}")

    pending = _pending_flows.pop(state, None)
    if pending is None:
        raise HTTPException(400, "Unknown or expired OAuth state. Start the login flow again.")

    flow = pending["flow"]
    candidate_id: str | None = pending.get("candidate_id")

    app, cache = _get_msal_app()
    result = app.acquire_token_by_auth_code_flow(
        auth_code_flow=flow,
        auth_response={"code": code, "state": state},
    )
    if "error" in result:
        raise HTTPException(
            400,
            f"Token exchange failed: {result.get('error_description') or result.get('error')}",
        )

    accounts = app.get_accounts()
    email = str((accounts[0].get("username") or "") if accounts else "")
    cache_path = _token_cache_path(email or None)
    _save_cache(cache, cache_path)

    # Link this Outlook account to the candidate
    if candidate_id and email:
        _link_candidate_email(candidate_id, email)

    frontend_origin = (
        str(os.getenv("CORS_ORIGINS") or "").split(",")[0].strip()
        or "http://localhost:5173"
    )
    redirect_url = f"{frontend_origin}?outlook_connected=1"
    if candidate_id:
        redirect_url += f"&candidate_id={candidate_id}"
    if email:
        redirect_url += f"&outlook_email={email}"
    return RedirectResponse(url=redirect_url)


@router.get("/status")
async def outlook_status(candidate_id: str | None = Query(None)):
    """
    Return whether Outlook is connected and which accounts are active.

    * With **?candidate_id=<uuid>** — shows only accounts linked to that candidate.
    * Without — shows all connected accounts.
    """
    if not _client_id():
        return {"connected": False, "reason": "OUTLOOK_CLIENT_ID not configured"}

    token_dir = Path("sessions/outlook_tokens")
    token_files: list[Path] = []

    if candidate_id:
        for em in _emails_for_candidate(candidate_id):
            p = _token_cache_path(em)
            if p.exists():
                token_files.append(p)
    elif token_dir.exists():
        token_files = [
            f for f in token_dir.glob("*.json") if f.name != "candidate_map.json"
        ]
        legacy = _token_cache_path()
        if legacy.exists() and legacy not in token_files:
            token_files.append(legacy)

    connected_accounts: list[str] = []
    for token_file in token_files:
        try:
            app, _ = _get_msal_app(token_file)
            for acct in app.get_accounts():
                username = str(acct.get("username") or "")
                if username and username not in connected_accounts:
                    connected_accounts.append(username)
        except Exception:
            pass

    if connected_accounts:
        return {"connected": True, "accounts": connected_accounts}
    return {
        "connected": False,
        "reason": "No active Outlook session. Visit /api/outlook/auth/login",
    }


@router.delete("/auth/logout")
async def outlook_logout(
    email: str | None = Query(None),
    candidate_id: str | None = Query(None),
):
    """
    Remove stored Outlook token(s).

    * **?email=** — remove a specific account's token.
    * **?candidate_id=** — remove all tokens linked to a candidate.
    * Neither — remove ALL tokens.
    """
    removed: list[str] = []

    if email:
        path = _token_cache_path(email)
        if path.exists():
            path.unlink()
            removed.append(email)
        # Scrub from all candidate mappings
        data = _load_candidate_map()
        for cid in data:
            if email in data[cid]:
                data[cid].remove(email)
        _save_candidate_map(data)

    elif candidate_id:
        for em in _emails_for_candidate(candidate_id):
            path = _token_cache_path(em)
            if path.exists():
                path.unlink()
                removed.append(em)
        data = _load_candidate_map()
        data.pop(candidate_id, None)
        _save_candidate_map(data)

    else:
        token_dir = Path("sessions/outlook_tokens")
        if token_dir.exists():
            for f in token_dir.glob("*.json"):
                if f.name != "candidate_map.json":
                    f.unlink()
                    removed.append(f.stem)
        legacy = _token_cache_path()
        if legacy.exists():
            legacy.unlink()
            removed.append("(legacy)")

    if not removed:
        return {"disconnected": False, "reason": "No token found"}
    return {"disconnected": True, "removed": removed}


# ── Resolve email helper ───────────────────────────────────────────────────────

def _resolve_email(candidate_id: str, email: str | None) -> str:
    """Pick which Outlook account to use for a candidate.

    Explicit ?email= overrides auto-selection from the candidate map.
    """
    if email:
        return email
    emails = _emails_for_candidate(candidate_id)
    if not emails:
        raise HTTPException(
            400,
            f"No Outlook account linked to candidate {candidate_id}. "
            "Login at /api/outlook/auth/login?candidate_id=<id>",
        )
    return emails[0]


# ── Connected accounts for a candidate ────────────────────────────────────────

@router.get("/{candidate_id}/accounts")
async def list_candidate_accounts(candidate_id: str):
    """List Outlook accounts connected to this candidate (with live token check)."""
    emails = _emails_for_candidate(candidate_id)
    active: list[dict] = []
    for em in emails:
        cache_path = _token_cache_path(em)
        if not cache_path.exists():
            continue
        try:
            app, _ = _get_msal_app(cache_path)
            if app.get_accounts(username=em):
                active.append({"email": em, "connected": True})
        except Exception:
            active.append({"email": em, "connected": False})
    return {"candidate_id": candidate_id, "accounts": active}


# ── Folders ────────────────────────────────────────────────────────────────────

@router.get("/{candidate_id}/folders")
async def list_folders(
    candidate_id: str,
    email: str | None = Query(None),
):
    """List all mail folders (Inbox, Sent Items, Drafts, Deleted Items, etc.)."""
    em = _resolve_email(candidate_id, email)
    data = await _graph("GET", "/me/mailFolders", em, params={"$top": 50})
    return {"email": em, "folders": data.get("value", [])}


# ── Messages — list (must be before /{message_id} routes) ─────────────────────

@router.get("/{candidate_id}/messages")
async def list_messages(
    candidate_id: str,
    email: str | None = Query(None),
    folder: str = Query(
        "inbox",
        description="Folder name or well-known name: inbox | sentitems | drafts | deleteditems | junkemail",
    ),
    search: str | None = Query(None, description="Full-text search (Graph $search)"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """List messages from a mail folder, newest first."""
    em = _resolve_email(candidate_id, email)
    params: dict[str, Any] = {
        "$top": limit,
        "$skip": skip,
        "$select": (
            "id,subject,from,toRecipients,receivedDateTime,sentDateTime,"
            "isRead,isDraft,bodyPreview,hasAttachments,importance"
        ),
        "$orderby": "receivedDateTime desc",
    }
    if search:
        params["$search"] = f'"{search}"'
        # $search and $orderby cannot be combined
        params.pop("$orderby", None)

    data = await _graph("GET", f"/me/mailFolders/{folder}/messages", em, params=params)
    return {
        "email": em,
        "folder": folder,
        "skip": skip,
        "limit": limit,
        "count": len(data.get("value", [])),
        "messages": data.get("value", []),
        "next_link": data.get("@odata.nextLink"),
    }


# ── Send and Draft — MUST be declared before /{message_id} ────────────────────

class EmailAddress(BaseModel):
    address: str
    name: str | None = None


class SendMailPayload(BaseModel):
    to: list[EmailAddress]
    cc: list[EmailAddress] = []
    bcc: list[EmailAddress] = []
    subject: str
    body: str
    body_type: str = "HTML"       # "HTML" or "Text"
    save_to_sent: bool = True


class DraftPayload(BaseModel):
    to: list[EmailAddress] = []
    cc: list[EmailAddress] = []
    bcc: list[EmailAddress] = []
    subject: str | None = None
    body: str | None = None
    body_type: str = "HTML"


class ReplyPayload(BaseModel):
    comment: str = ""
    to: list[EmailAddress] | None = None  # used only for forward


class MovePayload(BaseModel):
    destination_folder_id: str


class MarkReadPayload(BaseModel):
    is_read: bool = True


def _recipient(addr: EmailAddress) -> dict:
    r: dict = {"emailAddress": {"address": addr.address}}
    if addr.name:
        r["emailAddress"]["name"] = addr.name
    return r


def _build_message(payload: SendMailPayload | DraftPayload) -> dict:
    msg: dict = {}
    if payload.subject is not None:
        msg["subject"] = payload.subject
    if payload.body is not None:
        msg["body"] = {"contentType": payload.body_type, "content": payload.body}
    if payload.to:
        msg["toRecipients"] = [_recipient(a) for a in payload.to]
    if payload.cc:
        msg["ccRecipients"] = [_recipient(a) for a in payload.cc]
    if payload.bcc:
        msg["bccRecipients"] = [_recipient(a) for a in payload.bcc]
    return msg


@router.post("/{candidate_id}/messages/send", status_code=202)
async def send_email(
    candidate_id: str,
    payload: SendMailPayload,
    email: str | None = Query(None),
):
    """Send a new email immediately."""
    em = _resolve_email(candidate_id, email)
    body = {
        "message": _build_message(payload),
        "saveToSentItems": payload.save_to_sent,
    }
    await _graph("POST", "/me/sendMail", em, json_body=body, expected_status=202)
    return {"sent": True, "from": em, "to": [a.address for a in payload.to]}


@router.post("/{candidate_id}/messages/draft", status_code=201)
async def create_draft(
    candidate_id: str,
    payload: DraftPayload,
    email: str | None = Query(None),
):
    """Create a new draft message (not sent)."""
    em = _resolve_email(candidate_id, email)
    data = await _graph("POST", "/me/messages", em, json_body=_build_message(payload))
    return {"email": em, "draft": data}


# ── Single-message routes (/{message_id} wildcard — declare AFTER literal paths) ──

@router.get("/{candidate_id}/messages/{message_id}")
async def get_message(
    candidate_id: str,
    message_id: str,
    email: str | None = Query(None),
):
    """Get a single message with full HTML/text body."""
    em = _resolve_email(candidate_id, email)
    data = await _graph(
        "GET",
        f"/me/messages/{message_id}",
        em,
        params={
            "$select": (
                "id,subject,from,toRecipients,ccRecipients,bccRecipients,"
                "receivedDateTime,sentDateTime,isRead,isDraft,"
                "body,bodyPreview,hasAttachments,importance,conversationId"
            )
        },
    )
    return {"email": em, "message": data}


@router.patch("/{candidate_id}/messages/{message_id}")
async def update_draft(
    candidate_id: str,
    message_id: str,
    payload: DraftPayload,
    email: str | None = Query(None),
):
    """Update an existing draft (subject, body, recipients)."""
    em = _resolve_email(candidate_id, email)
    data = await _graph(
        "PATCH", f"/me/messages/{message_id}", em, json_body=_build_message(payload)
    )
    return {"email": em, "message": data}


@router.post("/{candidate_id}/messages/{message_id}/send", status_code=202)
async def send_draft(
    candidate_id: str,
    message_id: str,
    email: str | None = Query(None),
):
    """Send an existing draft message."""
    em = _resolve_email(candidate_id, email)
    await _graph(
        "POST", f"/me/messages/{message_id}/send", em, expected_status=202
    )
    return {"sent": True, "message_id": message_id, "from": em}


@router.post("/{candidate_id}/messages/{message_id}/reply", status_code=202)
async def reply_message(
    candidate_id: str,
    message_id: str,
    payload: ReplyPayload,
    email: str | None = Query(None),
):
    """Reply to a message."""
    em = _resolve_email(candidate_id, email)
    await _graph(
        "POST",
        f"/me/messages/{message_id}/reply",
        em,
        json_body={"comment": payload.comment},
        expected_status=202,
    )
    return {"replied": True, "message_id": message_id}


@router.post("/{candidate_id}/messages/{message_id}/reply-all", status_code=202)
async def reply_all_message(
    candidate_id: str,
    message_id: str,
    payload: ReplyPayload,
    email: str | None = Query(None),
):
    """Reply all to a message."""
    em = _resolve_email(candidate_id, email)
    await _graph(
        "POST",
        f"/me/messages/{message_id}/replyAll",
        em,
        json_body={"comment": payload.comment},
        expected_status=202,
    )
    return {"replied_all": True, "message_id": message_id}


@router.post("/{candidate_id}/messages/{message_id}/forward", status_code=202)
async def forward_message(
    candidate_id: str,
    message_id: str,
    payload: ReplyPayload,
    email: str | None = Query(None),
):
    """Forward a message to new recipients."""
    em = _resolve_email(candidate_id, email)
    body: dict = {"comment": payload.comment}
    if payload.to:
        body["toRecipients"] = [_recipient(a) for a in payload.to]
    await _graph(
        "POST",
        f"/me/messages/{message_id}/forward",
        em,
        json_body=body,
        expected_status=202,
    )
    return {"forwarded": True, "message_id": message_id}


@router.patch("/{candidate_id}/messages/{message_id}/read")
async def mark_read(
    candidate_id: str,
    message_id: str,
    payload: MarkReadPayload,
    email: str | None = Query(None),
):
    """Mark a message as read or unread."""
    em = _resolve_email(candidate_id, email)
    data = await _graph(
        "PATCH",
        f"/me/messages/{message_id}",
        em,
        json_body={"isRead": payload.is_read},
    )
    return {
        "email": em,
        "message_id": message_id,
        "is_read": data.get("isRead") if data else payload.is_read,
    }


@router.post("/{candidate_id}/messages/{message_id}/move")
async def move_message(
    candidate_id: str,
    message_id: str,
    payload: MovePayload,
    email: str | None = Query(None),
):
    """Move a message to another folder (use folder ID or well-known name)."""
    em = _resolve_email(candidate_id, email)
    data = await _graph(
        "POST",
        f"/me/messages/{message_id}/move",
        em,
        json_body={"destinationId": payload.destination_folder_id},
    )
    return {
        "email": em,
        "moved_to": payload.destination_folder_id,
        "new_message_id": data.get("id") if data else None,
    }


@router.delete("/{candidate_id}/messages/{message_id}", status_code=204)
async def delete_message(
    candidate_id: str,
    message_id: str,
    email: str | None = Query(None),
):
    """
    Delete a message (moves to Deleted Items).
    To permanently delete, move to 'deleteditems' first, then delete again.
    """
    em = _resolve_email(candidate_id, email)
    await _graph(
        "DELETE", f"/me/messages/{message_id}", em, expected_status=204
    )
