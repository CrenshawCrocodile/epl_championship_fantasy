#!/usr/bin/env python3
"""Rebuild an FPL league tracker page from the live FPL API.

Every run recomputes the whole season from scratch. There is no local state
file, so nothing can drift: if the API says it, the page says it, and if the
API and the page disagree the build fails instead of publishing.

  python3 build_tracker.py                       # Championship, into ./out
  python3 build_tracker.py --config league-one   # any league in leagues/*.json
  python3 build_tracker.py --previous out/previous.json   # keep old recap prose
  python3 build_tracker.py --recap '{"gw":4,"bullets":["..."]}'

Outputs (into --outdir):
  data.json     the full payload embedded in the page
  facts.json    structured facts per gameweek, for writing recap prose
  tracker.html  the finished page, ready to publish as an Artifact
"""

import argparse
import datetime
import json
import os
import sys

from fetch_fpl import FPLClient, FPLError, finalized_gameweeks

# ---------------------------------------------------------------------------
# League-specific config
# ---------------------------------------------------------------------------

# One JSON file per league in leagues/. These module-level names are filled in
# by load_config() before anything else runs; the values here are only the
# shape, not a default league.
DEFAULT_CONFIG = "championship"
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leagues")

LEAGUE_ID = None
PAGE_TITLE = None
BUY_IN = None
PAYOUT_PCTS = None
CONTACT_EMAIL = None

# The commissioner's standings row gets badged on the page. Matched on entry
# name so a manager renaming their team does not silently drop the badge.
COMMISSIONER_NAME = None
COMMISSIONER_TEAM = None

SEASON_LAST_GW = 38
TOP_OWNED_SHOWN = 3

# Club colours for the little team pills. Keyed on the API's short_name so a
# promoted or relegated club only needs one line added here.
TEAM_COLORS = {
    "ARS": "#EF0107", "AVL": "#95BFE5", "BOU": "#DA291C", "BRE": "#E30613",
    "BHA": "#0057B8", "BUR": "#6C1D45", "CHE": "#034694", "COV": "#78D0F3",
    "CRY": "#1B458F", "EVE": "#003399", "FUL": "#000000", "HUL": "#F5A12D",
    "IPS": "#3A64A3", "LEE": "#FFCD00", "LEI": "#003090", "LIV": "#C8102E",
    "LUT": "#F78F1E", "MCI": "#6CABDD", "MUN": "#DA291C", "NEW": "#241F20",
    "NFO": "#DD0000", "SHU": "#EE2737", "SOU": "#D71920", "SUN": "#EB172B",
    "TOT": "#132257", "WHU": "#7A263A", "WOL": "#FDB913",
}
FALLBACK_COLOR = "#37003C"

CHIP_LABELS = {
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
    "freehit": "Free Hit",
    "wildcard": "Wildcard",
    "manager": "Assistant Manager",
}

PLACE_WORDS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}

TEMPLATE_PLACEHOLDER = "__TRACKER_DATA__"
TITLE_PLACEHOLDER = "__PAGE_TITLE__"


class BuildError(RuntimeError):
    """A sanity check failed. The build stops; nothing gets published."""


def load_config(name):
    """Read leagues/<name>.json (or an explicit path) into the module config.

    Returns the raw dict so main() can pick up outdir and recap_dir."""
    global LEAGUE_ID, PAGE_TITLE, BUY_IN, PAYOUT_PCTS, CONTACT_EMAIL
    global COMMISSIONER_NAME, COMMISSIONER_TEAM
    path = name if os.path.isfile(name) else os.path.join(CONFIG_DIR, name + ".json")
    if not os.path.isfile(path):
        raise BuildError("no league config at %s" % path)
    with open(path) as fh:
        cfg = json.load(fh)
    try:
        LEAGUE_ID = int(cfg["league_id"])
        PAGE_TITLE = cfg["page_title"]
        BUY_IN = cfg["buy_in"]
        # JSON keys are strings; places are ints everywhere else.
        PAYOUT_PCTS = {int(k): v for k, v in cfg["payout_pcts"].items()}
        CONTACT_EMAIL = cfg["contact_email"]
        COMMISSIONER_NAME = cfg["commissioner_name"]
        COMMISSIONER_TEAM = cfg["commissioner_team"]
    except KeyError as exc:
        raise BuildError("league config %s is missing %s" % (path, exc))
    if abs(sum(PAYOUT_PCTS.values()) - 1.0) > 1e-9:
        raise BuildError("payout_pcts in %s do not sum to 100%%" % path)
    return cfg


