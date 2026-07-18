#!/usr/bin/env python3
"""
webapp.py — transport-agnostic request handling for the Foosball Tracker.

`handle(method, path, query, cookies, body, store)` does ALL routing,
validation, cookie/auth work, and rendering, returning
`(status, headers, body_bytes)`. Both entry points call it:
  * app.py           — local LAN ThreadingHTTPServer
  * api/index.py      — Vercel serverless handler

State that used to live on disk / in process memory is now stateless:
  * cookie signing key comes from store.get_secret() (APP_SECRET env / file)
  * the `who` cookie is an HMAC-signed JSON blob {"n": name}
  * trial mode is entirely in a signed `trial` cookie (see TRIAL SCHEMA below)
Nothing about trial mode ever touches the store / GitHub.

TRIAL COOKIE SCHEMA (HMAC-signed, base64url JSON):
    {"trial": true, "name": <str>, "sample": <bool>, "matches": [ <match rows> ]}
  - `sample` true  -> layer core.sample_matches() into the trial view
    (the 40 deterministic rows are regenerated, NOT stored in the cookie).
  - `matches`      -> only the user's own hand-recorded trial matches ride in
    the cookie, capped at TRIAL_MATCH_CAP (oldest dropped past the cap).
  Effective trial extra = (sample_matches() if sample) + matches, layered over
  the real matches from the store — same isolation contract as before.

Python 3 standard library only.
"""

import base64
import hashlib
import hmac
import http.cookies
import json
import urllib.parse
from datetime import datetime

import core
from store import get_secret

TRIAL_MATCH_CAP = 50  # bound cookie size (browsers cap a cookie near 4KB)
COOKIE_ATTRS = "Path=/; Max-Age=31536000; SameSite=Lax"


def _now_iso():
    return datetime.now().astimezone().isoformat()


# === Signed-cookie helpers ==================================================

def _sign(raw_bytes):
    return hmac.new(get_secret(), raw_bytes, hashlib.sha256).hexdigest()


def encode_signed(payload):
    """dict -> 'base64url(json).hexsig' (HMAC-SHA256 with the app secret)."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{b}.{_sign(raw)}"


def decode_signed(token):
    """Inverse of encode_signed; returns the dict, or None if tampered/invalid."""
    if not token or "." not in token:
        return None
    b, _, sig = token.rpartition(".")
    try:
        pad = "=" * (-len(b) % 4)
        raw = base64.urlsafe_b64decode(b + pad)
    except Exception:
        return None
    if not hmac.compare_digest(_sign(raw), sig):
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _parse_cookies(cookie_header):
    jar = http.cookies.SimpleCookie()
    if cookie_header:
        try:
            jar.load(cookie_header)
        except http.cookies.CookieError:
            pass
    return jar


def _get_who(jar):
    if "who" not in jar:
        return None
    payload = decode_signed(jar["who"].value)
    if isinstance(payload, dict):
        name = payload.get("n")
        return name if name else None
    return None


def _get_trial(jar):
    if "trial" not in jar:
        return None
    payload = decode_signed(jar["trial"].value)
    if isinstance(payload, dict) and payload.get("trial"):
        return payload
    return None


def _set_cookie(name, value):
    return f"{name}={value}; {COOKIE_ATTRS}"


def _clear_cookie(name):
    return f"{name}=; Path=/; Max-Age=0"


def _who_cookie(name):
    return _set_cookie("who", encode_signed({"n": name}))


def _trial_cookie(payload):
    return _set_cookie("trial", encode_signed(payload))


def _trial_extra_matches(trial):
    """Effective trial match set = sample (if enabled) + user's own matches."""
    extra = []
    if trial.get("sample"):
        extra.extend(core.sample_matches())
    extra.extend(trial.get("matches") or [])
    return extra


# === Response builders ======================================================

def _html(html_str, status=200, cookies=None):
    data = html_str.encode("utf-8")
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(data))),
    ]
    for c in (cookies or []):
        headers.append(("Set-Cookie", c))
    return status, headers, data


def _redirect(location, cookies=None):
    headers = [("Location", location), ("Content-Length", "0")]
    for c in (cookies or []):
        headers.append(("Set-Cookie", c))
    return 303, headers, b""


# === Request handling =======================================================

