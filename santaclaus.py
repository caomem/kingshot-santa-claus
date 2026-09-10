import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Backend used by https://ks-giftcode.centurygame.com/ (the public gift code page).
API_BASE = "https://kingshot-giftcode.centurygame.com/api"
ORIGIN = "https://ks-giftcode.centurygame.com"

# Every request is signed with md5(sorted_query + SIGN_SALT). The salt is baked
# into the site's JS bundle and changes whenever the frontend is rebuilt; if the
# API starts answering "sign error", re-extract it from js/src_pages_home_index_vue.*.js
SIGN_SALT = "mN4!pQs6JrYwV9"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": ORIGIN,
    "Referer": ORIGIN + "/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
}

# err_code -> (status, message), mirroring the site's own errorMsgMap.
ERR_CODES = {
    20000: ("SUCCESS", "Redeemed successfully. Please check your mail for rewards!"),
    40002: ("RATE_LIMITED", "Request too frequent, please try again later."),
    40003: ("BAD_REQUEST", "Please enter a Gift Code first."),
    40004: ("RATE_LIMITED", "Server busy. Please try again later."),
    40005: ("NOT_ELIGIBLE", "Claim limit exceeded. Unable to claim."),
    40006: ("NOT_ELIGIBLE", "Insufficient Town Center Level. Unable to claim."),
    40007: ("EXPIRED", "Expired, unable to claim."),
    40008: ("ALREADY_CLAIMED", "Gift has already been claimed!"),
    40009: ("PLAYER_ERROR", "Please sign in with the corresponding character before redeeming."),
    40011: ("ALREADY_CLAIMED", "The same Gift Code type can only be redeemed once!"),
    40012: ("NOT_ELIGIBLE", "Account registration duration does not meet the requirements."),
    40014: ("INVALID_CODE", "Gift Code not found! Please check the capitalization."),
    40015: ("INVALID_CODE", "Redemption Code Error"),
    40016: ("PENDING", "Server is busy. Rewards will be sent later."),
    40017: ("NOT_ELIGIBLE", "Account does not currently meet the redemption requirements."),
    40019: ("RATE_LIMITED", "You are performing too many actions at once. Try again later."),
    40020: ("PLAYER_ERROR", "Character info is incorrect. Check the Player ID / Kingdom."),
    40100: ("RATE_LIMITED", "Refreshing the verification code too frequently."),
    40101: ("RATE_LIMITED", "Server busy. Please try again later."),
    40102: ("CAPTCHA", "Code expired, please retry the verification."),
    40103: ("CAPTCHA", "Incorrect code, please retry the verification."),
}

RETRYABLE = {"RATE_LIMITED", "NETWORK"}

# When the server keeps throttling even after the per-code retries, grinding
# through the rest of the roster only collects rejections. Give up and let the
# next run resume from the history instead.
THROTTLE_GIVE_UP = 5

# sysexits.h EX_TEMPFAIL: "try again later", so cron can tell a throttled run
# apart from a broken one.
EX_TEMPFAIL = 75


class Throttled(Exception):
    """Abandon a run the server is refusing to serve."""

# Nothing will change on a later run, so the history file can skip these pairs.
TERMINAL = {"SUCCESS", "ALREADY_CLAIMED", "EXPIRED", "INVALID_CODE"}

# A code that is expired or unknown is expired or unknown for everybody: as soon
# as one player hits it, stop spending calls on the rest of the roster.
DEAD_CODE = {"EXPIRED", "INVALID_CODE"}

# Where to look for active codes. Independent on purpose: when one goes stale
# or offline the other still feeds the run. JSON is parsed as an API response,
# anything else is scraped as HTML.
CODE_SOURCES = [
    "https://kingshot.net/api//gift-codes",
    "https://kingshotwiki.com/giftcodes/",
]

