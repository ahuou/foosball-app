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

import difflib
import html
import json
import random
import urllib.parse
from datetime import datetime, timedelta, timezone

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


SIMILAR_THRESHOLD = 0.80   # difflib ratio at/above which a new name is flagged


def suggest_similar(name, existing_names, threshold=SIMILAR_THRESHOLD):
    """
    Soft "did you mean" guard. `name` is a NEW name (no exact case-insensitive
    match). Return the single best existing name it is fuzzy-similar to, or None.
    Similar iff difflib ratio >= threshold OR one name is a prefix of the other
    with <= 2 extra chars (catches Matte/Matteo, Mate/Matteo, Mat/Mate).
    """
    key = name.strip().lower()
    if not key:
        return None
    best_name, best_score = None, 0.0
    for e in existing_names:
        el = (e or "").strip().lower()
        if not el or el == key:
            continue
        ratio = difflib.SequenceMatcher(None, key, el).ratio()
        shorter, longer = sorted((key, el), key=len)
        prefix_hit = longer.startswith(shorter) and (len(longer) - len(shorter)) <= 2
        score = max(ratio, 0.90 if prefix_hit else 0.0)
        if (ratio >= threshold or prefix_hit) and score > best_score:
            best_name, best_score = e, score
    return best_name


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
# Zero-sum transfer. The bucket gives a favourite-X and underdog-X from the gap.
# The amount that moves is the WINNER's role-X: the winning side ADDS it to every
# member and the losing side SUBTRACTS the SAME amount from every member. So an
# upset (underdog wins) moves the big underdog-X both ways; an expected win
# (favourite wins) moves the small favourite-X both ways. Points are conserved
# within a match (winner gains == loser loses) but not across matches (X varies).
# Margin of victory is ignored.

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


# --- Score-margin multiplier (era-gated) ------------------------------------
# Matches with timestamp >= this cutoff have their transfer scaled by a linear
# score-margin multiplier; earlier matches are rule-frozen (multiplier 1.0) so
# they replay to bit-for-bit the same ratings as before this rule existed.
MARGIN_RULE_START = "2026-07-18T20:41:15+00:00"
_MARGIN_CUTOFF = datetime.fromisoformat(MARGIN_RULE_START)


def margin_multiplier(score_a, score_b):
    """Linear margin multiplier: 0.6 + 0.1*|winner-loser|. 10-8→0.8, 10-6→1.0
    (par), 10-0→1.6. No log, no cap."""
    try:
        margin = abs(int(score_a) - int(score_b))
    except (TypeError, ValueError):
        return 1.0
    return 0.6 + 0.1 * margin


def match_scored(m):
    """True iff the match's timestamp is at/after the margin-rule cutoff (so its
    transfer is score-weighted). Offset-robust via datetime parsing; naive
    timestamps are treated as UTC. Unparseable/absent -> pre-cutoff (False)."""
    try:
        dt = datetime.fromisoformat(m.get("timestamp_iso"))
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= _MARGIN_CUTOFF