def handle(method, path, query, cookies, body, store):
    """
    Transport-agnostic entry point. Returns (status:int,
    headers:list[(k,v)], body:bytes). `query` is the raw query string,
    `cookies` the raw Cookie header (or None), `body` the raw request bytes.
    """
    jar = _parse_cookies(cookies)
    who = _get_who(jar)
    trial = _get_trial(jar)
    is_trial = trial is not None
    extra = _trial_extra_matches(trial) if is_trial else None

    if method == "GET":
        return _handle_get(path, query, store, who, trial, is_trial, extra)
    if method == "POST":
        form = urllib.parse.parse_qs(
            body.decode("utf-8") if body else "", keep_blank_values=True)
        return _handle_post(path, form, store, who, trial, is_trial, extra)
    return _html(core.render_not_found(who, is_trial), 405)


def _seed_maps(players):
    """(singles_map, doubles_map): {name: seed(int)} per track (normalized rows)."""
    singles = {p["name"]: p.get("seed_singles", core.DEFAULT_SEED) for p in players}
    doubles = {p["name"]: p.get("seed_doubles", core.DEFAULT_SEED) for p in players}
    return singles, doubles


def _handle_get(path, query, store, who, trial, is_trial, extra):
    # --- Pre-auth routes (available logged out) ---
    if path == "/":
        if who:
            return _html(core.render_home(who, is_trial))
        roster = core.roster_names(store.read_players(), extra)
        return _html(core.render_login(roster))

    if path == "/login":
        return _redirect("/")   # the login screen now lives at /

    if path == "/trial":
        q = urllib.parse.parse_qs(query)
        name = (q.get("name", [""])[0]).strip() or "Guest"
        payload = {"trial": True, "name": name, "sample": False, "matches": []}
        return _redirect("/", [_who_cookie(name), _trial_cookie(payload)])

    if path == "/logout":
        return _redirect("/", [_clear_cookie("who"), _clear_cookie("trial")])

    # --- Everything below is behind "enter your name" (login or trial) ---
    if not who:
        return _redirect("/")

    if path == "/scores":
        q = urllib.parse.parse_qs(query)
        board = (q.get("board", ["singles"])[0]).strip().lower()
        players = store.read_players()
        names = [p["name"] for p in players]
        matches = store.read_matches() + (extra or [])
        s_map, d_map = _seed_maps(players)
        return _html(core.render_scores(matches, names, who, board=board,
                                        trial=is_trial, seed_singles=s_map,
                                        seed_doubles=d_map))

    if path == "/matrix":
        matches = store.read_matches() + (extra or [])
        roster = core.roster_names(store.read_players(), extra)
        return _html(core.render_matrix(matches, roster, who, is_trial))

    if path == "/how":
        return _html(core.render_how(who, is_trial))

    if path == "/record":
        roster = core.roster_names(store.read_players(), extra)
        return _html(core.render_record(roster, who, trial=is_trial))

    if path == "/history":
        matches = store.read_matches() + (extra or [])
        return _html(core.render_history(matches, who, is_trial))

    if path == "/edit":
        q = urllib.parse.parse_qs(query)
        mid = (q.get("id", [""])[0]).strip()
        match = next((m for m in store.read_matches() if str(m.get("id")) == mid), None)
        if match is None:
            return _redirect("/history")   # unknown/ephemeral id -> nothing to edit
        roster = core.roster_names(store.read_players(), extra)
        fmt, values = core.match_values(match)
        return _html(core.render_edit(roster, mid, who, fmt=fmt, values=values,
                                      trial=is_trial))

    if path == "/player":
        q = urllib.parse.parse_qs(query)
        name = (q.get("name", [""])[0]).strip()
        if not name:
            return _redirect("/")
        players = store.read_players()
        names = [p["name"] for p in players]
        matches = store.read_matches() + (extra or [])
        s_map, d_map = _seed_maps(players)
        html_out, status = core.render_player(matches, names, name, who, is_trial,
                                              seed_singles=s_map, seed_doubles=d_map)
        return _html(html_out, status)

    return _html(core.render_not_found(who, is_trial), 404)


def _handle_post(path, form, store, who, trial, is_trial, extra):
    if path == "/login":
        return _post_login(form, store, trial)
    if path == "/record":
        return _post_record(form, store, who, trial, is_trial, extra)
    if path == "/edit":
        return _post_edit(form, store, who, is_trial, extra)
    if path == "/delete":
        return _post_delete(form, store)
    if path == "/trial/load-sample":
        return _post_sample(trial, load=True)
    if path == "/trial/clear-sample":
        return _post_sample(trial, load=False)
    return _html(core.render_not_found(who, is_trial), 404)


