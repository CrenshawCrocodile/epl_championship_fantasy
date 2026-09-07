# FPL - Championship Tracker

A published page tracking the Fantasy Premier League classic league
**FPL - Championship** (league ID `1166922`): standings, weekly recaps,
league-only metrics and payout projections.

Live artifact: <https://claude.ai/code/artifact/18b62565-da55-4199-9262-73ebdde14b76>

## Run it

```bash
python3 build_tracker.py
```

No dependencies beyond the Python standard library. The build takes about a
minute: it makes roughly `4 × managers + gameweeks + 2` sequential API calls
(131 for 21 managers through GW3).

Outputs land in `out/`:

| File | What it is |
| --- | --- |
| `data.json` | the full payload embedded in the page |
| `facts.json` | structured facts per gameweek, the source for recap prose |
| `tracker.html` | the finished page, ready to publish |

### Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--template` | `template.html` | page shell with the `__TRACKER_DATA__` placeholder |
| `--outdir` | `out` | where the three output files go |
| `--previous` | none | a `data.json` extracted from the live artifact, so past recap prose carries forward |
| `--recap` | none | override one week's bullets: `{"gw":N,"bullets":[...]}` |
| `--recap-dir` | `recaps` | directory of hand-written `gwN.json` files |
| `--league` | `1166922` | any classic league ID |

Typical weekly run, keeping the wording already on the live page:

```bash
python3 build_tracker.py --previous out/previous.json
```

## How it works

### Recompute, never accumulate

Every run rebuilds the entire season from the API. There is no local state
file and no incremental update path, so there is nothing that can quietly
drift away from the official league table. If the API and the page ever
disagree, the fix is to run the build again.

### `fetch_fpl.py`

A thin wrapper over `fantasy.premierleague.com/api/`. Requests are strictly
sequential with a short pause between them, retrying with backoff. **Do not
add a thread pool**: parallel calls to this host time out, and a half-fetched
season is worse than a slow one. Results are memoized per run.

Endpoints used: `bootstrap-static/`, `leagues-classic/<id>/standings/`
(paginated), `entry/<id>/`, `entry/<id>/history/`,
`entry/<id>/event/<gw>/picks/`, `entry/<id>/transfers/`, `event/<gw>/live/`.

### `build_tracker.py`

League config lives in constants at the top of the file:

```
LEAGUE_ID    = 1166922
BUY_IN       = 300
PAYOUT_PCTS  = {1: .50, 2: .20, 3: .15, 4: .10, 5: .05}
CONTACT_EMAIL, COMMISSIONER_NAME, COMMISSIONER_TEAM
```

The pot is computed live: `BUY_IN × managers in the league`. Nothing about
the field size or the dollar amounts is hardcoded, so a manager joining or
leaving reflows the whole Payouts tab on the next run. There is no monthly
pot and no cup payout in this league; the Payouts tab is a pot overview plus
one projected/final table for places 1 through 5.

The commissioner (Damir Becirovic, *Show me the Mane*) is flagged with
`"commissioner": true` on their standings row and badged on the page.

### Sanity checks

The build calls `sys.exit(1)` rather than write bad data. Any of these fails
the run:

- **Unsettled gameweeks.** Only weeks that are both `finished` *and*
  `data_checked` are used. `finished` flips at the final whistle, before
  bonus points land; `data_checked` is the flag that says FPL has settled the
  week. Requiring both is what stops a week publishing with provisional
  scores.
- **Total points mismatch.** Each manager's history total must equal the
  league table's total for the current week.
- **Transfer counts.** Per gameweek, `event_transfers` from the history
  endpoint must equal the transfers the transfers endpoint lists for that
  week, and the season total must reconcile with `last_deadline_total_transfers`
  on the entry endpoint. One documented exception: FPL reports **zero**
  transfers for a week a Wildcard or Free Hit was active, because those moves
  are free and unlimited. Those weeks assert `event_transfers == 0` instead,
  and the standings column shows the true move count from the transfers
  endpoint, which is higher than the number FPL's own counter displays.
- **Captain picks.** Captain flags across the league must sum to exactly the
  number of managers, every week.
- **Commissioner present.** The configured commissioner must appear in the
  standings.
- **Template placeholder.** `__TRACKER_DATA__` must exist in the template
  before any output is written.

Ranks are the one case that logs instead of failing. FPL splits tied scores
on a sort key the public endpoints do not expose, so the build computes
competition ranks, compares them to the league API's `rank`/`last_rank`,
**logs any disagreement, and uses FPL's value**. Past gameweeks, where no API
ranking exists, fall back to competition ranking on total points. A real
example from GW3: two managers tied on 175 points, computed rank 16 for both,
FPL ranked them 16 and 17.

### Recap prose

Prose is the only thing the API cannot produce. Precedence, highest first:

1. `--recap '{"gw":N,"bullets":[...]}'` — explicit override for this run
2. `--previous` — the payload lifted from the live artifact, so weeks already
   published keep their exact wording
3. `recaps/gwN.json` — hand-written bullets checked into the repo
4. auto-generated from `facts.json` — last resort

A newly finalized gameweek always starts auto-generated. That version is
correct but flat, and is meant to be rewritten by hand from `facts.json`:
6 to 8 short bullets covering who leads and any change at the top, the week's
highest and lowest scores, the biggest riser and faller, the most captained
player and what it returned, the highest scoring owned player, and any chips
played. Use team names rather than manager names, no em dashes, and source
every number from `facts.json` rather than estimating it.

### `template.html`

One self-contained page. `const DEFAULT_DATA = __TRACKER_DATA__;` is the only
thing the build substitutes. Premier League purple (`#37003C` / `#240029`)
and green (`#00FF87`), matching the Rosner's Relegation Battle tracker so
both leagues read as the same product.

Three tabs — Insights and Overview, Standings, Payouts — plus a gameweek
picker in the header that rewinds Standings, Recap and League Metrics to any
completed week. Chips Played and the Payouts tab always show the latest
state. Team names throughout link to that team's official FPL page for the
gameweek in view; recap bullets are written as plain prose and the page
linkifies any team name it recognises, so nobody hand-writes anchor tags.

## Weekly update

See [ROUTINE.md](ROUTINE.md) for the exact recurring-check prompt.

## Where this lives

`/Users/leelay2.0/Repo/fpl-championship-tracker` on this Mac. The scheduled
task runs there.
