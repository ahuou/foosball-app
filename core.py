#!/usr/bin/env python3
"""
core.py — pure logic + HTML rendering for the Foosball Tracker.

NO transport, storage, or IO lives here. Every function takes data in
(match lists, player rows, request-derived values) and returns values or
HTML strings. This module is shared verbatim by both the local LAN server
(app.py) and the Vercel serverless entry (api/index.py), via webapp.py.

Contents:
    * Constants (ELO params, CSV headers, sample-data config)
    * Pure replay engine: compute_stats (ELO, peak, streaks, weeks-at-top,
      reign details) and compute_matrices (synergy + head-to-head)
    * Deterministic sample_matches()
    * Roster / canonicalization helpers
    * HTML rendering helpers (base_page + all page renderers)

Python 3 standard library only.
"""

import html
import json
import random
import urllib.parse
from datetime import datetime, timedelta

# === Constants ==============================================================

PLAYERS_HEADER = ["name", "created_at", "seed_singles", "seed_doubles"]
MATCHES_HEADER = [
    "id", "timestamp_iso", "format",
    "team_a", "team_b", "score_a", "score_b", "recorded_by",
]

START_RATING = 1000.0    # default seed for unseeded players
MIN_GAMES_FOR_TOP = 3   # must have played >= this many games to qualify as #1
MIN_MATRIX_GAMES = 1    # min shared games before a matrix cell is shown (else faded "—")

# New players may register with a chosen starting rating (seed_elo). A missing
# or blank seed reads as DEFAULT_SEED; registration input is clamped to bounds.
DEFAULT_SEED = 1000
SEED_MIN = 100
SEED_MAX = 3000

# Deterministic sample dataset (fixed seed + hardcoded base date, never now()).
SAMPLE_BASE_DATE = datetime(2026, 7, 1, 18, 0, 0)
SAMPLE_WEEK_SPAN = 8
SAMPLE_MATCH_COUNT = 40
SAMPLE_PLAYERS = [
    ("Magnus", 1360), ("Kasparov", 1330), ("Neo", 1300), ("Zizou", 1285),
    ("Ada", 1255), ("Pelé", 1245), ("Grace", 1225), ("Ronaldinho", 1210),
    ("Trinity", 1200), ("Mbappé", 1190), ("Turing", 1180), ("Morpheus", 1150),
    ("Linus", 1105), ("Dua", 1055),
]


# === Small pure helpers =====================================================

def _parse_team(raw):
    """'Alice;Bob' -> ['Alice', 'Bob']  (drops empties)."""
    return [p for p in (raw or "").split(";") if p.strip()]


def match_format(m):
    """Normalized format of a match: '1v1' or '2v2' (inferred from team sizes
    if the stored 'format' field is missing/odd)."""
    f = (m.get("format") or "").strip()
    if f in ("1v1", "2v2"):
        return f
    a, b = _parse_team(m.get("team_a")), _parse_team(m.get("team_b"))
    if len(a) == 1 and len(b) == 1:
        return "1v1"
    if len(a) == 2 and len(b) == 2:
        return "2v2"
    return f


def _iso_week_key(ts_iso):
    """ISO timestamp string -> (iso_year, iso_week) tuple, or None on parse fail."""
    try:
        dt = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return None
    y, w, _ = dt.date().isocalendar()
    return (y, w)


def _is_trial_id(mid):
    """Trial matches carry non-numeric ids ('t-…' sample, 'tu-…' user)."""
    return str(mid or "").startswith("t")


def canonical_name(players_rows, name):
    """
    Canonical stored display form for `name` if that player already exists
    (case-insensitive), else the trimmed input unchanged. Does NOT create.
    """
    name = name.strip()
    key = name.lower()
    for p in players_rows:
        if (p.get("name") or "").strip().lower() == key:
            return p["name"]
    return name