def money(amount):
    """Whole-dollar formatting. Payout percentages of a whole-dollar pot are
    exact here, but round anyway so a future odd buy-in cannot print cents."""
    return "$%s" % format(int(round(amount)), ",d")


def chip_label(code):
    return CHIP_LABELS.get(code, code)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def gather(client, league_id):
    """Pull everything the page needs, in one sequential pass."""
    boot = client.bootstrap()
    weeks = finalized_gameweeks(boot)
    if not weeks:
        raise BuildError(
            "no gameweek is both finished and data_checked yet; nothing to publish"
        )

    league = client.league_standings(league_id)
    rows = league["standings"]["results"]
    if not rows:
        raise BuildError("league %s returned an empty standings table" % league_id)

    managers = []
    for i, row in enumerate(rows, 1):
        entry_id = row["entry"]
        client._log(
            "fetch manager %d/%d: %s (%s)" % (i, len(rows), row["entry_name"], entry_id)
        )
        managers.append({
            "row": row,
            "entry": client.entry(entry_id),
            "history": client.entry_history(entry_id),
            "transfers": client.entry_transfers(entry_id),
            "picks": {gw: client.entry_picks(entry_id, gw) for gw in weeks},
        })

    live = {gw: client.live(gw) for gw in weeks}
    return boot, league, managers, live, weeks


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def index_players(boot):
    """element id -> display fields used by the page."""
    types = {t["id"]: t["singular_name_short"] for t in boot["element_types"]}
    teams = {t["id"]: t for t in boot["teams"]}
    out = {}
    for el in boot["elements"]:
        club = teams[el["team"]]
        out[el["id"]] = {
            "name": el["web_name"],
            "pos": types[el["element_type"]],
            "team": club["name"],
            "color": TEAM_COLORS.get(club["short_name"], FALLBACK_COLOR),
        }
    return out


def live_points(live_payload):
    """element id -> raw points scored in that gameweek (before any multiplier)."""
    return {e["id"]: e["stats"]["total_points"] for e in live_payload["elements"]}


def competition_ranks(totals):
    """Standard competition ranking (1, 2, 2, 4) over {entry: total_points}.

    Used for past gameweeks only. The current week always takes FPL's own
    ranking instead, because FPL breaks ties on a hidden sort key we cannot
    reproduce from the public endpoints.
    """
    ordered = sorted(totals.items(), key=lambda kv: -kv[1])
    ranks = {}
    for i, (entry_id, total) in enumerate(ordered):
        if i and total == ordered[i - 1][1]:
            ranks[entry_id] = ranks[ordered[i - 1][0]]
        else:
            ranks[entry_id] = i + 1
    return ranks


