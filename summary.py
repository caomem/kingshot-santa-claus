"""Readable report of one redemption run: `santaclaus summary`.

Reads a run's results CSV (logs/run-*.csv), fills in what the roster knows
(alliance, a name for 'ID'-only lines), and prints one table per kingdom with
the errors at the top.
"""

import argparse
import csv
import glob
import os
import re
import sys
import unicodedata

from santaclaus import STATUS_GLYPHS, load_players_from_file

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOGS = os.path.join(HERE, "logs")

# Filter letters. "Error" is everything that did not end in a claim — the same
# line --force draws — so `-f e` lists exactly what --force would retry.
CLAIMED_NOW = ("SUCCESS", "PENDING")
CLAIMED_BEFORE = ("ALREADY_CLAIMED",)
CATEGORIES = {"e": "errors", "s": "successes", "a": "already claimed"}

# Within the errors, the ones that need a human come first; the game simply
# saying no comes last.
ERROR_ORDER = [
    "SIGN_ERROR", "CAPTCHA", "UNKNOWN", "BAD_REQUEST", "PLAYER_ERROR",
    "SKIPPED_NO_KINGDOM", "NETWORK", "RATE_LIMITED",
    "INVALID_CODE", "EXPIRED", "NOT_ELIGIBLE",
]
REJECTED = {"INVALID_CODE", "EXPIRED", "NOT_ELIGIBLE"}

RESET = "\033[0m"
STYLES = {
    "error": "\033[31m",
    "rejected": "\033[33m",
    "success": "\033[32m",
    "claimed": "\033[2m",
    "heading": "\033[1;36m",
    "bold": "\033[1m",
    "dim": "\033[2m",
}


def category(status: str) -> str:
    if status in CLAIMED_NOW:
        return "s"
    if status in CLAIMED_BEFORE:
        return "a"
    return "e"


def rank(status: str) -> tuple:
    """Sort key: errors (worst first), then successes, then already claimed."""
    cat = category(status)
    if cat == "e":
        # A status this file does not know about is worth a look: put it first.
        pos = ERROR_ORDER.index(status) if status in ERROR_ORDER else -1
        return (0, pos)
    if cat == "s":
        return (1, CLAIMED_NOW.index(status))
    return (2, 0)


def style_for(status: str) -> str:
    cat = category(status)
    if cat == "s":
        return "success"
    if cat == "a":
        return "claimed"
    return "rejected" if status in REJECTED else "error"


def width(text: str) -> int:
    """Terminal columns taken by text: wide glyphs count 2, combining marks 0."""
    total = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in "‍︎️":
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


class Painter:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, style: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return STYLES[style] + text + RESET


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def list_runs(logs: str) -> list:
    """Run CSVs, oldest first. Runs that attempted nothing have no CSV."""
    return sorted(glob.glob(os.path.join(logs, "run-*.csv")))


def _run_index(text: str, runs: list) -> int:
    """'-2' (or '2') -> -2. An empty end or 0 means the last run, -1."""
    text = text.strip()
    n = int(text) if text else 0
    # `-l 2` reads as "two back" just like `-l -2`.
    return -abs(n) or -1


def pick_runs(logs: str, which) -> list:
    """The results CSVs to report on, oldest first."""
    runs = list_runs(logs)
    none_yet = f"summary: no run results in '{logs}' yet"

    if which is None:
        # Same run as `santaclaus --latest`: the last one that redeemed something.
        latest = os.path.join(logs, "latest.csv")
        if os.path.exists(latest):
            return [os.path.realpath(latest)]
        if not runs:
            raise SystemExit(none_yet)
        print("summary: no run has redeemed anything yet — showing the last run",
              file=sys.stderr)
        return [runs[-1]]

    if os.path.exists(which):
        return [which]

    try:
        if ":" in which:
            # A range, both ends included and in either order: 0:-4 and -4:-1
            # are the last four runs.
            a, b = (_run_index(end, runs) for end in which.split(":", 1))
            first, last = min(a, b), max(a, b)
            if not runs:
                raise SystemExit(none_yet)
            if -last > len(runs):
                raise SystemExit(
                    f"summary: only {len(runs)} run(s) with results in '{logs}'")
            if -first > len(runs):
                print(f"summary: only {len(runs)} run(s) with results — "
                      "showing all of them", file=sys.stderr)
                first = -len(runs)
            return runs[first:last + 1 or None]

        n = int(which)
    except ValueError:
        raise SystemExit(f"summary: '{which}' is neither a run number, a range "
                         "like 0:-4, nor a file")

    if n == 0:
        raise SystemExit("summary: runs count back from -1 (the last one)")
    n = -abs(n)
    if -n > len(runs):
        raise SystemExit(
            f"summary: only {len(runs)} run(s) with results in '{logs}' "
            f"(-l -1 … -l -{len(runs)})" if runs else none_yet
        )
    return [runs[n]]


