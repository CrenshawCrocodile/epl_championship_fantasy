# Recurring check: FPL - Championship tracker

Artifact: <https://claude.ai/code/artifact/18b62565-da55-4199-9262-73ebdde14b76>
Repo: <https://github.com/CrenshawCrocodile/epl_championship_fantasy>

Runs on a **claude.ai scheduled task**, not an in-session cron. In-session
crons die with the session; a scheduled task persists and starts a fresh
session each time, which is why step 1 below re-reads the live artifact
instead of assuming any state carried over.

Schedule: **Tuesdays at 09:00 America/Los_Angeles.** Premier League gameweeks
usually settle Sunday or Monday night once bonus points are applied, so a
Tuesday morning run finds the week already `data_checked`. Midweek gameweeks
settle a day or two later and simply get picked up by the following run. The
check is cheap and silent when nothing has changed, so a run that finds no
new gameweek costs nothing.

## Other leagues

League One and Premier League run the same routine as separate scheduled
tasks (`fpl-league-one-tracker`, `fpl-premier-league-tracker`), staggered
after this one on the same morning because the FPL API times out on parallel
requests. Their prompts are the one below with four substitutions: the
artifact URL from `leagues/<name>.json`, `--config <name>` on every build
command, `out/<name>/` for `previous.json`, `data.json`, `facts.json` and
`tracker.html`, and `recaps/<name>/gw<N>.json` for the new recap.

---

## The prompt

Paste this verbatim as the scheduled task's instructions.

> Update the FPL - Championship tracker at
> https://claude.ai/code/artifact/18b62565-da55-4199-9262-73ebdde14b76
>
> Clone <https://github.com/CrenshawCrocodile/epl_championship_fantasy> and
> work in it — it contains `build_tracker.py`, `fetch_fpl.py` and
> `template.html`.
>
> 1. **Read the live artifact first.** Call the Artifact tool with
>    `action: "read"` and that URL. Do this before anything else: a publish
>    from a session that has not read the artifact will be refused, and it is
>    also how you find out whether someone republished it since the last run.
>
> 2. **Extract the previous payload.** From the HTML you just read, pull the
>    object assigned to `DEFAULT_DATA` in the `<script>` block and save it as
>    `out/previous.json`. Note its `throughGW` value; call it
>    `PUBLISHED_GW`.
>
> 3. **Run the build.**
>    ```
>    python3 build_tracker.py --previous out/previous.json
>    ```
>    It takes about a minute and makes sequential API calls. If it exits
>    non-zero, **stop and report the error. Do not publish and do not work
>    around the failure** — a failed sanity check means the API data is not
>    safe to publish yet, usually because a gameweek has not finished settling.
>
> 4. **Compare.** Read `throughGW` from the new `out/data.json`.
>    - If it equals `PUBLISHED_GW`, no new gameweek has finalized. **Stop
>      quietly.** Do not republish and do not send a notification.
>    - If it is greater, continue to step 5.
>    - If it is *less* than `PUBLISHED_GW`, something is wrong. Stop and
>      report it; never publish a page that goes backwards.
>
> 5. **Write the recap for the new gameweek.** Open `out/facts.json` and read
>    the entry for the new week. Write 6 to 8 short bullets covering: who
>    leads and any change at the top, the week's highest and lowest scores,
>    the biggest riser and the biggest faller, the most captained player and
>    what it returned, the highest scoring player owned in the league, and any
>    chips played. Use **team names, not manager names**. No em dashes. Every
>    number must come from `facts.json` — never estimate one. Save the bullets
>    to `recaps/gw<N>.json` as `{"gw": N, "bullets": [...]}`, then rebuild so
>    they take effect:
>    ```
>    python3 build_tracker.py --previous out/previous.json
>    ```
>    Earlier weeks keep their existing wording automatically, because
>    `--previous` outranks everything except an explicit `--recap` override.
>
> 6. **Republish to the same URL.** Call the Artifact tool with
>    `file_path: out/tracker.html`, `url:
>    https://claude.ai/code/artifact/18b62565-da55-4199-9262-73ebdde14b76`,
>    and `label: "Through GW<N>"`. Passing the URL is what keeps the link
>    stable; publishing without it would create a second artifact and the
>    league's shared link would go stale. Do not pass a `favicon` — the
>    artifact keeps the one it has.
>
> 7. **Move the share pin to the new version** so everyone with the link sees
>    the update instead of the old snapshot, then report back: the gameweek
>    published, the current leader, and the artifact URL.

---

## Notes for whoever maintains this

- **Read before publish is not optional.** It is both a guard against
  clobbering someone else's republish and a hard requirement of the publish
  path.
- **The share pin is separate from publishing.** A republish creates a new
  version; viewers on the shared link stay on the pinned one until the pin is
  moved. Forgetting step 7 is the most likely way for the league to sit on a
  stale page while the artifact itself is current.
- **A failed build is the system working.** The sanity checks exist so a week
  never publishes with provisional bonus points or a transfer count that does
  not reconcile. Report the failure; do not bypass it.
- **Rank disagreements are logged, not fatal.** The build prints a WARNING and
  uses FPL's ranking. That is expected whenever scores are tied.
- Changing the schedule, or turning it off in the off-season, is done from
  the scheduled tasks list on claude.ai.
