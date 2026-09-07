"""Thin wrapper around the public Fantasy Premier League API.

The FPL API has no authentication and no documented rate limit, but it does
drop connections when you hit it hard. Every call here goes through a single
sequential `_get`. Do not add a thread pool: parallel requests to this host
time out, and a half-fetched season is worse than a slow one.

Endpoints used:
  bootstrap-static/                     players, teams, gameweek metadata
  leagues-classic/<id>/standings/       league table (paginated)
  entry/<id>/                           manager summary
  entry/<id>/history/                   per-gameweek points, chips played
  entry/<id>/event/<gw>/picks/          squad + captain for one gameweek
  entry/<id>/transfers/                 every transfer, with its gameweek
  event/<gw>/live/                      per-player points for one gameweek
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "https://fantasy.premierleague.com/api"

# The API 403s on the default urllib user agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class FPLError(RuntimeError):
    """Raised when the API cannot be read. Always fatal: never publish partial data."""


class FPLClient:
    def __init__(self, timeout=30, retries=4, pause=0.35, verbose=True):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause          # polite gap between sequential calls
        self.verbose = verbose
        self.calls = 0
        self._cache = {}

    # ---- transport ------------------------------------------------------

    def _get(self, path):
        """One sequential GET, with retries. Results are memoized per run."""
        if path in self._cache:
            return self._cache[path]

        url = "%s/%s" % (BASE, path.lstrip("/"))
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.calls += 1
                self._cache[path] = payload
                time.sleep(self.pause)
                return payload
            except urllib.error.HTTPError as exc:
                # A 404 will never succeed on retry; anything else might.
                if exc.code == 404:
                    raise FPLError("404 from FPL API: %s" % url)
                last = exc
            except Exception as exc:  # timeouts, resets, malformed JSON
                last = exc
            backoff = min(2 ** attempt, 16)
            if self.verbose:
                sys.stderr.write(
                    "  retry %d/%d after %s (%s)\n"
                    % (attempt, self.retries, backoff, last)
                )
            time.sleep(backoff)
        raise FPLError("giving up on %s after %d attempts: %s" % (url, self.retries, last))

    def _log(self, msg):
        if self.verbose:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    # ---- endpoints ------------------------------------------------------

    def bootstrap(self):
        """Players, teams, element types and gameweek status."""
        self._log("fetch bootstrap-static")
        return self._get("bootstrap-static/")

    def league_standings(self, league_id):
        """Full classic-league table, following pagination.

        Returns the raw payload with every page's results merged into
        `standings.results`, so callers see one flat list.
        """
        page = 1
        merged = None
        while True:
            self._log("fetch league %s standings page %d" % (league_id, page))
            payload = self._get(
                "leagues-classic/%s/standings/?page_standings=%d" % (league_id, page)
            )
            if merged is None:
                merged = payload
            else:
                merged["standings"]["results"].extend(payload["standings"]["results"])
            if not payload["standings"].get("has_next"):
                break
            page += 1
            if page > 50:
                raise FPLError("league %s paginated past 50 pages; refusing to loop" % league_id)
        return merged

    def entry(self, entry_id):
        """Manager summary. Carries `last_deadline_total_transfers`, which we
        cross-check the per-gameweek transfer counts against."""
        return self._get("entry/%s/" % entry_id)

    def entry_history(self, entry_id):
        """Per-gameweek points and the chips this manager has played."""
        return self._get("entry/%s/history/" % entry_id)

    def entry_picks(self, entry_id, gw):
        """The 15 picks, captain flags and active chip for one gameweek."""
        return self._get("entry/%s/event/%s/picks/" % (entry_id, gw))

    def entry_transfers(self, entry_id):
        """Every transfer this manager has made, each tagged with its gameweek."""
        return self._get("entry/%s/transfers/" % entry_id)

    def live(self, gw):
        """Per-player stats and points for one gameweek."""
        self._log("fetch live GW%s" % gw)
        return self._get("event/%s/live/" % gw)


def finalized_gameweeks(bootstrap):
    """Gameweek ids that are safe to publish.

    `finished` alone flips as soon as the last whistle goes, before bonus
    points are applied. `data_checked` is the flag that says FPL has settled
    the week. Requiring both is what stops a week publishing with provisional
    scores that then move.
    """
    return [
        e["id"]
        for e in bootstrap["events"]
        if e.get("finished") and e.get("data_checked")
    ]
