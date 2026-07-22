---
name: reddit-api
description: "Reddit via the official OAuth2 API: browse, search, read threads, post, comment, vote — no browser, no anti-bot cat-and-mouse."
version: 1.0.0
author: Hermes Agent (this fork)
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: reddit_client.json
    description: Reddit OAuth2 app credentials (client id/secret you register yourself)
  - path: reddit_token.json
    description: Reddit OAuth2 token (created by reddit_auth.py, refresh_token persists)
metadata:
  hermes:
    tags: [Reddit, social-media, OAuth, official-api]
    homepage: https://github.com/joeysmithjrs/hermes-agent
    related_skills: [xurl]
---

# Reddit API

Read and post to Reddit through Reddit's own OAuth2 API (`oauth.reddit.com`) —
the same pattern the `xurl` skill uses for X/Twitter. This exists specifically
because Reddit's website aggressively blocks browser automation (network-level
"you've been blocked by network security" walls that survive Browserbase's
proxy + stealth mode) — the API sidesteps that entirely, since it's not
pretending to be a browser at all.

Use this instead of the `browser` tool for anything Reddit. Browser automation
against reddit.com should be treated as unreliable in this environment.

---

## Secret Safety (MANDATORY)

- **Never** read, print, parse, summarize, upload, or send `reddit_client.json`
  or `reddit_token.json` to LLM context. They live in `$HERMES_HOME` and only
  the scripts in this skill should touch them.
- **Never** ask the user to paste their Reddit password into chat. This skill
  never uses a password — only OAuth2 authorization-code with a redirect, the
  same shape as a normal "Sign in with Reddit" web flow.
- App registration (creating the OAuth client) must be done by the user
  manually, outside the agent session, at reddit.com. The agent may relay the
  auth URL and accept the pasted-back code — that's normal OAuth, not a secret
  leak, since an authorization code is single-use and short-lived.
- Do not print the contents of `reddit_auth.py --setup` back to the user after
  running it — just confirm success/failure.

---

## Scripts

- `scripts/reddit_auth.py` — OAuth2 setup (run once per Reddit account)
- `scripts/reddit_api.py` — the actual API commands (browse, search, post, etc.)

Both are stdlib-only Python (no `praw`, no pip install needed).

---

## One-Time User Setup

Define a shorthand first:

```bash
RAUTH="python3 ${HERMES_HOME:-$HOME/.hermes}/skills/social-media/reddit-api/scripts/reddit_auth.py"
RAPI="python3 ${HERMES_HOME:-$HOME/.hermes}/skills/social-media/reddit-api/scripts/reddit_api.py"
```

### Step 0: Check if already set up

```bash
$RAUTH --check
```

If it prints `AUTHENTICATED`, skip straight to Usage.

### Step 1: User registers a Reddit app (outside the agent, or relayed by the agent — no secret exposure either way)

Direct the user to:

1. Go to https://www.reddit.com/prefs/apps (must be logged into the Reddit
   account that will do the posting/reading).
2. Click **"create app"** / **"create another app"**.
3. Name it anything (e.g. "hermes-agent").
4. Select type **"web app"** (not "script" — script apps use the password
   grant, which would require the account password; web app uses the
   authorization-code grant, which never touches the password after this
   one-time setup).
5. Set **redirect uri** to exactly: `http://localhost:1`
6. Click **create app**. Copy the **client ID** (the string under the app
   name, ~14 chars) and the **secret** (labeled "secret").

### Step 2: Store the app credentials

```bash
$RAUTH --setup --client-id CLIENT_ID --client-secret CLIENT_SECRET --username REDDIT_USERNAME
```

(`--username` is the Reddit account's username, no `u/` prefix — Reddit
requires it in the User-Agent string on every request, and blocks requests
with generic/missing User-Agents more aggressively than most APIs.)

### Step 3: Get the authorization URL

```bash
$RAUTH --auth-url
```

Send the printed URL to the user.

### Step 4: User authorizes

The user opens the URL, logs into Reddit (if not already), and clicks
**"Allow"**. They land on a page at `http://localhost:1/...` that **fails to
load** — this is expected, nothing runs on that port. The authorization code
is in the browser's address bar. They copy the full URL (or just the `code=`
value) and paste it back.

### Step 5: Exchange the code

```bash
$RAUTH --auth-code "PASTED_URL_OR_CODE"
```

### Step 6: Verify

```bash
$RAUTH --check
```

Should print `AUTHENTICATED: Token valid, live call succeeded as /u/<username>`.
This is a one-time setup — the refresh token persists across restarts, and
`reddit_api.py` auto-refreshes the access token before it expires.

---

## Usage

