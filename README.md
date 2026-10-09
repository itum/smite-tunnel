# Smite Tunnel

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/SmiteD.png"/>
    <source media="(prefers-color-scheme: light)" srcset="assets/SmiteL.png"/>
    <img src="assets/SmiteL.png" alt="Smite Tunnel Logo" width="200"/>
  </picture>

  **Dual-node tunnel control panel for Iran ↔ foreign servers.**

  GOST · Backhaul · Rathole · Chisel · FRP · wstunnel · bore

  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
  [![Docker](https://img.shields.io/badge/Docker-required-2496ED.svg)](https://www.docker.com/)
</div>

Smite Tunnel is a panel plus node agent for reverse and forward tunnels. The **panel runs on the Iran (central) server**. Each machine that should carry tunnel traffic runs a **node**:

| Role | What it does |
|------|----------------|
| **Iran node** | Public listen side. Runs reverse-tunnel *servers* and GOST forwarders. |
| **Foreign node** | Private / origin side. Runs reverse-tunnel *clients* and hosts your real service (Xray, SSH, …). |

The panel talks to both nodes over HTTP. When a GRE tunnel exists between Iran and foreign, **GOST automatically prefers the GRE inner IP** and **clamps TCP MSS (default 1360)** so large HTTPS (for example X/Twitter) does not stall.

---

## One-line install

Run as **root**. Docker is installed automatically if it is missing.

### Panel (Iran / central server)

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/itum/smite-tunnel/main/scripts/install.sh)"
```

Defaults: panel on port **8000**, SQLite, no HTTPS. After it finishes:

```bash
smite admin create
```

Open `http://YOUR_IRAN_IP:8000` and log in.

### Node (every tunnel host)

Install a node on **Iran** (role: Iran) **and** on **foreign** (role: Foreign).

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/itum/smite-tunnel/main/scripts/smite-node.sh)"
```

You will be asked for:

1. **Panel address** — Iran panel host, for example `185.126.7.74:8000`
2. **Panel API port** — usually `8000`
3. **Node API port** — default `8888`
4. **Node name**
5. **Role** — `1` Iran or `2` Foreign
6. **CA certificate** (paste, then empty line):
   - Iran node → panel **Iran Nodes → CA certificate** (`ca.crt`)
   - Foreign node → panel **Foreign Nodes → Foreign Server CA** (`ca-server.crt`)

The node registers itself. You should see it **connected** in the panel. Do **not** run a second panel on the foreign server.

---

## Typical setup

1. Install the **panel** on the Iran IP.
2. Create an admin: `smite admin create`.
3. Install an **Iran node** on the same (or another) Iran machine; paste the Iran CA.
4. Install a **foreign node** on the origin server; paste the Foreign CA.
5. In **Tunnels**, create a tunnel (GOST is the right choice when the service already listens on the foreign public IP).

Example: Xray/VLESS on foreign `:23534`. Create a **GOST TCP** tunnel with port `23534`. Clients use the Iran IP:

```
vless://UUID@IRAN_IP:23534?encryption=none&security=none&type=tcp#via-smite
```

Direct access remains `FOREIGN_IP:23534`. If GRE exists, the panel forwards Iran → GRE peer inner IP (for example `172.17.1.1`) instead of the lossy public path.

---

## Tunnel cores

| Core | Mode | Use when |
|------|------|----------|
| **GOST** | Forward (Iran → foreign) | Service already listens on foreign. Iran publishes the same port. Best for VLESS/TCP/UDP/WS/gRPC/TCPMux. Auto GRE + MSS. |
| **FRP** | Reverse | Foreign `frpc` connects to Iran `frps`. TCP/UDP. Local service on foreign is `127.0.0.1`. |
| **Chisel** | Reverse | HTTP/WS reverse TCP. Control port defaults to listen port + 10000. |
| **Rathole** | Reverse | TCP or WebSocket reverse tunnel. |
| **Backhaul** | Reverse | TCP, UDP, WS, WSMux, TCPMux, with extra mux/keepalive options. |
| **wstunnel** | Reverse | TCP (or UDP) over WebSocket; useful when only HTTP(S) is allowed. |
| **bore** | Reverse | Simple TCP reverse tunnel. **One shared server per Iran node on control port 7835.** Same secret for extra bore tunnels on that node. |

**Which to pick**

- Public origin already open on foreign (Xray inbound): **GOST**.
- Origin only on `127.0.0.1` on foreign: **FRP / Chisel / Rathole / Backhaul / wstunnel / bore**.
- If reverse cores connect but **download stays at zero**, the Iran↔foreign control path is dropping packets — switch to **GOST** (or GRE + GOST).

GOST types: TCP, UDP, WebSocket, gRPC, TCPMux.  
FRP / bore: TCP (FRP also UDP).  
Chisel: TCP. Rathole: TCP / WS. Backhaul: TCP, UDP, WS, WSMux, TCPMux. wstunnel: TCP / UDP.

---

## Network optimizations (automatic)

On tunnel apply, Iran nodes:

- Discover GRE/IP tunnels (`ip tunnel` / `ip link`)
- Register `gre_peers` (iface, public remote, inner IPs, MTU) with the panel
- Prefer GRE inner IP for GOST when the foreign public IP matches a GRE peer
- Enable TCP MTU probing and **TCPMSS 1360** on tunnel ports (GRE MTU 1472 → max MSS 1432; 1360 leaves room for PPPoE/mobile)

No extra panel clicks are required.

---

## CLI

Panel (`smite`):

```bash
smite admin create
smite admin update
smite status
smite logs
smite restart
smite update
```

Node (`smite-node`):

```bash
smite-node status
smite-node logs
smite-node restart
smite-node update
```

---

## Manual install

**Panel**

```bash
git clone https://github.com/itum/smite-tunnel.git /opt/smite
cd /opt/smite
cp .env.example .env
# set PANEL_PORT, SECRET_KEY, …
mkdir -p panel/data panel/certs
docker compose up -d --build
smite admin create   # or: bash cli/install_cli.sh first
```

**Node**

```bash
mkdir -p /opt/smite-node/certs /opt/smite-node/config
# copy ca.crt (Iran) or ca-server.crt as certs/ca.crt (Foreign)
cat >/opt/smite-node/.env <<EOF
NODE_API_PORT=8888
NODE_NAME=my-node
NODE_ROLE=iran
PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=PANEL_IP:8000
PANEL_API_PORT=8000
SMITE_VERSION=latest
EOF
# NODE_ROLE=foreign on the origin server
cp -a node/. /opt/smite-node/   # from this repo
cd /opt/smite-node && docker compose up -d --build
```

Images are built locally if `ghcr.io/zzedix/smite-panel` / `smite-node` are not available.

---

## Ports

| Port | Service |
|------|---------|
| 8000 | Panel HTTP (configurable) |
| 8888 | Node API |
| 80 / 443 | Optional nginx + Let's Encrypt (`install.sh` HTTPS prompt) |
| Tunnel ports | Whatever you set in Tunnels (for example 23534) |
| 7835 | Bore control (fixed) |

Open the panel port, node API if you manage from the panel host, and every **public listen** port on Iran.

---

## Requirements

- Linux, root, Docker + Docker Compose v2
- Iran install of Docker if needed: `curl -fsSL https://raw.githubusercontent.com/manageitir/docker/main/install-ubuntu.sh | sh`
- Panel and nodes must reach each other on the panel API port (default 8000)

---

## License

MIT. Original Smite project by [zZedix](https://github.com/zZedix/Smite). This tree is maintained at [itum/smite-tunnel](https://github.com/itum/smite-tunnel).
