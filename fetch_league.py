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
from collections import defaultdict
from pathlib import Path

import requests

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

BENCH_SLOT = 20
IR_SLOT = 21
NON_STARTER_SLOTS = {BENCH_SLOT, IR_SLOT}
PLAYER_SLOT_IDS = [0, 2, 4, 6, 16, 17]  # QB, RB, WR, TE, D/ST, K

TOP_AT_POSITION = 20        # "top 20 at position"
LATE_ROUND = 4              # drafted outside the top 3 rounds
BOX_SCORES_FROM = 2019      # ESPN only serves lineups/box scores from 2019 on
INJURY_SHARE = 0.4          # starter scored under 40% of projection...
INJURY_MIN_PROJECTION = 5.0 # ...was projected for at least this many points...
                            # ...and missed the next week (IR slot or no stats, not a bye)
COUNTED_PLAYOFF_TIERS = {"WINNERS_BRACKET"}  # consolation games don't count toward records

LINE_FIELDS = ["w", "l", "t", "pf", "xw", "g", "seed", "finish", "playoffs", "last",
               "champ", "inj", "ssC", "ssT", "r1Bust", "lateHits"]


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


def season_total(player: dict, year: int) -> float:
    for s in player.get("stats", []):
        if s.get("seasonId") == year and s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0:
            return s.get("appliedTotal", 0.0)
    return 0.0


def position_ranks(api: Espn, year: int, drafted: set[int]) -> dict[int, int]:
    """Season-long fantasy points rank within each player's position, in this league's scoring."""
    filt = {"players": {
        "filterSlotIds": {"value": PLAYER_SLOT_IDS},
        "filterStatsForSourceIds": {"value": [0]},
        "filterStatsForSplitTypeIds": {"value": [0]},
        "sortAppliedStatTotal": {"sortAsc": False, "sortPriority": 1, "value": f"00{year}"},
        "limit": 1500,
    }}
    data = api.league(year, ["kona_player_info"], filt=filt) or {}
    players = {p["id"]: p.get("player", p) for p in data.get("players", [])}
    missing = [pid for pid in drafted if pid not in players]
    if missing:
        more = api.league(year, ["kona_player_info"],
                          filt={"players": {"filterIds": {"value": missing}, "limit": len(missing)}}) or {}
        players.update({p["id"]: p.get("player", p) for p in more.get("players", [])})
    if not players:
        raise RuntimeError("ESPN returned no player stats")

    by_pos = defaultdict(list)
    for pid, pl in players.items():
        by_pos[pl.get("defaultPositionId")].append((season_total(pl, year), pid))
    ranks = {}
    for entries in by_pos.values():
        entries.sort(reverse=True)
        for n, (_, pid) in enumerate(entries, 1):
            ranks[pid] = n
    return ranks


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
    for sp, by_team in rosters.items():
        next_week = {e["id"]: e for entries in rosters.get(sp + 1, {}).values() for e in entries}
        for tid, entries in by_team.items():
            if (sp, tid) not in counted_sp:
                continue
            starters = [e for e in entries if e["slot"] not in NON_STARTER_SLOTS]
            bench = [e for e in entries if e["slot"] == BENCH_SLOT]
            for s in starters:
                ss_t[tid] += 1
                if not any(b["actual"] > s["actual"] and s["slot"] in b["eligible"] for b in bench):
                    ss_c[tid] += 1
                if s["played"] and s["proj"] >= INJURY_MIN_PROJECTION and s["actual"] < INJURY_SHARE * s["proj"]:
                    after = next_week.get(s["id"])
                    if after and (after["slot"] == IR_SLOT
                                  or (not after["played"] and byes.get(after["pro_team"]) != sp + 1)):
                        inj[tid] += 1
    return {"ssC": ss_c, "ssT": ss_t, "inj": inj}


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
            "inj": None, "ssC": None, "ssT": None, "r1Bust": None, "lateHits": None,
        }

    # Matchups: records, head-to-head games, and all-play expected wins
    games, weekly, counted = [], defaultdict(dict), set()
    for m in data.get("schedule", []):
        home, away = m.get("home"), m.get("away")
        if not home or not away or m.get("winner") == "UNDECIDED":
            continue
        mp = m["matchupPeriodId"]
        regular = mp <= reg_periods if reg_periods else m.get("playoffTierType", "NONE") == "NONE"
        if not regular and m.get("playoffTierType") not in COUNTED_PLAYOFF_TIERS:
            continue
        a, b = home["teamId"], away["teamId"]
        sa, sb = round(home.get("totalPoints", 0.0), 2), round(away.get("totalPoints", 0.0), 2)
        games.append({"wk": mp, "a": key_of[a], "b": key_of[b], "sa": sa, "sb": sb, "po": not regular})
        counted.update({(mp, a), (mp, b)})
        if regular:
            for tid, mine, theirs in ((a, sa, sb), (b, sb, sa)):
                ln = lines[key_of[tid]]
                ln["g"] += 1
                ln["pf"] = round(ln["pf"] + mine, 2)
                ln["w" if mine > theirs else "l" if mine < theirs else "t"] += 1
                weekly[mp][tid] = mine
    for scores in weekly.values():
        for tid, pts in scores.items():
            others = [p for o, p in scores.items() if o != tid]
            if others:
                beat = sum(p < pts for p in others) + 0.5 * sum(p == pts for p in others)
                lines[key_of[tid]]["xw"] += beat / len(others)
    for ln in lines.values():
        ln["xw"] = round(ln["xw"], 3)

    # Draft: first-round busts and late-round top-20 finishers (finished seasons only)
    picks = (data.get("draftDetail") or {}).get("picks") or []
    if picks and complete:
        try:
            ranks = position_ranks(api, year, {p["playerId"] for p in picks})
        except Exception as e:  # noqa: BLE001
            log(f"  {year}: couldn't load player season totals ({e}); skipping draft metrics")
            ranks = None
        if ranks is not None:
            for ln in lines.values():
                ln["r1Bust"], ln["lateHits"] = 0, 0
            for p in picks:
                k = key_of.get(p.get("teamId"))
                if k is None:
                    continue
                rank = ranks.get(p["playerId"])
                top = rank is not None and rank <= TOP_AT_POSITION
                if p.get("roundId") == 1 and not top:
                    lines[k]["r1Bust"] += 1
                elif p.get("roundId", 0) >= LATE_ROUND and top:
                    lines[k]["lateHits"] += 1

    # Weekly lineups: start/sit and mid-game injuries
    did_box = False
    if box_scores and year >= BOX_SCORES_FROM:
        try:
            lm = lineup_metrics(api, year, data, counted, complete)
        except Exception as e:  # noqa: BLE001
            log(f"  {year}: couldn't load box scores ({e}); skipping start/sit and injuries")
            lm = None
        if lm:
            did_box = True
            for tid, k in key_of.items():
                lines[k]["ssC"], lines[k]["ssT"], lines[k]["inj"] = lm["ssC"][tid], lm["ssT"][tid], lm["inj"][tid]

    return {"year": year, "size": size, "complete": complete, "boxScores": did_box,
            "name": (data.get("settings") or {}).get("name"), "lines": lines, "games": games}


