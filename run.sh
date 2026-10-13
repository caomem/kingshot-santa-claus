#!/usr/bin/env bash
#
# Redeem the current gift codes for the whole roster.
#
# The run is detached from the calling terminal: it survives Ctrl+C, a dropped
# SSH session or a closed tty. By default this script streams the run's output
# to stdout anyway, so it still behaves like a normal foreground command.
#
#   run.sh              start a run and stream it
#   run.sh --detach     start a run and return immediately
#   run.sh --follow     attach to the run in progress (or replay the last one)
#   run.sh --latest     print the last run that actually changed something
#   run.sh summary      tabulate a run by kingdom, errors first (summary --help)
#
# Any other argument is passed through to santaclaus.py and overrides the
# defaults below, e.g. `run.sh --detach -v verbose` or `run.sh --force`.

set -euo pipefail

# How many runs to keep on disk. Older ones are deleted after every run, except
# the one --latest points at, which is kept however old it gets.
keep_runs=${SANTACLAUS_KEEP_RUNS:-10}

# --- locate the repo, following symlinks --------------------------------
source_path=${BASH_SOURCE[0]}
while [ -L "$source_path" ]; do
    link_dir=$(cd -P "$(dirname "$source_path")" && pwd)
    source_path=$(readlink "$source_path")
    [[ $source_path == /* ]] || source_path=$link_dir/$source_path
done
repo=$(cd -P "$(dirname "$source_path")" && pwd)
self=$repo/run.sh

# santaclaus.py is standard library only, so the system interpreter will do.
python=$repo/.venv/bin/python
if [ ! -x "$python" ]; then
    python=$(command -v python3 || true)
    if [ -z "$python" ]; then
        echo "run.sh: no Python interpreter found (looked for $repo/.venv/bin/python)" >&2
        exit 1
    fi
fi

log_dir=$repo/logs

# `summary` is a report with flags of its own (-f, -l), so it must not reach
# the argument loop below, where those mean --follow and --latest.
if [ "${1:-}" = summary ]; then
    shift
    exec "$python" "$repo/santaclaus.py" summary --logs "$log_dir" -U "$repo/freemen.csv" "$@"
fi

mkdir -p "$log_dir"
lock_file=$log_dir/.run.lock
pid_file=$log_dir/.run.pid
current=$log_dir/current.log   # the run that just started, for --follow
latest=$log_dir/latest.log     # the last run that changed something

# --- housekeeping -------------------------------------------------------

# "Changed something" means at least one code was actually redeemed. A run where
# everything was already claimed or skipped is not worth keeping as the latest.
run_changed() {
    local csv=${1%.log}.csv
    [ -s "$csv" ] || return 1
    "$python" - "$csv" <<'PY'
import csv, sys
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    hit = any(row.get("status") in ("SUCCESS", "PENDING") for row in csv.DictReader(f))
sys.exit(0 if hit else 1)
PY
}

# Circular buffer: keep the newest $keep_runs runs, plus whatever --latest
# points at, and delete every artifact of the rest.
rotate_runs() {
    local keep_name="" name
    if [ -L "$latest" ]; then
        keep_name=$(basename "$(readlink "$latest")")
    fi

    ls -1 "$log_dir"/run-*.log 2>/dev/null | sort -r | tail -n +$((keep_runs + 1)) |
        while IFS= read -r old; do
            name=$(basename "$old")
            [ "$name" = "$keep_name" ] && continue
            rm -f "$old" "$old.status" "${old%.log}.csv"
        done
    return 0
}

# --- internal: the detached worker --------------------------------------
if [ "${1:-}" = "--exec-run" ]; then
    shift
    log=$1
    shift

    echo $$ >"$pid_file"
    rc=0
    "$@" >>"$log" 2>&1 || rc=$?
    printf '%s\n' "$rc" >"$log.status"

    if run_changed "$log"; then
        ln -sfn "$(basename "$log")" "$latest"
        ln -sfn "$(basename "${log%.log}.csv")" "${latest%.log}.csv"
    fi
    rotate_runs

    rm -f "$pid_file"
    exit "$rc"
fi

# --- arguments ----------------------------------------------------------
detach=false
follow=false
show_latest=false
extra=()
while [ $# -gt 0 ]; do
    case $1 in
        -d | --detach) detach=true ;;
        -f | --follow) follow=true ;;
        -l | --latest) show_latest=true ;;
        --)
            shift
            extra+=("$@")
            break
            ;;
        *) extra+=("$1") ;;
    esac
    shift
done

# --- streaming ----------------------------------------------------------
# Ctrl+C here only stops watching. The worker is in its own session, so it
# never sees the signal.
on_interrupt() {
    echo "run.sh: stopped watching; the run continues (run.sh --follow to re-attach)" >&2
    exit 130
}

stream_run() {
    local log=$1 pid=${2:-}
    trap on_interrupt INT
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        # --pid makes tail exit on its own once the worker is gone.
        tail -n +1 -f --pid="$pid" "$log"
    else
        cat "$log"
    fi
    trap - INT
}

# Exit with whatever the worker exited with, so `&&` chains and cron behave.
exit_with_status() {
    local log=$1
    if [ -s "$log.status" ]; then
        exit "$(cat "$log.status")"
    fi
    exit 0
}

attach_to_existing() {
    local log="" pid=""
    if [ -e "$current" ]; then
        log=$(readlink -f "$current")
    else
        # No pointer yet (first run after an upgrade): fall back to the newest
        # run on disk, which is what "in progress" means anyway.
        log=$(ls -1 "$log_dir"/run-*.log 2>/dev/null | sort -r | head -1 || true)
    fi
    if [ -z "$log" ] || [ ! -e "$log" ]; then
        echo "run.sh: no run to follow yet" >&2
        exit 1
    fi
    if [ -s "$pid_file" ]; then
        pid=$(cat "$pid_file" 2>/dev/null || true)
    fi
    stream_run "$log" "$pid"
    exit_with_status "$log"
}

if $show_latest; then
    target=""
    # -e follows the link, so a dangling pointer fails this too.
    if [ -e "$latest" ]; then
        target=$(readlink -f "$latest")
    fi
    if [ -z "$target" ] || [ ! -f "$target" ]; then
        if [ -L "$latest" ]; then
            # Pointed at a run that is gone: stale, not merely absent.
            rm -f "$latest" "${latest%.log}.csv"
        fi
        # Nothing has been redeemed yet, so show the last run instead of
        # sending the caller off to type a second command.
        echo "run.sh: no run has redeemed anything yet — showing the last run" >&2
        attach_to_existing
    fi
    echo "[i] last run with changes: ${target#"$repo"/}" >&2
    cat "$target"
    exit 0
fi

if $follow; then
    attach_to_existing
fi

# --- start a run --------------------------------------------------------
# Hold the lock on fd 9 and let the worker inherit it: the lock lives as long
# as the worker does, so a second invocation attaches instead of double-running.
exec 9>"$lock_file"
if ! flock -n 9; then
    exec 9>&-
    # --detach promises to return immediately, so it must not start streaming
    # here. This is the cron case: a scheduled run firing while the previous
    # one is still going should simply step aside.
    if $detach; then
        echo "run.sh: a run is already in progress — nothing to do" >&2
        exit 0
    fi
    echo "run.sh: a run is already in progress — attaching to it" >&2
    attach_to_existing
fi

# Runs are serialised by the lock, so a collision only happens when one finishes
# inside the same second the next starts. Cheap to rule out anyway.
stamp=$(date +%Y%m%d-%H%M%S)
suffix=""
n=1
while [ -e "$log_dir/run-$stamp$suffix.log" ]; do
    n=$((n + 1))
    suffix=.$n
done
stamp=$stamp$suffix
log=$log_dir/run-$stamp.log
: >"$log"
rm -f "$log.status"
ln -sfn "run-$stamp.log" "$current"

cmd=(
    "$python" "$repo/santaclaus.py"
    -U "$repo/freemen.csv"
    --kid 2279
    --history "$log_dir/history.csv"
    --out "$log_dir/run-$stamp.csv"
    --auto
    --verbose oneline
)
cmd+=(${extra[@]+"${extra[@]}"})

setsid "$self" --exec-run "$log" "${cmd[@]}" </dev/null >/dev/null 2>&1 &

exec 9>&-   # the worker holds the lock from here on

worker_pid=""
for _ in $(seq 1 60); do
    if [ -s "$pid_file" ]; then
        worker_pid=$(cat "$pid_file")
        break
    fi
    sleep 0.05
done

# Meta goes to stderr so stdout carries only the program's own output. A very
# short run can finish before the pid file is read; that is not worth reporting.
if [ -n "$worker_pid" ]; then
    echo "[i] worker $worker_pid -> ${log#"$repo"/}" >&2
else
    echo "[i] run -> ${log#"$repo"/}" >&2
fi

if $detach; then
    exit 0
fi

stream_run "$log" "$worker_pid"
exit_with_status "$log"
