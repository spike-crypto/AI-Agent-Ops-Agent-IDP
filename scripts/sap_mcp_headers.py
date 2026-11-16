"""Print MCP auth headers for SAP Integration Suite's MCP Gateway (client-credentials OAuth).

Claude Code runs this as a `headersHelper`: it must print one JSON object of headers.
Reads SAP_TOKEN_URL, SAP_CLIENT_ID and SAP_CLIENT_SECRET from `.env.sap-oauth` next to the
repo root (git-ignored), and caches the token in `.sap-token-cache.json` until a minute
before it expires, so Claude Code does not ask SAP for a token on every connection.

    python scripts/sap_mcp_headers.py          # prints {"Authorization": "Bearer ..."}
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.sap-oauth"
CACHE = ROOT / ".sap-token-cache.json"


def _config() -> dict:
    cfg = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    missing = [k for k in ("SAP_TOKEN_URL", "SAP_CLIENT_ID", "SAP_CLIENT_SECRET") if not cfg.get(k)]
    if missing:
        sys.exit(f"{ENV_FILE.name} is missing {', '.join(missing)}")
    return cfg


def _cached() -> str | None:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if data["expires_at"] - 60 > time.time():
            return data["access_token"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _fetch(cfg: dict) -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cfg["SAP_CLIENT_ID"],
                                   "client_secret": cfg["SAP_CLIENT_SECRET"]}).encode()
    req = urllib.request.Request(cfg["SAP_TOKEN_URL"], data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"SAP token request failed: HTTP {exc.code}")
    CACHE.write_text(json.dumps({"access_token": tok["access_token"],
                                 "expires_at": time.time() + int(tok.get("expires_in", 3600))}), encoding="utf-8")
    return tok["access_token"]


if __name__ == "__main__":
    token = _cached() or _fetch(_config())
    print(json.dumps({"Authorization": f"Bearer {token}"}))
