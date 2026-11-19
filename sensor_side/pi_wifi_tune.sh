#!/usr/bin/env bash
# Stop the Pi's WiFi from sleeping between packets.
#
# A Raspberry Pi puts its WiFi chip into power-save by default. The symptom is
# pings that jump between 2 ms and 1000 ms with no packet loss, and point-cloud
# downloads that take seconds although the signal is fine. Run ON THE PI, as your
# normal user (not root):
#
#     curl -4 -fL --connect-timeout 10 --max-time 60 -o pi_wifi_tune.sh \
#         https://raw.githubusercontent.com/VishAndChips1/BaseStationViewer/main/sensor_side/pi_wifi_tune.sh
#     bash pi_wifi_tune.sh
#
# It changes ONE thing (WiFi power-save) and nothing else: it does not touch the
# control service, the sensor pipeline, the EEPROM or any other setting. Safe to
# re-run. To undo:  sudo rm /etc/NetworkManager/conf.d/99-pitfusion3d-wifi.conf
#                   (or: sudo systemctl disable --now pitfusion3d-wifi.service) and reboot.
set -euo pipefail

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }

echo "PitFusion3D WiFi tuning - starting (user: $(id -un))"
if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not as root." >&2
    exit 1
fi
command -v iw >/dev/null || { echo "'iw' is not installed (sudo apt install iw), cannot continue." >&2; exit 1; }

IFACE="${PITFUSION_WIFI_IFACE:-$(iw dev 2>/dev/null | awk '$1=="Interface"{print $2; exit}')}"
if [ -z "$IFACE" ]; then
    echo "No WiFi interface found. Is this Pi connected by WiFi?" >&2
    exit 1
fi
if iw dev "$IFACE" info 2>/dev/null | grep -qE '^[[:space:]]*type AP'; then
    echo "$IFACE is acting as the WiFi hotspot (access point) right now."
    echo "Power-save only affects a Pi that is joined to someone else's WiFi, so there is"
    echo "nothing to change here. Run this script while the Pi is on your router's network."
    exit 0
fi
GATEWAY="$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')"

power_state() { iw dev "$IFACE" get power_save 2>/dev/null | sed 's/^ *//' || true; }
latency() {
    if [ -n "$GATEWAY" ]; then
        timeout 20 ping -c 10 -i 0.2 -q "$GATEWAY" 2>/dev/null \
            | awk -F'/' '/^(rtt|round-trip)/{printf "average %.0f ms, worst %.0f ms to the router", $5, $6}' || true
    fi
}

say "1/4  Now"
echo "Interface:  $IFACE"
echo "$(power_state)"
iw dev "$IFACE" link 2>/dev/null | grep -E 'signal|tx bitrate' | sed 's/^ */            /' || true
BEFORE="$(latency)"
[ -n "$BEFORE" ] && echo "Latency:    $BEFORE"

if ! sudo -n true 2>/dev/null; then
    echo "The next steps need sudo. Enter your password if asked:"
    sudo -v || { warn "sudo is required to change the WiFi setting."; exit 1; }
fi

say "2/4  Turning WiFi power-save off (takes effect immediately)"
timeout 15 sudo iw dev "$IFACE" set power_save off || { warn "could not change power-save on $IFACE"; exit 1; }

say "3/4  Making it permanent"
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    CONF="/etc/NetworkManager/conf.d/99-pitfusion3d-wifi.conf"
    printf '[connection]\n# 2 = disable WiFi power-save (written by pi_wifi_tune.sh)\nwifi.powersave = 2\n' \
        | sudo tee "$CONF" >/dev/null
    echo "NetworkManager will keep power-save off ($CONF)."
else
    UNIT="/etc/systemd/system/pitfusion3d-wifi.service"
    IW="$(command -v iw)"
    printf '[Unit]\nDescription=Keep WiFi power-save off (PitFusion3D)\nAfter=network.target\n\n[Service]\nType=oneshot\nExecStart=%s dev %s set power_save off\nRemainAfterExit=yes\n\n[Install]\nWantedBy=multi-user.target\n' \
        "$IW" "$IFACE" | sudo tee "$UNIT" >/dev/null
    timeout 30 sudo systemctl daemon-reload
    timeout 30 sudo systemctl enable pitfusion3d-wifi.service >/dev/null 2>&1 || warn "could not enable $UNIT"
    echo "A small systemd service will re-apply it at every boot ($UNIT)."
fi

say "4/4  Result"
echo "$(power_state)"
sleep 1
AFTER="$(latency)"
[ -n "$BEFORE" ] && echo "Before:     $BEFORE"
[ -n "$AFTER" ]  && echo "After:      $AFTER"
echo
echo "Done. Nothing else was changed. In the base station, the stream should now"
echo "refresh faster and more evenly. If latency is still high, the signal itself is"
echo "weak (check the signal line above: better than -70 dBm is healthy)."
