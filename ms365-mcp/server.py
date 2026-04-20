"""Hermes MS365 (Outlook Mail) MCP server.

Exposes four delegated-auth Microsoft Graph tools. Each call picks a
mailbox by name (default "abirnbaum"); the server keeps one MSAL token
cache per mailbox so every Graph request runs against `/me` - i.e. the
signed-in user's own Exchange Online inbox. No /users/{upn} routing, no
Mail.Read.Shared magic: the mailbox-per-token model sidesteps shared-
mailbox delegation entirely.

Tokens are bootstrapped once locally via
`scripts/ms365_login.py --mailbox <name>`, which writes to the
corresponding cache file below. MSAL refreshes silently per call;
caches are re-serialised whenever state changes.
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
# Present when the Azure app registration is Web/confidential (our case).
# MSAL wires it into refresh requests automatically. Unset -> public client.
CLIENT_SECRET = os.environ.get("MS365_CLIENT_SECRET")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))

# Known mailboxes, each with its own OAuth token cache. Add a row here +
# run `scripts/ms365_login.py --mailbox <name>` to onboard another
# mailbox (kundendienst, info, ...). No code changes elsewhere required.
MAILBOXES: dict[str, Path] = {
    "abirnbaum": HERMES_HOME / "ms365_tokens.json",
    "instandhaltung": HERMES_HOME / "ms365_tokens_instandhaltung.json",
}
DEFAULT_MAILBOX = "abirnbaum"

SCOPES = [
    "Mail.Read",
    "Mail.Send",
    "Mail.Read.Shared",
    "Mail.Send.Shared",
    "User.Read",
    "User.ReadBasic.All",
]
GRAPH = "https://graph.microsoft.com/v1.0"
_AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

mcp = FastMCP("hermes-ms365")

# Lazy-initialised per-mailbox clients. Lazy so a missing cache for one
# mailbox doesn't prevent the other from working.
_clients: dict[str, tuple[Any, SerializableTokenCache, Path]] = {}


def _resolve_mailbox(mailbox: str | None) -> str:
    """Normalise the mailbox argument to a registry key."""
    if not mailbox or mailbox == "me":
        return DEFAULT_MAILBOX
    key = mailbox.split("@", 1)[0].lower()
    if key not in MAILBOXES:
        raise RuntimeError(
            f"Unknown mailbox {mailbox!r}. Known: {sorted(MAILBOXES)}. "
            "To onboard a new one: add it to MAILBOXES and run "
            "`scripts/ms365_login.py --mailbox <name>`."
        )
    return key


def _client_for(mailbox: str) -> tuple[Any, SerializableTokenCache, Path]:
    if mailbox in _clients:
        return _clients[mailbox]
    path = MAILBOXES[mailbox]
    cache = SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    if CLIENT_SECRET:
        app = ConfidentialClientApplication(
            client_id=CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=_AUTHORITY,
            token_cache=cache,
        )
    else:
        app = PublicClientApplication(
            client_id=CLIENT_ID,
            authority=_AUTHORITY,
            token_cache=cache,
        )
    _clients[mailbox] = (app, cache, path)
    return _clients[mailbox]


def _access_token(mailbox: str) -> str:
    app, cache, path = _client_for(mailbox)
    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError(
            f"MS365 token cache empty for mailbox {mailbox!r} at {path}. "
            f"Run `python scripts/ms365_login.py --mailbox {mailbox}` "
            f"locally, then upload the resulting JSON to {path} via base64-SSH."
        )
    result = app.acquire_token_silent(scopes=SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise RuntimeError(
            f"MS365 silent token refresh failed for {mailbox!r}. "
            f"Re-run `python scripts/ms365_login.py --mailbox {mailbox}`. "
            f"Detail: {result!r}"
        )
    if cache.has_state_changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize(), encoding="utf-8")
    return result["access_token"]


def _graph_get(path: str, mailbox: str, params: dict[str, Any] | None = None) -> dict:
    r = httpx.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {_access_token(mailbox)}"},
        params=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _graph_post(path: str, mailbox: str, body: dict[str, Any]) -> None:
    r = httpx.post(
        f"{GRAPH}{path}",
        headers={
            "Authorization": f"Bearer {_access_token(mailbox)}",
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

    `mailbox`: None/"me" (default) = `abirnbaum` mailbox;
    `"instandhaltung"` = Instandhaltung mailbox. Known mailboxes are
    registered in server.py MAILBOXES; unknown names raise an error.

    Returns subject, sender, received timestamp, preview (~255 chars) and
    read/attachment flags - enough for the agent to triage without a
    second read call. `top` is clamped to [1, 50]. When `unread_only` is
    True, only messages with isRead=false are returned.

    Use `read_email(message_id, mailbox=...)` afterwards to fetch the
    full body; pass the same `mailbox` there.
    """
    mbox = _resolve_mailbox(mailbox)
    top = max(1, min(50, top))
    params: dict[str, Any] = {
        "$top": top,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments",
        "$orderby": "receivedDateTime desc",
    }
    if unread_only:
        params["$filter"] = "isRead eq false"
    data = _graph_get("/me/mailFolders/inbox/messages", mbox, params=params)
    return {
        "mailbox": mbox,
        "count": len(data.get("value", [])),
        "messages": [_trim_msg(m) for m in data.get("value", [])],
    }


@mcp.tool()
def read_email(message_id: str, mailbox: str | None = None) -> dict:
    """Fetch a single message in full, including the HTML body, recipients
    and attachment metadata (name + size only, no binary content).

    `message_id` is the opaque id returned by list_recent_emails or
    search_emails. Pass the same `mailbox` used in those calls.
    Marking-as-read is NOT performed.
    """
    mbox = _resolve_mailbox(mailbox)
    m = _graph_get(
        f"/me/messages/{message_id}",
        mbox,
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
        "mailbox": mbox,
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
            f"/me/messages/{message_id}/attachments",
            mbox,
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
    mbox = _resolve_mailbox(mailbox)
    top = max(1, min(50, top))
    data = _graph_get(
        "/me/messages",
        mbox,
        params={
            "$search": f'"{query}"',
            "$top": top,
            "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments",
        },
    )
    return {
        "mailbox": mbox,
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

    `mailbox` None/"me" (default) = send AS abirnbaum;
    `"instandhaltung"` = send AS Instandhaltung@buero-birnbaum.de.
    The From-address derives from the chosen mailbox's token - each
    mailbox authenticates itself, so the sender line matches.

    `to` and `cc` are lists of plain addresses. `body_type` is "HTML" or
    "Text". Mail is saved to Sent Items automatically (Graph default).

    **Precondition (enforced by SOUL.md, not by this tool):** Hermes must
    post a preview of the drafted mail to the user and receive explicit
    confirmation before calling this. The tool itself performs no
    interactive gate - it trusts the caller.
    """
    if not to:
        raise ValueError("'to' must contain at least one recipient")
    mbox = _resolve_mailbox(mailbox)
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
    _graph_post("/me/sendMail", mbox, payload)
    return {"ok": True, "mailbox": mbox, "to": to, "cc": cc or [], "subject": subject}


if __name__ == "__main__":
    mcp.run()
