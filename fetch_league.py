#!/usr/bin/env python3
"""
Pull an ESPN fantasy football league's history and write league.json for the
League Record Book page.

    python fetch_league.py --league-id 1234567

Private leagues also need two cookies from a signed-in espn.com browser session:

    ESPN_S2=... ESPN_SWID={...} python fetch_league.py --league-id 1234567

Finished seasons are cached in data/seasons/ so later runs only refetch the
current season. Pass --refresh to rebuild everything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

import requests

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

BENCH_SLOT = 20
IR_SLOT = 21
NON_STARTER_SLOTS = {BENCH_SLOT, IR_SLOT}
PLAYER_SLOT_IDS = [0, 2, 4, 6, 16, 17]  # QB, RB, WR, TE, D/ST, K

TOP_AT_POSITION = 20        # first-round bust: finished outside the top 20 at their position
GEM_TOP = {1: 8, 2: 20, 3: 20, 4: 8}  # late gem: finish top 8 at QB/TE, top 20 at RB/WR
LATE_ROUND = 7              # late gem: drafted in round 7 or later
POSITION_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
PLAYER_POOL = 600           # top players by season points pulled for rankings (covers 40+ deep at every position)

# Seasons this group played in a separate, earlier ESPN league: (league ID, first season, last season).
# They're pulled in as regular seasons. Managers match across leagues by ESPN account.
PREVIOUS_LEAGUES = [(1446935, 2016, 2018)]

# Display names that belong to one person who shows up under more than one ESPN account
# (for example after changing the email on their ESPN login). Each inner list is one person.
SAME_PERSON = [["Nathan G."], ["Austin R."], ["Kristopher A."], ["Justin B."], ["CJ K.", "chris k."]]
BOX_SCORES_FROM = 2019      # ESPN only serves lineups/box scores from 2019 on
INJURY_SHARE = 0.4          # injured mid-game: starter scored under 40% of projection...
INJURY_MIN_PROJECTION = 5.0 # ...was projected for at least this many points...
                            # ...and missed the next week (IR slot or no stats, not a bye)
TRADED_MESSAGE = 244        # message type for a completed trade in the league activity feed
SKIPPED_PLAYOFF_TIERS = ("LOSER", "CONSOLATION")  # consolation games don't count toward records
DRAFT_SKIP_POSITIONS = {5, 16}  # K and D/ST: nearly all go late and finish top 20, so they'd swamp "late gems"
CACHE_VERSION = 18            # bump when season processing changes so cached seasons get rebuilt

LINE_FIELDS = ["w", "l", "t", "pf", "xw", "g", "seed", "finish", "playoffs", "last",
               "champ", "inj", "ssC", "ssT", "r1Bust", "lateHits", "posPts"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Espn:
    def __init__(self, league_id: int, espn_s2: str | None, swid: str | None, delay: float = 0.35):
        self.league_id = league_id
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "league-record-book/1.0"
        if espn_s2 and swid:
            self.session.cookies.set("espn_s2", espn_s2)
            self.session.cookies.set("SWID", swid)

    def _get(self, url: str, params: list, filt: dict | None = None):
        headers = {"x-fantasy-filter": json.dumps(filt)} if filt else {}
        for attempt in range(5):
            time.sleep(self.delay)
            resp = self.session.get(url, params=params, headers=headers, timeout=45)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                log(f"  ESPN returned {resp.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            break
        if resp.status_code in (401, 403):
            sys.exit(f"ESPN refused access ({resp.status_code}). This looks like a private league: set ESPN_S2 and "
                     "ESPN_SWID from your espn.com browser cookies. If you already did, they may have expired.")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            sys.exit(f"ESPN sent a page instead of data for {resp.url}. Your cookies may have expired.")

    def league(self, year: int, views: list[str], extra: dict | None = None, filt: dict | None = None):
        params = [("view", v) for v in views] + list((extra or {}).items())
        if year < 2018:
            url = f"{BASE}/leagueHistory/{self.league_id}"
            params.append(("seasonId", year))
        else:
            url = f"{BASE}/seasons/{year}/segments/0/leagues/{self.league_id}"
        data = self._get(url, params, filt)
        if isinstance(data, list):  # leagueHistory wraps the season in a list
            data = data[0] if data else None
        return data

    def bye_weeks(self, year: int) -> dict[int, int]:
        data = self._get(f"{BASE}/seasons/{year}", [("view", "proTeamSchedules_wl")]) or {}
        return {t["id"]: t.get("byeWeek") for t in (data.get("settings") or {}).get("proTeams", [])}

    def week_kickoffs(self, year: int) -> dict[int, int]:
        """First NFL kickoff of each week, in epoch milliseconds."""
        data = self._get(f"{BASE}/seasons/{year}", [("view", "proTeamSchedules_wl")]) or {}
        first = {}
        for team in (data.get("settings") or {}).get("proTeams", []):
            for sp, games in (team.get("proGamesByScoringPeriod") or {}).items():
                for g in games:
                    if g.get("date"):
                        first[int(sp)] = min(first.get(int(sp), g["date"]), g["date"])
        return first

    def activity(self, year: int) -> list[dict]:
        """The league's Recent Activity feed for a season (adds, drops, trades), oldest first."""
        url = f"{BASE}/seasons/{year}/segments/0/leagues/{self.league_id}/communication/"
        start = int(dt.datetime(year, 6, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        end = int(dt.datetime(year + 1, 3, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        topics, offset, page = [], 0, 50
        while True:
            filt = {"topics": {
                "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
                "filterDateRange": {"value": start, "additionalValue": end},
                "sortMessageDate": {"sortPriority": 1, "sortAsc": True},
                "limit": page, "limitPerMessageSet": {"value": 25}, "offset": offset}}
            data = self._get(url, [("view", "kona_league_communication")], filt) or {}
            batch = data.get("topics", [])
            topics += batch
            if len(batch) < page or offset > 5000:
                return topics
            offset += page


# ---------- Helpers ----------

def team_name(team: dict) -> str:
    return (team.get("name")
            or f"{team.get('location', '')} {team.get('nickname', '')}".strip()
            or f"Team {team['id']}")


def member_names(data: dict) -> dict[str, str]:
    names = {}
    for m in data.get("members", []):
        first, last = (m.get("firstName") or "").strip(), (m.get("lastName") or "").strip()
        names[m["id"]] = f"{first} {last[:1]}.".strip() if first else (m.get("displayName") or "")
    return names


def owner_keys(teams: list[dict]) -> dict[int, str]:
    """Managers are tracked by ESPN owner ID so they carry across seasons and team renames."""
    keys, used = {}, set()
    for t in teams:
        owner = t.get("primaryOwner") or next(iter(t.get("owners") or []), None) or f"team-{t['id']}"
        if owner in used:  # one person running two teams in the same season
            owner = f"{owner}-{t['id']}"
        used.add(owner)
        keys[t["id"]] = owner
    return keys


def player_stats(api: Espn, year: int, needed: set[int]) -> dict[int, dict]:
    """Season total and week-by-week fantasy points for the top players (plus any we need), in this league's scoring."""
    sort = {"sortAsc": False, "sortPriority": 1, "value": f"00{year}"}
    # The same filter ESPN's own player pages use: season totals plus every week's points
    stat_filters = {"filterStatsForTopScoringPeriodIds": {"value": 18, "additionalValue": [f"00{year}", f"10{year}"]}}
    data = api.league(year, ["kona_player_info"], filt={"players": {
        "filterSlotIds": {"value": PLAYER_SLOT_IDS}, **stat_filters, "sortAppliedStatTotal": sort, "limit": PLAYER_POOL}}) or {}
    raw = {p["id"]: p.get("player", p) for p in data.get("players", [])}
    missing = [pid for pid in needed if pid not in raw]
    if missing:  # ESPN rejects a limit without a sort, so send both
        more = api.league(year, ["kona_player_info"], filt={"players": {
            "filterIds": {"value": missing}, **stat_filters, "sortAppliedStatTotal": sort, "limit": len(missing)}}) or {}
        raw.update({p["id"]: p.get("player", p) for p in more.get("players", [])})
    if not raw:
        raise RuntimeError("ESPN returned no player stats")

    out = {}
    for pid, pl in raw.items():
        total, weeks = 0.0, {}
        for s in pl.get("stats", []):
            if s.get("seasonId") != year or s.get("statSourceId") != 0:
                continue
            if s.get("statSplitTypeId") == 0:
                total = s.get("appliedTotal", 0.0)
            elif s.get("statSplitTypeId") == 1 and s.get("scoringPeriodId"):
                weeks[s["scoringPeriodId"]] = s.get("appliedTotal", 0.0)
        out[pid] = {"name": pl.get("fullName") or f"Player {pid}", "pos": pl.get("defaultPositionId"),
                    "total": total, "weeks": weeks}
    log(f"  {year}: player stats for {len(out)} players, {sum(1 for s in out.values() if s['weeks'])} with weekly points")
    return out


def position_ranks(stats: dict[int, dict], points) -> dict[int, int]:
    """Rank every player within their position by points(player) (highest = 1)."""
    by_pos = defaultdict(list)
    for pid, s in stats.items():
        by_pos[s["pos"]].append((points(pid), pid))
    ranks = {}
    for entries in by_pos.values():
        entries.sort(reverse=True)
        for n, (_, pid) in enumerate(entries, 1):
            ranks[pid] = n
    return ranks


def completed_trades(api: Espn, year: int, key_of: dict[int, str], last_sp: int) -> tuple[list[dict], dict]:
    """Completed trades from the league's Recent Activity feed: the same list as ESPN's Recent Activity page
    filtered to trades. Each trade is placed in the first week that kicked off after it went through."""
    topics = api.activity(year)
    kickoffs = api.week_kickoffs(year)
    team = lambda v: int(v) if str(v).lstrip("-").isdigit() else v  # noqa: E731
    kinds, found = defaultdict(int), {}
    for t in topics:
        moves = []
        for m in t.get("messages", []):
            kinds[m.get("messageTypeId")] += 1
            if m.get("messageTypeId") == TRADED_MESSAGE and m.get("targetId"):
                moves.append({"pid": m["targetId"], "from": team(m.get("from")), "to": team(m.get("to"))})
        if not moves:
            continue
        # The feed lists each trade twice, "Trade Accepted" then "Trade Processed", with the same players.
        # Keep one, dated by the later (processed) entry.
        key = frozenset((m["pid"], m["from"], m["to"]) for m in moves)
        date = t.get("date") or 0
        prev = found.get(key)
        if prev and abs(date - prev["date"]) < 14 * 86400 * 1000:
            prev["date"] = max(prev["date"], date)
            prev["entries"] += 1
            continue
        found[key] = {"date": date, "moves": moves, "entries": 1}
    trades = []
    for t in sorted(found.values(), key=lambda t: t["date"]):
        wk = min((sp for sp, ko in kickoffs.items() if ko > t["date"]), default=last_sp + 1)
        if wk > last_sp + 1:  # more than a week past the latest results
            continue
        trades.append({"sp": wk, "date": t["date"], "moves": t["moves"], "pending": wk > last_sp})
    unknown = sum(1 for t in trades for m in t["moves"] if m["from"] not in key_of or m["to"] not in key_of)
    diag = {"activityTopics": len(topics), "trades": len(trades), "messageTypes": {str(k): v for k, v in kinds.items()},
            "feedEntriesPerTrade": {str(n): sum(1 for t in found.values() if t["entries"] == n)
                                    for n in sorted({t["entries"] for t in found.values()})},
            "movesWithUnknownTeam": unknown}
    return trades, diag


# Lineup slots each position can fill: QB, RB, WR, TE, flex spots, K, D/ST
POS_SLOTS = {"QB": {0, 7}, "RB": {2, 3, 23, 7}, "WR": {3, 4, 5, 23, 7}, "TE": {5, 6, 23, 7}, "K": {17}, "D/ST": {16}}
MINOR_TRADE_STARTS = 2  # a trade is minor if no player in it started more than this many weeks for his new team


def transaction_history(api: Espn, year: int, weeks: list[int]) -> dict:
    """ESPN's weekly transaction records: every executed pickup (player, team, week), plus the executed
    trades this account can see (its own), which serve as a known answer to check inferred trades against."""
    adds, own_trades, teams_with_adds, kinds = set(), [], set(), defaultdict(int)
    for sp in weeks:
        try:
            resp = api.league(year, ["mTransactions2"], {"scoringPeriodId": sp}) or {}
        except Exception as e:  # noqa: BLE001
            log(f"  {year} week {sp}: couldn't load transactions ({e})")
            continue
        for t in resp.get("transactions", []):
            if t.get("status") != "EXECUTED":
                continue
            kinds[str(t.get("type"))] += 1
            items = t.get("items") or []
            for i in items:
                if i.get("type") == "ADD" and i.get("playerId"):
                    adds.add((i["playerId"], i.get("toTeamId"), t.get("scoringPeriodId") or sp))
                    teams_with_adds.add(i.get("toTeamId"))
            moves = {(i["playerId"], i.get("fromTeamId"), i.get("toTeamId")) for i in items if i.get("type") == "TRADE" and i.get("playerId")}
            if moves and moves not in own_trades:
                own_trades.append(moves)
    return {"adds": adds, "ownTrades": own_trades, "teamsWithAdds": teams_with_adds, "kinds": dict(kinds)}


def inferred_trades(rosters: dict[int, dict[int, list]], adds: set) -> list[dict]:
    """Trades read from week-to-week roster changes: players who went straight from one team's roster to
    another's, in both directions between the same two teams, leaving out any move ESPN records as a pickup."""
    out = []
    weeks = sorted(rosters)
    for prev, cur in zip(weeks, weeks[1:]):
        if cur != prev + 1:
            continue
        owner_before = {e["id"]: tid for tid, entries in rosters[prev].items() for e in entries}
        by_pair = defaultdict(list)
        for tid, entries in rosters[cur].items():
            for e in entries:
                was = owner_before.get(e["id"])
                if was is None or was == tid or any((e["id"], tid, w) in adds for w in (prev, cur)):
                    continue
                by_pair[frozenset((was, tid))].append({"pid": e["id"], "from": was, "to": tid})
        for moves in by_pair.values():
            if len({m["to"] for m in moves}) == 2:  # players went both ways
                out.append({"sp": cur, "date": None, "moves": moves})
    return out


def analyze_trades(trades: list[dict], stats: dict[int, dict], key_of: dict[int, str],
                   rosters: dict | None, scores: dict, reg_periods: int, last_sp: int) -> list[dict]:
    """For each side of each trade: who came and went, how those players ranked at the trade, for the rest of
    the season and at season's end, what the team scored before and after, and what it would have scored
    without the trade (its real lineup each week, with the incoming players swapped back out)."""
    window_cache = {}

    def window_points(pid, lo, hi):
        return sum(v for w, v in stats[pid]["weeks"].items() if lo <= w <= hi) if pid in stats else 0.0

    def ranks_for(lo, hi):
        if (lo, hi) not in window_cache:
            window_cache[(lo, hi)] = position_ranks(stats, lambda p: window_points(p, lo, hi))
        return window_cache[(lo, hi)]

    season_rank = position_ranks(stats, lambda p: stats[p]["total"])
    reg_last = min(reg_periods or last_sp, last_sp)

    def player(pid, wk, tid_for_started=None):
        s = stats.get(pid, {})
        before, after = ranks_for(1, wk - 1) if wk > 1 else {}, ranks_for(wk, last_sp)
        started, starts = None, None
        if rosters and tid_for_started is not None:  # what he did in his new team's starting lineup
            weeks_started = [e["actual"] for sp, by_team in rosters.items() if sp >= wk
                             for e in by_team.get(tid_for_started, []) if e["id"] == pid and e["slot"] not in NON_STARTER_SLOTS]
            started, starts = round(sum(weeks_started), 2), len(weeks_started)
        return {"pid": pid, "name": s.get("name", f"Player {pid}"), "pos": POSITION_NAMES.get(s.get("pos"), "?"),
                "rankBefore": before.get(pid), "rankAfter": after.get(pid) if pid in stats else None,
                "rankEnd": season_rank.get(pid), "ptsBefore": round(window_points(pid, 1, wk - 1), 2),
                "ptsAfter": round(window_points(pid, wk, last_sp), 2), "started": started, "starts": starts}

    def ppg(tid, lo, hi):
        vals = [s for (w, t), (s, _) in scores.items() if t == tid and lo <= w <= hi]
        return round(sum(vals) / len(vals), 2) if vals else None

    def without_trade(tid, wk, got_ids, gave):
        """Each regular-season week after the trade: the team's real starters, minus incoming players, with each
        open spot filled by who would realistically have started there: a player it traded away if he fits the
        spot, otherwise its bench player with the highest projection that week (not the best actual score,
        which would grade the team on hindsight)."""
        if not rosters:
            return None
        net, n, won, lost = 0.0, 0, 0, 0
        for sp in range(wk, reg_last + 1):
            if (sp, tid) not in scores or tid not in rosters.get(sp, {}):
                continue
            mine, theirs = scores[(sp, tid)]
            entries = rosters[sp][tid]
            removed = [e for e in entries if e["id"] in got_ids and e["slot"] not in NON_STARTER_SLOTS]
            traded_away = [{"pts": stats[p]["weeks"].get(sp, 0.0) if p in stats else 0.0,
                            "slots": POS_SLOTS.get(POSITION_NAMES.get((stats.get(p) or {}).get("pos")), set())} for p in gave]
            bench = [{"pts": e["actual"], "proj": e["proj"], "slots": e["eligible"]}
                     for e in entries if e["slot"] == BENCH_SLOT and e["id"] not in got_ids]
            cf = mine - sum(e["actual"] for e in removed)
            for e in removed:
                fits = [c for c in traded_away if e["slot"] in c["slots"]]
                if fits:
                    pick = max(fits, key=lambda c: c["pts"])
                    traded_away.remove(pick)
                else:
                    fits = [c for c in bench if e["slot"] in c["slots"]]
                    if not fits:
                        continue
                    pick = max(fits, key=lambda c: c["proj"])
                    bench.remove(pick)
                cf += pick["pts"]
            n += 1
            net += mine - cf
            if mine > theirs and cf <= theirs:
                won += 1
            elif mine <= theirs and cf > theirs:
                lost += 1
        return {"weeks": n, "net": round(net, 2), "perWeek": round(net / n, 2) if n else None, "wonBy": won, "lostBy": lost}

    out = []
    for t in trades:
        wk = t["sp"]
        sides = []
        for tid in sorted({m["to"] for m in t["moves"]} | {m["from"] for m in t["moves"]}):
            if tid not in key_of:
                continue
            got_ids = {m["pid"] for m in t["moves"] if m["to"] == tid}
            gave_ids = [m["pid"] for m in t["moves"] if m["from"] == tid]
            got = [{**player(p, wk, tid), "from": key_of.get(next(m["from"] for m in t["moves"] if m["pid"] == p and m["to"] == tid))} for p in got_ids]
            gave = []
            for p in gave_ids:
                dest = next(m["to"] for m in t["moves"] if m["pid"] == p and m["from"] == tid)
                gave.append({**player(p, wk, dest), "to": key_of.get(dest)})
            sides.append({"m": key_of[tid], "got": got, "gave": gave,
                          "started": round(sum(g["started"] or 0 for g in got), 2) if rosters else None,
                          "after": round(sum(g["ptsAfter"] for g in got), 2),
                          "ppgBefore": ppg(tid, 1, wk - 1), "ppgAfter": ppg(tid, wk, reg_last),
                          "cf": without_trade(tid, wk, got_ids, gave_ids)})
        # Minor: nobody in the deal started more than MINOR_TRADE_STARTS weeks for his new team
        most_starts = max([g["starts"] or 0 for s in sides for g in s["got"]] or [0])
        if len(sides) >= 2:
            out.append({"wk": wk, "date": t["date"], "sides": sides, "minor": bool(rosters) and most_starts <= MINOR_TRADE_STARTS, "pending": t.get("pending", False) or wk > last_sp})
    return out


def parse_entry(entry: dict, sp: int) -> dict:
    pool = entry.get("playerPoolEntry") or {}
    player = pool.get("player") or {}
    actual, proj, played = None, 0.0, False
    for s in player.get("stats", []):
        if s.get("scoringPeriodId") != sp:
            continue
        if s.get("statSourceId") == 0:
            actual = s.get("appliedTotal", 0.0)
            played = bool(s.get("stats"))
        elif s.get("statSourceId") == 1:
            proj = s.get("appliedTotal", 0.0)
    return {
        "id": player.get("id"),
        "slot": entry.get("lineupSlotId"),
        "eligible": set(player.get("eligibleSlots", [])),
        "actual": actual if actual is not None else pool.get("appliedStatTotal", 0.0),
        "proj": proj,
        "played": played,
        "pro_team": player.get("proTeamId"),
        "pos": POSITION_NAMES.get(player.get("defaultPositionId"), "Other"),
        "acq": entry.get("acquisitionType"),  # DRAFT, ADD or TRADE
        "acqDate": entry.get("acquisitionDate"),
    }


# ---------- One season ----------

def lineup_metrics(api: Espn, year: int, data: dict, counted: set, complete: bool) -> dict | None:
    """Start/sit accuracy and mid-game injuries per ESPN team ID, from weekly box scores."""
    status = data.get("status", {})
    sched = data["settings"]["scheduleSettings"]
    reg = sched.get("matchupPeriodCount") or 0
    periods = sched.get("matchupPeriods") or {str(i): [i] for i in range(1, reg + 1)}
    last_done = status.get("finalScoringPeriod", 99) if complete else status.get("latestScoringPeriod", 1) - 1

    try:
        byes = api.bye_weeks(year)
    except Exception as e:  # noqa: BLE001
        log(f"  couldn't load bye weeks ({e}); byes may be miscounted as injuries")
        byes = {}

    rosters: dict[int, dict[int, list]] = defaultdict(dict)  # scoring period -> team -> entries
    counted_sp = set()  # (scoring period, team) pairs that belong to a counted game
    for mp_str, sps in sorted(periods.items(), key=lambda kv: int(kv[0])):
        mp = int(mp_str)
        teams_here = {tid for (m, tid) in counted if m == mp}
        if not teams_here:
            continue
        for sp in sps:
            if sp > last_done:
                continue
            resp = api.league(year, ["mMatchupScore", "mBoxscore"], {"scoringPeriodId": sp},
                              {"schedule": {"filterMatchupPeriodIds": {"value": [mp]}}}) or {}
            for m in resp.get("schedule", []):
                if m.get("matchupPeriodId") != mp:
                    continue
                for side in ("home", "away"):
                    t = m.get(side) or {}
                    roster = t.get("rosterForCurrentScoringPeriod") or {}
                    if "teamId" not in t or not roster.get("entries"):
                        continue
                    rosters[sp][t["teamId"]] = [parse_entry(e, sp) for e in roster["entries"]]
                    if t["teamId"] in teams_here:
                        counted_sp.add((sp, t["teamId"]))
    if not rosters:
        return None

    ss_c, ss_t, inj = defaultdict(int), defaultdict(int), defaultdict(int)
    pos_pts = defaultdict(lambda: defaultdict(float))  # team -> position -> starting-lineup points
    for sp, by_team in rosters.items():
        next_week = {e["id"]: e for entries in rosters.get(sp + 1, {}).values() for e in entries}
        for tid, entries in by_team.items():
            if (sp, tid) not in counted_sp:
                continue
            starters = [e for e in entries if e["slot"] not in NON_STARTER_SLOTS]
            bench = [e for e in entries if e["slot"] == BENCH_SLOT]
            for s in starters:
                pos_pts[tid][s["pos"]] += s["actual"]
                ss_t[tid] += 1
                if not any(b["actual"] > s["actual"] and s["slot"] in b["eligible"] for b in bench):
                    ss_c[tid] += 1
                if s["played"] and s["proj"] >= INJURY_MIN_PROJECTION and s["actual"] < INJURY_SHARE * s["proj"]:
                    after = next_week.get(s["id"])
                    if after and (after["slot"] == IR_SLOT
                                  or (not after["played"] and byes.get(after["pro_team"]) != sp + 1)):
                        inj[tid] += 1
    return {"ssC": ss_c, "ssT": ss_t, "inj": inj, "rosters": rosters,
            "posPts": {tid: {p: round(v, 2) for p, v in by.items()} for tid, by in pos_pts.items()}}


def process_season(api: Espn, year: int, box_scores: bool) -> dict | None:
    data = api.league(year, ["mTeam", "mMatchup", "mSettings", "mDraftDetail", "mStandings"])
    if not data or not data.get("teams"):
        log(f"{year}: no league data, skipping")
        return None

    sched = (data.get("settings") or {}).get("scheduleSettings", {})
    reg_periods = sched.get("matchupPeriodCount") or 0
    playoff_teams = sched.get("playoffTeamCount") or 0
    teams = data["teams"]
    size = len(teams)
    names = member_names(data)
    key_of = owner_keys(teams)
    final_rank = {t["id"]: t.get("rankCalculatedFinal") or t.get("rankFinal") or 0 for t in teams}
    complete = all(r > 0 for r in final_rank.values())

    lines = {}
    for t in teams:
        k = key_of[t["id"]]
        finish = final_rank[t["id"]] if complete else None
        seed = t.get("playoffSeed")
        lines[k] = {
            "manager": names.get(k) or team_name(t), "team": team_name(t),
            "w": 0, "l": 0, "t": 0, "pf": 0.0, "xw": 0.0, "g": 0,
            "seed": seed, "finish": finish,
            "playoffs": (seed is not None and seed <= playoff_teams) if complete and playoff_teams else None,
            "last": (finish == size) if finish else None,
            "champ": (finish == 1) if finish else None,
            "inj": None, "ssC": None, "ssT": None, "r1Bust": None, "lateHits": None, "posPts": None,
        }

    # Matchups: records, head-to-head games, and all-play expected wins
    # Championship bracket only: both teams must have a playoff seed and still be alive.
    # ESPN doesn't always label consolation games, so track eliminations ourselves.
    alive = {t["id"] for t in teams if t.get("playoffSeed") and t["playoffSeed"] <= playoff_teams}
    eliminated_after = {}  # team -> matchup period they were knocked out in

    games, weekly, counted, tiers = [], defaultdict(dict), set(), defaultdict(int)
    results = defaultdict(dict)  # team -> regular-season matchup period -> 1 win, 0 loss, 0.5 tie
    week_scores = {}  # (regular-season week, team) -> (team score, opponent score)
    for m in sorted(data.get("schedule", []), key=lambda m: m.get("matchupPeriodId", 0)):
        home, away = m.get("home"), m.get("away")
        if not home or not away or m.get("winner") == "UNDECIDED":
            continue
        mp = m["matchupPeriodId"]
        tier = str(m.get("playoffTierType") or "NONE")
        regular = mp <= reg_periods if reg_periods else tier == "NONE"
        if not regular:
            tiers[tier] += 1
            if any(s in tier.upper() for s in SKIPPED_PLAYOFF_TIERS):
                continue
            a, b = home["teamId"], away["teamId"]
            if not all(t in alive and eliminated_after.get(t, mp) >= mp for t in (a, b)):
                continue
        a, b = home["teamId"], away["teamId"]
        sa, sb = round(home.get("totalPoints", 0.0), 2), round(away.get("totalPoints", 0.0), 2)
        if not regular:  # ESPN's winner field already applies the league's playoff tiebreaker
            loser = b if m.get("winner") == "HOME" else a if m.get("winner") == "AWAY" else (b if sa > sb else a)
            eliminated_after.setdefault(loser, mp)
        games.append({"wk": mp, "a": key_of[a], "b": key_of[b], "sa": sa, "sb": sb, "po": not regular})
        counted.update({(mp, a), (mp, b)})
        if regular:
            for tid, mine, theirs in ((a, sa, sb), (b, sb, sa)):
                ln = lines[key_of[tid]]
                ln["g"] += 1
                ln["pf"] = round(ln["pf"] + mine, 2)
                ln["w" if mine > theirs else "l" if mine < theirs else "t"] += 1
                weekly[mp][tid] = mine
                results[tid][mp] = 1 if mine > theirs else 0 if mine < theirs else 0.5
                week_scores[(mp, tid)] = (mine, theirs)
    for scores in weekly.values():
        for tid, pts in scores.items():
            others = [p for o, p in scores.items() if o != tid]
            if others:
                beat = sum(p < pts for p in others) + 0.5 * sum(p == pts for p in others)
                lines[key_of[tid]]["xw"] += beat / len(others)
    for ln in lines.values():
        ln["xw"] = round(ln["xw"], 3)
    log(f"  {year}: {len(games)} games counted, {sum(g['po'] for g in games)} playoff; "
        f"playoff matchups by tier: {dict(tiers) or 'none found'}")

    status = data.get("status") or {}
    last_sp = (status.get("finalScoringPeriod") if complete else (status.get("latestScoringPeriod") or 1) - 1) or 17

    # Weekly lineups: start/sit and mid-game injuries (also the source for trades)
    did_box, rosters = False, None
    if box_scores and year >= BOX_SCORES_FROM:
        try:
            lm = lineup_metrics(api, year, data, counted, complete)
        except Exception as e:  # noqa: BLE001
            log(f"  {year}: couldn't load box scores ({e}); skipping start/sit and injuries")
            lm = None
        if lm:
            did_box, rosters = True, lm["rosters"]
            for tid, k in key_of.items():
                lines[k]["ssC"], lines[k]["ssT"], lines[k]["inj"] = lm["ssC"][tid], lm["ssT"][tid], lm["inj"][tid]
                lines[k]["posPts"] = lm["posPts"].get(tid)

    trade_errors = []
    # Trades. This season: the league's Recent Activity feed (the real list). Past seasons: week-to-week roster
    # changes between two teams, minus anything ESPN records as a pickup. That's only trusted when ESPN's
    # records cover every team's pickups and the method finds exactly this account's own known trades.
    feed, diag = [], {}
    if year >= 2018:
        try:
            feed, diag = completed_trades(api, year, key_of, last_sp)
        except Exception as e:  # noqa: BLE001
            log(f"  {year}: couldn't load the activity feed ({e})")
            diag = {"error": str(e)[:200]}
    guess = []
    if not feed and rosters:
        try:
            hist = transaction_history(api, year, sorted(rosters))
            guess = inferred_trades(rosters, hist["adds"])
            own = hist["ownTrades"]
            mine = set.intersection(*[{f for _, f, _ in tr} | {to for _, _, to in tr} for tr in own]) if own else set()
            guess_moves = {(m["pid"], m["to"]) for t in guess for m in t["moves"]}
            guess_mine = [t for t in guess if mine & ({m["to"] for m in t["moves"]} | {m["from"] for m in t["moves"]})]
            matched = sum(1 for tr in own if all((p, to) in guess_moves for p, _, to in tr))
            coverage = len(hist["teamsWithAdds"] & set(key_of))
            trusted = coverage >= len(key_of) - 1 and matched == len(own) and len(guess_mine) == len(own)
            diag.update({"source": "rosters", "inferredTrades": len(guess), "teamsWithPickups": coverage,
                         "pickups": len(hist["adds"]), "ownTrades": len(own), "ownFound": matched,
                         "inferredForOwnTeam": len(guess_mine), "trusted": trusted, "transactionKinds": hist["kinds"]})
            if not trusted:
                guess = []
        except Exception:  # noqa: BLE001
            guess = []
            trade_errors.append(traceback.format_exc(limit=4)[-1500:])
    else:
        diag["source"] = "activity" if feed else "none"
    trades = feed or guess
    log(f"  {year}: trades {diag}")

    # Player points: season totals for the draft, week by week for trades
    picks = (data.get("draftDetail") or {}).get("picks") or []
    if not picks:
        log(f"  {year}: ESPN returned no draft picks; skipping draft metrics")
    stats = None
    if picks or trades:
        try:
            stats = player_stats(api, year, {p["playerId"] for p in picks} | {m["pid"] for t in trades for m in t["moves"]})
        except Exception as e:  # noqa: BLE001
            log(f"  {year}: couldn't load player stats ({e}); skipping draft and trade metrics")

    # Draft: first-round busts and late-round gems (finished seasons only)
    if picks and complete and stats:
        season_rank = position_ranks(stats, lambda p: stats[p]["total"])
        for ln in lines.values():
            ln["r1Bust"], ln["lateHits"] = 0, 0
        for p in picks:
            k, s = key_of.get(p.get("teamId")), stats.get(p["playerId"])
            if k is None or (s and s["pos"] in DRAFT_SKIP_POSITIONS):
                continue
            rank = season_rank.get(p["playerId"])  # not in the pool at all = far outside the top 20
            if p.get("roundId") == 1 and (rank is None or rank > TOP_AT_POSITION):
                lines[k]["r1Bust"] += 1
            elif p.get("roundId", 0) >= LATE_ROUND and rank is not None and rank <= GEM_TOP.get(s["pos"], 0):
                lines[k]["lateHits"] += 1

    # Every pick, for the Draft tab. During the season, ranks and points are to date and the pick is marked live.
    finish_rank = position_ranks(stats, lambda p: stats[p]["total"]) if stats else {}
    draft = []
    for p in picks:
        k, s = key_of.get(p.get("teamId")), (stats or {}).get(p["playerId"], {})
        if k is None:
            continue
        draft.append({"m": k, "r": p.get("roundId"), "p": p.get("overallPickNumber") or p.get("id"),
                      "pid": p["playerId"], "name": s.get("name") or f"Player {p['playerId']}",
                      "pos": POSITION_NAMES.get(s.get("pos"), "?"), "rank": finish_rank.get(p["playerId"]),
                      "pts": round(s["total"], 1) if s.get("total") is not None else None,
                      "keeper": bool(p.get("keeper")), "live": not complete})

    try:
        trade_report = analyze_trades(trades, stats, key_of, rosters, week_scores, reg_periods, last_sp) if trades and stats else []
    except Exception:  # noqa: BLE001  (a bug here shouldn't sink the whole update; the error is saved for review)
        trade_report = []
        trade_errors.append(traceback.format_exc(limit=4)[-1500:])
    if trade_errors:
        log(f"  {year}: trade analysis failed:\n" + "\n".join(trade_errors))
        diag["errors"] = trade_errors

    return {"v": CACHE_VERSION, "year": year, "size": size, "complete": complete, "boxScores": did_box,
            "playoffTeams": playoff_teams, "name": (data.get("settings") or {}).get("name"),
            "lines": lines, "games": games, "trades": trade_report, "tradeCheck": diag, "draft": draft}


# ---------- Whole league ----------

def person_aliases(results: list[dict]) -> dict[str, str]:
    """Map every ESPN owner key to one key per person, merging the accounts listed in SAME_PERSON."""
    group_of = {name.strip().lower(): n for n, names in enumerate(SAME_PERSON) for name in names}
    canonical, alias = {}, {}
    for r in results:
        seen_this_season = set()
        for k, ln in r["lines"].items():
            group = group_of.get((ln["manager"] or "").strip().lower())
            target = canonical.setdefault(group, k) if group is not None else k
            if target in seen_this_season:  # two of these accounts in one season can't be the same person
                log(f"  {r['year']}: '{ln['manager']}' appears twice; keeping those teams separate")
                target = k
            alias.setdefault(k, target)
            seen_this_season.add(alias[k])
    return alias


def assemble(league_id: int, results: list[dict]) -> dict:
    alias = person_aliases(results)
    order, info = [], {}
    for r in results:  # oldest to newest, so the latest name and team name win
        for k, ln in r["lines"].items():
            k = alias[k]
            if k not in info:
                order.append(k)
            info[k] = {"name": ln["manager"], "team": ln["team"]}
    idx = {k: i for i, k in enumerate(order)}
    ix = lambda k: idx[alias.get(k, k)]  # noqa: E731

    seasons, sizes, playoff_teams, games, trades, draft = {}, {}, {}, [], [], []
    for r in results:
        row = [None] * len(order)
        for k, ln in r["lines"].items():
            row[ix(k)] = {f: ln[f] for f in LINE_FIELDS}
        seasons[str(r["year"])] = row
        sizes[str(r["year"])] = r["size"]
        playoff_teams[str(r["year"])] = r.get("playoffTeams")
        games += [{"y": r["year"], "wk": g["wk"], "a": ix(g["a"]), "b": ix(g["b"]),
                   "sa": g["sa"], "sb": g["sb"], "po": g["po"]} for g in r["games"]]
        draft += [{**d, "y": r["year"], "m": ix(d["m"])} for d in r.get("draft", [])]
        for t in r.get("trades", []):
            trades.append({"y": r["year"], "wk": t["wk"], "date": t["date"], "sides": [
                {**s, "m": ix(s["m"]), "got": [{**g, "from": ix(g["from"]) if g["from"] else None} for g in s["got"]],
                 "gave": [{**g, "to": ix(g["to"]) if g.get("to") else None} for g in s.get("gave", [])]}
                for s in t["sides"]]})

    box_years = [r["year"] for r in results if r["boxScores"]]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "league": {"id": league_id, "name": next((r["name"] for r in reversed(results) if r["name"]), f"League {league_id}")},
        "managers": [info[k] for k in order],
        "years": [r["year"] for r in results],
        "sizes": sizes,
        "playoffTeams": playoff_teams,
        "boxScoresFrom": min(box_years) if box_years else None,
        "seasons": seasons,
        "games": games,
        "trades": trades,
        "draft": draft,
        "tradeCheck": {str(r["year"]): r.get("tradeCheck") for r in results},
        "previousLeagues": [{"id": pid, "first": a, "last": b} for pid, a, b in PREVIOUS_LEAGUES
                            if any(r.get("leagueId") == pid for r in results)],
    }


def previous_league_champions(api: Espn, first: int, last: int) -> dict:
    """Champion and runner-up for every season an older league existed, for the page's title counts."""
    seasons, name = [], None
    for year in range(first, last + 1):
        try:
            data = api.league(year, ["mTeam", "mSettings", "mStandings"])
        except SystemExit as e:  # refused for this season; try the rest
            log(f"{year}: {e}")
            continue
        if not data or not data.get("teams"):
            continue
        names = member_names(data)
        key_of = owner_keys(data["teams"])
        rank = lambda t: t.get("rankCalculatedFinal") or t.get("rankFinal") or 0  # noqa: E731
        who = lambda t: {"name": names.get(key_of[t["id"]]) or team_name(t), "team": team_name(t)}  # noqa: E731
        champ = next((t for t in data["teams"] if rank(t) == 1), None)
        runner = next((t for t in data["teams"] if rank(t) == 2), None)
        name = (data.get("settings") or {}).get("name") or name
        seasons.append({"year": year, "size": len(data["teams"]),
                        "champion": who(champ) if champ else None, "runnerUp": who(runner) if runner else None})
        log(f"{year}: champion {seasons[-1]['champion']}")
    return {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "league": {"id": api.league_id, "name": name}, "seasons": seasons}


def current_season() -> int:
    today = dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Build league.json for the League Record Book page.")
    ap.add_argument("--league-id", default=os.environ.get("ESPN_LEAGUE_ID"), help="ESPN league ID (or ESPN_LEAGUE_ID)")
    ap.add_argument("--start", type=int, help="First season (default: the league's first season)")
    ap.add_argument("--end", type=int, help="Last season (default: current season)")
    ap.add_argument("--out", default="docs/league.json", help="Where to write the page data")
    ap.add_argument("--cache", default="data/seasons", help="Folder for finished-season cache")
    ap.add_argument("--refresh", action="store_true", help="Ignore cached seasons and refetch")
    ap.add_argument("--no-box-scores", action="store_true", help="Skip weekly lineups (faster; no start/sit or injury data)")
    ap.add_argument("--champions-only", action="store_true",
                    help="Only list each season's champion and runner-up (for an older league), written to --out")
    args = ap.parse_args()

    if not args.league_id:
        sys.exit("Pass --league-id or set ESPN_LEAGUE_ID.")
    league_id = int(args.league_id)
    s2, swid = os.environ.get("ESPN_S2", "").strip(), os.environ.get("ESPN_SWID", "").strip()
    log(f"Cookies: espn_s2 {'set (' + str(len(s2)) + ' chars)' if s2 else 'NOT set'}, "
        f"SWID {'set (' + str(len(swid)) + ' chars)' if swid else 'NOT set'}")
    api = Espn(league_id, s2 or None, swid or None)

    if args.champions_only:
        report = previous_league_champions(api, args.start or 2010, args.end or current_season())
        if not report["seasons"]:
            sys.exit(f"ESPN returned no seasons for league {league_id}. Your account may no longer have access to it.")
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1))
        log(f"Wrote {out} ({len(report['seasons'])} seasons)")
        return

    end = args.end or current_season()
    latest = api.league(end, ["mSettings"])
    if latest is None and not args.end:
        end -= 1
        latest = api.league(end, ["mSettings"])
    if latest is None:
        sys.exit(f"ESPN has no league {league_id} for {end}. Check the league ID.")
    start = args.start or min((latest.get("status") or {}).get("previousSeasons") or [end])
    log(f"League {league_id}: seasons {start}–{end}")

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    want_box = not args.no_box_scores

    # Earlier seasons played in a separate ESPN league come first, then this league's seasons
    plan = []
    for prev_id, first, last in PREVIOUS_LEAGUES:
        prev_api = Espn(prev_id, s2 or None, swid or None)
        plan += [(prev_api, y, cache / f"{prev_id}-{y}.json") for y in range(first, last + 1) if y < start]
    plan += [(api, y, cache / f"{y}.json") for y in range(start, end + 1)]

    results = []
    for season_api, year, path in plan:
        if path.exists() and not args.refresh:
            r = json.loads(path.read_text())
            if r.get("v") == CACHE_VERSION and (r["boxScores"] or not want_box or year < BOX_SCORES_FROM):
                log(f"{year}: from cache")
                results.append(r)
                continue
        log(f"{year}: fetching" + ("" if season_api is api else f" from previous league {season_api.league_id}"))
        try:
            r = process_season(season_api, year, want_box)
        except SystemExit as e:
            if season_api is api:
                raise
            log(f"{year}: skipped previous league season ({e})")  # don't let the old league sink the weekly update
            continue
        if not r:
            continue
        r["leagueId"] = season_api.league_id
        if r["complete"] and not (r.get("tradeCheck") or {}).get("errors"):  # retry failed seasons next run
            path.write_text(json.dumps(r, separators=(",", ":")))
        results.append(r)

    if not results:
        sys.exit("No seasons could be loaded.")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(assemble(league_id, results), separators=(",", ":")))
    log(f"Wrote {out} ({len(results)} seasons)")


if __name__ == "__main__":
    main()
