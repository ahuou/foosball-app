# Hosting the Foosball Tracker

Two ways to run the exact same app:

| Mode | Entry point | Storage backend | State |
|------|-------------|-----------------|-------|
| **Local LAN** | `python app.py` → `:5000` | `LocalStore` — CSV under `./data/` + best-effort `git commit` | `secret.key` file (auto-created) |
| **Vercel (serverless)** | `api/index.py` | `GitHubStore` — CSVs in a private data repo via the GitHub Contents API | env vars |

The code is split so both share one core:

- `core.py` — pure logic: ELO replay (rating, peak, streaks, weeks-at-top, reigns), `compute_matrices`, `sample_matches()`, and all HTML rendering. No IO.
- `store.py` — storage abstraction. `LocalStore` (CSV + git) and `GitHubStore` (Contents API, `urllib.request` only). `get_store()` picks the backend from env; `get_secret()` resolves the cookie key.
- `webapp.py` — transport-agnostic `handle(method, path, query, cookies, body, store)` → `(status, headers, body)`. All routing/validation/cookies/trial live here.
- `app.py` — local `ThreadingHTTPServer` adapter.
- `api/index.py` — Vercel `handler(BaseHTTPRequestHandler)` adapter.

## Run locally (zero config)

```bash
cd foosballTracker
python app.py
```

Serves `http://<your-lan-ip>:5000/`. Uses `LocalStore`; creates `data/*.csv` and `secret.key` on first run. No environment variables needed. This mode is unchanged from before the serverless work.

## Deploy to Vercel with GitHub as the database

### Why a SEPARATE data repo
Match writes commit CSV files. If the data lived in the **app** repo, every recorded match would push a commit to the repo Vercel deploys from — triggering a **redeploy on every game**. So the data goes in its own private repo (`foosball-data`); the app repo stays static and is only redeployed when you change code.

### 1. Create the two repositories
- **App repo** — this code (push it to GitHub, e.g. `you/foosball-app`).
- **Data repo** — a **private** repo `you/foosball-data` containing two header-only files:

  `data/players.csv`
  ```
  name,created_at,seed_elo
  ```
  `data/matches.csv`
  ```
  id,timestamp_iso,format,team_a,team_b,score_a,score_b,recorded_by
  ```

  (The `data/` prefix is the default; override with `GITHUB_DATA_PREFIX`.)

### 2. Create a fine-grained Personal Access Token
GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token:
- **Resource owner:** your account/org.
- **Repository access:** *Only select repositories* → `foosball-data` (the data repo **only**, never the app repo).
- **Permissions:** Repository → **Contents: Read and write**. Nothing else.
- Copy the token (`github_pat_…`). Treat it as a secret — it is never logged by this app and must never be committed.

### 3. Deploy the app repo
```bash
npm i -g vercel      # if needed
cd foosballTracker
vercel               # link/deploy; follow prompts
```
`vercel.json` rewrites every path to the `api/index` function; `requirements.txt` is empty (stdlib only), which tells Vercel to use the Python runtime.

### 4. Set environment variables (Vercel → Project → Settings → Environment Variables)
| Var | Required | Example | Notes |
|-----|----------|---------|-------|
| `APP_SECRET` | **yes** | `openssl rand -hex 32` | HMAC key signing the `who` + trial cookies. Set a strong random value; keep it stable so logins/trials survive across cold starts. |
| `GITHUB_TOKEN` | **yes** | `github_pat_…` | The fine-grained PAT from step 2. |
| `GITHUB_REPO` | **yes** | `you/foosball-data` | `owner/repo` of the **data** repo. |
| `GITHUB_BRANCH` | no | `main` | Defaults to `main`. |
| `GITHUB_DATA_PREFIX` | no | `data/` | Path prefix inside the data repo. Defaults to `data/`. |

Redeploy (or `vercel --prod`) after setting env vars. Selecting the backend is automatic: **GitHubStore** is used iff both `GITHUB_TOKEN` and `GITHUB_REPO` are present; otherwise **LocalStore**.

### How GitHubStore writes
`read_players()` / `read_matches()` do `GET /repos/{repo}/contents/{path}?ref={branch}` (base64 content + `sha`; a 404 means the file is empty/absent). Writes are **read-modify-write**: re-GET the `sha`, apply the change, `PUT` with `{message, content, sha, branch}`. On a `409`/`422` sha conflict the write re-fetches the latest `sha` and retries (up to 3×), so concurrent match submissions don't clobber each other.

## Security / privacy caveat (no authentication)
There are **no passwords** — anyone who can reach the URL can pick any name and log matches (honor system). On the LAN that's fine. On a public Vercel URL, consider:
- **Vercel Access protection** (Deployment Protection / password) on the project, or
- put it behind **Tailscale** / a VPN and don't expose it publicly, or
- add a shared-password gate in front of `webapp.handle` (a single `APP_PASSWORD` check setting an authenticated cookie) if you need a light barrier.

Trial mode is always safe to expose: trial matches live only in the visitor's signed cookie and never touch the store or GitHub.
