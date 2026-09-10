# Santa Claus
 Kingshot Gift Code Redeem

Talks directly to the gift code API behind
<https://ks-giftcode.centurygame.com/> (`POST /api/gift_code`, md5-signed).
Standard library only — no browser, no extra dependencies.

## Install

```bash
make install     # link ~/.local/bin/santaclaus -> run.sh
make cron        # run it every 10 minutes
make status      # show what is installed
```

| Target | Does |
| --- | --- |
| `install` | symlink `run.sh` into `$(BIN)`, default `~/.local/bin` |
| `uninstall` | remove the symlink and the cron entry |
| `cron` | schedule `run.sh --detach` every `$(INTERVAL)` minutes |
| `uncron` | remove the cron entry |
| `status` | symlink, cron entry, ledger size, runs kept |
| `clean` | remove the cron entry and the run logs, **keeping the ledger** |
| `purge` | also delete the ledger; requires `CONFIRM=yes` |

Override on the command line: `make cron INTERVAL=30`, `make install BIN=/usr/local/bin`.

The cron entry is tagged with a `# santaclaus` comment and rewritten by tag, so
`make cron` is idempotent and leaves your other crontab entries alone.

`clean` deliberately keeps `logs/history.csv`. That ledger is the only reason a
run costs two requests instead of five hundred — delete it and the next run
re-attempts every player against every code, which is a reliable way to get
rate-limited. `purge` will do it anyway if you insist.

## Usage

```bash
# one player
python santaclaus.py -u "caomem,360963190,415" -c CHILLWEEKEND

# a roster, all in the same kingdom
python santaclaus.py -U freemen.csv --kid 415 -c CODE1 CODE2

# codes from a file
python santaclaus.py -U freemen.csv --kid 415 -C codes.txt
```

Player file accepts `ID`, `Name,ID` or `Name,ID,Kingdom` per line. A per-line
kingdom overrides `--kid`; a player with neither is skipped.

## run.sh

`run.sh` carries the settings for one particular roster (players file, kingdom,
paths) so a scheduled run is a single command. It resolves its own location,
following symlinks, so it can live on `PATH` and be called from anywhere:

```bash
ln -s /path/to/santaclaus/run.sh ~/.local/bin/santaclaus
```

The redemption itself runs **detached**, in its own session, writing to
`logs/run-<timestamp>.log`. The script then streams that log to stdout, so it
still feels like an ordinary foreground command — but Ctrl+C or a dropped SSH
session only stops the watching, never the run.

| Command | Does |
| --- | --- |
| `santaclaus` | start a run and stream it |
| `santaclaus --detach` | start a run and return immediately |
| `santaclaus --follow` | attach to the run in progress, or replay the last one |
| `santaclaus --latest` | print the last run that actually changed something |

Extra arguments are passed through and override the defaults, so
`santaclaus --detach -v verbose` or `santaclaus --force` work as expected.

A second invocation while a run is in progress attaches to it instead of
redeeming the whole roster twice — the lock is held by the worker itself, via
an inherited file descriptor, so it is released exactly when the run ends.
The worker's exit status is recorded and re-raised by the script, so `&&`
chains and cron still see failures.

### Kept logs

Runs are a circular buffer: the newest `SANTACLAUS_KEEP_RUNS` (default 10) are
kept and everything older is deleted, log and CSV together.

Two pointers survive rotation:

- `logs/current.log` — the run that started last, whatever it did. This is what
  `--follow` attaches to.
- `logs/latest.log` and `logs/latest.csv` — the last run that **changed
  something**, meaning at least one code came back `SUCCESS` or `PENDING`. Runs
  where everything was already claimed or skipped never take this pointer.

The run `latest` points at is exempt from rotation, however old it gets. So
after a week of hourly runs that found nothing new, `santaclaus --latest` still
prints the run where the last code was actually redeemed, while the routine
no-op runs have long since cleaned themselves up.

A run that crashes writes no CSV and therefore never becomes `latest`.

## Auto-check

`--auto` pulls the currently active codes instead of listing them by hand:

```bash
python santaclaus.py -U freemen.csv --kid 2279 --auto
```

Two independent sources are queried and their results merged, so one going
stale or offline does not stop the run:

| Source | Shape | Expiry |
| --- | --- | --- |
| `kingshot.net/api//gift-codes` | JSON API | honours `expiresAt` (`null` = never expires) |
| `kingshotwiki.com/giftcodes/` | HTML, `<span class="code">` | page only lists live codes |

A source that fails to fetch, fails to parse, or returns nothing is reported
and skipped — the others still feed the run. Content type decides the parser,
so `--source` accepts either kind. Pass `--source` to replace the defaults;
repeat it for more than one:

```bash
python santaclaus.py -U freemen.csv --kid 2279 --auto \
    --source https://kingshotwiki.com/giftcodes/
```

`--auto` combines with `-c` / `-C`, and duplicates are dropped case-insensitively.

If the wiki ever stops yielding codes, the run prints
`returned no codes — its layout may have changed` rather than failing quietly;
the selector to fix is `HTML_CODE_RE` in `santaclaus.py`.

## History

Every attempt is appended to `logs/history.csv` (`--history` to move it,
`--no-history` to turn it off) as `player_id,kingdom_id,code,status`, flushed
per row so an interrupted run keeps what it did.

