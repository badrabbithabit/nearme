#!/bin/sh
# Start/stop the nearme dev server WITHOUT killing the machine's other Pythons.
#
# Never use `taskkill //F //IM python.exe` here: that name matches the user's
# Strata model server (127.0.0.1:8081) and SearXNG (127.0.0.1:8888), and has
# already taken both down once. This script records a PID and kills only that.
#
#   ./devserver.sh start [port]   ./devserver.sh stop   ./devserver.sh status
set -e
cd "$(dirname "$0")"
PORT="${2:-8388}"
PIDF=.server.pid

case "$1" in
  start)
    if [ -f "$PIDF" ] && tasklist //FI "PID eq $(cat $PIDF)" 2>/dev/null | grep -qi python; then
      echo "already running, pid $(cat $PIDF) on port $(sed -n 2p $PIDF)"
      exit 0
    fi
    (python server.py --port "$PORT" > server.log 2>&1 &)
    sleep 6
    PID=$(netstat -ano | grep -E "127.0.0.1:$PORT.*LISTENING" | awk '{print $5}' | head -1)
    if [ -z "$PID" ]; then echo "FAILED to start; server.log:"; tail -20 server.log; exit 1; fi
    printf '%s\n%s\n' "$PID" "$PORT" > "$PIDF"
    echo "listening on 127.0.0.1:$PORT (pid $PID)"
    ;;
  stop)
    [ -f "$PIDF" ] || { echo "no $PIDF"; exit 0; }
    PID=$(sed -n 1p "$PIDF")
    taskkill //F //PID "$PID" >/dev/null 2>&1 && echo "stopped pid $PID" || echo "pid $PID already gone"
    rm -f "$PIDF"
    ;;
  status)
    if [ -f "$PIDF" ]; then
      PID=$(sed -n 1p "$PIDF")
      tasklist //FI "PID eq $PID" 2>/dev/null | grep -qi python \
        && echo "running: pid $PID port $(sed -n 2p $PIDF)" || echo "pid $PID not running (stale $PIDF)"
    else
      echo "not started via devserver.sh"
    fi
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start "$PORT"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status} [port]"; exit 2
    ;;
esac
