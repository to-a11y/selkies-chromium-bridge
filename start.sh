#!/bin/bash
set -e

mkdir -p \
  "$XDG_RUNTIME_DIR" \
  "$PULSE_RUNTIME_PATH"

chmod 700 "$XDG_RUNTIME_DIR"

echo '=== Xvfb ==='

# docker restart keeps /tmp in the container writable layer.
# Remove stale X11 files left by a previous Xvfb process.
rm -f /tmp/.X99-lock
rm -f /tmp/.X11-unix/X99
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

Xvfb :99 \
  -screen 0 1280x800x24 \
  +extension COMPOSITE \
  +extension DAMAGE \
  +extension RANDR \
  +extension RENDER \
  +extension MIT-SHM \
  +extension XFIXES \
  +extension XTEST \
  -nolisten tcp \
  -ac \
  -noreset \
  >/tmp/xvfb.log 2>&1 &

XVFB_PID=$!

echo "Xvfb PID=$XVFB_PID"

# Do not trust the socket file. Wait until X really answers.
XVFB_OK=0

for i in $(seq 1 100); do
  if DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
    XVFB_OK=1
    break
  fi

  if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "ERROR: Xvfb exited during startup"
    cat /tmp/xvfb.log || true
    exit 1
  fi

  sleep 0.1
done

if [ "$XVFB_OK" != "1" ]; then
  echo "ERROR: Xvfb did not become ready"
  cat /tmp/xvfb.log || true
  exit 1
fi

echo "Xvfb is ready"

echo '=== Openbox ==='

DISPLAY=:99 openbox &

echo '=== PulseAudio ==='

mkdir -p /run/pulse
rm -f /run/pulse/native
chown -R pulse:pulse /run/pulse

pulseaudio \
  --system \
  --daemonize=yes \
  --exit-idle-time=-1 \
  --disallow-exit \
  --log-target=file:/tmp/pulseaudio.log \
  -n \
  -L "module-native-protocol-unix socket=/run/pulse/native auth-anonymous=1" \
  -L "module-null-sink sink_name=output rate=48000 channels=2"

for i in $(seq 1 50); do
  [ -S /run/pulse/native ] && break
  sleep 0.1
done

if [ ! -S /run/pulse/native ]; then
  echo "PulseAudio socket did not appear"
  cat /tmp/pulseaudio.log || true
  exit 1
fi

echo '=== PulseAudio devices ==='
PULSE_SERVER=unix:/run/pulse/native pactl list short sinks || true
PULSE_SERVER=unix:/run/pulse/native pactl list short sources || true

sleep 1

echo '=== Chromium watchdog ==='

mkdir -p /data/chrome-profile

chrome_watchdog() {
  while true; do
    echo "Cleaning stale Chrome profile locks..."

    rm -f       /data/chrome-profile/SingletonLock       /data/chrome-profile/SingletonSocket       /data/chrome-profile/SingletonCookie

    echo "Starting Google Chrome..."

    DISPLAY=:99 google-chrome-stable \
      --no-sandbox \
      --disable-dev-shm-usage \
      --no-first-run \
      --no-default-browser-check \
      --user-data-dir=/data/chrome-profile \
      --remote-debugging-address=127.0.0.1 \
      --remote-debugging-port=9222 \
      --remote-allow-origins=* \
      --window-size=1280,800 \
      --start-maximized \
      --restore-last-session \


    EXIT_CODE=$?

    echo "Chrome exited code=$EXIT_CODE; restarting in 1 second..."

    sleep 1
  done
}

chrome_watchdog &

sleep 2

echo '=== Waiting for Chrome CDP ==='

for i in $(seq 1 50); do
  if curl -fsS http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    echo 'Chrome CDP ready'
    break
  fi
  sleep 0.2
done

echo '=== FileBridge ==='
python3 /filebridge.py &

sleep 1

echo '=== Selkies NEW ==='

exec selkies \
  --addr=0.0.0.0 \
  --port=8080 \
  --enable-https=false \
  --enable-basic-auth=false \
  --encoder=h264enc-striped \
  --enable-resize=false
