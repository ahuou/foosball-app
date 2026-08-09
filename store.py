#!/usr/bin/env python3
"""
store.py — storage abstraction for the Foosball Tracker.

Two backends behind one interface so the same webapp runs on the LAN (local
CSV + git) and on Vercel (GitHub Contents API as the database):

    Store (base)
      read_players()               -> list[dict]  (rows of players.csv)
      write_player(name)           -> canonical display name (create if new)
      read_matches()               -> list[dict]  (rows of matches.csv)
      append_match(row)            -> new match id (row is a dict w/o 'id')
      delete_player(name)          -> True if the roster row was removed
      read_requests() / append_request(row) / update_request(...)
      set_request_status(...) / delete_request(id)   (requests.csv)
      read_replies()  / append_reply(row)            (request_replies.csv)

    LocalStore   — CSV under ./data/, best-effort `git commit` (today's LAN
                   semantics, unchanged).
    GitHubStore  — reads/writes the CSVs in a separate private data repo via
                   the GitHub Contents API (urllib.request only). Read-modify-
                   write with sha; retries on 409/422 conflict (re-fetch sha).

Factory get_store() picks GitHubStore when GITHUB_TOKEN + GITHUB_REPO are set,
else LocalStore. get_secret() resolves the cookie-signing key (APP_SECRET env,
else a local secret.key file for zero-config local dev).

Python 3 standard library only. The token is never logged.
"""

import base64
import csv
import io
import os
import secrets
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from core import (PLAYERS_HEADER, MATCHES_HEADER, REQUESTS_HEADER,
                  REPLIES_HEADER, DEFAULT_SEED)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(APP_DIR, "data")
DEFAULT_SECRET_FILE = os.path.join(APP_DIR, "secret.key")
GITHUB_API = "https://api.github.com"


# === CSV (de)serialization (shared by both backends) ========================