def build_payload(boot, league, managers, live, weeks, previous, recap_override,
                  recap_dir, warnings):
    players = index_players(boot)
    gw_points = {gw: live_points(live[gw]) for gw in weeks}
    through = max(weeks)
    n_managers = len(managers)

    # --- per-manager season series ------------------------------------------
    series = {}
    for m in managers:
        entry_id = m["row"]["entry"]
        by_event = {h["event"]: h for h in m["history"]["current"]}
        missing = [gw for gw in weeks if gw not in by_event]
        if missing:
            raise BuildError(
                "%s (entry %s) has no history row for finalized gameweek(s) %s"
                % (m["row"]["entry_name"], entry_id, missing)
            )
        series[entry_id] = by_event

    # --- chips played, indexed per manager and gameweek ---------------------
    # Needed before the transfer reconciliation below: FPL deliberately reports
    # zero transfers for a week a Wildcard or Free Hit was active, because those
    # moves are unlimited and free. The transfers endpoint still lists them.
    FREE_TRANSFER_CHIPS = ("wildcard", "freehit")
    chips_by_gw = {}
    for m in managers:
        entry_id = m["row"]["entry"]
        chips_by_gw[entry_id] = {c["event"]: c["name"]
                                 for c in m["history"].get("chips", [])}

    # --- transfer counts, cross-checked against the entry endpoint ----------
    transfers_by_gw = {}
    for m in managers:
        entry_id = m["row"]["entry"]
        counted = {}
        for t in m["transfers"]:
            counted[t["event"]] = counted.get(t["event"], 0) + 1
        transfers_by_gw[entry_id] = counted

        for gw in weeks:
            chip = chips_by_gw[entry_id].get(gw)
            listed = counted.get(gw, 0)
            recorded = series[entry_id][gw]["event_transfers"]
            if chip in FREE_TRANSFER_CHIPS:
                # FPL zeroes the counter on these weeks; only assert that.
                if recorded != 0:
                    raise BuildError(
                        "%s (entry %s) played %s in GW%d but FPL still counted %d "
                        "transfers" % (m["row"]["entry_name"], entry_id, chip, gw,
                                       recorded)
                    )
                continue
            if recorded != listed:
                raise BuildError(
                    "GW%d transfer mismatch for %s (entry %s): history says %d, "
                    "transfers endpoint lists %d"
                    % (gw, m["row"]["entry_name"], entry_id, recorded, listed)
                )

        # The entry endpoint's counter uses the same convention as history, so
        # the two must land on the same number.
        from_history = sum(series[entry_id][gw]["event_transfers"] for gw in weeks)
        declared = m["entry"].get("last_deadline_total_transfers")
        if declared is not None and declared != from_history:
            # The entry endpoint runs to the *next* deadline, so it may lead the
            # finalized weeks. Only a shortfall means the numbers really disagree.
            if declared < from_history:
                raise BuildError(
                    "transfer mismatch for %s (entry %s): entry endpoint says %d, "
                    "finalized gameweeks total %d"
                    % (m["row"]["entry_name"], entry_id, declared, from_history)
                )
            warnings.append(
                "entry %s reports %d counted transfers vs %d through GW%d "
                "(transfers already made for an upcoming week)"
                % (entry_id, declared, from_history, through)
            )

    # --- ranks per week -----------------------------------------------------
    computed = {}
    for gw in weeks:
        computed[gw] = competition_ranks(
            {m["row"]["entry"]: series[m["row"]["entry"]][gw]["total_points"]
             for m in managers}
        )

    api_rank = {r["entry"]: r["rank"] for r in league["standings"]["results"]}
    api_last = {r["entry"]: r["last_rank"] for r in league["standings"]["results"]}
    api_total = {r["entry"]: r["total"] for r in league["standings"]["results"]}
    api_sort = {r["entry"]: r["rank_sort"] for r in league["standings"]["results"]}

    # Totals are hard data on both sides; a mismatch means we are reading a
    # different season than the league table and must not publish.
    for m in managers:
        entry_id = m["row"]["entry"]
        mine = series[entry_id][through]["total_points"]
        if mine != api_total[entry_id]:
            raise BuildError(
                "total points mismatch for %s (entry %s): history says %d, "
                "league table says %d"
                % (m["row"]["entry_name"], entry_id, mine, api_total[entry_id])
            )

    # Ranks: FPL wins. We only compare so a divergence gets logged.
    for m in managers:
        entry_id = m["row"]["entry"]
        if computed[through][entry_id] != api_rank[entry_id]:
            warnings.append(
                "rank disagreement for entry %s (%s) in GW%d: computed %d, "
                "FPL says %d; using FPL"
                % (entry_id, m["row"]["entry_name"], through,
                   computed[through][entry_id], api_rank[entry_id])
            )
    ranks = dict(computed)
    ranks[through] = dict(api_rank)

    # --- captain / ownership metrics per week -------------------------------
    weekly = {}
    cumulative_pts = {}
    for gw in weeks:
        captains = {}
        owners = {}
        for m in managers:
            picks = m["picks"][gw]["picks"]
            seen = set()
            for p in picks:
                if p["element"] not in seen:
                    owners[p["element"]] = owners.get(p["element"], 0) + 1
                    seen.add(p["element"])
                if p["is_captain"]:
                    captains[p["element"]] = captains.get(p["element"], 0) + 1

        captain_total = sum(captains.values())
        if captain_total != n_managers:
            raise BuildError(
                "GW%d captain picks sum to %d but the league has %d managers"
                % (gw, captain_total, n_managers)
            )

        for el, pts in gw_points[gw].items():
            cumulative_pts[el] = cumulative_pts.get(el, 0) + pts

        top_cap = max(captains.items(), key=lambda kv: (kv[1], gw_points[gw].get(kv[0], 0)))
        top_owned = max(
            owners.keys(),
            key=lambda el: (gw_points[gw].get(el, 0), owners[el]),
        )
        most_owned = sorted(
            owners.items(),
            key=lambda kv: (-kv[1], -gw_points[gw].get(kv[0], 0), players[kv[0]]["name"]),
        )[:TOP_OWNED_SHOWN]

        weekly[gw] = {
            "captains": captains,
            "owners": owners,
            "mostCaptained": dict(
                players[top_cap[0]],
                count=top_cap[1], total=n_managers,
                pts=gw_points[gw].get(top_cap[0], 0),
            ),
            "topPlayer": dict(
                players[top_owned],
                pts=gw_points[gw].get(top_owned, 0),
                count=owners[top_owned], total=n_managers,
            ),
            "mostOwned": [
                dict(
                    players[el],
                    count=cnt, total=n_managers,
                    gwPts=gw_points[gw].get(el, 0),
                    seasonPts=sum(gw_points[w].get(el, 0) for w in weeks if w <= gw),
                )
                for el, cnt in most_owned
            ],
        }

    # --- chips --------------------------------------------------------------
    chips = []
    for m in managers:
        entry_id = m["row"]["entry"]
        for c in m["history"].get("chips", []):
            gw = c["event"]
            if gw not in weeks:
                continue  # chip played in a week that has not settled yet
            chips.append({
                "manager": m["row"]["player_name"],
                "team": m["row"]["entry_name"],
                "entry": entry_id,
                "chip": c["name"],
                "gw": gw,
                "pts": series[entry_id][gw]["points"],
            })
    chips.sort(key=lambda c: (c["chip"], c["gw"], -c["pts"]))

    # --- standings tables ---------------------------------------------------
    def standings_for(gw):
        rows = []
        for m in managers:
            entry_id = m["row"]["entry"]
            h = series[entry_id][gw]
            if gw == through:
                move = None if api_last[entry_id] in (0, None) else api_last[entry_id] - api_rank[entry_id]
            else:
                prev = weeks[weeks.index(gw) - 1] if weeks.index(gw) > 0 else None
                move = None if prev is None else ranks[prev][entry_id] - ranks[gw][entry_id]
            rows.append({
                "entry": entry_id,
                "name": m["row"]["player_name"],
                "team": m["row"]["entry_name"],
                "total": h["total_points"],
                "lastGW": h["points"],
                "transfers": sum(
                    transfers_by_gw[entry_id].get(w, 0) for w in weeks if w <= gw
                ),
                "hits": sum(
                    series[entry_id][w]["event_transfers_cost"] for w in weeks if w <= gw
                ),
                "rank": ranks[gw][entry_id],
                "move": move,
                "commissioner": (
                    m["row"]["player_name"] == COMMISSIONER_NAME
                    or m["row"]["entry_name"].lower() == COMMISSIONER_TEAM.lower()
                ),
            })
        if gw == through:
            rows.sort(key=lambda r: api_sort[r["entry"]])
        else:
            rows.sort(key=lambda r: (r["rank"], -r["total"], r["team"]))
        return rows

    tables = {gw: standings_for(gw) for gw in weeks}

    if not any(r["commissioner"] for r in tables[through]):
        raise BuildError(
            "commissioner %s / %s is not in the league standings"
            % (COMMISSIONER_NAME, COMMISSIONER_TEAM)
        )

    # --- facts, for writing recap prose -------------------------------------
    facts = build_facts(weeks, tables, weekly, chips, ranks, through, n_managers)

    # --- recap prose, by precedence -----------------------------------------
    summaries = {}
    for gw in weeks:
        summaries[gw] = resolve_recap(
            gw, previous, recap_override, recap_dir, facts[gw]
        )

    # --- payouts ------------------------------------------------------------
    pot = BUY_IN * n_managers
    paid_places = max(PAYOUT_PCTS)
    season_done = through >= SEASON_LAST_GW
    current = tables[through]
    payouts = []
    for place in sorted(PAYOUT_PCTS):
        # FPL shares a rank on a tie and then skips the next one, so a place can
        # have no row at all. Say so rather than printing a blank line.
        at_place = [r for r in current if r["rank"] == place]
        row = at_place[0] if at_place else None
        payouts.append({
            "place": PLACE_WORDS.get(place, "%dth" % place),
            "name": row["name"] if row else "—",
            "team": row["team"] if row else "vacant, tied above",
            "pts": row["total"] if row else None,
            "payout": money(pot * PAYOUT_PCTS[place]),
        })

    split_caption = " / ".join(
        "%d%%" % round(PAYOUT_PCTS[p] * 100) for p in sorted(PAYOUT_PCTS)
    )

    notes = [
        "Only gameweeks that FPL has marked both finished and data checked appear "
        "here, so a week never shows up before its bonus points have landed.",
        "The gameweek picker in the header rewinds the Standings, the Recap and the "
        "League Metrics to any completed week. Chips Played and the Payouts tab "
        "always show the latest state.",
        "Ranks for the current gameweek come straight from FPL's own league table, "
        "including how it splits ties. Earlier weeks are ranked on total points.",
        "The movement arrow next to each manager's name compares this week's rank to "
        "last week's. Gameweek 1 has no arrows because it was the first snapshot.",
        "\"Transfers\" counts every move a manager has made through the selected "
        "gameweek, including the ones made on a Wildcard or Free Hit. FPL's own "
        "transfer counter leaves those out because they are free and unlimited.",
        "\"Hits\" is the points a manager has given up to transfers, four per move "
        "beyond the free one. Wildcard and Free Hit moves never cost a hit.",
        "The whole season is recomputed from the FPL API on every update, so nothing "
        "on this page can drift out of sync with the official league table.",
        "Team names in the Recap, the Standings and Chips Played all link to that "
        "team's official FPL page for the relevant gameweek.",
    ]

    payload = {
        "leagueName": league["league"]["name"],
        "leagueId": league["league"]["id"],
        "updated": datetime.date.today().isoformat(),
        "throughGW": through,
        "managerCount": n_managers,
        "paidPlaces": paid_places,
        "payoutFinal": season_done,
        "contactEmail": CONTACT_EMAIL,
        "pot": {
            "totalPot": money(pot),
            "buyInCaption": "%s buy-in × %d" % (money(BUY_IN), n_managers),
            "participants": n_managers,
            "splitCaption": split_caption,
        },
        "payouts": payouts,
        "payoutFootnote": (
            "Final places and payouts. The season is complete through Gameweek "
            "%d." % through
            if season_done else
            "Standings through Gameweek %d. Places and payouts move every week and "
            "are not final until the season ends." % through
        ),
        "gameweeks": {
            str(gw): {
                "standings": tables[gw],
                "gwSummary": summaries[gw],
                "mostCaptained": weekly[gw]["mostCaptained"],
                "topPlayer": weekly[gw]["topPlayer"],
                "mostOwned": weekly[gw]["mostOwned"],
            }
            for gw in weeks
        },
        "chips": chips,
        "notes": notes,
    }
    return payload, facts