# The wiki wraps every code in <span class="code">…</span>.
HTML_CODE_RE = re.compile(
    r'<span[^>]*class="[^"]*\bcode\b[^"]*"[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")

DEFAULT_HISTORY = os.path.join("logs", "history.csv")
HISTORY_FIELDS = ["timestamp", "player_id", "kingdom_id", "code", "status"]

# Compact markers for --output oneline, in the order they are printed.
STATUS_GLYPHS = {
    "SUCCESS": "\u2714",             # claimed now
    "ALREADY_CLAIMED": "\u2713",     # was already claimed
    "PENDING": "\u2026",             # server will deliver later
    "NOT_ELIGIBLE": "\u2205",
    "EXPIRED": "\u231b",
    "INVALID_CODE": "\u2717",
    "BAD_REQUEST": "!",
    "PLAYER_ERROR": "\u2691",
    "SKIPPED_NO_KINGDOM": "\u2298",
    "RATE_LIMITED": "\u27f3",
    "NETWORK": "\u26a1",
    "CAPTCHA": "\u26bf",
    "UNKNOWN": "?",
    "SKIPPED_KNOWN": "\u23ed",
}
GLYPH_ORDER = list(STATUS_GLYPHS)


class Reporter:
    """Routes output by mode. Anomalies always go to stderr, even when quiet."""

    def __init__(self, mode: str = "verbose"):
        self.mode = mode
        self.tally = {}
        # Repaint in place only when a terminal is watching.
        self.live = mode == "oneline" and sys.stdout.isatty()
        self._painted = False

    def say(self, message: str = "", end: str = "\n", flush: bool = False):
        """Routine progress: verbose only."""
        if self.mode == "verbose":
            print(message, end=end, flush=flush)

    def warn(self, message: str):
        """Something went wrong or looks off. Never silenced."""
        self._clear()
        print(message, file=sys.stderr, flush=True)

    def count(self, status: str, n: int = 1):
        self.tally[status] = self.tally.get(status, 0) + n
        if self.live:
            self._paint()

    def _clear(self):
        if self._painted:
            print("\r\033[K", end="", flush=True)
            self._painted = False

    def _paint(self):
        print("\r\033[K" + self.compact(), end="", flush=True)
        self._painted = True

    def compact(self) -> str:
        return " ".join(
            f"{STATUS_GLYPHS[s]}{self.tally[s]}"
            for s in GLYPH_ORDER
            if self.tally.get(s)
        )

    def long(self) -> str:
        ordered = sorted(k for k in self.tally if k != "SKIPPED_KNOWN")
        if "SKIPPED_KNOWN" in self.tally:
            ordered.append("SKIPPED_KNOWN")
        return ", ".join(f"{k}={self.tally[k]}" for k in ordered)

    def finish(self, players: int, codes: int):
        self._clear()
        if self.mode == "quiet":
            return
        if self.mode == "oneline":
            # Nothing happened at all: say nothing, like a clean git prompt.
            if self.tally:
                print(f"{players}p {codes}c  {self.compact()}")
            return
        print("\nSummary: " + (self.long() or "nothing attempted"))


def sign_params(params: dict) -> dict:
    """Return params plus the md5 signature the API expects."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params))
    signature = hashlib.md5((payload + SIGN_SALT).encode("utf-8")).hexdigest()
    return {**params, "sign": signature}


def api_post(path: str, params: dict, timeout: float = 20.0) -> dict:
    """POST a signed request and return the decoded JSON body."""
    data = urllib.parse.urlencode(sign_params(params)).encode("utf-8")
    request = urllib.request.Request(API_BASE + path, data=data, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        retry_after = None
        try:
            retry_after = float(e.headers.get("Retry-After"))
        except (AttributeError, TypeError, ValueError):
            pass
        return {
            "code": -1,
            "msg": f"HTTP {e.code} {e.reason}".strip(),
            "err_code": None,
            "http": e.code,
            "retry_after": retry_after,
        }
    except Exception as e:
        # Timeouts and connection resets are transient, not a verdict on the code.
        return {
            "code": -1,
            "msg": f"{type(e).__name__}: {e}",
            "err_code": None,
            "network": True,
        }


def classify(payload: dict) -> tuple:
    """Map an API response to (status, message). Never optimistic by default."""
    err_code = payload.get("err_code")
    raw_msg = str(payload.get("msg") or "").strip()

    if payload.get("code") == 0:
        status, message = ERR_CODES.get(err_code, ("SUCCESS", raw_msg or "Redeemed"))
        return status, message

    if err_code in ERR_CODES:
        return ERR_CODES[err_code]

    # The API also throttles at the HTTP layer, which says nothing about the code.
    http = payload.get("http")
    if http == 429:
        return "RATE_LIMITED", raw_msg or "HTTP 429 Too Many Requests"
    if http and 500 <= http < 600:
        return "NETWORK", raw_msg or f"HTTP {http}"
    if payload.get("network"):
        return "NETWORK", raw_msg or "connection failed"

    return "UNKNOWN", f"code={payload.get('code')} err_code={err_code} msg={raw_msg}"


def redeem(fid: str, kid: str, cdk: str, retries: int, backoff: float, out: "Reporter") -> tuple:
    """Redeem one code for one player, retrying only on rate limiting."""
    waited = False
    for attempt in range(retries + 1):
        payload = api_post(
            "/gift_code",
            {"fid": fid, "kid": kid, "cdk": cdk, "time": int(time.time())},
        )
        status, message = classify(payload)
        if status not in RETRYABLE or attempt == retries:
            if waited:
                # Retry notes broke the caller's line; re-indent before the verdict.
                out.say("\n      ", end="", flush=True)
            return status, message

        wait = payload.get("retry_after") or backoff * (2 ** attempt)
        wait = min(max(wait, 1.0), 120.0)
        out.say(
            f"\n      [~] rate limited, retry {attempt + 1}/{retries} in {wait:.0f}s",
            end="",
            flush=True,
        )
        waited = True
        time.sleep(wait)
    return status, message


def parse_player(raw: str, default_kid) -> dict:
    """Parse 'ID', 'Name,ID' or 'Name,ID,KingdomID' into a player dict."""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) == 1:
        name, player_id, kid = "N/A", parts[0], default_kid
    elif len(parts) == 2:
        name, player_id, kid = parts[0], parts[1], default_kid
    else:
        name, player_id, kid = parts[0], parts[1], parts[2] or default_kid
    return {"name": name, "id": player_id, "kid": kid}


def load_players_from_file(file_path: str, default_kid) -> list:
    if not os.path.exists(file_path):
        print(f"[!] Error: File not found at '{file_path}'", file=sys.stderr)
        sys.exit(1)

    players = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            player = parse_player(line, default_kid)
            if player["id"]:
                players.append(player)
    return players


def load_codes_from_file(file_path: str) -> list:
    if not os.path.exists(file_path):
        print(f"[!] Error: File not found at '{file_path}'", file=sys.stderr)
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def _is_active(entry: dict) -> bool:
    """A null/absent expiry means the code never expires."""
    expires_at = entry.get("expiresAt")
    if not expires_at:
        return True
    try:
        deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return True
    return deadline > datetime.now(timezone.utc)


def _parse_api_codes(body: str) -> list:
    """Read the {data: {giftCodes: [...]}} shape, honouring expiresAt."""
    payload = json.loads(body)
    entries = (payload.get("data") or {}).get("giftCodes") or []
    codes, expired = [], 0
    for entry in entries:
        code = (entry.get("code") or "").strip()
        if not code:
            continue
        if _is_active(entry):
            codes.append(code)
        else:
            expired += 1
    return codes, expired


def _parse_html_codes(body: str) -> list:
    """Scrape <span class="code">…</span>. No expiry info: the page only lists live codes."""
    codes = []
    for raw in HTML_CODE_RE.findall(body):
        code = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if CODE_SHAPE_RE.match(code):
            codes.append(code)
    return codes, 0


def fetch_codes(sources=None, timeout: float = 20.0, out: "Reporter" = None) -> list:
    """Collect active gift codes from every source, tolerating individual failures."""
    if isinstance(sources, str):
        sources = [sources]
    sources = sources or CODE_SOURCES
    out = out or Reporter()

    collected = []
    for source in sources:
        request = urllib.request.Request(
            source, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "*/*"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read().decode("utf-8", errors="replace")
        except Exception as e:
            out.warn(f"[!] Source failed, continuing without it: '{source}' — {type(e).__name__}: {e}")
            continue

        try:
            if "json" in content_type.lower() or body.lstrip()[:1] == "{":
                codes, expired = _parse_api_codes(body)
            else:
                codes, expired = _parse_html_codes(body)
        except Exception as e:
            out.warn(f"[!] Could not parse '{source}' — {type(e).__name__}: {e}")
            continue

        if not codes:
            out.warn(f"[!] '{source}' returned no codes — its layout may have changed.")
            continue

        fresh = [c for c in codes if c.lower() not in {x.lower() for x in collected}]
        collected += fresh
        detail = f", {len(fresh)} new" if fresh and len(fresh) != len(codes) else ""
        tail = f", {expired} expired ignored" if expired else ""
        out.say(f"[i] {source}: {len(codes)} code(s){detail}{tail}")

    if not collected:
        out.warn("[!] No codes found in any source.")
    return collected


def _history_header(file_path: str):
    """Return the ledger's header row, or None when the file is absent/empty."""
    try:
        with open(file_path, "r", newline="", encoding="utf-8") as f:
            return next(csv.reader(f), None)
    except OSError:
        return None


def load_history(file_path: str, out: "Reporter" = None) -> dict:
    """Map (player_id, code) -> status for attempts already settled."""
    out = out or Reporter()
    if not file_path or not os.path.exists(file_path):
        return {}

    header = _history_header(file_path)
    if header is None:
        return {}
    if header != HISTORY_FIELDS:
        # Reading this positionally would silently shift every column and
        # quietly disable deduplication, so refuse instead of guessing.
        out.warn(
            f"[!] '{file_path}' is not a history ledger (header is {header}); "
            "ignoring it — every pair will be treated as untried."
        )
        return {}

    history, malformed = {}, 0
    with open(file_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) != len(HISTORY_FIELDS):
                malformed += 1
                continue
            record = dict(zip(HISTORY_FIELDS, row))
            key = (record["player_id"], record["code"])
            status = record["status"]
            # A terminal status sticks; anything else stays open for a retry.
            if status in TERMINAL or key not in history:
                history[key] = status

    if malformed:
        out.warn(f"[!] Skipped {malformed} malformed row(s) in '{file_path}'.")
    return history


def open_history(file_path: str, out: "Reporter" = None):
    """Open the ledger for appending, writing the header only on creation."""
    out = out or Reporter()
    if not file_path:
        return None, None

    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        header = _history_header(file_path)
        if header != HISTORY_FIELDS:
            # Appending would pile a second schema onto the first. Set the file
            # aside instead of destroying it, and start a clean ledger.
            spoiled = f"{file_path}.bad-{datetime.now():%Y%m%d-%H%M%S}"
            os.rename(file_path, spoiled)
            out.warn(f"[!] '{file_path}' had the wrong header; moved to '{spoiled}'.")

    is_new = not os.path.exists(file_path) or os.path.getsize(file_path) == 0
    handle = open(file_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
    if is_new:
        writer.writeheader()
        handle.flush()
    return handle, writer


def summarize_skips(items: list, max_codes: int = 4) -> str:
    """One-line digest of skipped (code, status) pairs."""
    tally = {}
    for _, status in items:
        tally[status] = tally.get(status, 0) + 1
    reasons = ", ".join(f"{s.lower()}x{n}" for s, n in sorted(tally.items()))

    names = [code for code, _ in items]
    shown = ", ".join(names[:max_codes])
    if len(names) > max_codes:
        shown += f" +{len(names) - max_codes} more"
    return f"{len(names)} known ({reasons}): {shown}"


def redeem_batch(
    players: list,
    codes: list,
    delay: float = 1.5,
    retries: int = 3,
    output_csv: str = "redemption_results.csv",
    history_file: str = DEFAULT_HISTORY,
    ignore_history: bool = False,
    out: "Reporter" = None,
):
    out = out or Reporter()
    results = []
    throttled_streak = 0
    history = {} if ignore_history else load_history(history_file, out)
    handle, ledger = open_history(history_file, out)
    dead_codes = {}
    exit_code = 0

    out.say("\n==================================================")
    out.say("  KINGSHOT AUTO-REDEEMER CLI (Batch Mode)")
    out.say(f"  Total Players: {len(players)}")
    out.say(f"  Total Codes:   {len(codes)}")
    if history:
        out.say(f"  History:       {len(history)} attempt(s) in '{history_file}'")
    out.say("==================================================\n")

    def record(name, fid, kid, code, status, message):
        results.append(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "player_name": name,
                "player_id": fid,
                "kingdom_id": kid,
                "code": code,
                "status": status,
                "message": message,
            }
        )
        out.count(status)
        if ledger:
            ledger.writerow(
                {
                    "timestamp": results[-1]["timestamp"],
                    "player_id": fid,
                    "kingdom_id": kid,
                    "code": code,
                    "status": status,
                }
            )
            handle.flush()

    # Players whose codes are all known: held back and printed as one line.
    quiet = []

    def flush_quiet():
        if not quiet:
            return
        total = sum(len(entry[3]) for entry in quiet)
        if len(quiet) == 1:
            p_idx, name, fid, items = quiet[0]
            out.say(f"[{p_idx}/{len(players)}] ⏭  '{name}' ({fid}) — {summarize_skips(items)}")
        else:
            first, last = quiet[0][0], quiet[-1][0]
            out.say(
                f"[{first}-{last}/{len(players)}] ⏭  {len(quiet)} players already "
                f"up to date ({total} attempts skipped)"
            )
        quiet.clear()

    try:
        for p_idx, player in enumerate(players, 1):
            name, fid, kid = player["name"], player["id"], player["kid"]

            if not kid:
                flush_quiet()
                out.say(f"\n[{p_idx}/{len(players)}] 👤 '{name}' (ID: {fid}, Kingdom: {kid})")
                out.say("   [!] Skipped: no kingdom set. Use --kid or a 'Name,ID,Kingdom' line.")
                for code in codes:
                    record(name, fid, "", code, "SKIPPED_NO_KINGDOM",
                           "Kingdom ID is required by the gift code API")
                continue

            # Decide up front what is worth a request, so the noise can be batched.
            pending, known = [], []
            for code in codes:
                code = code.strip()
                previous = dead_codes.get(code) or history.get((fid, code))
                if previous in TERMINAL:
                    known.append((code, previous))
                else:
                    pending.append(code)

            if known:
                out.count("SKIPPED_KNOWN", len(known))

            if not pending:
                quiet.append((p_idx, name, fid, known))
                continue

            flush_quiet()
            out.say(f"\n[{p_idx}/{len(players)}] 👤 '{name}' (ID: {fid}, Kingdom: {kid})")
            if known:
                out.say(f"   ⏭  {summarize_skips(known)}")

            for c_idx, code in enumerate(pending, 1):
                out.say(f"   ({c_idx}/{len(pending)}) 🎁 '{code}'...", end=" ", flush=True)

                status, message = redeem(fid, kid, code, retries, delay, out)
                out.say(f"-> [{status}] ({message})")

                record(name, fid, kid, code, status, message)

                # Retries were already exhausted, so this is the server still
                # saying no. A few in a row means the run is pointless.
                if status in RETRYABLE:
                    throttled_streak += 1
                    if throttled_streak >= THROTTLE_GIVE_UP:
                        raise Throttled(
                            f"[!] Server still refusing after {THROTTLE_GIVE_UP} "
                            "throttled codes in a row — stopping here. "
                            "Re-run later; the history resumes where this left off."
                        )
                else:
                    throttled_streak = 0

                if status in DEAD_CODE:
                    dead_codes[code] = status
                    out.say(f"   [!] '{code}' is dead for everyone — dropping it for the rest.")

                time.sleep(delay)

        flush_quiet()
    except Throttled as e:
        flush_quiet()
        out.warn(str(e))
        exit_code = EX_TEMPFAIL
    except KeyboardInterrupt:
        flush_quiet()
        out.warn("[!] Interrupted — progress up to here is in the history file.")
        exit_code = 130
    finally:
        if handle:
            handle.close()

    if results and output_csv:
        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "player_name",
                    "player_id",
                    "kingdom_id",
                    "code",
                    "status",
                    "message",
                ],
            )
            writer.writeheader()
            writer.writerows(results)
        out.say(f"\n[✓] Detailed report saved to: '{output_csv}'")

    out.finish(len(players), len(codes))
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Kingshot Santa Claus: Gift Code Redeemer"
    )

    player_group = parser.add_mutually_exclusive_group(required=True)
    player_group.add_argument(
        "-u",
        "--userid",
        help="Single 'ID', 'Name,ID' or 'Name,ID,Kingdom' (e.g. 'caomem,360963190,415')",
    )
    player_group.add_argument(
        "-U",
        "--users-file",
        help="Path to file with 'ID', 'Name,ID' or 'Name,ID,Kingdom' per line",
    )

    code_group = parser.add_mutually_exclusive_group()
    code_group.add_argument(
        "-c", "--code", nargs="+", help="One or multiple codes separated by space"
    )
    code_group.add_argument(
        "-C",
        "--codes-file",
        help="Path to .txt file with Gift Codes (one per line)",
    )

    parser.add_argument(
        "-a",
        "--auto",
        action="store_true",
        help="Fetch currently active gift codes from --source",
    )
    parser.add_argument(
        "--source",
        action="append",
        metavar="URL",
        help="Code source for --auto; repeatable. Replaces the defaults: "
        + ", ".join(CODE_SOURCES),
    )
    parser.add_argument(
        # Takes a level rather than being a boolean, the way ffmpeg's -v does.
        "-v",
        "--verbose",
        choices=("verbose", "oneline", "quiet"),
        default="verbose",
        metavar="LEVEL",
        help="verbose: full log (default). oneline: one compact summary line. "
        "quiet: nothing on stdout. Warnings and errors always go to stderr.",
    )
    parser.add_argument(
        "--history",
        default=DEFAULT_HISTORY,
        help=f"Ledger of past attempts, skipped on later runs (default: {DEFAULT_HISTORY})",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not read or write the history ledger",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retry pairs the history already settled (still records them)",
    )
    parser.add_argument(
        "-k",
        "--kid",
        help="Default Kingdom ID, used for entries that don't carry their own",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Delay between redemption attempts in seconds (default: 1.5s)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries when the API rate-limits the request (default: 3)",
    )
    parser.add_argument(
        "--out",
        default="redemption_results.csv",
        help="Output CSV file path (default: redemption_results.csv)",
    )

    args = parser.parse_args()

    if args.userid:
        players = [parse_player(args.userid, args.kid)]
    else:
        players = load_players_from_file(args.users_file, args.kid)

    out = Reporter(args.verbose)

    codes = []
    if args.code:
        codes += args.code
    elif args.codes_file:
        codes += load_codes_from_file(args.codes_file)
    if args.auto:
        codes += fetch_codes(args.source, out=out)

    if not (args.code or args.codes_file or args.auto):
        parser.error("no codes given: use -c/--code, -C/--codes-file or -a/--auto")

    # Preserve order while dropping duplicates between the CLI and the API.
    codes = list(dict.fromkeys(c.strip() for c in codes if c.strip()))
    if not codes:
        out.warn("[!] No codes to redeem.")
        sys.exit(1)

    sys.exit(redeem_batch(
        players=players,
        codes=codes,
        delay=args.delay,
        retries=args.retries,
        output_csv=args.out,
        history_file=None if args.no_history else args.history,
        ignore_history=args.force,
        out=out,
    ))
