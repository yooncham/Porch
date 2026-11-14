#!/usr/bin/env bash
# Run Porch's own Matter Server on this Mac. Leave it running in its own Terminal tab.
#
#   deploy/home/matter_server.sh
#
# Its dashboard, for pairing devices, is at http://127.0.0.1:5580. It listens on this Mac only.
# ~/.config/porch-hub/matter holds Porch's Matter credentials: back it up. If it is lost, every
# device has to be paired again.
set -euo pipefail

CONF="${PORCH_CONFIG_DIR:-$HOME/.config/porch-hub}"
mkdir -p "$CONF/matter"

# Matter finds devices through the Wi-Fi interface (en0 on most MacBooks).
IFACE="${PORCH_MATTER_INTERFACE:-}"
if [ -z "$IFACE" ] && command -v networksetup >/dev/null; then
  IFACE="$(networksetup -listallhardwareports | awk '/^Hardware Port: Wi-Fi/ { getline; print $2; exit }')"
fi
IFACE="${IFACE:-en0}"

echo "Matter Server: storage $CONF/matter, network interface $IFACE"
echo "Dashboard: http://127.0.0.1:5580 (Ctrl+C stops the server)"
exec npx --yes matter-server --storage-path "$CONF/matter" --listen-address 127.0.0.1 --primary-interface "$IFACE"