# ---------------------------------------------------------------------------
# Facts and recap prose
# ---------------------------------------------------------------------------

def build_facts(weeks, tables, weekly, chips, ranks, through, n_managers):
    """Structured facts per gameweek. Everything a recap bullet might cite
    lives here, so prose can be written without going back to the API."""
    facts = {}
    for i, gw in enumerate(weeks):
        rows = tables[gw]
        prev_gw = weeks[i - 1] if i else None
        by_pts = sorted(rows, key=lambda r: -r["lastGW"])
        leader = next(r for r in rows if r["rank"] == 1)
        prev_leader = None
        if prev_gw is not None:
            prev_leader = next(
                (r for r in tables[prev_gw] if r["rank"] == 1), None
            )

        movers = [r for r in rows if r["move"] is not None]
        riser = max(movers, key=lambda r: r["move"]) if movers else None
        faller = min(movers, key=lambda r: r["move"]) if movers else None

        week_chips = [c for c in chips if c["gw"] == gw]
        chip_counts = {}
        for c in week_chips:
            chip_counts.setdefault(c["chip"], []).append(c["team"])

        scores = sorted(r["lastGW"] for r in rows)
        mid = len(scores) // 2
        median = scores[mid] if len(scores) % 2 else (scores[mid - 1] + scores[mid]) / 2.0

        facts[gw] = {
            "gw": gw,
            "managers": n_managers,
            "leader": {"team": leader["team"], "manager": leader["name"],
                       "total": leader["total"], "gwPts": leader["lastGW"]},
            "leadChanged": bool(prev_leader and prev_leader["team"] != leader["team"]),
            "previousLeader": (
                {"team": prev_leader["team"], "total": prev_leader["total"],
                 "rank": next(r["rank"] for r in rows if r["team"] == prev_leader["team"])}
                if prev_leader else None
            ),
            "leadMargin": (
                leader["total"] - max(
                    [r["total"] for r in rows if r["rank"] > 1] or [leader["total"]]
                )
            ),
            "highestScore": {"team": by_pts[0]["team"], "manager": by_pts[0]["name"],
                             "pts": by_pts[0]["lastGW"], "rank": by_pts[0]["rank"]},
            "lowestScore": {"team": by_pts[-1]["team"], "manager": by_pts[-1]["name"],
                            "pts": by_pts[-1]["lastGW"], "rank": by_pts[-1]["rank"]},
            "medianScore": median,
            "biggestRiser": (
                {"team": riser["team"], "places": riser["move"], "rank": riser["rank"],
                 "total": riser["total"], "gwPts": riser["lastGW"]}
                if riser and riser["move"] > 0 else None
            ),
            "biggestFaller": (
                {"team": faller["team"], "places": faller["move"], "rank": faller["rank"],
                 "total": faller["total"], "gwPts": faller["lastGW"]}
                if faller and faller["move"] < 0 else None
            ),
            "mostCaptained": weekly[gw]["mostCaptained"],
            "topPlayer": weekly[gw]["topPlayer"],
            "mostOwned": weekly[gw]["mostOwned"],
            "chipsPlayed": [
                {"chip": k, "label": chip_label(k), "count": len(v), "teams": v}
                for k, v in sorted(chip_counts.items(), key=lambda kv: -len(kv[1]))
            ],
            "totalTransfers": sum(r["transfers"] for r in rows) - (
                sum(r["transfers"] for r in tables[prev_gw]) if prev_gw else 0
            ),
            "standings": [
                {"rank": r["rank"], "team": r["team"], "manager": r["name"],
                 "total": r["total"], "gwPts": r["lastGW"], "move": r["move"]}
                for r in rows
            ],
        }
    return facts