def _post_login(form, store, trial):
    name = (form.get("name", [""])[0]).strip()
    if not name:
        roster = core.roster_names(store.read_players())
        return _html(core.render_login(roster, None), 400)
    # Parse both starting ELOs independently: non-numeric -> default; clamped.
    # Ignored by the store if the (case-insensitive) name already exists.
    def _seed(field):
        try:
            v = int((form.get(field, [""])[0]).strip())
        except (TypeError, ValueError):
            v = core.DEFAULT_SEED
        return max(core.SEED_MIN, min(core.SEED_MAX, v))

    canonical = store.write_player(name, _seed("seed_singles"), _seed("seed_doubles"))
    # A real login exits any trial session (clear the trial cookie).
    return _redirect("/", [_who_cookie(canonical), _clear_cookie("trial")])


def _match_fields(form):
    """Extract (fmt, values) from a record/edit form body."""
    fmt = (form.get("format", ["1v1"])[0]).strip()
    values = {k: (form.get(k, [""])[0]).strip()
              for k in ("a1", "a2", "b1", "b2", "score_a", "score_b")}
    return fmt, values


def _validate_match(fmt, values):
    """
    Validate a match exactly like /record. Returns
    (error_or_None, team_a, team_b, score_a, score_b).
    """
    if fmt not in ("1v1", "2v2"):
        return ("Invalid format.", None, None, None, None)
    if fmt == "1v1":
        team_a, team_b = [values["a1"]], [values["b1"]]
    else:
        team_a, team_b = [values["a1"], values["a2"]], [values["b1"], values["b2"]]

    if any(not n for n in team_a + team_b):
        return ("All player slots must be filled.", None, None, None, None)
    lowered = [n.lower() for n in team_a + team_b]
    if len(set(lowered)) != len(lowered):
        return ("All players must be distinct.", None, None, None, None)

    try:
        score_a = int(values["score_a"])
        score_b = int(values["score_b"])
    except (TypeError, ValueError):
        return ("Scores must be whole numbers.", None, None, None, None)
    if score_a < 0 or score_b < 0:
        return ("Scores must be non-negative.", None, None, None, None)
    if score_a == score_b:
        return ("Ties are not allowed — one side must win.", None, None, None, None)
    return (None, team_a, team_b, score_a, score_b)


def _confirmed_fields(form):
    """Fields whose new name the user already confirmed via a 'create anyway' token."""
    return [f for f in ("a1", "a2", "b1", "b2") if form.get("confirm_" + f)]


def _fuzzy_flags(team_a, team_b, roster, form):
    """
    Soft "did you mean" guard. For each submitted name that is NOT an exact
    (case-insensitive) roster member and was NOT confirmed via confirm_<field>,
    find the single best fuzzy-similar existing name. Returns a list of
    {field, typed, suggestion}. Empty => nothing to warn about.
    """
    pairs = [("a1", team_a[0])]
    if len(team_a) > 1:
        pairs.append(("a2", team_a[1]))
    pairs.append(("b1", team_b[0]))
    if len(team_b) > 1:
        pairs.append(("b2", team_b[1]))
    lower_roster = {r.strip().lower() for r in roster}
    flags = []
    for field, name in pairs:
        if name.strip().lower() in lower_roster:
            continue  # exact existing player -> canonicalized as usual
        if form.get("confirm_" + field):
            continue  # user chose "create anyway" for this field
        suggestion = core.suggest_similar(name, roster)
        if suggestion:
            flags.append({"field": field, "typed": name, "suggestion": suggestion})
    return flags


