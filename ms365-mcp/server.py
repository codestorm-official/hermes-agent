"""Hermes MS365 (Outlook Mail) MCP server.

Exposes four delegated-auth Microsoft Graph tools for the buero-birnbaum
mailbox: list, read, search, send. Token acquisition is device-code flow
bootstrapped once locally (see scripts/ms365_login.py) and written into
the Railway persistent volume at $HERMES_HOME/ms365_tokens.json. MSAL
refreshes silently on every call; the cache is re-serialised whenever
state changes. httpx drives the REST endpoints directly - the official
msgraph-sdk-python pulls a heavy async stack we don't need for four
synchronous calls.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from msal import ConfidentialClientApplication, PublicClientApplication, SerializableTokenCache

CLIENT_ID = os.environ["MS365_CLIENT_ID"]
TENANT_ID = os.environ["MS365_TENANT_ID"]
# Optional - when present, we switch to ConfidentialClientApplication, which
# is required whenever the Azure app registration is a Web/confidential
# client (the usual default). Refresh calls then include the secret
# automatically. Leave unset for public-client-only app registrations.
CLIENT_SECRET = os.environ.get("MS365_CLIENT_SECRET")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
CACHE_PATH = HERMES_HOME / "ms365_tokens.json"

SCOPES = [
    "Mail.Read",
    "Mail.Send",
    "Mail.Read.Shared",
    "Mail.Send.Shared",
    "User.Read",
]
GRAPH = "https://graph.microsoft.com/v1.0"


def _mailbox_prefix(mailbox: str | None) -> str:
    """Resolve the Graph path prefix for a given mailbox reference.

    `None` (default) -> `/me`, i.e. the signed-in user's own mailbox
    (abirnbaum@buero-birnbaum.de).

    Any other value is treated as a shared-mailbox UPN or id and routed
    via `/users/<id>`. The signed-in user must have delegated access to
    the shared mailbox (Exchange admin grants this), and the token must
    carry `Mail.Read.Shared` / `Mail.Send.Shared` - both covered by the
    consent granted via scripts/ms365_login.py.

    Common callers use the short alias 'instandhaltung' for
    'instandhaltung@buero-birnbaum.de' - we expand it for ergonomics.
    """
    if not mailbox or mailbox == "me":
        return "/me"
    if "@" not in mailbox:
        mailbox = f"{mailbox}@buero-birnbaum.de"
    return f"/users/{mailbox}"

_cache = SerializableTokenCache()
if CACHE_PATH.exists():
    _cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))

_authority = f"https://login.microsoftonline.com/{TENANT_ID}"
if CLIENT_SECRET:
    _app = ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=_authority,
        token_cache=_cache,
    )
else:
    _app = PublicClientApplication(
        client_id=CLIENT_ID,
        authority=_authority,
        token_cache=_cache,
    )

mcp = FastMCP("hermes-ms365")


def _persist_cache() -> None:
    if _cache.has_state_changed:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(_cache.serialize(), encoding="utf-8")


def _access_token() -> str:
    accounts = _app.get_accounts()
    if not accounts:
        raise RuntimeError(
            f"MS365 token cache empty at {CACHE_PATH}. "
            "Run scripts/ms365_login.py locally and upload the file to "
            "/data/.hermes/ms365_tokens.json via base64-SSH."
        )
    result = _app.acquire_token_silent(scopes=SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        # Refresh token itself expired (90d rolling inactivity window) or
        # was revoked. Re-bootstrap is the only recovery path.
        raise RuntimeError(
            "MS365 silent token refresh failed. Re-run scripts/ms365_login.py "
            f"locally and replace {CACHE_PATH}. Detail: {result!r}"
        )
    _persist_cache()
    return result["access_token"]


def _graph_get(path: str, params: dict[str, Any] | None = None) -> dict:
    r = httpx.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        params=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _graph_post(path: str, body: dict[str, Any]) -> None:
    r = httpx.post(
        f"{GRAPH}{path}",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30.0,
    )
    r.raise_for_status()


def _trim_msg(m: dict) -> dict:
    """Condense a Graph message payload to the fields we return to the agent."""
    frm = (m.get("from") or {}).get("emailAddress") or {}
    return {
        "id": m.get("id"),
        "subject": m.get("subject"),
        "from": {"name": frm.get("name"), "address": frm.get("address")},
        "received": m.get("receivedDateTime"),
        "preview": m.get("bodyPreview"),
        "is_read": m.get("isRead"),
        "has_attachments": m.get("hasAttachments"),
    }


@mcp.tool()
def list_recent_emails(
    top: int = 20,
    unread_only: bool = False,
    mailbox: str | None = None,
) -> dict:
    """List the most recent messages from a mailbox's Inbox.

    `mailbox`: None (default) = Ari's own buero-birnbaum inbox;
    `"instandhaltung"` or any UPN like `"instandhaltung@buero-birnbaum.de"`
    = shared mailbox (requires delegated access, already in scope).

    Returns subject, sender, received timestamp, preview (~255 chars) and
    read/attachment flags - enough for the agent to triage without a
    second read call. `top` is clamped to [1, 50]. When `unread_only` is
    True, only messages with isRead=false are returned.

    Use `read_email(message_id, mailbox=...)` afterwards to fetch the
    full body; pass the same `mailbox` there.
    """
    top = max(1, min(50, top))
    params: dict[str, Any] = {
        "$top": top,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments",
        "$orderby": "receivedDateTime desc",
    }
    if unread_only:
        params["$filter"] = "isRead eq false"
    data = _graph_get(f"{_mailbox_prefix(mailbox)}/mailFolders/inbox/messages", params=params)
    return {
        "mailbox": mailbox or "me",
        "count": len(data.get("value", [])),
        "messages": [_trim_msg(m) for m in data.get("value", [])],
    }


@mcp.tool()
def read_email(message_id: str, mailbox: str | None = None) -> dict:
    """Fetch a single message in full, including the HTML body, recipients
    and attachment metadata (name + size only, no binary content).

    `message_id` is the opaque id returned by list_recent_emails or
    search_emails. Pass the same `mailbox` used in those calls.
    Marking-as-read is NOT performed - callers that want read-receipt
    semantics should add that explicitly (out of scope v1).
    """
    prefix = _mailbox_prefix(mailbox)
    m = _graph_get(
        f"{prefix}/messages/{message_id}",
        params={
            "$select": (
                "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                "body,isRead,hasAttachments"
            ),
        },
    )
    to = [r.get("emailAddress", {}) for r in m.get("toRecipients", []) or []]
    cc = [r.get("emailAddress", {}) for r in m.get("ccRecipients", []) or []]
    frm = (m.get("from") or {}).get("emailAddress") or {}
    body = m.get("body") or {}
    result = {
        "id": m.get("id"),
        "subject": m.get("subject"),
        "from": {"name": frm.get("name"), "address": frm.get("address")},
        "to": [{"name": r.get("name"), "address": r.get("address")} for r in to],
        "cc": [{"name": r.get("name"), "address": r.get("address")} for r in cc],
        "received": m.get("receivedDateTime"),
        "is_read": m.get("isRead"),
        "body_type": body.get("contentType"),
        "body": body.get("content"),
        "has_attachments": m.get("hasAttachments"),
    }
    if m.get("hasAttachments"):
        atts = _graph_get(
            f"{prefix}/messages/{message_id}/attachments",
            params={"$select": "id,name,size,contentType,isInline"},
        )
        result["attachments"] = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "size": a.get("size"),
                "content_type": a.get("contentType"),
                "inline": a.get("isInline"),
            }
            for a in atts.get("value", [])
        ]
    return result


@mcp.tool()
def search_emails(query: str, top: int = 20, mailbox: str | None = None) -> dict:
    """Full-text search across a mailbox (subject + body + sender).

    `mailbox` follows the same convention as list_recent_emails.

    Uses Graph's KQL `$search` parameter, which must be quoted. Examples
    for `query`: `ostendorf`, `from:ostendorf@ra-ostendorf.de`, `subject:Kaution`,
    `received>=2026-04-01`. `top` is clamped to [1, 50]. Results are
    trimmed to the same preview shape as list_recent_emails.

    Note: $search + $orderby are mutually exclusive on Graph. Ordering
    here is relevance-descending by Graph default.
    """
    top = max(1, min(50, top))
    data = _graph_get(
        f"{_mailbox_prefix(mailbox)}/messages",
        params={
            "$search": f'"{query}"',
            "$top": top,
            "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments",
        },
    )
    return {
        "mailbox": mailbox or "me",
        "query": query,
        "count": len(data.get("value", [])),
        "messages": [_trim_msg(m) for m in data.get("value", [])],
    }


@mcp.tool()
def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    body_type: str = "HTML",
    mailbox: str | None = None,
) -> dict:
    """Send a mail via Graph sendMail.

    `mailbox` None (default) = from abirnbaum@buero-birnbaum.de;
    `"instandhaltung"` or any shared-mailbox UPN = send *as* that mailbox
    (needs delegated Send-As permission, already covered by
    Mail.Send.Shared in consent).

    `to` and `cc` are lists of plain addresses. `body_type` is "HTML" or
    "Text". Mail is saved to Sent Items automatically (Graph default).

    **Precondition (enforced by SOUL.md, not by this tool):** Hermes must
    post a preview of the drafted mail to the user and receive explicit
    confirmation before calling this. The tool itself performs no
    interactive gate - it trusts the caller.
    """
    if not to:
        raise ValueError("'to' must contain at least one recipient")
    body_type = "HTML" if body_type.upper() == "HTML" else "Text"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": body_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        },
        "saveToSentItems": True,
    }
    if cc:
        payload["message"]["ccRecipients"] = [
            {"emailAddress": {"address": a}} for a in cc
        ]
    _graph_post(f"{_mailbox_prefix(mailbox)}/sendMail", payload)
    return {"ok": True, "mailbox": mailbox or "me", "to": to, "cc": cc or [], "subject": subject}


if __name__ == "__main__":
    mcp.run()