On the next run a `(player, code)` pair is skipped when it already settled as
`SUCCESS`, `ALREADY_CLAIMED`, `EXPIRED` or `INVALID_CODE`. Everything else
stays open: `NOT_ELIGIBLE` depends on Town Center level and account age,
`PLAYER_ERROR` usually means a wrong kingdom you will want to fix and retry.
Use `--force` to attempt everything regardless.

Within a single run, a code that comes back `EXPIRED` or `INVALID_CODE` is
dropped for the remaining players — it is dead for everyone, so there is no
point spending the rest of the roster on it.

Skipped work is never printed one line at a time. Codes already settled for a
player are folded into a single `⏭` line, and consecutive players with nothing
left to do collapse into one:

```
[1-20/117] ⏭  20 players already up to date (80 attempts skipped)

[21/117] 👤 'Luna' (ID: 363027632, Kingdom: 2279)
   ⏭  2 known (already_claimedx2): KS0907, Kingshot888
   (1/2) 🎁 'KS0909'... -> [SUCCESS] (Redeemed successfully...)
```

So a run where nothing is new stays a handful of lines instead of one per
player per code.

## Kingdom ID is mandatory

The site now asks for Player ID **and** Kingdom, and the API rejects requests
without `kid` (`err_code 40020`, "Character info is incorrect"). There is no
public endpoint to look a player's kingdom up — `/api/player` returns 404 — so
it has to come from `--kid` or the third CSV column.

## Verbosity

`-v/--verbose` takes a level, the way `ffmpeg -v` does, and picks how much
reaches stdout:

| Level | stdout |
| --- | --- |
| `verbose` (default) | the full log shown above |
| `oneline` | a single compact line, and nothing at all if nothing happened |
| `quiet` | nothing |

Warnings and errors always go to **stderr**, in every mode — a failed source or
a changed page layout is never swallowed, so `quiet` is safe for cron. The CSV
report and the history ledger are written the same way regardless of mode.

```console
$ python santaclaus.py -U freemen.csv --kid 2279 --auto -v oneline
117p 4c  ✔7 ✓224 ⚑6 ⟳2 ⏭432
```

`Np Mc` is players and codes, followed by one marker per status seen:

| | | | |
| --- | --- | --- | --- |
| ✔ success | ✓ already claimed | … pending | ∅ not eligible |
| ⌛ expired | ✗ invalid code | ⚑ player error | ⊘ no kingdom |
| ⟳ rate limited | ⚡ network | ⚿ captcha | ? unknown |
| ⏭ skipped (known) | | | |

On a terminal the line repaints in place as the run progresses; when piped it
is printed once at the end.

Exit status is `1` when there was nothing to redeem (no codes given, or every
source failed), `0` otherwise.

## Result statuses

`SUCCESS`, `ALREADY_CLAIMED`, `EXPIRED`, `INVALID_CODE`, `NOT_ELIGIBLE`,
`PLAYER_ERROR`, `RATE_LIMITED`, `NETWORK`, `PENDING`, `CAPTCHA`,
`SKIPPED_NO_KINGDOM`, `UNKNOWN`. Anything the API doesn't explain lands in
`UNKNOWN` with the raw payload in the message column — a redemption is only
ever reported as `SUCCESS` when the API says so.

### Throttling

The API throttles in two different ways and both are retried with exponential
backoff (`--retries`, default 3):

- an `err_code` in the JSON body (`40002`, `40004`, `40019`, `40101`) →
  `RATE_LIMITED`
- **HTTP 429** on the request itself → also `RATE_LIMITED`, honouring the
  `Retry-After` header when the server sends one

HTTP 5xx and connection failures become `NETWORK`, retried the same way. None
of these are terminal, so the pairs stay open for the next run.

A whole roster is large enough to get throttled part-way through. When the
server is still refusing after five codes in a row have exhausted their
retries, the run stops rather than collecting hundreds of rejections, and exits
`75` (`EX_TEMPFAIL`) so cron can tell "try again later" from a real failure.
The history means the next run picks up where it stopped.

Exit codes: `0` normal, `1` nothing to redeem, `75` throttled and abandoned,
`130` interrupted.

## Signing salt

Requests are signed with `md5(sorted_query + salt)`. The salt is a literal in
the site's JS bundle and changes whenever the frontend is rebuilt, at which
point the API answers `Sign Error` to everything.

**This is handled automatically.** The first rejected signature in a run stops
the run in place, rediscovers the salt from the live site, caches it, and
retries the attempt that failed. Nothing is lost and no configuration changes.

Discovery starts from the site URL alone — nothing about filenames or build
hashes is hardcoded:

1. read the page, take the `<script>` sources
2. read `app.js` for the lazy chunk's name; its build hash is the one `app.js`
   itself carries, with the other hashes in the file as fallbacks
3. collect every short opaque string literal across all bundles
4. probe each against `/api/gift_code_config`, which takes only a timestamp,
   touches no player and changes nothing, until one signs correctly

On the current build that is 42 candidates, the right one found eighth, in
about nine seconds. Symbol-bearing candidates are tried first because salts
tend to carry punctuation and identifiers do not — but the whole list is
probed, so an alphanumeric replacement is still found.

The result is cached in `logs/salt` (override with `SANTACLAUS_SALT_CACHE`),
which takes precedence over the `SIGN_SALT` constant. `make clean` and
`make purge` both leave it alone.

To check or refresh it by hand:

```bash
santaclaus.py --update-salt
```

It exits `0` without probing when the current salt still works. If discovery
fails — the site is unreachable, or the bundle layout changed beyond
recognition — the run aborts on the **first** request rather than sending the
whole roster into a wall, and exits `1`.