def run_title(path: str) -> str:
    m = re.match(r"run-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", os.path.basename(path))
    if not m:
        return os.path.basename(path)
    y, mo, d, h, mi, _ = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}"


def load_roster(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    return {p["id"]: p for p in load_players_from_file(path, None)}


def load_rows(path: str, roster: dict) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        known = roster.get(row.get("player_id"), {})
        name = row.get("player_name") or ""
        if not name or name == "N/A":
            name = known.get("name") or name or "N/A"
        row["player_name"] = name
        # Runs from before the alliance column only have the roster to go on.
        row["alliance"] = row.get("alliance") or known.get("alliance") or ""
        row["kingdom_id"] = row.get("kingdom_id") or "?"
        row["status"] = row.get("status") or "UNKNOWN"
    return rows


def merge(runs: list) -> list:
    """Rows of several runs, keeping the newest outcome of each (player, code).

    A later ALREADY_CLAIMED does not hide the SUCCESS that caused it.
    """
    pairs = {}
    for rows in runs:
        for row in rows:
            key = (row["player_id"], row["code"])
            before = pairs.get(key)
            if (before and category(before["status"]) == "s"
                    and category(row["status"]) == "a"):
                continue
            pairs.pop(key, None)  # re-insert so the order follows the newest
            pairs[key] = row
    return list(pairs.values())


def group(rows: list) -> list:
    """One line per player and outcome, with the codes it applied to."""
    lines = {}
    for row in rows:
        status = row["status"]
        # Successes all say the same thing; errors keep their reason apart
        # (NOT_ELIGIBLE can mean Town Center level or a claim limit).
        detail = row.get("message", "") if category(status) == "e" else ""
        key = (row["player_id"], status, detail)
        if key not in lines:
            lines[key] = {**row, "codes": [], "detail": detail}
        if row["code"] not in lines[key]["codes"]:
            lines[key]["codes"].append(row["code"])
    return sorted(
        lines.values(),
        # Players without an alliance go after those with one.
        key=lambda r: (rank(r["status"]), not r["alliance"], r["alliance"].lower(),
                       r["player_name"].lower(), r["player_id"]),
    )


def tally(rows: list, paint: Painter) -> str:
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    parts = []
    for status in sorted(counts, key=rank):
        glyph = STATUS_GLYPHS.get(status, "?")
        parts.append(paint(style_for(status), f"{glyph} {counts[status]} {status.lower()}"))
    return "  ".join(parts)


def table(lines: list, paint: Painter) -> list:
    with_alliance = any(r["alliance"] for r in lines)
    columns = ["STATUS", "PLAYER"] + (["ALLIANCE"] if with_alliance else []) + ["ID", "CODES"]
    with_detail = any(r["detail"] for r in lines)
    if with_detail:
        columns.append("DETAIL")

    cells = []
    for r in lines:
        row = [f"{STATUS_GLYPHS.get(r['status'], '?')} {r['status']}", r["player_name"]]
        if with_alliance:
            row.append(r["alliance"] or "-")
        row += [r["player_id"], ", ".join(r["codes"])]
        if with_detail:
            row.append(r["detail"])
        cells.append((r["status"], row))

    widths = [width(c) for c in columns]
    for _, row in cells:
        widths = [max(w, width(c)) for w, c in zip(widths, row)]

    def fmt(row):
        padded = [c + " " * (w - width(c)) for c, w in zip(row, widths)]
        return "  " + "  ".join(padded).rstrip()

    out = [paint("dim", fmt(columns))]
    for status, row in cells:
        out.append(paint(style_for(status), fmt(row)))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="santaclaus summary",
        description="Readable report of a redemption run, one table per kingdom, "
        "errors first.",
    )
    parser.add_argument(
        "-l",
        "--last",
        metavar="N|A:B|FILE",
        help="-1 is the last run that attempted anything, -2 the one before, … "
        "A range joins several: 0:-4 (or -1:-4) is the last four. A results "
        "CSV path also works. Default: the last run that redeemed something "
        "(what `santaclaus --latest` shows)",
    )
    parser.add_argument(
        "-f",
        "--filter",
        metavar="LETTERS",
        help="Only these outcomes: e = errors (anything not claimed: NOT_ELIGIBLE, "
        "PLAYER_ERROR, EXPIRED, …), s = successes, a = already claimed. "
        "E.g. `-f es`. Default: all",
    )
    parser.add_argument("-k", "--kingdom", action="append", metavar="ID",
                        help="Only this kingdom; repeatable")
    parser.add_argument("-U", "--users-file", metavar="FILE",
                        help="Roster to take alliances (and missing names) from")
    parser.add_argument("--logs", default=DEFAULT_LOGS, metavar="DIR",
                        help=f"Where the run results live (default: {DEFAULT_LOGS})")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="Colorize output (default: auto, only on a terminal "
                        "and when NO_COLOR is unset)")
    # argparse takes '-1:-3' for an option, not a value; glue it to its flag.
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, arg in enumerate(argv[:-1]):
        if arg in ("-l", "--last") and argv[i + 1].startswith("-"):
            argv[i:i + 2] = [f"--last={argv[i + 1]}"]
            break
    args = parser.parse_args(argv)

    wanted = set(CATEGORIES)
    if args.filter:
        wanted = set(args.filter.lower())
        unknown = wanted - set(CATEGORIES)
        if unknown:
            parser.error(
                f"unknown filter letter(s) {''.join(sorted(unknown))!r}; use "
                + ", ".join(f"{k} = {v}" for k, v in CATEGORIES.items())
            )

    paint = Painter(use_color(args.color))
    paths = pick_runs(args.logs, args.last)
    roster = load_roster(args.users_file)
    loaded = [load_rows(path, roster) for path in paths]
    attempts = sum(len(rows) for rows in loaded)
    rows = merge(loaded)
    shown = [
        r for r in rows
        if category(r["status"]) in wanted
        and (not args.kingdom or r["kingdom_id"] in args.kingdom)
    ]

    players = {r["player_id"] for r in rows}
    codes = {r["code"] for r in rows}
    if len(paths) == 1:
        title = run_title(paths[0])
        source = os.path.basename(paths[0])
        print(paint("bold", f"Run {title}")
              + (paint("dim", f"  {source}") if source != title else ""))
    else:
        start, end = run_title(paths[0]), run_title(paths[-1])
        if start[:11] == end[:11] and re.match(r"\d{4}-\d\d-\d\d ", start):
            end = end[11:]  # same day: the date once is enough
        print(paint("bold", f"Runs {start} → {end}")
              + paint("dim", f"  {len(paths)} runs, {attempts} attempt(s), "
                             "newest outcome per player and code"))
    noun = "attempt(s)" if len(paths) == 1 else "outcome(s)"
    print(f"{len(players)} player(s) · {len(codes)} code(s) · {len(rows)} {noun}")
    if rows:
        print(tally(rows, paint))
    if len(shown) != len(rows):
        print(paint("dim", f"showing {len(shown)} of {len(rows)} {noun}"))

    if not shown:
        print("\nNothing to show" + (" for this filter." if rows else "."))
        return 0

    kingdoms = {}
    for r in shown:
        kingdoms.setdefault(r["kingdom_id"], []).append(r)

    def kingdom_key(kid):
        return (0, int(kid)) if kid.isdigit() else (1, kid)

    for kid in sorted(kingdoms, key=kingdom_key):
        krows = kingdoms[kid]
        count = len({r["player_id"] for r in krows})
        print()
        print(paint("heading", f"Kingdom {kid}") + paint("dim", f"  {count} player(s)  ")
              + tally(krows, paint))
        for line in table(group(krows), paint):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
