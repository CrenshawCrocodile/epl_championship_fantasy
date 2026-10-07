# Recurring check: the three FPL league trackers

Repo: <https://github.com/CrenshawCrocodile/epl_championship_fantasy>

| League | Config | Artifact | Build output | Recaps |
|--------|--------|----------|--------------|--------|
| Championship | `championship` | <https://claude.ai/code/artifact/18b62565-da55-4199-9262-73ebdde14b76> | `out/` | `recaps/` |
| League One | `league-one` | <https://claude.ai/artifact/KYVxxgQsWo9cCufy7S5stC> | `out/league-one/` | `recaps/league-one/` |
| Premier League | `premier-league` | <https://claude.ai/artifact/Ttr7gnZDuuy3V7eC2bxVoG> | `out/premier-league/` | `recaps/premier-league/` |

## Where it runs

One **Claude cloud routine** covers all three leagues:
[FPL league trackers (Championship, League One, Premier League)](https://claude.ai/code/routines/trig_01NDZMDcHDoJXZhksbbi65PS).
It runs in Anthropic's cloud against a fresh clone of this repo, so no Mac has
to be awake. It was created on 2026-10-07 and replaces three separate Mac
scheduled tasks, now disabled (see "The old Mac tasks" below).

- **Schedule: daily at 10:00 UTC.** A run that finds no newly finalised
  gameweek publishes nothing, commits nothing and sends nothing, so the extra
  runs are harmless and a page updates within a day of bonus points settling.
  Midweek gameweeks need no special handling.
- **One routine, three leagues in sequence.** The FPL API times out on
  parallel requests. A single routine that builds the leagues back to back
  enforces "never two builds at once"; three staggered schedules could only
  hope for it. A whole run takes about five and a half minutes when nothing
  is new.
- **Environment: Default.** `fantasy.premierleague.com` is allowlisted there,
  and `SLACK_BOT_TOKEN` is set as an environment variable for the failure
  alert. The Slack connector is attached as a fallback.
- **Model:** `claude-sonnet-5-5`. The build is stdlib Python; nothing is
  installed.

## What a run does

For each league, in the order above:

1. **Read the live artifact first.** A publish from a session that has not
   read the artifact is refused, and the read is how the run learns what is
   published.
2. **Extract the previous payload.** The object assigned to `DEFAULT_DATA` in
   the page's `<script>` is saved as `<out>/previous.json`; its `throughGW`
   is `PUBLISHED_GW`.
3. **Build:** `python3 build_tracker.py --config <config> --previous <out>/previous.json`.
   One retry after 60 seconds, then the league is recorded as failed and the
   run moves on.
4. **Compare** the new `throughGW` with `PUBLISHED_GW`. Equal: the league is
   done, quietly. Lower: a failure, never published. Higher: continue.
5. **Write the recap** for each new gameweek from `<out>/facts.json` into
   `<recaps>/gw<N>.json` (6 to 8 bullets, team names not manager names, no em
   dashes, every number from `facts.json`), then rebuild so it takes effect.
   Earlier weeks keep their published wording because `--previous` outranks
   the recaps directory.
6. **Republish to the same URL** with the label `Through GW<N>`. The saved
   live HTML has to be read in full before the publish is accepted.
7. **Share pin.** All three artifacts are currently shared so that viewers
   see updates immediately, which makes this a no-op. If one is ever pinned
   to a version, the pin has to be moved after the publish.

After all three:

- **Recaps are pushed to `main`** (`git add recaps`, commit `GW<N> recaps`),
  only if at least one league published. Nothing under `out/` is committed:
  it is regenerated on every run.
- **Failure alert:** one Slack DM to Lyle (`U0BRRMVD010`) listing every
  failure, sent only if a league failed. A failure in one league does not
  stop the other two.

## The prompt

The routine's prompt is self-contained and is kept on the routine, not in
this repo, so editing this file does not change what runs. To change the
behaviour, edit the prompt at the routine link above (or ask Claude to update
the routine), then bring this description back in line.

## Checking on it

- Runs and their logs: the routine page above, or ask Claude for the
  routine's recent runs.
- First test run, 2026-10-07: all three leagues built from the cloud and
  stopped at GW5. The publish, push and alert paths had not yet run at that
  point; the first gameweek to finalise after that date is their first real
  exercise.
- To run a league by hand from a checkout:
  ```
  python3 build_tracker.py --config premier-league --previous out/premier-league/previous.json
  ```
  with `previous.json` lifted from the live artifact as in step 2.

## The old Mac tasks

`fpl-championship-tracker`, `fpl-league-one-tracker` and
`fpl-premier-league-tracker` were Claude desktop scheduled tasks on Lyle's
Mac, staggered on Tuesday mornings Pacific. They were disabled on 2026-10-07,
the day the cloud routine was created, and are paused rather than deleted.
The cloud routine is the only thing updating the trackers. If it ever fails
to publish, the fallback is to re-enable these tasks from the desktop app's
scheduled tasks list or to run a build by hand (see "Checking on it"). They
do not push recap files, so a week published by one of them needs its
`gw<N>.json` committed separately.

## Notes for whoever maintains this

- **Read before publish is not optional.** It is both a guard against
  clobbering someone else's republish and a hard requirement of the publish
  path.
- **A failed build is the system working.** The sanity checks exist so a week
  never publishes with provisional bonus points or a transfer count that does
  not reconcile. Report the failure; do not bypass it.
- **Rank disagreements are logged, not fatal.** The build prints a WARNING and
  uses FPL's ranking. That is expected whenever scores are tied.
- **Never parallelise the API calls**, inside a build or across leagues.
- **Adding a league:** add `leagues/<name>.json` and a row to the table in the
  routine's prompt. Do not create a second routine.
- Changing the schedule, or turning it off in the off-season, is done from
  the routine page on claude.ai.