# ---------- Whole league ----------

def assemble(league_id: int, results: list[dict]) -> dict:
    order, info = [], {}
    for r in results:  # oldest to newest, so the latest name and team name win
        for k, ln in r["lines"].items():
            if k not in info:
                order.append(k)
            info[k] = {"name": ln["manager"], "team": ln["team"]}
    idx = {k: i for i, k in enumerate(order)}

    seasons, sizes, games = {}, {}, []
    for r in results:
        row = [None] * len(order)
        for k, ln in r["lines"].items():
            row[idx[k]] = {f: ln[f] for f in LINE_FIELDS}
        seasons[str(r["year"])] = row
        sizes[str(r["year"])] = r["size"]
        games += [{"y": r["year"], "wk": g["wk"], "a": idx[g["a"]], "b": idx[g["b"]],
                   "sa": g["sa"], "sb": g["sb"], "po": g["po"]} for g in r["games"]]

    box_years = [r["year"] for r in results if r["boxScores"]]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "league": {"id": league_id, "name": next((r["name"] for r in reversed(results) if r["name"]), f"League {league_id}")},
        "managers": [info[k] for k in order],
        "years": [r["year"] for r in results],
        "sizes": sizes,
        "boxScoresFrom": min(box_years) if box_years else None,
        "seasons": seasons,
        "games": games,
    }


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
    args = ap.parse_args()

    if not args.league_id:
        sys.exit("Pass --league-id or set ESPN_LEAGUE_ID.")
    league_id = int(args.league_id)
    s2, swid = os.environ.get("ESPN_S2", "").strip(), os.environ.get("ESPN_SWID", "").strip()
    log(f"Cookies: espn_s2 {'set (' + str(len(s2)) + ' chars)' if s2 else 'NOT set'}, "
        f"SWID {'set (' + str(len(swid)) + ' chars)' if swid else 'NOT set'}")
    api = Espn(league_id, s2 or None, swid or None)

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
    results = []
    for year in range(start, end + 1):
        path = cache / f"{year}.json"
        if path.exists() and not args.refresh:
            r = json.loads(path.read_text())
            if r["boxScores"] or not want_box or year < BOX_SCORES_FROM:
                log(f"{year}: from cache")
                results.append(r)
                continue
        log(f"{year}: fetching")
        r = process_season(api, year, want_box)
        if not r:
            continue
        if r["complete"]:
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