def auto_recap(f):
    """Last-resort bullets, generated straight from facts.

    A freshly finalized week always lands here first. Every number is read from
    the facts dict, never estimated, so the auto version is publishable as-is
    and is meant to be rewritten by hand afterwards.
    """
    gw = f["gw"]
    b = []
    lead = f["leader"]
    if f["leadChanged"] and f["previousLeader"]:
        b.append(
            "%s is the new league leader on %d points, taking the top spot from %s, "
            "who sits %s." % (lead["team"], lead["total"], f["previousLeader"]["team"],
                              ordinal(f["previousLeader"]["rank"]))
        )
    else:
        b.append(
            "%s leads the league on %d points, %d clear of second."
            % (lead["team"], lead["total"], f["leadMargin"])
        )

    hi, lo = f["highestScore"], f["lowestScore"]
    b.append(
        "%s posted the highest score of the gameweek with %d points."
        % (hi["team"], hi["pts"])
    )
    b.append(
        "%s had the lowest score of the week with %d points, against a league median "
        "of %s." % (lo["team"], lo["pts"], trim_number(f["medianScore"]))
    )

    if f["biggestRiser"]:
        r = f["biggestRiser"]
        b.append(
            "%s was the biggest riser, up %d places to %s on %d points for the week."
            % (r["team"], r["places"], ordinal(r["rank"]), r["gwPts"])
        )
    if f["biggestFaller"]:
        r = f["biggestFaller"]
        b.append(
            "%s was the biggest faller, down %d places to %s after scoring %d."
            % (r["team"], abs(r["places"]), ordinal(r["rank"]), r["gwPts"])
        )

    mc = f["mostCaptained"]
    b.append(
        "%s was the most captained pick with %d of %d armbands, and returned %d points."
        % (mc["name"], mc["count"], mc["total"], mc["pts"])
    )

    tp = f["topPlayer"]
    b.append(
        "The highest scoring player owned in this league was %s of %s with %d points, "
        "held by %d of %d managers."
        % (tp["name"], tp["team"], tp["pts"], tp["count"], tp["total"])
    )

    if f["chipsPlayed"]:
        parts = ["%d played %s" % (c["count"], c["label"]) for c in f["chipsPlayed"]]
        b.append("Chips in Gameweek %d: %s." % (gw, ", ".join(parts)))
    else:
        b.append("No chips were played in Gameweek %d." % gw)

    return b


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suffix)