def roster_names(players_rows, extra_matches=None):
    """
    Ordered list of canonical player display names for autocomplete: the stored
    roster, plus (in a trial session) any players appearing only in the trial
    user's matches. De-duplicated case-insensitively, first-seen casing kept.
    """
    seen = set()
    names = []
    for p in players_rows:
        n = p.get("name")
        key = (n or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            names.append(n)
    for m in (extra_matches or []):
        for n in _parse_team(m.get("team_a")) + _parse_team(m.get("team_b")):
            key = n.strip().lower()
            if key and key not in seen:
                seen.add(key)
                names.append(n)
    return names


# === Sample data (deterministic) ============================================

def sample_matches():
    """
    Deterministic list of ~40 fake matches (mix of 1v1/2v2) across ~8 ISO weeks
    ending at SAMPLE_BASE_DATE. Same dict shape the replay engine consumes.
    Ids are 't-<n>' so they never collide with the numeric ids in matches.csv.
    """
    rng = random.Random(20260701)  # fixed seed -> deterministic
    names = [p[0] for p in SAMPLE_PLAYERS]
    skill = dict(SAMPLE_PLAYERS)
    span_days = SAMPLE_WEEK_SPAN * 7 - 1  # ~55 days
    matches = []
    n = SAMPLE_MATCH_COUNT
    for i in range(n):
        days_ago = round((n - 1 - i) * span_days / (n - 1))
        ts = SAMPLE_BASE_DATE - timedelta(days=days_ago, minutes=(n - i))

        fmt = "2v2" if rng.random() < 0.5 else "1v1"
        if fmt == "1v1":
            a, b = rng.sample(names, 2)
            team_a, team_b = [a], [b]
        else:
            four = rng.sample(names, 4)
            team_a, team_b = four[:2], four[2:]

        sa = sum(skill[x] for x in team_a) / len(team_a)
        sb = sum(skill[x] for x in team_b) / len(team_b)
        p_a = 1.0 / (1.0 + 10 ** ((sb - sa) / 400.0))
        a_wins = rng.random() < p_a
        loser_score = rng.randint(3, 9)
        score_a, score_b = (10, loser_score) if a_wins else (loser_score, 10)

        matches.append({
            "id": f"t-{i + 1}",
            "timestamp_iso": ts.isoformat(),
            "format": fmt,
            "team_a": ";".join(team_a),
            "team_b": ";".join(team_b),
            "score_a": str(score_a),
            "score_b": str(score_b),
            "recorded_by": "SampleBot",
        })
    return matches


# === Rating engine (gap-bucketed asymmetric point transfer) =================
#
# Not zero-sum. Each SIDE gets an X from its ROLE (favourite/underdog) based on
# the rating gap; the winning side ADDS its role-X to every member, the losing
# side SUBTRACTS its role-X from every member. Margin of victory is ignored.

def bucket_x(gap):
    """Rating gap -> (favourite_X, underdog_X). Boundaries are inclusive-upper."""
    g = abs(gap)
    if g <= 50:
        return 20, 20
    if g <= 150:
        return 15, 25
    if g <= 300:
        return 10, 30
    return 5, 35


def bucket_label(gap):
    """Human-readable bucket/category for a rating gap."""
    g = abs(gap)
    if g <= 50:
        return "≤ 50 (even)"
    if g <= 150:
        return "51–150"
    if g <= 300:
        return "151–300"
    return "> 300"


def rating_delta(rating_a, rating_b, a_won):
    """
    Given the two SIDE ratings and whether side A won, return
    (a_delta, b_delta, meta) where each delta is what EVERY player on that side
    moves by. A tie in ratings makes BOTH sides favourite.
    """
    gap = abs(rating_a - rating_b)
    fav_x, under_x = bucket_x(gap)
    a_is_fav = rating_a >= rating_b      # equal -> both favourite
    b_is_fav = rating_b >= rating_a
    a_x = fav_x if a_is_fav else under_x
    b_x = fav_x if b_is_fav else under_x
    a_delta = a_x if a_won else -a_x
    b_delta = b_x if (not a_won) else -b_x
    meta = {
        "gap": gap, "fav_x": fav_x, "under_x": under_x,
        "a_role": "favourite" if a_is_fav else "underdog",
        "b_role": "favourite" if b_is_fav else "underdog",
        "a_x": a_x, "b_x": b_x,
    }
    return a_delta, b_delta, meta


# === ELO engine (pure replay) ===============================================

def _reign_stats(timeline):
    """
    Given the chronologically ordered list of weekly champions (one entry per
    ISO week that HAD a qualified champion; weeks with no champion are simply
    absent — "consecutive" is defined over successive entries in THIS list, not
    over calendar weeks), return two dicts:
        longest[name] = longest maximal run of consecutive entries == name
        current[name] = trailing run length if `name` owns the most-recent
                        entry, else absent (treated as 0)
    """
    longest = {}
    current = {}
    if not timeline:
        return longest, current
    run_name, run_len = None, 0
    for champ in timeline:
        run_len = run_len + 1 if champ == run_name else 1
        run_name = champ
        if run_len > longest.get(champ, 0):
            longest[champ] = run_len
    last = timeline[-1]
    c = 0
    for champ in reversed(timeline):
        if champ == last:
            c += 1
        else:
            break
    current[last] = c
    return longest, current


def compute_stats(matches, roster=None, seed_map=None, fmt=None):
    """
    Pure function: replay a match list (sorted by timestamp asc) from scratch to
    derive current ratings and stats for ONE rating track. `matches` must already
    contain any trial/extra matches layered in by the caller — this does no IO.

    `fmt` ('1v1' or '2v2') filters the match list to that format so Singles and
    Doubles are two independent stat worlds; None means "all matches". `roster`
    is an iterable of player names to include even with 0 games. `seed_map` is a
    {name: seed} mapping giving each player's STARTING rating and peak for THIS
    track (seed_singles for '1v1', seed_doubles for '2v2'); players absent from
    it (e.g. trial-invented names) default to START_RATING.

    Returns {"players": {name: stat}, "leaderboard": [...], "matches": [...]}.
    Handles the empty-log case gracefully.
    """
    matches = list(matches or [])
    if fmt:
        matches = [m for m in matches if match_format(m) == fmt]
    matches.sort(key=lambda m: (m.get("timestamp_iso") or "", m.get("id") or ""))

    seed_map = seed_map or {}

    def seed_of(n):
        try:
            return float(seed_map.get(n, START_RATING))
        except (TypeError, ValueError):
            return START_RATING

    ratings = {}          # name -> float
    peak = {}             # name -> float (running max rating ever held)
    wins = {}
    losses = {}
    games = {}
    streak = {}           # signed int (+wins / -losses in a row)
    week_top = {}         # (iso_year, iso_week) -> leader at last match

    # Seed every known player (roster + seed_map) so 0-game players display
    # their chosen starting rating, not a flat 1000.
    for n in set(roster or []) | set(seed_map.keys()):
        ratings.setdefault(n, seed_of(n))
        peak.setdefault(n, seed_of(n))

    def rget(n):
        return ratings.get(n, seed_of(n))

    def qualified_leader():
        best_name, best_elo = None, None
        for n, g in games.items():
            if g >= MIN_GAMES_FOR_TOP:
                e = ratings.get(n, START_RATING)
                if best_elo is None or e > best_elo:
                    best_name, best_elo = n, e
        return best_name

    for m in matches:
        team_a = _parse_team(m.get("team_a"))
        team_b = _parse_team(m.get("team_b"))
        if not team_a or not team_b:
            continue
        try:
            score_a = int(m.get("score_a"))
            score_b = int(m.get("score_b"))
        except (TypeError, ValueError):
            continue

        for n in team_a + team_b:
            ratings.setdefault(n, seed_of(n))
            peak.setdefault(n, seed_of(n))
            wins.setdefault(n, 0)
            losses.setdefault(n, 0)
            games.setdefault(n, 0)
            streak.setdefault(n, 0)

        # Side rating = single player's rating (1v1) or simple team average (2v2).
        rating_a = sum(rget(n) for n in team_a) / len(team_a)
        rating_b = sum(rget(n) for n in team_b) / len(team_b)
        a_won = score_a > score_b

        # Gap-bucketed asymmetric point transfer (not zero-sum, margin ignored).
        a_delta, b_delta, _meta = rating_delta(rating_a, rating_b, a_won)
        for n in team_a:
            ratings[n] = rget(n) + a_delta
        for n in team_b:
            ratings[n] = rget(n) + b_delta

        # Peak ELO: running max after the update.
        for n in team_a + team_b:
            if ratings[n] > peak.get(n, START_RATING):
                peak[n] = ratings[n]

        for n in team_a:
            games[n] += 1
            if a_won:
                wins[n] += 1
                streak[n] = streak[n] + 1 if streak[n] > 0 else 1
            else:
                losses[n] += 1
                streak[n] = streak[n] - 1 if streak[n] < 0 else -1
        for n in team_b:
            games[n] += 1
            if not a_won:
                wins[n] += 1
                streak[n] = streak[n] + 1 if streak[n] > 0 else 1
            else:
                losses[n] += 1
                streak[n] = streak[n] - 1 if streak[n] < 0 else -1

        wk = _iso_week_key(m.get("timestamp_iso"))
        if wk is not None:
            leader = qualified_leader()
            if leader is not None:
                week_top[wk] = leader  # overwrite -> ends as last-match leader

    weeks_at_top = {}
    for _wk, leader in week_top.items():
        weeks_at_top[leader] = weeks_at_top.get(leader, 0) + 1

    champion_timeline = [week_top[wk] for wk in sorted(week_top)]
    longest_reign, current_reign = _reign_stats(champion_timeline)

    all_players = set(roster or [])
    all_players.update(ratings.keys())

    players_out = {}
    for n in all_players:
        g = games.get(n, 0)
        w = wins.get(n, 0)
        s = streak.get(n, 0)
        if s > 0:
            streak_str = f"W{s}"
        elif s < 0:
            streak_str = f"L{-s}"
        else:
            streak_str = "-"
        players_out[n] = {
            "name": n,
            "elo": ratings.get(n, seed_of(n)),
            "peak": peak.get(n, seed_of(n)),
            "wins": w,
            "losses": losses.get(n, 0),
            "games": g,
            "win_pct": (100.0 * w / g) if g else 0.0,
            "streak": streak_str,
            "weeks_at_top": weeks_at_top.get(n, 0),
            "longest_reign": longest_reign.get(n, 0),
            "current_reign": current_reign.get(n, 0),
        }

    leaderboard = sorted(
        players_out.values(),
        key=lambda s: (-s["elo"], -s["games"], s["name"].lower()),
    )

    return {
        "players": players_out,
        "leaderboard": leaderboard,
        "matches": matches,
    }


def match_breakdown(matches, roster=None, seed_map=None, match_id=None, fmt=None):
    """
    Replay only the `fmt`-format matches and return the rating breakdown for the
    match whose id is `match_id` (typically the just-recorded one): per-player
    old->new rating, role and X applied, plus (for doubles) the two side
    averages, gap and bucket. Filtering by `fmt` (and using that track's
    seed_map) means a 1v1 record moves only Singles and a 2v2 only Doubles.
    Returns None if the id isn't found. Pure — same seeded ratings as compute_stats.
    """
    matches = list(matches or [])
    if fmt:
        matches = [m for m in matches if match_format(m) == fmt]
    matches = sorted(matches,
                     key=lambda m: (m.get("timestamp_iso") or "", m.get("id") or ""))
    seed_map = seed_map or {}

    def seed_of(n):
        try:
            return float(seed_map.get(n, START_RATING))
        except (TypeError, ValueError):
            return START_RATING

    ratings = {}
    for n in set(roster or []) | set(seed_map.keys()):
        ratings.setdefault(n, seed_of(n))

    def rget(n):
        return ratings.get(n, seed_of(n))

    for m in matches:
        team_a = _parse_team(m.get("team_a"))
        team_b = _parse_team(m.get("team_b"))
        if not team_a or not team_b:
            continue
        try:
            score_a = int(m.get("score_a"))
            score_b = int(m.get("score_b"))
        except (TypeError, ValueError):
            continue
        for n in team_a + team_b:
            ratings.setdefault(n, seed_of(n))

        rating_a = sum(rget(n) for n in team_a) / len(team_a)
        rating_b = sum(rget(n) for n in team_b) / len(team_b)
        a_won = score_a > score_b
        a_delta, b_delta, meta = rating_delta(rating_a, rating_b, a_won)

        is_target = str(m.get("id")) == str(match_id)
        breakdown = None
        if is_target:
            players = []
            for n in team_a:
                old = rget(n)
                players.append({"name": n, "side": "A", "role": meta["a_role"],
                                "x": meta["a_x"], "delta": a_delta,
                                "old": old, "new": old + a_delta})
            for n in team_b:
                old = rget(n)
                players.append({"name": n, "side": "B", "role": meta["b_role"],
                                "x": meta["b_x"], "delta": b_delta,
                                "old": old, "new": old + b_delta})
            breakdown = {
                "fmt": m.get("format", ""),
                "doubles": len(team_a) == 2 or len(team_b) == 2,
                "team_a": team_a, "team_b": team_b,
                "score_a": score_a, "score_b": score_b, "a_won": a_won,
                "rating_a": rating_a, "rating_b": rating_b,
                "gap": meta["gap"], "fav_x": meta["fav_x"],
                "under_x": meta["under_x"], "bucket": bucket_label(meta["gap"]),
                "a_role": meta["a_role"], "b_role": meta["b_role"],
                "players": players,
            }

        for n in team_a:
            ratings[n] = rget(n) + a_delta
        for n in team_b:
            ratings[n] = rget(n) + b_delta

        if is_target:
            return breakdown

    return None


def _pair_key(a, b):
    """Unordered pair key: the two names sorted case-insensitively."""
    return tuple(sorted((a, b), key=lambda x: (x.lower(), x)))


def compute_matrices(matches):
    """
    Pure aggregate over the (already trial-layered) match list for /matrix.
    Returns synergy (2v2 same-team pairs) + head-to-head (opposing) counts.
    """
    matches = list(matches or [])
    synergy = {}
    synergy_players = set()
    h2h = {}
    h2h_players = set()

    def bump(d, key, won):
        cell = d.get(key)
        if cell is None:
            cell = {"games": 0, "wins": 0}
            d[key] = cell
        cell["games"] += 1
        if won:
            cell["wins"] += 1

    for m in matches:
        team_a = _parse_team(m.get("team_a"))
        team_b = _parse_team(m.get("team_b"))
        if not team_a or not team_b:
            continue
        try:
            score_a = int(m.get("score_a"))
            score_b = int(m.get("score_b"))
        except (TypeError, ValueError):
            continue
        a_won = score_a > score_b

        for team, team_won in ((team_a, a_won), (team_b, not a_won)):
            if len(team) == 2:
                key = _pair_key(team[0], team[1])
                bump(synergy, key, team_won)
                synergy_players.update(team)

        for pa in team_a:
            for pb in team_b:
                bump(h2h, (pa, pb), a_won)
                bump(h2h, (pb, pa), not a_won)
                h2h_players.add(pa)
                h2h_players.add(pb)

    order = lambda names: sorted(names, key=lambda x: (x.lower(), x))
    return {
        "synergy": synergy,
        "synergy_players": order(synergy_players),
        "h2h": h2h,
        "h2h_players": order(h2h_players),
    }


# === HTML rendering =========================================================

def esc(x):
    return html.escape(str(x))


PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; background: #f4f5f7; color: #1a1a1a;
}
.container { max-width: 860px; margin: 0 auto; padding: 16px; }
header.topbar {
  background: #1f2937; color: #fff; padding: 14px 16px;
}
header.topbar .inner {
  max-width: 860px; margin: 0 auto;
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 8px;
}
header.topbar a { color: #93c5fd; text-decoration: none; }
header.topbar a:hover { text-decoration: underline; }
header.topbar .brand { font-weight: 700; font-size: 1.15rem; color: #fff; }
header.topbar .who { font-size: 0.9rem; color: #d1d5db; }
h1, h2 { margin: 18px 0 10px; }
table { width: 100%; border-collapse: collapse; background: #fff;
  border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f0f2f5; font-size: 0.82rem; text-transform: uppercase;
  letter-spacing: 0.03em; color: #555; }
tr:last-child td { border-bottom: none; }
td a, .container a { color: #2563eb; text-decoration: none; }
td a:hover { text-decoration: underline; }
.rank { font-weight: 700; color: #6b7280; width: 2.4em; }
.elo { font-weight: 700; }
.win { color: #16a34a; font-weight: 600; }
.loss { color: #dc2626; font-weight: 600; }
.crown { color: #d97706; }
form.card, .card {
  background: #fff; padding: 18px; border-radius: 8px; margin: 12px 0;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
label { display: block; margin: 10px 0 4px; font-weight: 600; font-size: 0.9rem; }
input[type=text], input[type=number], select {
  width: 100%; padding: 9px 10px; border: 1px solid #cbd5e1;
  border-radius: 6px; font-size: 1rem; background: #fff; color: #111;
}
.btn {
  display: inline-block; margin-top: 14px; padding: 10px 18px;
  background: #2563eb; color: #fff; border: none; border-radius: 6px;
  font-size: 1rem; cursor: pointer; text-decoration: none;
}
.btn:hover { background: #1d4ed8; }
.btn.secondary { background: #6b7280; }
.error {
  background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c;
  padding: 10px 12px; border-radius: 6px; margin: 10px 0;
}
.muted { color: #6b7280; font-size: 0.9rem; }
.row2 { display: flex; gap: 12px; }
.row2 > div { flex: 1; }
.toggle { display: flex; gap: 8px; margin: 6px 0 4px; }
.toggle label { display: flex; align-items: center; gap: 6px; margin: 0;
  font-weight: 500; cursor: pointer; }
.feed li { margin: 4px 0; }
.mctl { white-space: nowrap; }
.btn-sm {
  display: inline-block; padding: 2px 9px; margin-left: 4px; font-size: 0.78rem;
  border: 1px solid #cbd5e1; border-radius: 5px; background: #fff; color: #2563eb;
  cursor: pointer; text-decoration: none; line-height: 1.6;
}
.btn-sm:hover { background: #eef2ff; }
.btn-sm.danger { color: #dc2626; border-color: #fecaca; }
.btn-sm.danger:hover { background: #fef2f2; }
.mctl form { display: inline; margin: 0; }
@media (prefers-color-scheme: dark) {
  .btn-sm { background: #111827; border-color: #4b5563; color: #93c5fd; }
  .btn-sm:hover { background: #1e293b; }
  .btn-sm.danger { color: #fca5a5; border-color: #7f1d1d; }
  .btn-sm.danger:hover { background: #3f1d1d; }
}
.trialbar {
  background: #7c3aed; color: #fff; text-align: center; font-weight: 600;
  padding: 10px 16px; font-size: 0.92rem;
}
.tag-trial {
  display: inline-block; background: #7c3aed; color: #fff; font-size: 0.7rem;
  font-weight: 700; padding: 1px 6px; border-radius: 4px; margin-left: 4px;
  vertical-align: middle; text-transform: uppercase; letter-spacing: 0.04em;
}
.matrix-wrap { overflow-x: auto; margin: 8px 0 4px; }
table.matrix { width: auto; min-width: 100%; }
table.matrix th, table.matrix td {
  padding: 4px 6px; text-align: center; font-size: 0.8rem;
  border: 1px solid #e5e7eb; white-space: nowrap;
}
table.matrix th.corner { background: transparent; border: none; }
table.matrix th.rowhead, table.matrix th.colhead {
  position: sticky; background: #f0f2f5; font-size: 0.72rem;
}
table.matrix th.rowhead { left: 0; text-align: right; }
table.matrix td.cell { color: #fff; font-weight: 700; line-height: 1.1; }
table.matrix td.cell small { display: block; font-weight: 400; opacity: 0.85; font-size: 0.65rem; }
table.matrix td.na { background: #f8fafc; color: #cbd5e1; font-weight: 400; }
table.matrix td.diag { background: #e5e7eb; }
.rank-list { list-style: none; padding: 0; }
.rank-list li { padding: 4px 0; border-bottom: 1px solid #eee; }
.rank-list .pct { font-weight: 700; }
@media (prefers-color-scheme: dark) {
  table.matrix th, table.matrix td { border-color: #374151; }
  table.matrix th.rowhead, table.matrix th.colhead { background: #374151; }
  table.matrix td.na { background: #1a2230; color: #4b5563; }
  table.matrix td.diag { background: #374151; }
  .rank-list li { border-bottom: 1px solid #374151; }
}
@media (prefers-color-scheme: dark) {
  body { background: #111827; color: #e5e7eb; }
  table, form.card, .card { background: #1f2937; box-shadow: none; }
  th { background: #374151; color: #cbd5e1; }
  th, td { border-bottom: 1px solid #374151; }
  input[type=text], input[type=number], select {
    background: #111827; color: #e5e7eb; border-color: #4b5563; }
  .muted, .rank { color: #9ca3af; }
  .error { background: #3f1d1d; border-color: #7f1d1d; color: #fca5a5; }
}
"""


def base_page(title, body, who=None, trial=False):
    if who:
        label = "Trial as" if trial else "Playing as"
        who_html = (
            f'<span class="who">{label}: <strong>{esc(who)}</strong></span> '
            f'&nbsp;<a href="/record">Record</a> &nbsp;<a href="/logout">Logout</a>'
        )
    else:
        who_html = ('<a href="/login">Pick your name</a> '
                    '&nbsp;<a href="/trial">&#129514; Try it out</a>')
    banner = ""
    if trial:
        banner = (
            "<div class='trialbar'>&#129514; TRIAL MODE — matches you record here "
            "are private to you and are deleted when you log out.</div>"
        )
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{PAGE_CSS}</style></head><body>"
        "<header class='topbar'><div class='inner'>"
        "<span class='brand'><a href='/' style='color:#fff'>&#9917; Foosball Tracker</a>"
        " &nbsp;<a href='/history' style='font-size:0.9rem'>History</a>"
        " &nbsp;<a href='/matrix' style='font-size:0.9rem'>Matrix</a></span>"
        f"<span>{who_html}</span>"
        "</div></header>"
        f"{banner}"
        f"<div class='container'>{body}</div>"
        "</body></html>"
    )


def sample_controls_html():
    """Trial-only control bar: load / clear the bundled sample dataset."""
    return (
        "<div class='card' style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
        "<span class='muted'>Sandbox tools:</span>"
        "<form method='post' action='/trial/load-sample' style='margin:0'>"
        "<button class='btn' type='submit'>&#128202; Load sample data</button>"
        "</form>"
        "<form method='post' action='/trial/clear-sample' style='margin:0'>"
        "<button class='btn secondary' type='submit'>Clear sample data</button>"
        "</form>"
        "</div>"
    )


def datalist_html(list_id, names):
    """Build a <datalist> from a list of canonical player-name strings."""
    opts = "".join(f"<option value='{esc(n)}'></option>" for n in names)
    return f"<datalist id='{esc(list_id)}'>{opts}</datalist>"


def _js_json(obj):
    """
    Serialize `obj` to JSON safe to embed inside an inline <script> tag.
    ensure_ascii=True (default) already escapes U+2028/U+2029 and all non-ASCII;
    we additionally neutralize <, >, & so the payload can't break out of the
    script context.
    """
    s = json.dumps(obj)
    return (s.replace("<", "\\u003c")
             .replace(">", "\\u003e")
             .replace("&", "\\u0026"))


def roster_autocomplete_script(names):
    """
    Inline JS: build a {lowercased: canonical} lookup from the injected roster,
    then snap any name input (marked data-roster) to the canonical casing on
    blur / change when it case-insensitively matches. Unmatched names are left
    exactly as typed. The map is derived from `names` — nothing hardcoded.
    """
    mapping = {}
    for n in names:
        mapping.setdefault(n.strip().lower(), n)
    return (
        "<script>"
        f"(function(){{var ROSTER={_js_json(mapping)};"
        "function snap(el){var v=(el.value||'').trim();if(!v)return;"
        "var c=ROSTER[v.toLowerCase()];if(c){el.value=c;}}"
        "document.querySelectorAll('input[data-roster]').forEach(function(el){"
        "el.addEventListener('blur',function(){snap(el);});"
        "el.addEventListener('change',function(){snap(el);});});})();"
        "</script>"
    )


def _team_names(raw):
    return ", ".join(esc(n) for n in _parse_team(raw))


def _board_table(data, heading):
    """One leaderboard table for a single format. Lists ALL players (even at 0
    games); shows the empty message only when the roster is genuinely empty."""
    lb = data["leaderboard"]
    if not lb:
        return (f"<h2>{heading}</h2><div class='card muted'>No players yet — "
                "register on the login page.</div>")
    rows = []
    for i, s in enumerate(lb, start=1):
        crown = " &#128081;" if s["weeks_at_top"] > 0 and i == 1 else ""
        rows.append(
            "<tr>"
            f"<td class='rank'>{i}</td>"
            f"<td><a href='/player?name={urllib.parse.quote(s['name'])}'>{esc(s['name'])}</a>{crown}</td>"
            f"<td class='elo'>{round(s['elo'])}</td>"
            f"<td class='muted'>{round(s['peak'])}</td>"
            f"<td>{s['wins']}-{s['losses']}</td>"
            f"<td>{s['win_pct']:.0f}%</td>"
            f"<td class='crown'>{s['weeks_at_top']}</td>"
            f"<td>{esc(s['streak'])}</td>"
            f"<td>{s['games']}</td>"
            "</tr>"
        )
    return (
        f"<h2>{heading}</h2><table><thead><tr>"
        "<th>#</th><th>Player</th><th>ELO</th><th>Peak</th><th>W-L</th><th>Win%</th>"
        "<th>Weeks #1</th><th>Streak</th><th>Games</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _match_controls(m, editable):
    """
    Edit + Delete controls for a match. Rendered ONLY for real stored matches:
    trial/sample rows (ids starting with 't') are ephemeral and not in the store
    (marked "trial" instead), and controls are suppressed entirely in trial mode.
    """
    mid = str(m.get("id", ""))
    if _is_trial_id(mid):
        return "<span class='tag-trial'>trial</span>"
    if not editable:
        return ""
    return (
        "<span class='mctl'>"
        f"<a class='btn-sm' href='/edit?id={urllib.parse.quote(mid)}'>Edit</a>"
        "<form method='post' action='/delete' "
        "onsubmit=\"return confirm('Delete this match? Stats will recompute.');\">"
        f"<input type='hidden' name='id' value='{esc(mid)}'>"
        "<button class='btn-sm danger' type='submit'>Delete</button></form>"
        "</span>"
    )


def _recent_feed(matches, editable=False):
    """Combined recent-matches feed across both formats (latest ~15)."""
    ordered = sorted(matches or [],
                     key=lambda m: (m.get("timestamp_iso") or "", m.get("id") or ""))
    ordered = list(reversed(ordered))[:15]
    if not ordered:
        return "<h2>Recent matches</h2><p class='muted'>No matches recorded yet.</p>"
    items = []
    for m in ordered:
        a = _team_names(m.get("team_a"))
        b = _team_names(m.get("team_b"))
        sa, sb = m.get("score_a"), m.get("score_b")
        try:
            a_won = int(sa) > int(sb)
        except (TypeError, ValueError):
            a_won = True
        a_cls = "win" if a_won else "loss"
        b_cls = "loss" if a_won else "win"
        when = esc((m.get("timestamp_iso") or "")[:16].replace("T", " "))
        items.append(
            f"<li><span class='{a_cls}'>{a}</span> "
            f"<strong>{esc(sa)}–{esc(sb)}</strong> "
            f"<span class='{b_cls}'>{b}</span> "
            f"<span class='muted'>({esc(m.get('format',''))}, {when})</span> "
            f"{_match_controls(m, editable)}</li>"
        )
    return "<h2>Recent matches</h2><ul class='feed'>" + "".join(items) + "</ul>"


def render_index(matches, roster, who=None, trial=False,
                 seed_singles=None, seed_doubles=None):
    singles = compute_stats(matches, roster, seed_map=seed_singles, fmt="1v1")
    doubles = compute_stats(matches, roster, seed_map=seed_doubles, fmt="2v2")
    controls = sample_controls_html() if trial else ""
    body = (
        "<h1>Leaderboards</h1>" + controls +
        _board_table(singles, "Singles (1v1)") +
        _board_table(doubles, "Doubles (2v2)") +
        _recent_feed(matches, editable=not trial)
    )
    return base_page("Foosball Tracker", body, who, trial=trial)


def render_history(matches, who=None, trial=False):
    """Full match log, newest first, with Edit/Delete on real stored matches."""
    editable = not trial
    ordered = sorted(matches or [],
                     key=lambda m: (m.get("timestamp_iso") or "", m.get("id") or ""))
    ordered = list(reversed(ordered))
    if not ordered:
        body = ("<h1>Match history</h1><p class='muted'>No matches recorded yet. "
                "<a href='/record'>Record the first match!</a></p>")
        return base_page("History — Foosball Tracker", body, who, trial=trial)

    rows = []
    for m in ordered:
        a = _team_names(m.get("team_a"))
        b = _team_names(m.get("team_b"))
        sa, sb = m.get("score_a"), m.get("score_b")
        try:
            a_won = int(sa) > int(sb)
        except (TypeError, ValueError):
            a_won = True
        a_cls = "win" if a_won else "loss"
        b_cls = "loss" if a_won else "win"
        when = esc((m.get("timestamp_iso") or "")[:16].replace("T", " "))
        rows.append(
            "<tr>"
            f"<td class='muted'>{when}</td>"
            f"<td>{esc(match_format(m))}</td>"
            f"<td><span class='{a_cls}'>{a}</span> "
            f"<strong>{esc(sa)}–{esc(sb)}</strong> "
            f"<span class='{b_cls}'>{b}</span></td>"
            f"<td>{_match_controls(m, editable)}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>When</th><th>Format</th><th>Match</th>"
        "<th>Actions</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    note = ("<p class='muted'>Trial/sample rows are marked “trial” and can't be "
            "edited (they're not saved).</p>") if trial else ""
    body = "<h1>Match history</h1>" + table + note
    return base_page("History — Foosball Tracker", body, who, trial=trial)


def render_login(roster, who=None, trial=False):
    body = (
        "<h1>Pick your name</h1>"
        "<form class='card' method='post' action='/login'>"
        "<p class='muted'>No password — honor system. Type a new name or pick an existing one.</p>"
        "<label for='name'>Your name</label>"
        "<input type='text' id='name' name='name' list='players' data-roster autocomplete='off' required autofocus>"
        + datalist_html("players", roster) +
        "<p class='muted' style='margin-top:12px'>Starting ELOs — new players "
        "only; ignored if you already exist. Singles and Doubles are separate.</p>"
        "<div class='row2'>"
        "<div><label for='seed_singles'>Singles starting ELO</label>"
        f"<input type='number' id='seed_singles' name='seed_singles' value='{DEFAULT_SEED}' "
        f"min='{SEED_MIN}' max='{SEED_MAX}'></div>"
        "<div><label for='seed_doubles'>Doubles starting ELO</label>"
        f"<input type='number' id='seed_doubles' name='seed_doubles' value='{DEFAULT_SEED}' "
        f"min='{SEED_MIN}' max='{SEED_MAX}'></div>"
        "</div>"
        "<button class='btn' type='submit'>Continue</button>"
        "</form>"
        "<form class='card' method='get' action='/trial'>"
        "<h2 style='margin-top:0'>&#129514; Try it out (trial mode)</h2>"
        "<p class='muted'>Experiment in a private sandbox: you see the real "
        "leaderboard as a base and can record matches to play around. Trial "
        "matches are visible only to you, never saved to disk, and deleted "
        "when you log out.</p>"
        "<label for='trialname'>Trial name</label>"
        "<input type='text' id='trialname' name='name' value='Guest' autocomplete='off'>"
        "<button class='btn secondary' type='submit'>Enter trial mode</button>"
        "</form>"
        + roster_autocomplete_script(roster)
    )
    return base_page("Login — Foosball Tracker", body, who, trial=trial)


def _match_form(action, roster, fmt="1v1", values=None, submit_label="Save match",
                hidden=""):
    """Shared match form (record + edit): 1v1/2v2 toggle, slots, scores."""
    values = values or {}

    def val(k):
        return esc(values.get(k, ""))

    dl = datalist_html("players", roster)
    checked_1 = "checked" if fmt != "2v2" else ""
    checked_2 = "checked" if fmt == "2v2" else ""
    return (
        f"<form class='card' method='post' action='{esc(action)}'>"
        + hidden +
        "<label>Format</label>"
        "<div class='toggle'>"
        f"<label><input type='radio' name='format' value='1v1' {checked_1} onclick='setFmt(false)'> 1v1</label>"
        f"<label><input type='radio' name='format' value='2v2' {checked_2} onclick='setFmt(true)'> 2v2</label>"
        "</div>"
        "<div class='row2'>"
        "<div>"
        "<label>Team A</label>"
        f"<input type='text' name='a1' list='players' data-roster placeholder='Player 1' autocomplete='off' value='{val('a1')}' required>"
        f"<input type='text' name='a2' list='players' data-roster placeholder='Player 2' autocomplete='off' value='{val('a2')}' "
        "class='slot2' style='margin-top:8px'>"
        "</div>"
        "<div>"
        "<label>Team B</label>"
        f"<input type='text' name='b1' list='players' data-roster placeholder='Player 1' autocomplete='off' value='{val('b1')}' required>"
        f"<input type='text' name='b2' list='players' data-roster placeholder='Player 2' autocomplete='off' value='{val('b2')}' "
        "class='slot2' style='margin-top:8px'>"
        "</div>"
        "</div>"
        "<div class='row2'>"
        f"<div><label>Score A</label><input type='number' name='score_a' min='0' value='{val('score_a')}' required></div>"
        f"<div><label>Score B</label><input type='number' name='score_b' min='0' value='{val('score_b')}' required></div>"
        "</div>"
        + dl +
        f"<button class='btn' type='submit'>{esc(submit_label)}</button>"
        "</form>"
        "<script>"
        "function setFmt(is2v2){"
        "  document.querySelectorAll('.slot2').forEach(function(el){"
        "    el.style.display = is2v2 ? 'block' : 'none';"
        "    if(!is2v2){ el.value=''; }"
        "  });"
        "}"
        "setFmt(document.querySelector('input[name=format]:checked').value==='2v2');"
        "</script>"
        + roster_autocomplete_script(roster)
    )


def render_record(roster, who=None, error=None, fmt="1v1", values=None, trial=False):
    err_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    controls = sample_controls_html() if trial else ""
    body = ("<h1>Record a match</h1>" + err_html + controls
            + _match_form("/record", roster, fmt, values, "Save match"))
    return base_page("Record — Foosball Tracker", body, who, trial=trial)


def render_edit(roster, match_id, who=None, error=None, fmt="1v1", values=None,
                trial=False):
    err_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    hidden = f"<input type='hidden' name='id' value='{esc(match_id)}'>"
    body = (
        f"<h1>Edit match #{esc(match_id)}</h1>" + err_html
        + _match_form("/edit", roster, fmt, values, "Save changes", hidden=hidden)
        + "<p style='margin-top:12px'><a href='/history'>&larr; Back to history</a></p>"
    )
    return base_page("Edit match — Foosball Tracker", body, who, trial=trial)


def match_values(m):
    """Extract a record/edit form `values` dict + format from a stored match."""
    team_a = _parse_team(m.get("team_a"))
    team_b = _parse_team(m.get("team_b"))
    values = {
        "a1": team_a[0] if len(team_a) > 0 else "",
        "a2": team_a[1] if len(team_a) > 1 else "",
        "b1": team_b[0] if len(team_b) > 0 else "",
        "b2": team_b[1] if len(team_b) > 1 else "",
        "score_a": str(m.get("score_a", "")),
        "score_b": str(m.get("score_b", "")),
    }
    return match_format(m), values


def _zero_stat(name, seed):
    return {"name": name, "elo": float(seed), "peak": float(seed), "wins": 0,
            "losses": 0, "games": 0, "win_pct": 0.0, "streak": "-",
            "weeks_at_top": 0, "longest_reign": 0, "current_reign": 0}


def _player_stat_block(heading, stat):
    reign_detail = ""
    if stat["weeks_at_top"] > 0:
        reign_detail = (f" &nbsp;<span class='muted'>(longest reign "
                        f"{stat['longest_reign']}, current {stat['current_reign']})</span>")
    return (
        "<div class='card'>"
        f"<h3 style='margin-top:0'>{heading}</h3>"
        f"<p><span class='elo' style='font-size:1.6rem'>{round(stat['elo'])}</span> "
        "<span class='muted'>ELO</span> &nbsp;·&nbsp; "
        f"<span class='muted'>peak</span> <strong>{round(stat['peak'])}</strong></p>"
        f"<p><strong>{stat['wins']}-{stat['losses']}</strong> "
        f"({stat['win_pct']:.0f}% win) &nbsp;·&nbsp; "
        f"{stat['games']} games &nbsp;·&nbsp; "
        f"streak {esc(stat['streak'])}</p>"
        f"<p>Weeks at #1: <span class='crown'>{stat['weeks_at_top']}</span>"
        f"{reign_detail}</p>"
        "</div>"
    )


def render_player(matches, roster, name, who=None, trial=False,
                  seed_singles=None, seed_doubles=None):
    singles = compute_stats(matches, roster, seed_map=seed_singles, fmt="1v1")
    doubles = compute_stats(matches, roster, seed_map=seed_doubles, fmt="2v2")
    key = name.strip().lower()

    canonical = None
    for pool in (singles["players"], doubles["players"]):
        for n in pool:
            if n.strip().lower() == key:
                canonical = n
                break
        if canonical:
            break
    if canonical is None:
        body = ("<h1>Unknown player</h1>"
                f"<p class='muted'>No player named “{esc(name)}”.</p>"
                "<p><a href='/'>&larr; Back to leaderboard</a></p>")
        return base_page("Player — Foosball Tracker", body, who, trial=trial), 404

    s_seed = (seed_singles or {}).get(canonical, DEFAULT_SEED)
    d_seed = (seed_doubles or {}).get(canonical, DEFAULT_SEED)
    s_stat = singles["players"].get(canonical, _zero_stat(canonical, s_seed))
    d_stat = doubles["players"].get(canonical, _zero_stat(canonical, d_seed))

    # Combined match history (both formats), most recent first.
    all_matches = sorted(matches or [],
                         key=lambda m: (m.get("timestamp_iso") or "", m.get("id") or ""))
    hist = []
    for m in reversed(all_matches):
        team_a = _parse_team(m.get("team_a"))
        team_b = _parse_team(m.get("team_b"))
        low_a = [x.lower() for x in team_a]
        low_b = [x.lower() for x in team_b]
        if key not in low_a and key not in low_b:
            continue
        try:
            sa, sb = int(m.get("score_a")), int(m.get("score_b"))
        except (TypeError, ValueError):
            continue
        on_a = key in low_a
        my_team = team_a if on_a else team_b
        opp_team = team_b if on_a else team_a
        my_score = sa if on_a else sb
        opp_score = sb if on_a else sa
        won = my_score > opp_score
        partners = [p for p in my_team if p.lower() != key]
        partner_str = (" (+ " + ", ".join(esc(p) for p in partners) + ")") if partners else ""
        opp_str = ", ".join(esc(p) for p in opp_team)
        when = esc((m.get("timestamp_iso") or "")[:16].replace("T", " "))
        res_cls = "win" if won else "loss"
        res_txt = "W" if won else "L"
        hist.append(
            "<tr>"
            f"<td class='{res_cls}'>{res_txt}</td>"
            f"<td>{my_score}–{opp_score}</td>"
            f"<td>vs {opp_str}{partner_str}</td>"
            f"<td class='muted'>{esc(m.get('format',''))}</td>"
            f"<td class='muted'>{when}</td>"
            "</tr>"
        )

    if hist:
        hist_table = (
            "<h2>Match history</h2><table><thead><tr>"
            "<th>Result</th><th>Score</th><th>Opponents</th><th>Format</th><th>When</th>"
            "</tr></thead><tbody>" + "".join(hist) + "</tbody></table>"
        )
    else:
        hist_table = "<p class='muted'>No matches played yet.</p>"

    blocks = (
        "<div class='row2'>"
        + _player_stat_block("Singles (1v1)", s_stat)
        + _player_stat_block("Doubles (2v2)", d_stat)
        + "</div>"
    )
    body = (
        f"<h1>{esc(canonical)}</h1>" + blocks + hist_table +
        "<p style='margin-top:16px'><a href='/'>&larr; Back to leaderboard</a></p>"
    )
    return base_page(f"{canonical} — Foosball Tracker", body, who, trial=trial), 200


def _heat_color(pct):
    """Map a win% (0..100) to a red->green heat color."""
    hue = max(0.0, min(120.0, pct * 1.2))  # 0=red, 120=green
    return f"hsl({hue:.0f}, 62%, 42%)"


def _matrix_cell(cell):
    """Render one matrix <td> from a {games, wins} dict (or None)."""
    games = cell["games"] if cell else 0
    if games < MIN_MATRIX_GAMES:
        title = f"{games} game(s) — not enough data"
        return f"<td class='na' title='{esc(title)}'>&mdash;</td>"
    pct = 100.0 * cell["wins"] / games
    losses = games - cell["wins"]
    title = f"{cell['wins']}-{losses} in {games} games ({pct:.0f}%)"
    return (f"<td class='cell' style='background:{_heat_color(pct)}' "
            f"title='{esc(title)}'>{pct:.0f}%<small>{games}g</small></td>")


def _matrix_table(players, lookup):
    """Heat-colored matrix; lookup(row,col) -> {games,wins} or None; diag blank."""
    head = "<th class='corner'></th>" + "".join(
        f"<th class='colhead'>{esc(c)}</th>" for c in players)
    rows = []
    for r in players:
        cells = [f"<th class='rowhead'>{esc(r)}</th>"]
        for c in players:
            if r == c:
                cells.append("<td class='diag'></td>")
            else:
                cells.append(_matrix_cell(lookup(r, c)))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return ("<div class='matrix-wrap'><table class='matrix'><thead><tr>"
            + head + "</tr></thead><tbody>" + "".join(rows)
            + "</tbody></table></div>")


def render_matrix(matches, roster=None, who=None, trial=False):
    mx = compute_matrices(matches)
    syn, syn_players = mx["synergy"], mx["synergy_players"]
    h2h, h2h_players = mx["h2h"], mx["h2h_players"]

    # Axis = every known player (roster + anyone appearing in matches), so the
    # grids list all names even at 0 games; empty cells render as faded "—".
    order = lambda names: sorted(set(names), key=lambda x: (x.lower(), x))
    players_axis = order(list(roster or []) + syn_players + h2h_players)

    if players_axis:
        syn_table = _matrix_table(
            players_axis, lambda r, c: syn.get(_pair_key(r, c)))
        h2h_table = _matrix_table(players_axis, lambda r, c: h2h.get((r, c)))
    else:
        empty = ("<p class='muted'>No players yet — register on the login "
                 "page (or load the sample data in trial mode).</p>")
        syn_table = h2h_table = empty

    ranked = []
    for (n1, n2), cell in syn.items():
        if cell["games"] >= MIN_MATRIX_GAMES:
            pct = 100.0 * cell["wins"] / cell["games"]
            ranked.append((pct, cell["games"], n1, n2))

    def rank_list(items):
        lis = []
        for pct, g, n1, n2 in items:
            lis.append(
                f"<li><span class='pct'>{pct:.0f}%</span> "
                f"{esc(n1)} &amp; {esc(n2)} "
                f"<span class='muted'>({g} games)</span></li>")
        return "<ul class='rank-list'>" + "".join(lis) + "</ul>"

    if ranked:
        best = sorted(ranked, key=lambda x: (-x[0], -x[1]))[:8]
        worst = sorted(ranked, key=lambda x: (x[0], -x[1]))[:8]
        best_html = "<h3>Best partnerships</h3>" + rank_list(best)
        worst_html = "<h3>Worst partnerships</h3>" + rank_list(worst)
    else:
        best_html = worst_html = ("<p class='muted'>No 2v2 pairings yet "
                                  "(need &ge; 1 game together).</p>")

    legend = ("<p class='muted'>Cells: row's win% vs column. Hover for W-L and "
              "games. &mdash; = not played yet. "
              "Heat: <span style='color:hsl(0,62%,42%)'>&#9632;</span> low "
              "&rarr; <span style='color:hsl(120,62%,42%)'>&#9632;</span> high.</p>")

    body = (
        "<h1>Correlation matrices</h1>" + legend +
        "<h2>Partner synergy (2v2)</h2>" + syn_table +
        "<div class='row2' style='margin-top:12px'>"
        f"<div class='card'>{best_html}</div>"
        f"<div class='card'>{worst_html}</div>"
        "</div>"
        "<h2 style='margin-top:24px'>Head-to-head</h2>" + h2h_table +
        "<p style='margin-top:16px'><a href='/'>&larr; Back to leaderboard</a></p>"
    )
    return base_page("Matrix — Foosball Tracker", body, who, trial=trial)


def render_breakdown(bd, who=None, trial=False):
    """Post-record result view: per-player old->new/role/X, plus doubles detail."""
    if bd is None:
        return base_page("Match recorded",
                         "<h1>Match recorded</h1>"
                         "<p><a href='/'>&larr; Back to leaderboard</a></p>",
                         who, trial=trial)

    a_names = ", ".join(esc(n) for n in bd["team_a"])
    b_names = ", ".join(esc(n) for n in bd["team_b"])
    title = "Trial match recorded (private)" if trial else "Match recorded"

    rows = []
    for p in bd["players"]:
        d = p["delta"]
        sign = "+" if d >= 0 else "−"
        cls = "win" if d >= 0 else "loss"
        rows.append(
            "<tr>"
            f"<td>{esc(p['name'])}</td>"
            f"<td>{p['side']}</td>"
            f"<td>{esc(p['role'])}</td>"
            f"<td>{round(p['old'])} &rarr; <strong>{round(p['new'])}</strong></td>"
            f"<td class='{cls}'>{sign}{abs(d)}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Player</th><th>Side</th><th>Role</th>"
        "<th>Rating</th><th>X applied</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )

    a_cls = "win" if bd["a_won"] else "loss"
    b_cls = "loss" if bd["a_won"] else "win"
    detail = (
        "<div class='card'>"
        f"<p><strong>{esc(bd['fmt'])}</strong> &nbsp; "
        f"<span class='{a_cls}'>{a_names}</span> "
        f"<strong>{bd['score_a']}–{bd['score_b']}</strong> "
        f"<span class='{b_cls}'>{b_names}</span></p>"
    )
    if bd["doubles"]:
        detail += (
            f"<p class='muted'>Team A avg <strong>{round(bd['rating_a'])}</strong> "
            f"({bd['a_role']}) &nbsp;·&nbsp; Team B avg "
            f"<strong>{round(bd['rating_b'])}</strong> ({bd['b_role']})</p>"
        )
    detail += (
        f"<p>Gap <strong>{round(bd['gap'])}</strong> &rarr; category "
        f"<strong>{esc(bd['bucket'])}</strong> "
        f"<span class='muted'>(favourite X {bd['fav_x']}, "
        f"underdog X {bd['under_x']})</span></p>"
        "</div>"
    )

    body = (
        f"<h1>{esc(title)}</h1>" + detail + table +
        "<p style='margin-top:16px'><a class='btn' href='/'>Back to leaderboard</a></p>"
    )
    return base_page(title, body, who, trial=trial)


def render_not_found(who=None, trial=False):
    return base_page(
        "Not found",
        "<h1>404</h1><p>No such page. <a href='/'>Home</a></p>",
        who, trial=trial,
    )
