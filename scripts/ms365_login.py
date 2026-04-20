"""One-shot MS365 device-code bootstrap.

Run locally from a machine with a browser. Loads the buero M365 creds,
walks the user through the device-code prompt, and writes
`ms365_tokens.json` to the current directory. Upload that file into the
Railway persistent volume at /data/.hermes/ms365_tokens.json via the
base64-inline SSH trick (see reference_hermes_railway.md Gotcha #7).

After that first bootstrap, MSAL's refresh flow keeps the cache alive as
long as Ari uses Hermes regularly (Azure's rolling inactivity window is
~90 days for refresh tokens).

Usage:
    # Credentials file path can be overridden via MS365_CRED_FILE.
    python scripts/ms365_login.py

Reads:
    C:/Users/aribi/OneDrive/Desktop/chaim-private-credentials/.buero_m365_credentials.json
    (tenant_id, client_id, email; client_secret is NOT used - this is a
     public-client device-code flow).

Writes:
    ./ms365_tokens.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from msal import PublicClientApplication, SerializableTokenCache

SCOPES = ["Mail.Read", "Mail.Send", "User.Read"]
DEFAULT_CRED = Path(
    "C:/Users/aribi/OneDrive/Desktop/chaim-private-credentials/"
    ".buero_m365_credentials.json"
)


def main() -> int:
    cred_path = Path(os.environ.get("MS365_CRED_FILE", DEFAULT_CRED))
    if not cred_path.exists():
        print(f"ERROR: credentials file not found: {cred_path}", file=sys.stderr)
        return 1
    creds = json.loads(cred_path.read_text(encoding="utf-8"))

    cache = SerializableTokenCache()
    app = PublicClientApplication(
        client_id=creds["client_id"],
        authority=f"https://login.microsoftonline.com/{creds['tenant_id']}",
        token_cache=cache,
    )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("ERROR: device flow init failed", file=sys.stderr)
        print(json.dumps(flow, indent=2), file=sys.stderr)
        return 2

    print(flow["message"])
    print(f"Sign in as: {creds.get('email', '(buero account)')}")
    print("Waiting for consent...")

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print("ERROR: token acquisition failed", file=sys.stderr)
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 3

    out = Path("ms365_tokens.json")
    out.write_text(cache.serialize(), encoding="utf-8")
    print(f"OK - wrote {out.resolve()}")
    print(
        "Next: upload via base64-SSH:\n"
        "  B64=$(base64 -w0 ms365_tokens.json) && \\\n"
        "  cd /c/Users/aribi/code/hermes-setup && \\\n"
        "  MSYS_NO_PATHCONV=1 railway ssh --service hermes-agent \\\n"
        "    \"echo '$B64' | base64 -d > /data/.hermes/ms365_tokens.json && \\\n"
        "     chmod 600 /data/.hermes/ms365_tokens.json\""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