def _post_record(form, store, who, trial, is_trial, extra):
    if not who:
        return _redirect("/")

    fmt, values = _match_fields(form)
    confirmed = _confirmed_fields(form)

    def fail(msg=None, flags=None):
        roster = core.roster_names(store.read_players(), extra)
        return _html(core.render_record(roster, who, error=msg, fmt=fmt,
                                        values=values, trial=is_trial,
                                        flags=flags, confirmed=confirmed), 400)

    error, team_a, team_b, score_a, score_b = _validate_match(fmt, values)
    if error:
        return fail(error)

    # Soft duplicate guard: re-render (preserving values) if any near-miss name.
    roster = core.roster_names(store.read_players(), extra)
    flags = _fuzzy_flags(team_a, team_b, roster, form)
    if flags:
        return fail(flags=flags)

    if is_trial:
        # Canonicalize against known players WITHOUT creating any; append to the
        # signed trial cookie. Nothing touches the store / GitHub.
        players = store.read_players()
        team_a = [core.canonical_name(players, n) for n in team_a]
        team_b = [core.canonical_name(players, n) for n in team_b]
        matches = list(trial.get("matches") or [])
        new_id = f"tu-{len(matches) + 1}"
        matches.append({
            "id": new_id,
            "timestamp_iso": _now_iso(),
            "format": fmt,
            "team_a": ";".join(team_a),
            "team_b": ";".join(team_b),
            "score_a": str(score_a),
            "score_b": str(score_b),
            "recorded_by": who,
        })
        if len(matches) > TRIAL_MATCH_CAP:
            matches = matches[-TRIAL_MATCH_CAP:]  # drop oldest to bound size
        payload = dict(trial)
        payload["matches"] = matches
        # Breakdown over the recorded format's track; re-set the trial cookie.
        rec_fmt = core.match_format(matches[-1])
        s_map, d_map = _seed_maps(players)
        track_seed = s_map if rec_fmt == "1v1" else d_map
        all_matches = store.read_matches() + _trial_extra_matches(payload)
        bd = core.match_breakdown(all_matches, [p["name"] for p in players],
                                  track_seed, new_id, fmt=rec_fmt)
        return _html(core.render_breakdown(bd, who, trial=True),
                     cookies=[_trial_cookie(payload)])

    # --- Normal: canonicalize / auto-create players, then persist. ---
    team_a = [store.write_player(n) for n in team_a]
    team_b = [store.write_player(n) for n in team_b]
    new_id = store.append_match({
        "timestamp_iso": _now_iso(),
        "format": fmt,
        "team_a": ";".join(team_a),
        "team_b": ";".join(team_b),
        "score_a": score_a,
        "score_b": score_b,
        "recorded_by": who,
    })
    # Always show the result breakdown for the recorded format's track.
    players = store.read_players()
    s_map, d_map = _seed_maps(players)
    track_seed = s_map if fmt == "1v1" else d_map
    bd = core.match_breakdown(store.read_matches(), [p["name"] for p in players],
                              track_seed, new_id, fmt=fmt)
    return _html(core.render_breakdown(bd, who, trial=False))


def _post_edit(form, store, who, is_trial, extra):
    """Edit a real stored match: validate like /record, then update_match."""
    mid = (form.get("id", [""])[0]).strip()
    fmt, values = _match_fields(form)
    confirmed = _confirmed_fields(form)

    def fail(msg=None, flags=None):
        roster = core.roster_names(store.read_players(), extra)
        return _html(core.render_edit(roster, mid, who, error=msg, fmt=fmt,
                                      values=values, trial=is_trial,
                                      flags=flags, confirmed=confirmed), 400)

    # Only real, currently-stored matches are editable.
    if not mid or not any(str(m.get("id")) == mid for m in store.read_matches()):
        return _redirect("/history")

    error, team_a, team_b, score_a, score_b = _validate_match(fmt, values)
    if error:
        return fail(error)

    # Same soft duplicate guard as /record.
    roster = core.roster_names(store.read_players(), extra)
    flags = _fuzzy_flags(team_a, team_b, roster, form)
    if flags:
        return fail(flags=flags)

    # Auto-create any new player names (same as recording).
    team_a = [store.write_player(n) for n in team_a]
    team_b = [store.write_player(n) for n in team_b]
    store.update_match(mid, {
        "format": fmt,
        "team_a": ";".join(team_a),
        "team_b": ";".join(team_b),
        "score_a": score_a,
        "score_b": score_b,
    })
    return _redirect("/history")


def _post_delete(form, store):
    """Delete a real stored match (POST-only so links/prefetch can't trigger it)."""
    mid = (form.get("id", [""])[0]).strip()
    if mid:
        store.delete_match(mid)   # no-op if id not found / ephemeral
    return _redirect("/history")


def _post_sample(trial, load):
    """
    Load (sample=true) or clear (sample=false) the sample dataset in the trial
    cookie. Both reset the user's own trial matches, matching the original
    reset-then-load / clear semantics. Requires an active trial cookie.
    """
    if trial is None:
        return _redirect("/")
    payload = dict(trial)
    payload["sample"] = bool(load)
    payload["matches"] = []
    return _redirect("/", [_trial_cookie(payload)])


# === BaseHTTPRequestHandler adapter (shared by app.py + api/index.py) =======

def serve_via_bhrh(request_handler, method, store):
    """
    Drive `handle(...)` from a BaseHTTPRequestHandler instance and write the
    response. Used by both the local server and the Vercel serverless handler.
    """
    parsed = urllib.parse.urlparse(request_handler.path)
    try:
        length = int(request_handler.headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        length = 0
    body = request_handler.rfile.read(length) if length else b""
    cookie_header = request_handler.headers.get("Cookie")

    status, headers, out = handle(
        method, parsed.path, parsed.query, cookie_header, body, store)

    request_handler.send_response(status)
    for key, value in headers:
        request_handler.send_header(key, value)
    request_handler.end_headers()
    if out:
        request_handler.wfile.write(out)
