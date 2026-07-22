#!/usr/bin/env python3
"""Reddit OAuth2 setup for Hermes Agent.

Fully non-interactive — designed to be driven by the agent via terminal commands.
The agent mediates between this script and the user (works on CLI, Telegram, Discord, etc.)
Stdlib-only: no third-party dependencies (no ``praw``), matching Hermes' other
lightweight API-client skills.

Commands:
  reddit_auth.py --check                                   # Is auth valid? Exit 0 = yes, 1 = no
  reddit_auth.py --setup --client-id ID --client-secret S --username NAME
                                                             # Store OAuth app credentials
  reddit_auth.py --auth-url                                 # Print the OAuth URL for user to visit
  reddit_auth.py --auth-code CODE_OR_URL                     # Exchange auth code for token
  reddit_auth.py --revoke                                    # Revoke and delete stored token

Agent workflow:
  1. Run --check. If exit 0, auth is good — skip setup.
  2. Ask the user for their Reddit app's client ID, client secret, and Reddit
     username (the "app" must be created by the user themselves, out of band,
     at https://www.reddit.com/prefs/apps as a "web app" — see SKILL.md).
     Run --setup with those three values.
  3. Run --auth-url. Send the printed URL to the user.
  4. User opens the URL, logs into Reddit, approves access, and lands on a
     page that fails to load (http://localhost:1/...) — that's expected.
     They copy the full URL (or just the `code` query param) from the
     browser's address bar and paste it back.
  5. Run --auth-code with what they pasted.
  6. Run --check to verify. Done — the refresh token persists, so this is a
     one-time setup per Reddit account.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _hermes_home import display_hermes_home, get_hermes_home

HERMES_HOME = get_hermes_home()
CLIENT_PATH = HERMES_HOME / "reddit_client.json"
TOKEN_PATH = HERMES_HOME / "reddit_token.json"
PENDING_AUTH_PATH = HERMES_HOME / "reddit_oauth_pending.json"

AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Reddit deprecated its own "out of band" flow years ago. Like the
# google-workspace skill, we use a localhost redirect that will fail to
# load in the browser — the authorization code stays visible in the
# address bar for the user to copy. Must match the redirect URI entered
# in the Reddit app settings exactly.
REDIRECT_URI = "http://localhost:1"

SCOPES = ["identity", "read", "submit", "vote", "save", "subscribe", "history", "mysubreddits", "edit"]


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX perms (e.g. Windows)


def _load_client() -> dict:
    client = _load_json(CLIENT_PATH)
    if not client.get("client_id") or not client.get("client_secret"):
        print("ERROR: No Reddit app credentials stored. Run --setup first.")
        sys.exit(1)
    return client


def user_agent_for(username: str) -> str:
    return f"hermes-agent-fork:reddit-api-skill:v1.0 (by /u/{username})"


def store_client(client_id: str, client_secret: str, username: str):
    _write_json(
        CLIENT_PATH,
        {"client_id": client_id, "client_secret": client_secret, "username": username},
    )
    print(f"OK: Reddit app credentials saved to {CLIENT_PATH}")


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_request(client: dict, body: dict) -> dict:
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": _basic_auth_header(client["client_id"], client["client_secret"]),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": user_agent_for(client["username"]),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(f"ERROR: Reddit returned HTTP {e.code}: {body_text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: Connection to Reddit failed: {e.reason}")
        sys.exit(1)


def get_auth_url():
    client = _load_client()
    state = base64.urlsafe_b64encode(str(time.time()).encode()).decode().rstrip("=")
    params = {
        "client_id": client["client_id"],
        "response_type": "code",
        "state": state,
        "redirect_uri": REDIRECT_URI,
        "duration": "permanent",  # required to get a refresh_token back
        "scope": " ".join(SCOPES),
    }
    _write_json(PENDING_AUTH_PATH, {"state": state})
    print(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")


def _extract_code_and_state(code_or_url: str) -> tuple[str, str | None]:
    if not code_or_url.startswith("http"):
        return code_or_url, None
    parsed = urllib.parse.urlparse(code_or_url)
    params = urllib.parse.parse_qs(parsed.query)
    if "error" in params:
        print(f"ERROR: Reddit denied authorization: {params['error'][0]}")
        sys.exit(1)
    if "code" not in params:
        print("ERROR: No 'code' parameter found in the pasted URL.")
        sys.exit(1)
    return params["code"][0], params.get("state", [None])[0]


def exchange_auth_code(code_or_url: str):
    client = _load_client()
    pending = _load_json(PENDING_AUTH_PATH)
    if not pending.get("state"):
        print("ERROR: No pending OAuth session found. Run --auth-url first.")
        sys.exit(1)

    code, returned_state = _extract_code_and_state(code_or_url)
    if returned_state and returned_state != pending["state"]:
        print("ERROR: OAuth state mismatch. Run --auth-url again to start a fresh session.")
        sys.exit(1)

    token = _token_request(
        client, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    )
    if "refresh_token" not in token:
        print(f"ERROR: No refresh_token in response (duration=permanent should always return one): {token}")
        sys.exit(1)

    token["obtained_at"] = time.time()
    _write_json(TOKEN_PATH, token)
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    print(f"OK: Authenticated as /u/{client['username']}. Token saved to {TOKEN_PATH}")
    print(f"Profile-scoped token location: {display_hermes_home()}/reddit_token.json")


def get_valid_access_token() -> tuple[str, str]:
    """Return (access_token, user_agent), refreshing if expired. Used by reddit_api.py."""
    client = _load_client()
    token = _load_json(TOKEN_PATH)
    if not token.get("refresh_token"):
        print("ERROR: Not authenticated. Run reddit_auth.py --auth-url then --auth-code.")
        sys.exit(1)

    obtained_at = token.get("obtained_at", 0)
    expires_in = token.get("expires_in", 3600)
    if time.time() < obtained_at + expires_in - 60:  # 60s safety margin
        return token["access_token"], user_agent_for(client["username"])

    refreshed = _token_request(client, {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    refreshed.setdefault("refresh_token", token["refresh_token"])  # Reddit omits it on refresh
    refreshed["obtained_at"] = time.time()
    _write_json(TOKEN_PATH, refreshed)
    return refreshed["access_token"], user_agent_for(client["username"])


def check_auth() -> bool:
    if not CLIENT_PATH.exists():
        print(f"NOT_CONFIGURED: No app credentials at {CLIENT_PATH}. Run --setup.")
        return False
    if not TOKEN_PATH.exists():
        print(f"NOT_AUTHENTICATED: No token at {TOKEN_PATH}. Run --auth-url then --auth-code.")
        return False
    try:
        access_token, user_agent = get_valid_access_token()
    except SystemExit:
        return False
    req = urllib.request.Request(
        "https://oauth.reddit.com/api/v1/me",
        headers={"Authorization": f"bearer {access_token}", "User-Agent": user_agent},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            me = json.loads(resp.read().decode())
        print(f"AUTHENTICATED: Token valid, live call succeeded as /u/{me.get('name', '?')}")
        return True
    except Exception as e:
        print(f"LIVE_CHECK_FAILED: {e}")
        return False


def revoke():
    if not TOKEN_PATH.exists():
        print("No token to revoke.")
        return
    client = _load_client()
    token = _load_json(TOKEN_PATH)
    if token.get("refresh_token"):
        data = urllib.parse.urlencode(
            {"token": token["refresh_token"], "token_type_hint": "refresh_token"}
        ).encode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/revoke_token",
            data=data,
            method="POST",
            headers={
                "Authorization": _basic_auth_header(client["client_id"], client["client_secret"]),
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": user_agent_for(client["username"]),
            },
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            print("Token revoked with Reddit.")
        except Exception as e:
            print(f"Remote revocation failed (token may already be invalid): {e}")
    TOKEN_PATH.unlink(missing_ok=True)
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    print(f"Deleted {TOKEN_PATH}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Reddit OAuth2 setup for Hermes")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--setup", action="store_true", help="Use with --client-id/--client-secret/--username")
    group.add_argument("--auth-url", action="store_true")
    group.add_argument("--auth-code", metavar="CODE_OR_URL")
    group.add_argument("--revoke", action="store_true")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--username", help="Your Reddit username (no u/ prefix) — required in the User-Agent")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check_auth() else 1)
    elif args.setup:
        if not (args.client_id and args.client_secret and args.username):
            print("ERROR: --setup requires --client-id, --client-secret, and --username")
            sys.exit(1)
        store_client(args.client_id, args.client_secret, args.username)
    elif args.auth_url:
        get_auth_url()
    elif args.auth_code:
        exchange_auth_code(args.auth_code)
    elif args.revoke:
        revoke()


if __name__ == "__main__":
    main()
