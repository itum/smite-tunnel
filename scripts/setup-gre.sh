#!/bin/bash
# Smite GRE helper — detect underlay NIC automatically (not hardcoded to eth0)
# Usage:
#   NODE_ROLE=iran GRE_PEER_IP=1.2.3.4 ./scripts/setup-gre.sh
#   ./scripts/setup-gre.sh --role foreign --peer 5.6.7.8

set -euo pipefail

ROLE="${NODE_ROLE:-iran}"
PEER="${GRE_PEER_IP:-}"
NETWORK="${GRE_NETWORK:-172.17.1.0/30}"
IFACE="${GRE_IFACE:-smite-gre}"
MTU="${GRE_MTU:-1472}"
CONFIG_DIR="${SMITE_NODE_CONFIG:-/etc/smite-node}"

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --peer) PEER="$2"; shift 2 ;;
    --network) NETWORK="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --mtu) MTU="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root"
  exit 1
fi

if [ -z "$PEER" ]; then
  echo "GRE peer public IP required (--peer or GRE_PEER_IP)"
  exit 1
fi

detect_local() {
  ip -4 route get "$PEER" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
}

LOCAL="$(detect_local)"
if [ -z "$LOCAL" ]; then
  LOCAL="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
fi
UNDERLAY="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"

if [ -z "$LOCAL" ]; then
  echo "Could not detect local public IP"
  exit 1
fi

# /30 hosts: first=foreign(.1), second=iran(.2)
read -r FOREIGN_INNER IRAN_INNER < <(python3 - <<PY
import ipaddress
hosts=[str(h) for h in ipaddress.ip_network("$NETWORK", strict=False).hosts()]
print(hosts[0], hosts[1])
PY
)

if [ "$ROLE" = "foreign" ]; then
  LOCAL_INNER="$FOREIGN_INNER"
  PEER_INNER="$IRAN_INNER"
else
  LOCAL_INNER="$IRAN_INNER"
  PEER_INNER="$FOREIGN_INNER"
  ROLE="iran"
fi

PREFIX="$(python3 - <<PY
import ipaddress
print(ipaddress.ip_network("$NETWORK", strict=False).prefixlen)
PY
)"

echo "Underlay NIC : ${UNDERLAY:-unknown}"
echo "Local public : $LOCAL"
echo "Peer public  : $PEER"
echo "Role         : $ROLE"
echo "Local inner  : $LOCAL_INNER/$PREFIX"
echo "Peer inner   : $PEER_INNER"
echo "GRE iface    : $IFACE (mtu $MTU)"

modprobe ip_gre 2>/dev/null || true
modprobe gre 2>/dev/null || true
ip link del "$IFACE" 2>/dev/null || true
ip tunnel add "$IFACE" mode gre remote "$PEER" local "$LOCAL" ttl 255
ip link set "$IFACE" mtu "$MTU" up
ip addr replace "$LOCAL_INNER/$PREFIX" dev "$IFACE"

mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_DIR/gre.json" <<EOF
{
  "iface": "$IFACE",
  "role": "$ROLE",
  "local_public": "$LOCAL",
  "remote_public": "$PEER",
  "local_inner": "$LOCAL_INNER",
  "peer_inner": "$PEER_INNER",
  "network": "$NETWORK",
  "prefixlen": $PREFIX,
  "mtu": $MTU,
  "underlay_iface": "${UNDERLAY:-}"
}
EOF

cat > "$CONFIG_DIR/gre-up.sh" <<EOF
#!/bin/bash
set -e
modprobe ip_gre 2>/dev/null || true
modprobe gre 2>/dev/null || true
ip link del "$IFACE" 2>/dev/null || true
ip tunnel add "$IFACE" mode gre remote "$PEER" local "$LOCAL" ttl 255
ip link set "$IFACE" mtu "$MTU" up
ip addr replace "$LOCAL_INNER/$PREFIX" dev "$IFACE"
EOF
chmod +x "$CONFIG_DIR/gre-up.sh"

cat > /etc/systemd/system/smite-gre.service <<EOF
[Unit]
Description=Smite GRE tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$CONFIG_DIR/gre-up.sh
ExecStop=/sbin/ip link del $IFACE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now smite-gre.service >/dev/null 2>&1 || systemctl enable smite-gre.service || true

echo "✅ GRE is up. Persist via smite-gre.service"
ip -br addr show "$IFACE" || true
