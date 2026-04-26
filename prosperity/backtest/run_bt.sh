#!/usr/bin/env bash
set -euo pipefail

# Wrapper around backtest/run.py:
# - First arg: strategy file (e.g. final.py)
# - Following positional args until first --flag: DAYS... (e.g. 1-0 1-1 or 1)
# - Remaining args are passed through to prosperity4bt CLI

if [[ $# -lt 2 ]]; then
  cat <<'EOF'
Usage:
  ./backtest/run_bt.sh <strategy.py> <day-or-round> [more-days...] [--extra-flags]

Examples:
  ./backtest/run_bt.sh final.py 1-0
  ./backtest/run_bt.sh round1trade/final_ash.py 2 --out backtest/runs/mytest.log
  ./backtest/run_bt.sh round1trade/new_ash_strategy.py 2 --out backtest/runs/mytest.log
  ./backtest/run_bt.sh final.py 1-0 1-1 --out backtest/runs/mytest.log
  ./backtest/run_bt.sh round1trade/final.py 1--2 2--1 2-0 2-1 --merge-pnl --out backtest/runs/mytest.log


    ./backtest/run_bt.sh round1trade/final_root.py 2--1 2-0 --merge-pnl --out backtest/runs/pepper_root.log

Notes:
  - Single day: <round>-<day>  (e.g. 1-0)
  - Whole round: <round>       (e.g. 1)
  - For negative day, use <round>--<abs(day)> (e.g. 1--1)
EOF
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3}"
STRATEGY="$1"
shift

declare -a DAYS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --*)
      break
      ;;
    *)
      DAYS+=("$1")
      shift
      ;;
  esac
done

if [[ ${#DAYS[@]} -eq 0 ]]; then
  echo "Error: at least one day/round is required."
  exit 1
fi

HAS_DATA=0
DATA_DIR=""
declare -a PASSTHRU=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data)
      HAS_DATA=1
      shift
      if [[ $# -eq 0 ]]; then
        echo "Error: --data requires a directory argument."
        exit 1
      fi
      DATA_DIR="$1"
      shift
      ;;
    --data=*)
      HAS_DATA=1
      DATA_DIR="${1#--data=}"
      shift
      ;;
    *)
      PASSTHRU+=("$1")
      shift
      ;;
  esac
done

if [[ $HAS_DATA -eq 0 ]]; then
  DATA_DIR="./data/bt"
fi

normalize_data_dir_if_needed() {
  local src_dir="$1"

  if [[ ! -d "$src_dir" ]]; then
    echo "Error: data directory not found: $src_dir"
    exit 1
  fi

  if compgen -G "$src_dir/round*/prices_round_*_day_*.csv" > /dev/null; then
    echo "$src_dir"
    return
  fi

  if ! compgen -G "$src_dir/prices_round_*_day_*.csv" > /dev/null; then
    echo "$src_dir"
    return
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"

  for f in "$src_dir"/{prices,trades,observations}_round_*_day_*.csv; do
    [[ -e "$f" ]] || continue
    local base
    base="$(basename "$f")"

    if [[ "$base" =~ _round_([0-9]+)_day_ ]]; then
      local round_num
      round_num="${BASH_REMATCH[1]}"
      mkdir -p "$tmp_dir/round$round_num"
      ln -s "$(cd "$src_dir" && pwd)/$base" "$tmp_dir/round$round_num/$base"
    fi
  done

  trap 'rm -rf "$tmp_dir"' EXIT
  echo "$tmp_dir"
}

DATA_DIR="$(normalize_data_dir_if_needed "$DATA_DIR")"

cmd=("$PYTHON_BIN" backtest/run.py "$STRATEGY" "${DAYS[@]}")
cmd+=(--data "$DATA_DIR")
cmd+=("${PASSTHRU[@]}")

echo "Running: ${cmd[*]}"
exec "${cmd[@]}"