def trim_number(x):
    return str(int(x)) if float(x).is_integer() else ("%.1f" % x)


def resolve_recap(gw, previous, recap_override, recap_dir, facts_for_gw):
    """Recap precedence: --recap > --previous > recaps/gwN.json > auto.

    Prose is the one thing the API cannot produce, so anything already written
    wins over anything generated. --recap is an explicit override for the week
    being written this run.
    """
    if recap_override and recap_override.get("gw") == gw:
        return list(recap_override["bullets"])

    if previous:
        prev_week = (previous.get("gameweeks") or {}).get(str(gw))
        if prev_week and prev_week.get("gwSummary"):
            return list(prev_week["gwSummary"])

    path = os.path.join(recap_dir, "gw%d.json" % gw)
    if os.path.isfile(path):
        with open(path) as fh:
            blob = json.load(fh)
        bullets = blob.get("bullets") if isinstance(blob, dict) else blob
        if bullets:
            return list(bullets)

    return auto_recap(facts_for_gw)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(template_path, payload):
    with open(template_path) as fh:
        template = fh.read()
    if TEMPLATE_PLACEHOLDER not in template:
        raise BuildError(
            "template %s has no %s placeholder; refusing to write output"
            % (template_path, TEMPLATE_PLACEHOLDER)
        )
    blob = json.dumps(payload, indent=2, ensure_ascii=False)
    # A team name containing "</script>" would otherwise close the tag early.
    blob = blob.replace("</", "<\\/")
    title = (PAGE_TITLE.replace("&", "&amp;").replace("<", "&lt;"))
    return (template.replace(TITLE_PLACEHOLDER, title)
                    .replace(TEMPLATE_PLACEHOLDER, blob))


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help="league config: a name in leagues/ or a path to a JSON file")
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--outdir", help="default: the config's outdir")
    ap.add_argument("--previous",
                    help="data.json extracted from the live artifact, so past "
                         "recap prose carries forward")
    ap.add_argument("--recap",
                    help='override this week\'s bullets: {"gw":N,"bullets":[...]}')
    ap.add_argument("--recap-dir", help="default: the config's recap_dir")
    ap.add_argument("--league", type=int,
                    help="override the config's league ID (money rules still "
                         "come from the config)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    args.outdir = args.outdir or cfg.get("outdir", "out")
    args.recap_dir = args.recap_dir or cfg.get("recap_dir", "recaps")
    args.league = args.league or LEAGUE_ID

    previous = None
    if args.previous:
        with open(args.previous) as fh:
            previous = json.load(fh)

    recap_override = None
    if args.recap:
        recap_override = json.loads(args.recap)
        if "gw" not in recap_override or "bullets" not in recap_override:
            raise BuildError('--recap needs both "gw" and "bullets"')

    client = FPLClient()
    warnings = []
    boot, league, managers, live, weeks = gather(client, args.league)

    sys.stderr.write(
        "finalized gameweeks: %s (%d API calls so far)\n"
        % (", ".join(str(w) for w in weeks), client.calls)
    )

    payload, facts = build_payload(
        boot, league, managers, live, weeks, previous, recap_override,
        args.recap_dir, warnings,
    )

    html = render(args.template, payload)

    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(args.outdir, "data.json"), "w") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(args.outdir, "facts.json"), "w") as fh:
        json.dump({str(k): v for k, v in facts.items()}, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(args.outdir, "tracker.html"), "w") as fh:
        fh.write(html)

    for w in warnings:
        sys.stderr.write("WARNING: %s\n" % w)
    print("built %s through GW%d: %d managers, pot %s, %d API calls" % (
        payload["leagueName"], payload["throughGW"], payload["managerCount"],
        payload["pot"]["totalPot"], client.calls,
    ))
    print("wrote %s/{data.json,facts.json,tracker.html}" % args.outdir)


if __name__ == "__main__":
    try:
        main()
    except (BuildError, FPLError) as exc:
        sys.stderr.write("BUILD FAILED: %s\n" % exc)
        sys.exit(1)