def parse_csv(text):
    """CSV text -> list of row dicts (keyed by header). Empty/None -> []."""
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def rows_to_csv(header, rows):
    """List of row dicts -> CSV text with `header` columns (extras ignored)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def _now_iso():
    return datetime.now().astimezone().isoformat()


def _seed_int(value):
    """Parse a seed cell -> int. Missing/blank/invalid -> default."""
    try:
        s = str(value).strip()
        return int(s) if s else DEFAULT_SEED
    except (TypeError, ValueError):
        return DEFAULT_SEED


def _normalize_players(rows):
    """
    Keep named rows; expose seed_singles + seed_doubles as ints. Tolerates all
    three historical shapes (DictReader keys depend on the file's header):
      * 2-col  name,created_at              -> both seeds = default
      * 3-col  name,created_at,seed_elo     -> legacy single seed for BOTH tracks
      * 4-col  name,created_at,seed_singles,seed_doubles -> read both
    """
    out = []
    for r in rows:
        if not r.get("name"):
            continue
        has_split = ("seed_singles" in r) or ("seed_doubles" in r)
        if has_split:
            s_singles = _seed_int(r.get("seed_singles"))
            s_doubles = _seed_int(r.get("seed_doubles"))
        elif "seed_elo" in r:
            legacy = _seed_int(r.get("seed_elo"))   # legacy single seed -> both
            s_singles = s_doubles = legacy
        else:
            s_singles = s_doubles = DEFAULT_SEED
        r["seed_singles"] = s_singles
        r["seed_doubles"] = s_doubles
        out.append(r)
    return out


# === Secret resolution ======================================================

_SECRET_CACHE = None


def get_secret():
    """
    Cookie-signing key as bytes:
      1. APP_SECRET env var (required for serverless / GitHub mode), else
      2. a local secret.key file (read, or created on first run) for zero-config
         local dev, else
      3. an ephemeral random key (last resort; cookies won't survive a restart).
    """
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    env = os.environ.get("APP_SECRET")
    if env:
        _SECRET_CACHE = env.encode("utf-8")
        return _SECRET_CACHE
    path = os.environ.get("APP_SECRET_FILE") or DEFAULT_SECRET_FILE
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _SECRET_CACHE = f.read().strip().encode("utf-8")
        else:
            val = secrets.token_hex(32)
            with open(path, "w", encoding="utf-8") as f:
                f.write(val)
            _SECRET_CACHE = val.encode("utf-8")
    except OSError:
        # Read-only FS (e.g. serverless without APP_SECRET): ephemeral key.
        _SECRET_CACHE = secrets.token_hex(32).encode("utf-8")
    return _SECRET_CACHE


# === Base interface =========================================================

class Store:
    def read_players(self):
        raise NotImplementedError

    def write_player(self, name):
        raise NotImplementedError

    def read_matches(self):
        raise NotImplementedError

    def append_match(self, row):
        raise NotImplementedError

    def delete_match(self, match_id):
        raise NotImplementedError

    def update_match(self, match_id, fields, editor=None):
        raise NotImplementedError

    def delete_player(self, name):
        raise NotImplementedError

    def read_requests(self):
        raise NotImplementedError

    def append_request(self, row):
        raise NotImplementedError

    def update_request(self, request_id, title, content):
        raise NotImplementedError

    def set_request_status(self, request_id, status, by):
        raise NotImplementedError

    def delete_request(self, request_id):
        raise NotImplementedError

    def read_replies(self):
        raise NotImplementedError

    def append_reply(self, row):
        raise NotImplementedError


def _new_row_id(existing_rows):
    """Next free 'id' for any id-keyed CSV (matches, requests, replies)."""
    used = {r.get("id") for r in existing_rows}
    new_id = str(len(existing_rows) + 1)
    while new_id in used:
        new_id = str(int(new_id) + 1)
    return new_id


def _normalize_matches(rows):
    """Keep rows with an id; ensure the audit columns exist (old 8-col rows ->
    edited_by/edited_at default to '')."""
    out = []
    for r in rows:
        if not r.get("id"):
            continue
        r["edited_by"] = r.get("edited_by") or ""
        r["edited_at"] = r.get("edited_at") or ""
        out.append(r)
    return out


def _apply_match_update(rows, match_id, fields, editor=None):
    """
    In-place: replace the mutable fields of the row whose id == match_id, and
    stamp the audit trail (edited_by = editor, edited_at = now). Keeps the
    original id, timestamp_iso, and recorded_by. Returns True if the id existed.
    """
    match_id = str(match_id)
    for r in rows:
        if str(r.get("id")) == match_id:
            r["format"] = fields["format"]
            r["team_a"] = fields["team_a"]
            r["team_b"] = fields["team_b"]
            r["score_a"] = str(int(fields["score_a"]))
            r["score_b"] = str(int(fields["score_b"]))
            r["edited_by"] = editor or ""
            r["edited_at"] = _now_iso()
            return True
    return False


# --- Requests board (requests.csv + request_replies.csv) --------------------

def _normalize_requests(rows):
    """Keep rows with an id; fill missing columns with '' and default a blank
    status to 'unseen' (the state every request is created in)."""
    out = []
    for r in rows:
        if not r.get("id"):
            continue
        for col in REQUESTS_HEADER:
            r[col] = r.get(col) or ""
        r["status"] = r["status"].strip() or "unseen"
        out.append(r)
    return out


def _normalize_replies(rows):
    """Keep rows with an id; fill missing columns with ''."""
    out = []
    for r in rows:
        if not r.get("id"):
            continue
        for col in REPLIES_HEADER:
            r[col] = r.get(col) or ""
        out.append(r)
    return out


def _build_request(rows, row):
    """(new_id, full request row) for an append. Always born 'unseen'."""
    new_id = _new_row_id(rows)
    return new_id, {
        "id": new_id,
        "timestamp_iso": row.get("timestamp_iso") or _now_iso(),
        "author": row.get("author") or "",
        "title": row.get("title") or "",
        "content": row.get("content") or "",
        "status": "unseen",
        "status_by": "", "status_at": "", "edited_at": "",
    }


def _build_reply(rows, row):
    """(new_id, full reply row) for an append. Replies are write-once."""
    new_id = _new_row_id(rows)
    return new_id, {
        "id": new_id,
        "request_id": str(row.get("request_id") or ""),
        "timestamp_iso": row.get("timestamp_iso") or _now_iso(),
        "author": row.get("author") or "",
        "content": row.get("content") or "",
    }


def _apply_request_update(rows, request_id, title, content):
    """In-place title/content edit + edited_at stamp. Author, timestamp and
    status are untouched. Returns True if the id existed."""
    request_id = str(request_id)
    for r in rows:
        if str(r.get("id")) == request_id:
            r["title"] = title
            r["content"] = content
            r["edited_at"] = _now_iso()
            return True
    return False


def _apply_request_status(rows, request_id, status, by):
    """In-place status change (anyone logged in may do this); stamps who/when.
    Returns True if the id existed."""
    request_id = str(request_id)
    for r in rows:
        if str(r.get("id")) == request_id:
            r["status"] = status
            r["status_by"] = by or ""
            r["status_at"] = _now_iso()
            return True
    return False


# === LocalStore (CSV + git, unchanged LAN semantics) ========================

class LocalStore(Store):
    def __init__(self, data_dir=None, app_dir=None):
        self.app_dir = app_dir or APP_DIR
        self.data_dir = data_dir or os.path.join(self.app_dir, "data")
        self.players_csv = os.path.join(self.data_dir, "players.csv")
        self.matches_csv = os.path.join(self.data_dir, "matches.csv")
        self.requests_csv = os.path.join(self.data_dir, "requests.csv")
        self.replies_csv = os.path.join(self.data_dir, "request_replies.csv")
        self._lock = threading.Lock()
        self.ensure_storage()

    def ensure_storage(self):
        os.makedirs(self.data_dir, exist_ok=True)
        for path, header in ((self.players_csv, PLAYERS_HEADER),
                             (self.matches_csv, MATCHES_HEADER),
                             (self.requests_csv, REQUESTS_HEADER),
                             (self.replies_csv, REPLIES_HEADER)):
            if not os.path.exists(path):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(header)

    def read_players(self):
        if not os.path.exists(self.players_csv):
            return []
        with open(self.players_csv, "r", newline="", encoding="utf-8") as f:
            return _normalize_players(list(csv.DictReader(f)))

    def read_matches(self):
        if not os.path.exists(self.matches_csv):
            return []
        with open(self.matches_csv, "r", newline="", encoding="utf-8") as f:
            return _normalize_matches(list(csv.DictReader(f)))

    def write_player(self, name, seed_singles=DEFAULT_SEED, seed_doubles=DEFAULT_SEED):
        name = name.strip()
        if not name:
            raise ValueError("empty player name")
        with self._lock:
            rows = self.read_players()  # normalized (seeds as ints)
            key = name.lower()
            for p in rows:
                if (p.get("name") or "").strip().lower() == key:
                    return p["name"]  # exists; seeds unchanged, canonical form
            rows.append({
                "name": name, "created_at": _now_iso(),
                "seed_singles": int(seed_singles), "seed_doubles": int(seed_doubles),
            })
            # Rewrite with the canonical 4-column header so legacy (2/3-column)
            # files are transparently upgraded and stay consistent.
            with open(self.players_csv, "w", newline="", encoding="utf-8") as f:
                f.write(rows_to_csv(PLAYERS_HEADER, rows))
        self._git_commit(f"add player {name}")
        return name

    def append_match(self, row):
        with self._lock:
            rows = self.read_matches()   # normalized (audit cols present)
            new_id = _new_row_id(rows)
            rows.append({
                "id": new_id,
                "timestamp_iso": row.get("timestamp_iso") or _now_iso(),
                "format": row["format"],
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "score_a": str(int(row["score_a"])),
                "score_b": str(int(row["score_b"])),
                "recorded_by": row["recorded_by"],
                "edited_by": "", "edited_at": "",
            })
            # Rewrite with the canonical 10-column header so a legacy 8-column
            # file upgrades on first write (rows stay aligned).
            self._rewrite_matches(rows)
        self._git_commit(f"match {new_id}")
        return new_id

    def _rewrite_matches(self, rows):
        with open(self.matches_csv, "w", newline="", encoding="utf-8") as f:
            f.write(rows_to_csv(MATCHES_HEADER, rows))

    def delete_match(self, match_id):
        match_id = str(match_id)
        with self._lock:
            rows = self.read_matches()
            kept = [r for r in rows if str(r.get("id")) != match_id]
            if len(kept) == len(rows):
                return False  # id not found
            self._rewrite_matches(kept)
        self._git_commit(f"delete match {match_id}")
        return True

    def update_match(self, match_id, fields, editor=None):
        match_id = str(match_id)
        with self._lock:
            rows = self.read_matches()
            found = _apply_match_update(rows, match_id, fields, editor)
            if not found:
                return False
            self._rewrite_matches(rows)
        self._git_commit(f"edit match {match_id}")
        return True

    def delete_player(self, name):
        """
        Remove ONLY the roster row (case-insensitive). Matches are deliberately
        left alone — a removed player with games still shows up in the boards
        via replay. Returns True if a row was removed.
        """
        key = (name or "").strip().lower()
        if not key:
            return False
        with self._lock:
            rows = self.read_players()
            gone = [r for r in rows if (r.get("name") or "").strip().lower() == key]
            if not gone:
                return False
            kept = [r for r in rows if (r.get("name") or "").strip().lower() != key]
            with open(self.players_csv, "w", newline="", encoding="utf-8") as f:
                f.write(rows_to_csv(PLAYERS_HEADER, kept))
        self._git_commit(f"remove player {gone[0]['name']}")
        return True

    # -- requests board ----------------------------------------------------
    def read_requests(self):
        if not os.path.exists(self.requests_csv):
            return []
        with open(self.requests_csv, "r", newline="", encoding="utf-8") as f:
            return _normalize_requests(list(csv.DictReader(f)))

    def read_replies(self):
        if not os.path.exists(self.replies_csv):
            return []
        with open(self.replies_csv, "r", newline="", encoding="utf-8") as f:
            return _normalize_replies(list(csv.DictReader(f)))

    def _rewrite_requests(self, rows):
        with open(self.requests_csv, "w", newline="", encoding="utf-8") as f:
            f.write(rows_to_csv(REQUESTS_HEADER, rows))

    def _rewrite_replies(self, rows):
        with open(self.replies_csv, "w", newline="", encoding="utf-8") as f:
            f.write(rows_to_csv(REPLIES_HEADER, rows))

    def append_request(self, row):
        with self._lock:
            rows = self.read_requests()
            new_id, full = _build_request(rows, row)
            self._rewrite_requests(rows + [full])
        self._git_commit(f"request {new_id}")
        return new_id

    def update_request(self, request_id, title, content):
        request_id = str(request_id)
        with self._lock:
            rows = self.read_requests()
            if not _apply_request_update(rows, request_id, title, content):
                return False
            self._rewrite_requests(rows)
        self._git_commit(f"edit request {request_id}")
        return True

    def set_request_status(self, request_id, status, by):
        request_id = str(request_id)
        with self._lock:
            rows = self.read_requests()
            if not _apply_request_status(rows, request_id, status, by):
                return False
            self._rewrite_requests(rows)
        self._git_commit(f"request {request_id} -> {status}")
        return True

    def delete_request(self, request_id):
        """Remove the request AND every reply attached to it."""
        request_id = str(request_id)
        with self._lock:
            rows = self.read_requests()
            kept = [r for r in rows if str(r.get("id")) != request_id]
            if len(kept) == len(rows):
                return False  # id not found
            self._rewrite_requests(kept)
            replies = self.read_replies()
            kept_replies = [r for r in replies
                            if str(r.get("request_id")) != request_id]
            if len(kept_replies) != len(replies):
                self._rewrite_replies(kept_replies)
        self._git_commit(f"delete request {request_id}")
        return True

    def append_reply(self, row):
        with self._lock:
            rows = self.read_replies()
            new_id, full = _build_reply(rows, row)
            self._rewrite_replies(rows + [full])
        self._git_commit(f"reply {new_id}")
        return new_id

    def _git_commit(self, message):
        """Best-effort commit of data/. Any failure is swallowed."""
        try:
            subprocess.run(["git", "-C", self.app_dir, "add", "data/"],
                           check=False, capture_output=True, timeout=10)
            subprocess.run(["git", "-C", self.app_dir, "commit", "-m", message],
                           check=False, capture_output=True, timeout=10)
        except Exception:
            pass


# === GitHubStore (Contents API as the database) =============================

class _Conflict(Exception):
    """Raised on a PUT sha conflict so the caller re-fetches and retries."""


class GitHubStore(Store):
    def __init__(self, token, repo, branch="main", data_prefix="data/"):
        self.token = token
        self.repo = repo
        self.branch = branch or "main"
        prefix = (data_prefix or "data/").strip("/")
        self.prefix = (prefix + "/") if prefix else ""
        self.players_path = self.prefix + "players.csv"
        self.matches_path = self.prefix + "matches.csv"
        self.requests_path = self.prefix + "requests.csv"
        self.replies_path = self.prefix + "request_replies.csv"

    # -- low-level HTTP (token sent as Bearer; never logged) ---------------
    def _api(self, method, path, params=None, json_body=None):
        import json as _json
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = _json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "foosball-tracker")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (_json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8")
                payload = _json.loads(raw) if raw else None
            except Exception:
                payload = None
            return e.code, payload

    def _get_file(self, path):
        """Return (text, sha) or (None, None) if the file does not exist."""
        status, payload = self._api("GET", path, params={"ref": self.branch})
        if status == 200 and payload:
            content = (payload.get("content") or "").replace("\n", "")
            text = base64.b64decode(content).decode("utf-8") if content else ""
            return text, payload.get("sha")
        if status == 404:
            return None, None
        raise RuntimeError(f"GitHub GET {path} failed with status {status}")

    def _put_file(self, path, text, sha, message):
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        status, _payload = self._api("PUT", path, json_body=body)
        if status in (200, 201):
            return
        if status in (409, 422):
            raise _Conflict()
        raise RuntimeError(f"GitHub PUT {path} failed with status {status}")

    def _modify_file(self, path, header, mutate):
        """
        Read-modify-write with retry. `mutate(rows)` returns
        (new_rows_or_None, result, message); None new_rows means "no write".
        Retries up to 3x on a sha conflict, re-fetching the latest sha.
        """
        last_exc = None
        for _ in range(3):
            text, sha = self._get_file(path)
            rows = parse_csv(text) if text is not None else []
            new_rows, result, message = mutate(rows)
            if new_rows is None:
                return result
            body = rows_to_csv(header, new_rows)
            try:
                self._put_file(path, body, sha, message)
                return result
            except _Conflict as e:
                last_exc = e
                continue
        raise last_exc or RuntimeError("GitHub write failed after retries")

    # -- Store interface ---------------------------------------------------
    def read_players(self):
        text, _sha = self._get_file(self.players_path)
        return _normalize_players(parse_csv(text))

    def read_matches(self):
        text, _sha = self._get_file(self.matches_path)
        return _normalize_matches(parse_csv(text))

    def write_player(self, name, seed_singles=DEFAULT_SEED, seed_doubles=DEFAULT_SEED):
        name = name.strip()
        if not name:
            raise ValueError("empty player name")

        def mutate(rows):
            # Normalize existing rows first so legacy (2/3-column) seeds are
            # carried into the canonical 4-column rewrite instead of being lost.
            rows = _normalize_players(rows)
            key = name.lower()
            for r in rows:
                if (r.get("name") or "").strip().lower() == key:
                    return (None, r["name"], None)  # exists; seeds unchanged
            new_rows = rows + [{
                "name": name,
                "created_at": _now_iso(),
                "seed_singles": int(seed_singles),
                "seed_doubles": int(seed_doubles),
            }]
            return (new_rows, name, f"add player {name}")

        return self._modify_file(self.players_path, PLAYERS_HEADER, mutate)

    def append_match(self, row):
        def mutate(rows):
            # Normalize existing rows so a legacy 8-column blob upgrades cleanly.
            rows = _normalize_matches(rows)
            new_id = _new_row_id(rows)
            full = {
                "id": new_id,
                "timestamp_iso": row.get("timestamp_iso") or _now_iso(),
                "format": row["format"],
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "score_a": str(int(row["score_a"])),
                "score_b": str(int(row["score_b"])),
                "recorded_by": row["recorded_by"],
                "edited_by": "", "edited_at": "",
            }
            return (rows + [full], new_id, f"match {new_id}")

        return self._modify_file(self.matches_path, MATCHES_HEADER, mutate)

    def delete_match(self, match_id):
        match_id = str(match_id)

        def mutate(rows):
            kept = [r for r in rows if str(r.get("id")) != match_id]
            if len(kept) == len(rows):
                return (None, False, None)  # id not found; no write
            return (kept, True, f"delete match {match_id}")

        return self._modify_file(self.matches_path, MATCHES_HEADER, mutate)

    def update_match(self, match_id, fields, editor=None):
        match_id = str(match_id)

        def mutate(rows):
            # Normalize first so untargeted legacy rows gain the audit columns
            # and the rewrite lands the full 10-column header.
            rows = _normalize_matches(rows)
            if not _apply_match_update(rows, match_id, fields, editor):
                return (None, False, None)  # id not found; no write
            return (rows, True, f"edit match {match_id}")

        return self._modify_file(self.matches_path, MATCHES_HEADER, mutate)

    def delete_player(self, name):
        """Remove ONLY the roster row (case-insensitive); matches untouched."""
        key = (name or "").strip().lower()
        if not key:
            return False

        def mutate(rows):
            # Normalize first so untargeted legacy rows keep their seeds in the
            # canonical 4-column rewrite.
            rows = _normalize_players(rows)
            gone = [r for r in rows if (r.get("name") or "").strip().lower() == key]
            if not gone:
                return (None, False, None)  # not on the roster; no write
            kept = [r for r in rows if (r.get("name") or "").strip().lower() != key]
            return (kept, True, f"remove player {gone[0]['name']}")

        return self._modify_file(self.players_path, PLAYERS_HEADER, mutate)

    # -- requests board ----------------------------------------------------
    def read_requests(self):
        text, _sha = self._get_file(self.requests_path)
        return _normalize_requests(parse_csv(text))

    def read_replies(self):
        text, _sha = self._get_file(self.replies_path)
        return _normalize_replies(parse_csv(text))

    def append_request(self, row):
        def mutate(rows):
            rows = _normalize_requests(rows)
            new_id, full = _build_request(rows, row)
            return (rows + [full], new_id, f"request {new_id}")

        return self._modify_file(self.requests_path, REQUESTS_HEADER, mutate)

    def update_request(self, request_id, title, content):
        request_id = str(request_id)

        def mutate(rows):
            rows = _normalize_requests(rows)
            if not _apply_request_update(rows, request_id, title, content):
                return (None, False, None)  # id not found; no write
            return (rows, True, f"edit request {request_id}")

        return self._modify_file(self.requests_path, REQUESTS_HEADER, mutate)

    def set_request_status(self, request_id, status, by):
        request_id = str(request_id)

        def mutate(rows):
            rows = _normalize_requests(rows)
            if not _apply_request_status(rows, request_id, status, by):
                return (None, False, None)  # id not found; no write
            return (rows, True, f"request {request_id} -> {status}")

        return self._modify_file(self.requests_path, REQUESTS_HEADER, mutate)

    def delete_request(self, request_id):
        """Two sequential writes: the request first, then its replies (each
        skipped when nothing matched)."""
        request_id = str(request_id)

        def mutate(rows):
            kept = [r for r in rows if str(r.get("id")) != request_id]
            if len(kept) == len(rows):
                return (None, False, None)  # id not found; no write
            return (kept, True, f"delete request {request_id}")

        removed = self._modify_file(self.requests_path, REQUESTS_HEADER, mutate)

        def mutate_replies(rows):
            kept = [r for r in rows if str(r.get("request_id")) != request_id]
            if len(kept) == len(rows):
                return (None, False, None)  # no replies to drop; no write
            return (kept, True, f"delete replies of request {request_id}")

        self._modify_file(self.replies_path, REPLIES_HEADER, mutate_replies)
        return removed

    def append_reply(self, row):
        def mutate(rows):
            rows = _normalize_replies(rows)
            new_id, full = _build_reply(rows, row)
            return (rows + [full], new_id, f"reply {new_id}")

        return self._modify_file(self.replies_path, REPLIES_HEADER, mutate)


# === Factory ================================================================

def get_store():
    """GitHubStore if GITHUB_TOKEN + GITHUB_REPO are set, else LocalStore."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if token and repo:
        return GitHubStore(
            token, repo,
            branch=os.environ.get("GITHUB_BRANCH", "main"),
            data_prefix=os.environ.get("GITHUB_DATA_PREFIX", "data/"),
        )
    return LocalStore()
