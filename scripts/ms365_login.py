"""One-shot MS365 auth-code bootstrap (Confidential Client).

Runs a short-lived HTTP server on http://localhost:8400/callback, opens
the Microsoft login page in the default browser, captures the
authorization code from the redirect, and exchanges it for a token
cache. Writes `ms365_tokens.json` to the current directory.

Azure prerequisite (one-time):
    App registration > Authentication > Platform configurations > Web >
    Redirect URIs must include:  http://localhost:8400/callback

After this bootstrap, upload `ms365_tokens.json` to Railway:
    B64=$(base64 -w0 ms365_tokens.json)
    cd /c/Users/aribi/code/hermes-setup
    MSYS_NO_PATHCONV=1 railway ssh --service hermes-agent \\
      "echo '$B64' | base64 -d > /data/.hermes/ms365_tokens.json && \\
       chmod 600 /data/.hermes/ms365_tokens.json"

Usage:
    python scripts/ms365_login.py --mailbox abirnbaum     # default
    python scripts/ms365_login.py --mailbox instandhaltung
    # optional: MS365_CRED_FILE=/path/to/creds.json to override default

Reads creds file with tenant_id, client_id, client_secret, email. The
same Azure app registration serves every mailbox - just log in as the
mailbox account (Instandhaltung@... with its own password, etc.) when
the browser prompt comes up.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from msal import ConfidentialClientApplication, SerializableTokenCache

SCOPES = [
    "Mail.Read",
    "Mail.Send",
    "Mail.Read.Shared",
    "Mail.Send.Shared",
    "User.Read",
    "User.ReadBasic.All",
]
DEFAULT_CRED = Path(
    "C:/Users/aribi/OneDrive/Desktop/chaim-private-credentials/"
    ".hermes_m365_credentials.json"
)
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8400
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

MAILBOX_HINTS: dict[str, str] = {
    "abirnbaum": "abirnbaum@buero-birnbaum.de",
    "instandhaltung": "Instandhaltung@buero-birnbaum.de",
}
OUT_NAMES: dict[str, str] = {
    "abirnbaum": "ms365_tokens.json",
    "instandhaltung": "ms365_tokens_instandhaltung.json",
}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the first /callback hit and exposes its query params."""

    captured: dict[str, str] | None = None

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if not self.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        type(self).captured = params
        body = (
            b"<html><body style='font-family:sans-serif;padding:2em'>"
            b"<h2>OK - du kannst dieses Fenster schliessen.</h2>"
            b"<p>Token wird jetzt im Terminal erzeugt.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence access logs
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap MS365 token cache for a named mailbox.")
    parser.add_argument(
        "--mailbox",
        default="abirnbaum",
        choices=sorted(OUT_NAMES),
        help="Which mailbox to authenticate. Determines output filename. Default: abirnbaum.",
    )
    args = parser.parse_args()
    mailbox = args.mailbox

    cred_path = Path(os.environ.get("MS365_CRED_FILE", DEFAULT_CRED))
    if not cred_path.exists():
        print(f"ERROR: credentials file not found: {cred_path}", file=sys.stderr)
        return 1
    creds = json.loads(cred_path.read_text(encoding="utf-8"))

    secret = creds.get("client_secret")
    if not secret:
        print("ERROR: client_secret missing from credentials file", file=sys.stderr)
        return 1

    cache = SerializableTokenCache()
    app = ConfidentialClientApplication(
        client_id=creds["client_id"],
        client_credential=secret,
        authority=f"https://login.microsoftonline.com/{creds['tenant_id']}",
        token_cache=cache,
    )

    flow = app.initiate_auth_code_flow(scopes=SCOPES, redirect_uri=REDIRECT_URI)
    if "auth_uri" not in flow:
        print("ERROR: auth code flow init failed", file=sys.stderr)
        print(json.dumps(flow, indent=2), file=sys.stderr)
        return 2

    server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    print(f"Waiting on {REDIRECT_URI}")
    print(f"Target mailbox: {mailbox}")
    print(f"Sign in as: {MAILBOX_HINTS.get(mailbox, creds.get('email', '(buero account)'))}")
    print("Opening browser...")
    print(f"(If it doesn't open: {flow['auth_uri']})")
    try:
        webbrowser.open(flow["auth_uri"])
    except Exception:
        pass

    # Block until the callback handler grabs the code.
    import time

    deadline = time.time() + 300  # 5 min
    while _CallbackHandler.captured is None and time.time() < deadline:
        time.sleep(0.5)
    server.shutdown()

    captured = _CallbackHandler.captured
    if not captured:
        print("ERROR: no callback received within 5 minutes", file=sys.stderr)
        return 3
    if "error" in captured:
        print(f"ERROR: auth server returned: {captured}", file=sys.stderr)
        return 4

    result = app.acquire_token_by_auth_code_flow(flow, captured)
    if "access_token" not in result:
        print("ERROR: token exchange failed", file=sys.stderr)
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 5

    out = Path(OUT_NAMES[mailbox])
    out.write_text(cache.serialize(), encoding="utf-8")
    print(f"OK - wrote {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