All commands print JSON to stdout.

```bash
$RAPI whoami

$RAPI hot AI_Agents --limit 10
$RAPI new AI_Agents --limit 10
$RAPI top AI_Agents --limit 5 --time month

$RAPI search "agentic workflows" --subreddit AI_Agents --limit 10 --sort top --time month
$RAPI search "agentic workflows" --limit 10                    # site-wide search

$RAPI read 1abc2d3 --comments 20                                 # post + comment tree
$RAPI read t3_1abc2d3 --comments 20                               # fullname form also works

$RAPI submit AI_Agents "My post title" "Body text of the post."
$RAPI submit-link AI_Agents "Cool link" "https://example.com/article"

$RAPI comment t3_1abc2d3 "Nice post!"                             # reply to a post
$RAPI comment t1_kx9f2a1 "Agreed."                                # reply to a comment

$RAPI vote t3_1abc2d3 up                                          # up | down | clear
$RAPI save t3_1abc2d3
$RAPI unsave t3_1abc2d3
$RAPI delete t1_kx9f2a1                                           # only your own posts/comments

$RAPI subscribe AI_Agents
$RAPI unsubscribe AI_Agents
```

`hot`/`new`/`top`/`search`/`read` all return each item's `fullname`
(`t3_...` for posts, `t1_...` for comments) — use that directly as the
argument to `comment`, `vote`, `save`, or `delete`. No need to hand-construct
fullnames.

---

## Agent Workflow

1. Run `$RAUTH --check`. If not authenticated, walk the user through One-Time
   User Setup above before attempting anything else.
2. For read-only tasks (browse, search, summarize), just call the relevant
   `reddit_api.py` command directly — no confirmation needed.
3. Before any write action (`submit`, `submit-link`, `comment`, `vote`,
   `delete`, `subscribe`), confirm the exact content and target with the user
   first — these post as their real Reddit account.
4. If a command errors with `HTTP 403` or `HTTP 401`, the token may need
   re-authentication — re-run the One-Time User Setup from Step 3 (the app
   credentials from Step 2 don't need to be re-entered).
5. Never fall back to the `browser` tool for Reddit reads/writes just because
   this skill errors — surface the error to the user instead. Browser
   automation against reddit.com is unreliable in this environment (see
   header note above); silently falling back would risk hitting the same
   anti-bot wall this skill exists to avoid.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `NOT_CONFIGURED` | `--setup` never run | Run Step 1–2 of One-Time User Setup |
| `NOT_AUTHENTICATED` | `--setup` done but no token yet | Run Step 3–5 |
| `ERROR: OAuth state mismatch` | `--auth-code` run against a stale `--auth-url` | Re-run `--auth-url`, use the fresh link |
| `HTTP 401` on API calls | Refresh token revoked (user revoked app access on reddit.com, or app deleted) | Re-run `--auth-url` / `--auth-code` |
| `HTTP 403` on `submit`/`comment` | Subreddit requires account age/karma minimums, or is quarantined/private | Check the subreddit's posting rules; not fixable from this skill |
| `HTTP 429` | Rate limited | Wait and retry; Reddit's OAuth rate limits are per-client, generous for normal use |
| `RATELIMIT` error in the JSON body of a `submit`/`comment` response | Reddit's spam-prevention cooldown between posts (not the same as HTTP 429) | Wait the duration Reddit reports and retry |

---

## Notes

- **Reddit API pricing**: Reddit introduced paid tiers for high-volume/commercial
  API use in 2023. Personal, low-volume OAuth use (an individual account
  reading/posting occasionally) has historically stayed within Reddit's free
  tier rate limits, but terms and limits can change — if you hit unexpected
  `403`/`429`s at normal usage levels, check Reddit's current API terms at
  https://www.reddit.com/wiki/api/ rather than assuming it's a bug here.
- **Rate limits**: enforced per OAuth client. Write actions (submit, comment,
  vote) are more tightly limited than reads.
- **`duration=permanent`**: the auth URL always requests this, so the refresh
  token doesn't expire on its own — only re-run setup if the user revokes
  access or deletes the app on reddit.com.
- **One Reddit account per app/token pair**: to manage a second account, run
  `--setup`/`--auth-url`/`--auth-code` again with a second app's credentials
  and store under a different `HERMES_HOME` (e.g. a separate profile).

---

## Attribution

- Reddit OAuth2 API docs: https://www.reddit.com/dev/api, https://github.com/reddit-archive/reddit/wiki/OAuth2
- Hermes adaptation: written for this fork, following the same setup pattern as `google-workspace/scripts/setup.py` and the safety conventions of the bundled `xurl` skill.
