#!/usr/bin/env python3
"""Reddit API CLI helper — read and post to Reddit via the official OAuth2 API.

Run reddit_auth.py first (--setup, --auth-url, --auth-code) to authenticate.
Stdlib-only, no third-party dependencies. All commands print JSON to stdout.

Usage:
    python3 reddit_api.py whoami
    python3 reddit_api.py hot SUBREDDIT [--limit 10]
    python3 reddit_api.py new SUBREDDIT [--limit 10]
    python3 reddit_api.py top SUBREDDIT [--limit 10] [--time day|week|month|year|all]
    python3 reddit_api.py search QUERY [--subreddit SUB] [--limit 10] [--sort relevance|hot|top|new] [--time all]
    python3 reddit_api.py read POST_ID [--comments 20]
    python3 reddit_api.py submit SUBREDDIT TITLE TEXT
    python3 reddit_api.py submit-link SUBREDDIT TITLE URL
    python3 reddit_api.py comment PARENT_FULLNAME TEXT
    python3 reddit_api.py vote FULLNAME up|down|clear
    python3 reddit_api.py save FULLNAME
    python3 reddit_api.py unsave FULLNAME
    python3 reddit_api.py delete FULLNAME
    python3 reddit_api.py subscribe SUBREDDIT
    python3 reddit_api.py unsubscribe SUBREDDIT

POST_ID accepts either a bare id36 (e.g. "1abc2d") or a full fullname
(e.g. "t3_1abc2d"); the "t3_" prefix is added automatically where required.
FULLNAME arguments (comment/vote/save/delete) require the full "t1_"/"t3_"
form since Reddit's API needs the type prefix to know what kind of thing
it is — `read` and search results both print each item's fullname for
exactly this purpose.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from reddit_auth import get_valid_access_token

API_BASE = "https://oauth.reddit.com"
TRUNCATE = 500


def _request(method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
    access_token, user_agent = get_valid_access_token()
    url = f"{API_BASE}{path}"
    if params:
        url += f"?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"bearer {access_token}",
            "User-Agent": user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        print(json.dumps({"error": f"HTTP {e.code}", "detail": err_body}))
        sys.exit(1)
    except urllib.error.URLError as e:
        print(json.dumps({"error": "connection_failed", "detail": str(e.reason)}))
        sys.exit(1)


def _truncate(text: str, n: int = TRUNCATE) -> str:
    if text is None:
        return ""
    return text if len(text) <= n else text[:n] + "…"


def _fullname_or_kind3(id_or_fullname: str) -> str:
    return id_or_fullname if "_" in id_or_fullname else f"t3_{id_or_fullname}"


def _bare_id(fullname_or_id: str) -> str:
    return fullname_or_id.split("_", 1)[1] if "_" in fullname_or_id else fullname_or_id


def _slim_post(child: dict) -> dict:
    d = child.get("data", {})
    return {
        "fullname": f"t3_{d.get('id')}",
        "title": d.get("title"),
        "author": d.get("author"),
        "subreddit": d.get("subreddit"),
        "score": d.get("score"),
        "upvote_ratio": d.get("upvote_ratio"),
        "num_comments": d.get("num_comments"),
        "created_utc": d.get("created_utc"),
        "is_self": d.get("is_self"),
        "url": d.get("url"),
        "selftext": _truncate(d.get("selftext", "")),
        "permalink": f"https://reddit.com{d.get('permalink', '')}",
    }


def _slim_comment(child: dict, depth: int = 0) -> dict | None:
    d = child.get("data", {})
    if child.get("kind") != "t1":
        return None
    out = {
        "fullname": f"t1_{d.get('id')}",
        "author": d.get("author"),
        "body": _truncate(d.get("body", "")),
        "score": d.get("score"),
        "created_utc": d.get("created_utc"),
        "depth": depth,
    }
    replies = d.get("replies")
    if isinstance(replies, dict):
        children = replies.get("data", {}).get("children", [])
        nested = [c for c in (_slim_comment(rc, depth + 1) for rc in children) if c]
        if nested:
            out["replies"] = nested
    return out


def _listing(path: str, params: dict) -> list:
    resp = _request("GET", path, params=params)
    children = resp.get("data", {}).get("children", [])
    return [_slim_post(c) for c in children]


def cmd_whoami():
    me = _request("GET", "/api/v1/me")
    print(json.dumps({
        "name": me.get("name"),
        "id": f"t2_{me.get('id')}",
        "link_karma": me.get("link_karma"),
        "comment_karma": me.get("comment_karma"),
        "created_utc": me.get("created_utc"),
    }, indent=2))


def cmd_listing(kind: str, subreddit: str, limit: int, time_filter: str | None = None):
    path = f"/r/{subreddit}/{kind}"
    params = {"limit": limit}
    if time_filter:
        params["t"] = time_filter
    print(json.dumps(_listing(path, params), indent=2))


def cmd_search(query: str, subreddit: str | None, limit: int, sort: str, time_filter: str):
    if subreddit:
        path = f"/r/{subreddit}/search"
        params = {"q": query, "restrict_sr": "true", "limit": limit, "sort": sort, "t": time_filter}
    else:
        path = "/search"
        params = {"q": query, "limit": limit, "sort": sort, "t": time_filter}
    print(json.dumps(_listing(path, params), indent=2))


def cmd_read(post_id: str, comment_limit: int):
    fullname = _fullname_or_kind3(post_id)
    bare = _bare_id(fullname)
    resp = _request("GET", f"/comments/{bare}", params={"limit": comment_limit})
    if not isinstance(resp, list) or len(resp) < 2:
        print(json.dumps({"error": "unexpected_response", "detail": resp}))
        sys.exit(1)
    post_children = resp[0].get("data", {}).get("children", [])
    comment_children = resp[1].get("data", {}).get("children", [])
    post = _slim_post(post_children[0]) if post_children else None
    comments = [c for c in (_slim_comment(c) for c in comment_children) if c]
    print(json.dumps({"post": post, "comments": comments}, indent=2))


def cmd_submit(subreddit: str, title: str, text: str):
    resp = _request("POST", "/api/submit", data={
        "sr": subreddit, "kind": "self", "title": title, "text": text, "api_type": "json",
    })
    print(json.dumps(resp.get("json", resp), indent=2))


def cmd_submit_link(subreddit: str, title: str, url: str):
    resp = _request("POST", "/api/submit", data={
        "sr": subreddit, "kind": "link", "title": title, "url": url, "api_type": "json",
    })
    print(json.dumps(resp.get("json", resp), indent=2))


def cmd_comment(parent_fullname: str, text: str):
    resp = _request("POST", "/api/comment", data={
        "thing_id": parent_fullname, "text": text, "api_type": "json",
    })
    print(json.dumps(resp.get("json", resp), indent=2))


def cmd_vote(fullname: str, direction: str):
    dir_map = {"up": 1, "down": -1, "clear": 0}
    if direction not in dir_map:
        print(json.dumps({"error": "invalid_direction", "detail": "use up, down, or clear"}))
        sys.exit(1)
    _request("POST", "/api/vote", data={"id": fullname, "dir": dir_map[direction]})
    print(json.dumps({"ok": True, "id": fullname, "vote": direction}))


def cmd_save(fullname: str, unsave: bool = False):
    _request("POST", "/api/unsave" if unsave else "/api/save", data={"id": fullname})
    print(json.dumps({"ok": True, "id": fullname, "saved": not unsave}))


def cmd_delete(fullname: str):
    _request("POST", "/api/del", data={"id": fullname})
    print(json.dumps({"ok": True, "deleted": fullname}))


def cmd_subscribe(subreddit: str, unsubscribe: bool = False):
    _request("POST", "/api/subscribe", data={
        "sr_name": subreddit, "action": "unsub" if unsubscribe else "sub",
    })
    print(json.dumps({"ok": True, "subreddit": subreddit, "subscribed": not unsubscribe}))


def _flag(args: list, name: str, default):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else default
        return type(default)(val) if default is not None else val
    return default


def main():
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return

    cmd, rest = args[0], args[1:]

    if cmd == "whoami":
        cmd_whoami()
    elif cmd in {"hot", "new"} and rest:
        cmd_listing(cmd, rest[0], _flag(rest, "--limit", 10))
    elif cmd == "top" and rest:
        cmd_listing("top", rest[0], _flag(rest, "--limit", 10), _flag(rest, "--time", "day"))
    elif cmd == "search" and rest:
        # query is everything up to the first --flag
        flag_positions = [i for i, a in enumerate(rest) if a.startswith("--")]
        end = flag_positions[0] if flag_positions else len(rest)
        query = " ".join(rest[:end])
        cmd_search(
            query,
            _flag(rest, "--subreddit", None),
            _flag(rest, "--limit", 10),
            _flag(rest, "--sort", "relevance"),
            _flag(rest, "--time", "all"),
        )
    elif cmd == "read" and rest:
        cmd_read(rest[0], _flag(rest, "--comments", 20))
    elif cmd == "submit" and len(rest) >= 3:
        cmd_submit(rest[0], rest[1], " ".join(rest[2:]))
    elif cmd == "submit-link" and len(rest) >= 3:
        cmd_submit_link(rest[0], rest[1], rest[2])
    elif cmd == "comment" and len(rest) >= 2:
        cmd_comment(rest[0], " ".join(rest[1:]))
    elif cmd == "vote" and len(rest) >= 2:
        cmd_vote(rest[0], rest[1])
    elif cmd == "save" and rest:
        cmd_save(rest[0])
    elif cmd == "unsave" and rest:
        cmd_save(rest[0], unsave=True)
    elif cmd == "delete" and rest:
        cmd_delete(rest[0])
    elif cmd == "subscribe" and rest:
        cmd_subscribe(rest[0])
    elif cmd == "unsubscribe" and rest:
        cmd_subscribe(rest[0], unsubscribe=True)
    else:
        print(f"Unknown or incomplete command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