def rating_delta(rating_a, rating_b, a_won, score_mult=1.0):
    """
    Zero-sum transfer: the loser loses exactly what the winner gains. The base
    amount T0 is the WINNER's role-based X (upset moves the full underdog-X both
    ways; an expected win moves the smaller favourite-X). The actual transfer is
    T = round(T0 * score_mult), so `score_mult=1.0` reproduces the pre-margin
    behaviour exactly (T0 is an int → round is identity). A tie in ratings makes
    BOTH sides favourite. Returns (a_delta, b_delta, meta); each delta is what
    EVERY player on that side moves by.
    """
    gap = abs(rating_a - rating_b)
    fav_x, under_x = bucket_x(gap)
    a_is_fav = rating_a >= rating_b      # equal -> both favourite
    b_is_fav = rating_b >= rating_a
    a_role_x = fav_x if a_is_fav else under_x   # each side's own role X
    b_role_x = fav_x if b_is_fav else under_x
    base_transfer = a_role_x if a_won else b_role_x   # T0 = the WINNER's role X
    transfer = round(base_transfer * score_mult)      # T = round(T0 * mult)
    a_delta = transfer if a_won else -transfer
    b_delta = transfer if (not a_won) else -transfer
    meta = {
        "gap": gap, "fav_x": fav_x, "under_x": under_x,
        "a_role": "favourite" if a_is_fav else "underdog",
        "b_role": "favourite" if b_is_fav else "underdog",
        "a_x": abs(a_delta), "b_x": abs(b_delta),  # actual amount each side moved
        "base_transfer": base_transfer,
        "transfer": transfer,
        "score_mult": score_mult,
        "margin": round((score_mult - 0.6) / 0.1),  # inverse of margin_multiplier
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

        # Zero-sum transfer, gap-bucketed; scaled by the score margin only for
        # post-cutoff matches (older matches stay rule-frozen at mult 1.0).
        sm = margin_multiplier(score_a, score_b) if match_scored(m) else 1.0
        a_delta, b_delta, _meta = rating_delta(rating_a, rating_b, a_won, score_mult=sm)
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
        scored = match_scored(m)
        sm = margin_multiplier(score_a, score_b) if scored else 1.0
        a_delta, b_delta, meta = rating_delta(rating_a, rating_b, a_won, score_mult=sm)

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
                "scored": scored, "score_mult": meta["score_mult"],
                "margin": abs(score_a - score_b), "transfer": meta["transfer"],
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
.ac-wrap { position: relative; }
.ac-list {
  position: absolute; left: 0; right: 0; top: 100%; z-index: 30;
  background: #fff; border: 1px solid #cbd5e1; border-radius: 6px;
  max-height: 220px; overflow-y: auto; box-shadow: 0 6px 16px rgba(0,0,0,0.15);
  margin-top: 2px;
}
.ac-item { padding: 9px 11px; cursor: pointer; font-size: 0.95rem; }
.ac-item:hover, .ac-item.active { background: #eef2ff; }
.dym {
  background: #fffbeb; border: 1px solid #fde68a; color: #92400e;
  padding: 9px 12px; border-radius: 6px; margin: 8px 0;
}
.dym .btn-sm { margin-top: 0; }
@media (prefers-color-scheme: dark) {
  .btn-sm { background: #111827; border-color: #4b5563; color: #93c5fd; }
  .btn-sm:hover { background: #1e293b; }
  .btn-sm.danger { color: #fca5a5; border-color: #7f1d1d; }
  .btn-sm.danger:hover { background: #3f1d1d; }
  .ac-list { background: #1f2937; border-color: #4b5563; }
  .ac-item:hover, .ac-item.active { background: #374151; }
  .dym { background: #3a2e12; border-color: #78591c; color: #fcd34d; }
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

/* ===== phone-first shell ===== */
.shell { max-width: 480px; margin: 0 auto; padding: 18px 16px 24px; }
.shell.has-tabs { padding-bottom: 92px; }
h1 { font-size: 1.5rem; margin: 4px 0 14px; }
.btn-lg {
  display: block; width: 100%; box-sizing: border-box; text-align: center;
  padding: 18px 16px; margin: 12px 0; min-height: 56px;
  font-size: 1.15rem; font-weight: 700; border-radius: 14px;
  background: #2563eb; color: #fff; border: none; cursor: pointer;
  text-decoration: none; line-height: 1.3;
}
.btn-lg.secondary { background: #6b7280; }
.btn-lg:active { filter: brightness(0.94); }
.hub-greet { font-size: 1.5rem; font-weight: 700; margin: 14px 0 2px; }
.hub-sub { color: #6b7280; margin: 0 0 22px; font-size: 0.95rem; }
.backlink { display: inline-block; margin-bottom: 6px; color: #2563eb;
  text-decoration: none; font-size: 1rem; padding: 8px 2px; min-height: 40px; }
.seg { display: flex; width: 100%; border: 1px solid #cbd5e1; border-radius: 12px;
  overflow: hidden; margin: 12px 0 16px; }
.seg > * { flex: 1; text-align: center; padding: 13px 6px; min-height: 46px;
  font-weight: 600; text-decoration: none; color: #2563eb; background: #fff;
  cursor: pointer; border: none; font-size: 1rem; display: flex;
  align-items: center; justify-content: center; }
.seg > *.active, .seg-fmt label:has(input:checked) { background: #2563eb; color: #fff; }
.seg > * + * { border-left: 1px solid #cbd5e1; }
.seg-fmt input { position: absolute; opacity: 0; width: 0; height: 0; }
.yourcard { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 14px;
  padding: 14px 16px; margin: 4px 0 8px; }
.yourcard h2 { margin: 0 0 6px; font-size: 1.05rem; }
.yourcard .track { display: flex; justify-content: space-between;
  align-items: baseline; padding: 6px 0; }
.yourcard .track b { font-size: 1.15rem; }
.lb { display: flex; flex-direction: column; gap: 8px; }
.lb-row { display: flex; align-items: center; gap: 12px; min-height: 52px;
  padding: 10px 14px; border-radius: 12px; background: #fff; text-decoration: none;
  color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.lb-row .lb-rank { font-weight: 700; color: #6b7280; min-width: 1.6em; }
.lb-row .lb-name { flex: 1; font-weight: 600; }
.lb-row .lb-elo { font-weight: 700; font-size: 1.1rem; }
.lb-row .lb-wl { color: #6b7280; font-size: 0.8rem; min-width: 3em; text-align: right; }
.tabbar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 40;
  display: flex; background: #fff; border-top: 1px solid #e5e7eb;
  max-width: 480px; margin: 0 auto; }
.tabbar a { flex: 1; text-align: center; padding: 8px 4px 10px; text-decoration: none;
  color: #6b7280; font-size: 0.72rem; min-height: 56px; }
.tabbar a .ic { display: block; font-size: 1.3rem; line-height: 1.5; }
.tabbar a.active { color: #2563eb; font-weight: 700; }
@media (prefers-color-scheme: dark) {
  .hub-sub, .lb-row .lb-wl, .lb-row .lb-rank, .tabbar a { color: #9ca3af; }
  .seg { border-color: #4b5563; }
  .seg > * { background: #1f2937; color: #93c5fd; }
  .seg > *.active, .seg-fmt label:has(input:checked) { background: #2563eb; color: #fff; }
  .seg > * + * { border-color: #4b5563; }
  .yourcard { background: #172033; border-color: #1e3a5f; }
  .lb-row { background: #1f2937; box-shadow: none; }
  .tabbar { background: #1f2937; border-color: #374151; }
}
"""


def _tab_bar(active):
    """Fixed bottom tab bar for the Check-scores section (Board/Matrix/History)."""
    tabs = [
        ("board", "/scores", "&#128202;", "Board"),
        ("matrix", "/matrix", "&#128200;", "Matrix"),
        ("history", "/history", "&#128340;", "History"),
    ]
    links = "".join(
        f"<a class='{'active' if key == active else ''}' href='{href}'>"
        f"<span class='ic'>{ic}</span>{label}</a>"
        for key, href, ic, label in tabs)
    return f"<nav class='tabbar'>{links}</nav>"


def base_page(title, body, who=None, trial=False, tabs=None):
    """
    Minimal phone-first shell: a centered max-width column, an optional trial
    banner, and (on the Check-scores routes) a fixed bottom tab bar. The old
    dense top nav is gone — navigation is home-hub buttons + bottom tabs + back
    links rendered inside each page's body. `who` is accepted for call
    compatibility but no longer drives any chrome.
    """
    banner = ""
    if trial:
        banner = (
            "<div class='trialbar'>&#129514; TRIAL MODE — matches you record here "
            "are private to you and are deleted when you log out.</div>"
        )
    shell_cls = "shell has-tabs" if tabs else "shell"
    tabbar = _tab_bar(tabs) if tabs else ""
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{PAGE_CSS}</style></head><body>"
        f"{banner}"
        f"<div class='{shell_cls}'>{body}</div>"
        f"{tabbar}"
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
    Inline JS for each name input (marked data-roster):
      * a custom vanilla type-ahead dropdown (case-insensitive substring of the
        roster, keyboard up/down/enter + click/tap select) — reliable on mobile,
        the primary UX (the native <datalist> stays as a fallback);
      * the existing canonical-casing snap on blur/change.
    Candidate names come only from the injected roster (ROSTER) — nothing
    hardcoded. Everything is JSON-encoded safely for the script context.
    """
    mapping = {}
    for n in names:
        mapping.setdefault(n.strip().lower(), n)
    return (
        "<script>"
        f"(function(){{var ROSTER={_js_json(mapping)};"
        "var NAMES=Object.keys(ROSTER).map(function(k){return ROSTER[k];});"
        "function snap(el){var v=(el.value||'').trim();if(!v)return;"
        "var c=ROSTER[v.toLowerCase()];if(c){el.value=c;}}"
        "function setup(input){"
        "  var wrap=document.createElement('div');wrap.className='ac-wrap';"
        "  input.parentNode.insertBefore(wrap,input);wrap.appendChild(input);"
        "  var list=document.createElement('div');list.className='ac-list';"
        "  list.style.display='none';wrap.appendChild(list);"
        "  var items=[],active=-1;"
        "  function choose(n){input.value=n;hide();}"
        "  function hide(){list.style.display='none';active=-1;}"
        "  function setActive(i){items.forEach(function(o){o.className='ac-item';});"
        "    if(i>=0&&i<items.length){items[i].className='ac-item active';active=i;"
        "      items[i].scrollIntoView({block:'nearest'});}else{active=-1;}}"
        "  function render(){var q=(input.value||'').trim().toLowerCase();"
        "    list.innerHTML='';items=[];active=-1;"
        "    if(!q){hide();return;}"
        "    var ms=NAMES.filter(function(n){return n.toLowerCase().indexOf(q)>=0;}).slice(0,8);"
        "    if(ms.length===0||(ms.length===1&&ms[0].toLowerCase()===q)){hide();return;}"
        "    ms.forEach(function(n){var o=document.createElement('div');o.className='ac-item';"
        "      o.textContent=n;o.addEventListener('mousedown',function(e){e.preventDefault();choose(n);});"
        "      list.appendChild(o);items.push(o);});list.style.display='block';}"
        "  input.addEventListener('input',render);"
        "  input.addEventListener('focus',render);"
        "  input.addEventListener('keydown',function(e){"
        "    if(list.style.display==='none')return;"
        "    if(e.key==='ArrowDown'){e.preventDefault();setActive(Math.min(active+1,items.length-1));}"
        "    else if(e.key==='ArrowUp'){e.preventDefault();setActive(Math.max(active-1,0));}"
        "    else if(e.key==='Enter'){if(active>=0){e.preventDefault();choose(items[active].textContent);}}"
        "    else if(e.key==='Escape'){hide();}});"
        "  input.addEventListener('blur',function(){setTimeout(function(){hide();snap(input);},150);});"
        "  input.addEventListener('change',function(){snap(input);});}"
        "document.querySelectorAll('input[data-roster]').forEach(setup);})();"
        "</script>"
    )


def _team_names(raw):
    return ", ".join(esc(n) for n in _parse_team(raw))


def _compact_board(data):
    """Slim, tap-through leaderboard: rank · name · ELO · small W-L. Lists ALL
    players (even 0 games); empty only when the roster is genuinely empty."""
    lb = data["leaderboard"]
    if not lb:
        return ("<p class='muted'>No players yet — enter your name on the home "
                "screen to get started.</p>")
    rows = []
    for i, s in enumerate(lb, start=1):
        crown = " &#128081;" if s["weeks_at_top"] > 0 and i == 1 else ""
        rows.append(
            f"<a class='lb-row' href='/player?name={urllib.parse.quote(s['name'])}'>"
            f"<span class='lb-rank'>{i}</span>"
            f"<span class='lb-name'>{esc(s['name'])}{crown}</span>"
            f"<span class='lb-elo'>{round(s['elo'])}</span>"
            f"<span class='lb-wl'>{s['wins']}-{s['losses']}</span>"
            "</a>"
        )
    return "<div class='lb'>" + "".join(rows) + "</div>"


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


def render_home(who, trial=False):
    """Logged-in hub: greeting + two big buttons. Nothing else."""
    body = (
        f"<p class='hub-greet'>Hi, {esc(who)} &#128075;</p>"
        f"<p class='hub-sub'>Playing as <strong>{esc(who)}</strong> &nbsp;·&nbsp; "
        "<a href='/logout'>Log out / switch</a></p>"
        "<a class='btn-lg' href='/scores'>&#128202; Check scores</a>"
        "<a class='btn-lg' href='/record'>&#10133; Record a match</a>"
    )
    return base_page("Foosball Tracker", body, trial=trial)


def _rank_and_stat(data, name):
    """Return (rank, stat) of `name` in a board leaderboard, or (None, None)."""
    key = (name or "").strip().lower()
    for i, s in enumerate(data["leaderboard"], start=1):
        if s["name"].strip().lower() == key:
            return i, s
    return None, None


def render_scores(matches, roster, who, board="singles", trial=False,
                  seed_singles=None, seed_doubles=None):
    """Board tab: your card, Singles/Doubles toggle, compact tap-through board."""
    singles = compute_stats(matches, roster, seed_map=seed_singles, fmt="1v1")
    doubles = compute_stats(matches, roster, seed_map=seed_doubles, fmt="2v2")
    board = "doubles" if board == "doubles" else "singles"

    s_rank, s_stat = _rank_and_stat(singles, who)
    d_rank, d_stat = _rank_and_stat(doubles, who)

    def track(label, rank, stat):
        if stat is None:
            return (f"<div class='track'><span>{label}</span>"
                    "<span class='muted'>not played yet</span></div>")
        return (f"<div class='track'><span>{label} "
                f"<span class='muted'>#{rank}</span></span>"
                f"<span><b>{round(stat['elo'])}</b> "
                f"<span class='muted'>({stat['wins']}-{stat['losses']})</span></span></div>")

    your_card = (
        "<div class='yourcard'>"
        f"<h2>You — {esc(who)}</h2>"
        + track("Singles", s_rank, s_stat)
        + track("Doubles", d_rank, d_stat)
        + "</div>"
    )

    seg = (
        "<div class='seg'>"
        f"<a class='{'active' if board == 'singles' else ''}' href='/scores?board=singles'>Singles</a>"
        f"<a class='{'active' if board == 'doubles' else ''}' href='/scores?board=doubles'>Doubles</a>"
        "</div>"
    )
    board_data = singles if board == "singles" else doubles
    controls = sample_controls_html() if trial else ""
    body = (
        "<a class='backlink' href='/'>&#8249; Home</a>"
        "<h1>Scores</h1>" + your_card + seg
        + _compact_board(board_data) + controls
    )
    return base_page("Scores — Foosball Tracker", body, trial=trial, tabs="board")


def render_history(matches, who=None, trial=False):
    """Full match log, newest first, with Edit/Delete on real stored matches."""
    editable = not trial
    ordered = sorted(matches or [],
                     key=lambda m: (m.get("timestamp_iso") or "", m.get("id") or ""))
    ordered = list(reversed(ordered))
    home = "<a class='backlink' href='/'>&#8249; Home</a>"
    controls = sample_controls_html() if trial else ""
    if not ordered:
        body = (home + "<h1>Match history</h1>" + controls +
                "<p class='muted'>No matches recorded yet. "
                "<a href='/record'>Record the first match!</a></p>")
        return base_page("History — Foosball Tracker", body, trial=trial, tabs="history")

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
    body = home + "<h1>Match history</h1>" + controls + table + note
    return base_page("History — Foosball Tracker", body, trial=trial, tabs="history")


def render_login(roster, who=None, trial=False):
    """Logged-out home: one clean screen — name + Enter, seeds tucked away."""
    body = (
        "<h1>&#127955; Foosball Tracker</h1>"
        "<p class='hub-sub'>Type your name to check scores or record a match. "
        "No password — honor system.</p>"
        "<form method='post' action='/login'>"
        "<label for='name'>Your name</label>"
        "<input type='text' id='name' name='name' list='players' data-roster "
        "autocomplete='off' required autofocus>"
        + datalist_html("players", roster) +
        "<details style='margin:12px 0'>"
        "<summary style='cursor:pointer;color:#2563eb;padding:6px 0'>"
        "New here? Set starting ELO</summary>"
        "<p class='muted' style='margin:8px 0 4px'>New players only; ignored if "
        "you already exist. Singles &amp; Doubles are separate.</p>"
        "<div class='row2'>"
        "<div><label for='seed_singles'>Singles</label>"
        f"<input type='number' id='seed_singles' name='seed_singles' value='{DEFAULT_SEED}' "
        f"min='{SEED_MIN}' max='{SEED_MAX}' inputmode='numeric'></div>"
        "<div><label for='seed_doubles'>Doubles</label>"
        f"<input type='number' id='seed_doubles' name='seed_doubles' value='{DEFAULT_SEED}' "
        f"min='{SEED_MIN}' max='{SEED_MAX}' inputmode='numeric'></div>"
        "</div>"
        "</details>"
        "<button class='btn-lg' type='submit'>Enter</button>"
        "</form>"
        "<p style='text-align:center;margin-top:18px'>"
        "<a href='/trial'>Just browsing? &#129514; Try it out</a></p>"
        + roster_autocomplete_script(roster)
    )
    return base_page("Foosball Tracker", body, trial=trial)


def _did_you_mean_html(flags):
    """
    Soft duplicate notice inside the match form: per flagged name, offer
    "Use <existing>" (fills the field) or "No, create <typed>" (adds a
    per-field confirm token and resubmits). Buttons wire up via data attributes
    (no inline JS escaping headaches); names are HTML-escaped.
    """
    if not flags:
        return ""
    rows = []
    for fl in flags:
        rows.append(
            f"<div class='dym' data-field='{esc(fl['field'])}' "
            f"data-sug='{esc(fl['suggestion'])}'>"
            f"‘{esc(fl['typed'])}’ looks like existing player "
            f"‘{esc(fl['suggestion'])}’. "
            f"<button type='button' class='btn-sm dym-use'>Use ‘{esc(fl['suggestion'])}’</button> "
            f"<button type='button' class='btn-sm dym-new'>No, create ‘{esc(fl['typed'])}’</button>"
            "</div>"
        )
    script = (
        "<script>"
        "document.querySelectorAll('.dym').forEach(function(d){"
        "var frm=document.getElementById('matchform');"
        "var field=d.getAttribute('data-field'),sug=d.getAttribute('data-sug');"
        "d.querySelector('.dym-use').addEventListener('click',function(){"
        "  if(frm.elements[field]){frm.elements[field].value=sug;}d.parentNode.removeChild(d);});"
        "d.querySelector('.dym-new').addEventListener('click',function(){"
        "  var i=document.createElement('input');i.type='hidden';i.name='confirm_'+field;"
        "  i.value='1';frm.appendChild(i);frm.submit();});});"
        "</script>"
    )
    return ("<div class='error'>Possible duplicate players — please check:</div>"
            + "".join(rows) + script)


def _match_form(action, roster, fmt="1v1", values=None, submit_label="Save match",
                hidden="", flags=None, confirmed=None):
    """Shared match form (record + edit): 1v1/2v2 toggle, slots, scores."""
    values = values or {}

    def val(k):
        return esc(values.get(k, ""))

    dl = datalist_html("players", roster)
    checked_1 = "checked" if fmt != "2v2" else ""
    checked_2 = "checked" if fmt == "2v2" else ""
    # Re-emit already-accepted create-new confirmations so multi-name flows
    # don't lose earlier confirmations across resubmits.
    confirm_hidden = "".join(
        f"<input type='hidden' name='confirm_{esc(f)}' value='1'>"
        for f in (confirmed or []))
    return (
        f"<form id='matchform' class='card' method='post' action='{esc(action)}'>"
        + hidden + confirm_hidden + _did_you_mean_html(flags) +
        "<label>Format</label>"
        "<div class='seg seg-fmt'>"
        f"<label><input type='radio' name='format' value='1v1' {checked_1} onclick='setFmt(false)'>1v1</label>"
        f"<label><input type='radio' name='format' value='2v2' {checked_2} onclick='setFmt(true)'>2v2</label>"
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
        f"<div><label>Score A</label><input type='number' name='score_a' min='0' inputmode='numeric' value='{val('score_a')}' required></div>"
        f"<div><label>Score B</label><input type='number' name='score_b' min='0' inputmode='numeric' value='{val('score_b')}' required></div>"
        "</div>"
        + dl +
        f"<button class='btn-lg' type='submit'>{esc(submit_label)}</button>"
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


def render_record(roster, who=None, error=None, fmt="1v1", values=None, trial=False,
                  flags=None, confirmed=None):
    err_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    controls = sample_controls_html() if trial else ""
    body = ("<a class='backlink' href='/'>&#8249; Home</a>"
            "<h1>Record a match</h1>" + err_html + controls
            + _match_form("/record", roster, fmt, values, "Record",
                          flags=flags, confirmed=confirmed))
    return base_page("Record — Foosball Tracker", body, trial=trial)


def render_edit(roster, match_id, who=None, error=None, fmt="1v1", values=None,
                trial=False, flags=None, confirmed=None):
    err_html = f"<div class='error'>{esc(error)}</div>" if error else ""
    hidden = f"<input type='hidden' name='id' value='{esc(match_id)}'>"
    body = (
        "<a class='backlink' href='/history'>&#8249; Back to history</a>"
        f"<h1>Edit match #{esc(match_id)}</h1>" + err_html
        + _match_form("/edit", roster, fmt, values, "Save changes", hidden=hidden,
                      flags=flags, confirmed=confirmed)
    )
    return base_page("Edit match — Foosball Tracker", body, trial=trial)


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
        body = ("<a class='backlink' href='/scores'>&#8249; Back</a>"
                "<h1>Unknown player</h1>"
                f"<p class='muted'>No player named “{esc(name)}”.</p>")
        return base_page("Player — Foosball Tracker", body, trial=trial), 404

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
        "<a class='backlink' href='/scores'>&#8249; Back</a>"
        f"<h1>{esc(canonical)}</h1>" + blocks + hist_table
    )
    return base_page(f"{canonical} — Foosball Tracker", body, trial=trial), 200


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
        "<a class='backlink' href='/'>&#8249; Home</a>"
        "<h1>Correlation matrices</h1>" + legend +
        "<h2>Partner synergy (2v2)</h2>" + syn_table +
        f"<div class='card'>{best_html}</div>"
        f"<div class='card'>{worst_html}</div>"
        "<h2 style='margin-top:24px'>Head-to-head</h2>" + h2h_table
    )
    return base_page("Matrix — Foosball Tracker", body, trial=trial, tabs="matrix")


def render_breakdown(bd, who=None, trial=False):
    """Post-record result view: per-player old->new/role/X, plus doubles detail."""
    if bd is None:
        return base_page("Match recorded",
                         "<h1>Match recorded</h1>"
                         "<a class='btn-lg' href='/scores'>&#128202; See the board</a>"
                         "<p style='text-align:center'><a href='/'>&#8249; Home</a></p>",
                         trial=trial)

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
    )
    # Score-margin line only for post-cutoff (scored) matches.
    if bd.get("scored"):
        detail += (
            f"<p>Score margin {bd['score_a']}–{bd['score_b']} &rarr; "
            f"<strong>&times;{bd['score_mult']:.1f}</strong> "
            f"<span class='muted'>(transfer {bd['transfer']})</span></p>"
        )
    detail += "</div>"

    body = (
        f"<h1>{esc(title)}</h1>" + detail + table
        + "<a class='btn-lg' href='/scores'>&#128202; See the board</a>"
        + "<a class='btn-lg secondary' href='/record'>&#10133; Record another</a>"
        + "<p style='text-align:center;margin-top:6px'><a href='/'>&#8249; Home</a></p>"
    )
    return base_page(title, body, trial=trial)


def render_not_found(who=None, trial=False):
    return base_page(
        "Not found",
        "<h1>404</h1><p>No such page. <a href='/'>Home</a></p>",
        trial=trial,
    )
