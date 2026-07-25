#!/usr/bin/env bash
# tools/sim.sh — one command for the local imaging_sat stack.
#
#   ./tools/sim.sh start              # sim + bridge + ui + exerciser
#   ./tools/sim.sh start sim bridge   # any subset, in dependency order
#   ./tools/sim.sh status             # what's running, where, and the URLs
#   ./tools/sim.sh stop               # stop everything (or a subset)
#   ./tools/sim.sh restart            # stop + start
#   ./tools/sim.sh logs sim           # tail one component's log
#
# Components and ports:
#   sim        xtce-sim run       TCP :5001  (5000 belongs to molniya-viewer)
#   bridge     xtce-sim bridge    SSE http://127.0.0.1:8600/telemetry/stream
#   ui         xtce-sim ui        console http://127.0.0.1:8080/
#   exerciser  xtce-sim exercise  looping, one send per second
#
# molniya-viewer is NOT started here (separate repo); point it at the
# bridge with:  XTCE_SSE_URL=http://localhost:8600/telemetry/stream ./run.sh
#
# Uses .venv/bin/xtce-sim directly (not `uv run`, which may re-sync the
# venv as a side effect). Logs and pidfiles live under /tmp/xtce-sim-stack.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XTCE="$ROOT/.venv/bin/xtce-sim"
DEF="$ROOT/examples/imaging_sat/imaging_sat.xml"
RUN_DIR="/tmp/xtce-sim-stack"

SIM_PORT=5001
UI_PORT=8080
SSE_PORT=8600
INSTANCE=stack
SAT_ID=90001
SAT_NAME=IMAGING-SAT-1
SAT_COLOR="#ffcc00"

ALL=(sim bridge ui exerciser)

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

wanted() {
  local name=$1 want
  shift
  for want in "$@"; do
    if [[ $want == "$name" ]]; then
      return 0
    fi
  done
  return 1
}

pidfile() { echo "$RUN_DIR/$1.pid"; }
logfile() { echo "$RUN_DIR/$1.log"; }

alive() {
  local pid_path
  pid_path="$(pidfile "$1")"
  [[ -f "$pid_path" ]] && kill -0 "$(cat "$pid_path")" 2>/dev/null
}

port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- && return 0
  return 1
}

wait_for_port() {
  local port=$1 name=$2 tries=0
  until port_open "$port"; do
    tries=$((tries + 1))
    if [[ $tries -gt 20 ]]; then
      echo "ERROR: $name never opened port $port — see $(logfile "$name")" >&2
      exit 1
    fi
    sleep 0.5
  done
}

launch() {
  local name=$1
  shift
  if alive "$name"; then
    echo "$name: already running (pid $(cat "$(pidfile "$name")"))"
    return 0
  fi
  nohup "$@" >"$(logfile "$name")" 2>&1 &
  echo $! >"$(pidfile "$name")"
  echo "$name: started (pid $!, log $(logfile "$name"))"
}

start_one() {
  case $1 in
    sim)
      if ! alive sim && port_open "$SIM_PORT"; then
        echo "ERROR: something else already holds port $SIM_PORT" >&2
        exit 1
      fi
      launch sim "$XTCE" run "$DEF" --port "$SIM_PORT" --id "$INSTANCE" --interval 1
      wait_for_port "$SIM_PORT" sim
      ;;
    bridge)
      launch bridge "$XTCE" bridge --port "$SIM_PORT" --def "$DEF" \
        --sat-id "$SAT_ID" --name "$SAT_NAME" --color "$SAT_COLOR" --sse-port "$SSE_PORT"
      ;;
    ui)
      launch ui "$XTCE" ui --port "$SIM_PORT" --def "$DEF" --http-port "$UI_PORT"
      ;;
    exerciser)
      launch exerciser "$XTCE" exercise --def "$DEF" --port "$SIM_PORT" --loop --pause 1
      ;;
    *)
      echo "ERROR: unknown component '$1' (one of: ${ALL[*]})" >&2
      exit 1
      ;;
  esac
}

stop_one() {
  local name=$1 pid_path pid
  pid_path="$(pidfile "$name")"
  if ! alive "$name"; then
    rm -f "$pid_path"
    echo "$name: not running"
    return 0
  fi
  pid="$(cat "$pid_path")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    echo "$name: force-killed (pid $pid)"
  else
    echo "$name: stopped (pid $pid)"
  fi
  rm -f "$pid_path"
}

status_one() {
  local name=$1 extra=""
  case $name in
    sim) extra="  CCSDS 127.0.0.1:$SIM_PORT" ;;
    bridge) extra="  http://127.0.0.1:$SSE_PORT/telemetry/stream" ;;
    ui) extra="  http://127.0.0.1:$UI_PORT/" ;;
  esac
  if alive "$name"; then
    printf "  %-10s running  pid %-8s%s\n" "$name" "$(cat "$(pidfile "$name")")" "$extra"
  else
    printf "  %-10s stopped%s\n" "$name" "$extra"
  fi
}

main() {
  [[ $# -ge 1 ]] || usage
  local action=$1
  shift
  local targets=("$@")
  [[ ${#targets[@]} -gt 0 ]] || targets=("${ALL[@]}")
  mkdir -p "$RUN_DIR"
  [[ -x "$XTCE" ]] || { echo "ERROR: $XTCE not found — run 'uv sync --extra dev' first" >&2; exit 1; }

  case $action in
    start)
      # Dependency order regardless of how the subset was typed: the sim
      # first, its clients after.
      for name in "${ALL[@]}"; do
        if wanted "$name" "${targets[@]}"; then
          start_one "$name"
        fi
      done
      ;;
    stop)
      # Clients first, the sim last, so nothing spends its final seconds
      # logging reconnect noise.
      for ((i = ${#ALL[@]} - 1; i >= 0; i--)); do
        if wanted "${ALL[$i]}" "${targets[@]}"; then
          stop_one "${ALL[$i]}"
        fi
      done
      ;;
    restart)
      "$0" stop "${targets[@]}"
      "$0" start "${targets[@]}"
      ;;
    status)
      echo "xtce-sim stack ($RUN_DIR):"
      for name in "${ALL[@]}"; do status_one "$name"; done
      if port_open 5000; then
        echo "  molniya-viewer appears up: http://localhost:5000  (not managed here)"
      fi
      ;;
    logs)
      [[ ${#targets[@]} -eq 1 && ${targets[0]} != "" ]] || { echo "usage: $0 logs <component>" >&2; exit 1; }
      exec tail -f "$(logfile "${targets[0]}")"
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
