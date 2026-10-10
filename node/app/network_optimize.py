"""Network helpers: GRE discovery and TCP MSS/MTU optimizations for tunnels."""
from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Safe default for client paths (PPPoE/CGNAT/mobile) and GRE (MTU 1472 → MSS ≤ 1432).
DEFAULT_TCP_MSS = 1360
MIN_TCP_MSS = 536


def mss_for_mtu(mtu: int) -> int:
    """IPv4 (20) + TCP (20) headers → MSS = MTU - 40, clamped to a safe range."""
    return max(MIN_TCP_MSS, min(DEFAULT_TCP_MSS, int(mtu) - 40))


def _which(name: str) -> Optional[str]:
    import shutil
    found = shutil.which(name)
    if found:
        return found
    for path in (f"/usr/sbin/{name}", f"/sbin/{name}", f"/usr/bin/{name}", f"/bin/{name}"):
        if Path(path).exists():
            return path
    return None


def _run(cmd: List[str], timeout: int = 5) -> str:
    try:
        resolved = list(cmd)
        if resolved:
            bin_path = _which(resolved[0]) if "/" not in resolved[0] else resolved[0]
            if not bin_path:
                return ""
            resolved[0] = bin_path
        proc = subprocess.run(
            resolved,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.stdout or ""
    except Exception as e:
        logger.debug(f"Command failed {cmd}: {e}")
        return ""


def _peer_inner_from_cidr(local_ip: str, prefixlen: int) -> Optional[str]:
    try:
        net = ipaddress.ip_network(f"{local_ip}/{prefixlen}", strict=False)
        hosts = [str(h) for h in net.hosts()]
        if not hosts:
            return None
        if local_ip in hosts and len(hosts) >= 2:
            for h in hosts:
                if h != local_ip:
                    return h
        if len(hosts) == 1:
            return hosts[0]
        return hosts[0] if hosts[0] != local_ip else (hosts[1] if len(hosts) > 1 else None)
    except Exception:
        return None


def discover_gre_peers() -> List[Dict[str, Any]]:
    """
    Discover local GRE/IP tunnels and their inner addressing.

    Returns items like:
      {
        "iface": "gre-b-1",
        "local": "185.126.7.74",
        "remote": "91.107.147.56",
        "local_inner": "172.17.1.2",
        "peer_inner": "172.17.1.1",
        "mtu": 1472,
      }
    """
    peers: List[Dict[str, Any]] = []
    tunnels: Dict[str, Dict[str, str]] = {}

    tunnel_out = _run(["ip", "-o", "tunnel", "show"])
    # gre-b-1: gre/ip remote 91.107.147.56 local 185.126.7.74 ttl 255 ...
    tunnel_re = re.compile(
        r"^(?P<iface>\S+):\s+\S+\s+remote\s+(?P<remote>\S+)\s+local\s+(?P<local>\S+)",
        re.MULTILINE,
    )
    for m in tunnel_re.finditer(tunnel_out):
        tunnels[m.group("iface")] = {
            "remote": m.group("remote"),
            "local": m.group("local"),
        }

    if not tunnels:
        link_out = _run(["ip", "-d", "link", "show"])
        for block in re.split(r"\n(?=\d+:)", link_out):
            iface_m = re.match(r"\d+:\s+([^:@\s]+)", block)
            if not iface_m:
                continue
            iface = iface_m.group(1)
            gre_peer = re.search(r"link/gre\s+(\S+)\s+peer\s+(\S+)", block)
            gre_rl = re.search(r"gre remote\s+(\S+)\s+local\s+(\S+)", block)
            if gre_peer:
                # link/gre LOCAL peer REMOTE
                tunnels[iface] = {"local": gre_peer.group(1), "remote": gre_peer.group(2)}
            elif gre_rl:
                tunnels[iface] = {"remote": gre_rl.group(1), "local": gre_rl.group(2)}

    addr_out = _run(["ip", "-o", "-4", "addr", "show"])
    addr_re = re.compile(
        r"^\d+:\s+(?P<iface>\S+)\s+inet\s+(?P<ip>[0-9.]+)/(?P<pre>\d+)",
        re.MULTILINE,
    )
    addrs: Dict[str, Dict[str, Any]] = {}
    for m in addr_re.finditer(addr_out):
        addrs[m.group("iface")] = {"ip": m.group("ip"), "prefix": int(m.group("pre"))}

    mtu_map: Dict[str, int] = {}
    for line in _run(["ip", "-o", "link", "show"]).splitlines():
        m = re.match(r"^\d+:\s+([^:@\s]+).*?\smtu\s+(\d+)", line)
        if m:
            mtu_map[m.group(1)] = int(m.group(2))

    for iface, meta in tunnels.items():
        addr = addrs.get(iface)
        if not addr:
            continue
        peers.append(
            {
                "iface": iface,
                "local": meta.get("local"),
                "remote": meta.get("remote"),
                "local_inner": addr["ip"],
                "peer_inner": _peer_inner_from_cidr(addr["ip"], addr["prefix"]),
                "mtu": mtu_map.get(iface, 1472),
            }
        )

    return peers


def _iptables_bin() -> Optional[str]:
    return _which("iptables")


def _iptables_has_rule(table: str, chain: str, args: Sequence[str]) -> bool:
    ipt = _iptables_bin()
    if not ipt:
        return False
    check = [ipt, "-t", table, "-C", chain, *args]
    try:
        return subprocess.run(check, capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _iptables_ensure(table: str, chain: str, args: Sequence[str]) -> None:
    ipt = _iptables_bin()
    if not ipt:
        logger.warning("iptables not found; skipping MSS clamp rule")
        return
    if _iptables_has_rule(table, chain, args):
        return
    cmd = [ipt, "-t", table, "-A", chain, *args]
    try:
        subprocess.run(cmd, capture_output=True, timeout=5, check=False)
    except Exception as e:
        logger.warning(f"Failed to add iptables rule {cmd}: {e}")


def ensure_mtu_optimizations(
    listen_ports: Optional[Sequence[int]] = None,
    path_mtu: Optional[int] = None,
    mss: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Enable TCP MTU probing and clamp SYN MSS so large HTTPS (e.g. X/Twitter)
    works through proxied/tunneled paths.
    """
    if mss is None:
        mss = mss_for_mtu(path_mtu or 1472)
    mss = int(mss)

    applied: Dict[str, Any] = {"mss": mss, "tcp_mtu_probing": False, "ports": []}

    try:
        # Prefer /proc writes (sysctl binary may be absent in slim images).
        for path, value in (
            ("/proc/sys/net/ipv4/tcp_mtu_probing", "1"),
            ("/proc/sys/net/ipv4/tcp_base_mss", str(min(mss, 1024))),
        ):
            try:
                with open(path, "w") as f:
                    f.write(value)
            except Exception:
                bin_sysctl = _which("sysctl")
                if bin_sysctl:
                    key = path.split("/proc/sys/")[-1].replace("/", ".")
                    subprocess.run(
                        [bin_sysctl, "-w", f"{key}={value}"],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
        applied["tcp_mtu_probing"] = True
        conf = "/etc/sysctl.d/99-smite-mtu.conf"
        try:
            Path("/etc/sysctl.d").mkdir(parents=True, exist_ok=True)
            with open(conf, "w") as f:
                f.write("net.ipv4.tcp_mtu_probing = 1\n")
                f.write(f"net.ipv4.tcp_base_mss = {min(mss, 1024)}\n")
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Failed to enable tcp_mtu_probing: {e}")

    # Global POSTROUTING clamp (covers GRE/public egress)
    _iptables_ensure(
        "mangle",
        "POSTROUTING",
        ["-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", str(mss)],
    )
    _iptables_ensure(
        "mangle",
        "FORWARD",
        ["-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", str(mss)],
    )

    ports = [int(p) for p in (listen_ports or []) if str(p).isdigit() or isinstance(p, int)]
    for port in ports:
        _iptables_ensure(
            "mangle",
            "INPUT",
            [
                "-p", "tcp", "--dport", str(port),
                "--tcp-flags", "SYN,RST", "SYN",
                "-j", "TCPMSS", "--set-mss", str(mss),
            ],
        )
        _iptables_ensure(
            "mangle",
            "OUTPUT",
            [
                "-p", "tcp", "--sport", str(port),
                "--tcp-flags", "SYN,RST", "SYN",
                "-j", "TCPMSS", "--set-mss", str(mss),
            ],
        )
        applied["ports"].append(port)

    logger.info(f"MTU optimizations applied: mss={mss}, ports={applied['ports']}")
    return applied


def pick_gre_forward_ip(target_public_ip: str, gre_peers: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Find GRE peer_inner for a remote public IP, if a matching GRE exists."""
    if not target_public_ip:
        return None
    for gre in gre_peers if gre_peers is not None else discover_gre_peers():
        if gre.get("remote") == target_public_ip and gre.get("peer_inner"):
            return gre
    return None


# ---------------------------------------------------------------------------
# Firewall: automatically open tunnel ports (iptables + UFW)
# ---------------------------------------------------------------------------

# Keys whose values are listen-side (data) ports of a tunnel spec.
_LISTEN_PORT_KEYS = ("listen_port", "remote_port", "reverse_port", "public_port")
# Keys whose values are control-plane ports (FRP bind, backhaul/chisel/
# rathole/wstunnel control, ...). Always TCP.
_CONTROL_PORT_KEYS = ("bind_port", "control_port", "server_port")
_MAX_AUTO_PORTS = 64


def _valid_port(value: Any) -> Optional[int]:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _port_from_mapping(raw: str) -> Optional[int]:
    """Extract the public/listen side from 'PORT=host:port' style entries."""
    left = raw.split("=", 1)[0].strip()
    if ":" in left:
        left = left.rsplit(":", 1)[-1].strip()
    return _valid_port(left)


def collect_tunnel_ports(spec: Dict[str, Any]) -> Dict[str, List[int]]:
    """
    Split a tunnel spec into listen-side ports (need TCP+UDP) and
    control-plane ports (TCP only). Over-allowing is harmless: ACCEPT
    rules create no listeners, they just stop the firewall dropping
    traffic to ports the tunnel actually binds.
    """
    listen: set = set()
    control: set = set()

    raw_ports = spec.get("ports") or []
    if isinstance(raw_ports, (int, str)):
        raw_ports = [p.strip() for p in str(raw_ports).split(",")]
    if isinstance(raw_ports, list):
        for item in raw_ports:
            if isinstance(item, dict):
                for key in ("local", "remote", "listen_port", "public_port"):
                    port = _valid_port(item.get(key))
                    if port:
                        listen.add(port)
            elif isinstance(item, int):
                port = _valid_port(item)
                if port:
                    listen.add(port)
            elif isinstance(item, str):
                port = _port_from_mapping(item)
                if port:
                    listen.add(port)

    for key in _LISTEN_PORT_KEYS:
        port = _valid_port(spec.get(key))
        if port:
            listen.add(port)
    for key in _CONTROL_PORT_KEYS:
        port = _valid_port(spec.get(key))
        if port:
            control.add(port)

    return {
        "tcp": sorted(listen | control)[:_MAX_AUTO_PORTS],
        "udp": sorted(listen)[:_MAX_AUTO_PORTS],
    }


def _ufw_active() -> bool:
    ufw = _which("ufw")
    if not ufw:
        return False
    try:
        proc = subprocess.run([ufw, "status"], capture_output=True, text=True, timeout=5)
        return "Status: active" in (proc.stdout or "")
    except Exception:
        return False


def _ufw_allow(args: List[str]) -> None:
    ufw = _which("ufw")
    if not ufw:
        return
    try:
        # Idempotent: ufw skips rules that already exist with the same comment.
        subprocess.run([ufw, *args], capture_output=True, text=True, timeout=15, check=False)
    except Exception as e:
        logger.debug(f"ufw {' '.join(args)} failed: {e}")


def _filter_bins() -> List[str]:
    bins = []
    for name in ("iptables", "ip6tables"):
        path = _which(name)
        if path:
            bins.append(path)
    return bins


def _user_chain(bin_path: str) -> str:
    """
    Chain where ACCEPTs are evaluated before any REJECT. When UFW manages
    the firewall, ufw-user-input is reached before ufw-reject-input, so an
    appended ACCEPT there takes effect; a plain appended INPUT rule would
    never be reached. Without UFW, plain INPUT is correct.
    """
    try:
        proc = subprocess.run(
            [bin_path, "-t", "filter", "-L", "ufw-user-input", "-n"],
            capture_output=True, timeout=5,
        )
        if proc.returncode == 0:
            return "ufw-user-input"
    except Exception:
        pass
    return "INPUT"


def _accept_ensure(bin_path: str, chain: str, args: Sequence[str]) -> bool:
    """Idempotent ACCEPT append. True when the rule is present afterwards."""
    check = [bin_path, "-t", "filter", "-C", chain, *args, "-j", "ACCEPT"]
    try:
        if subprocess.run(check, capture_output=True, timeout=5).returncode == 0:
            return True
    except Exception:
        return False
    cmd = [bin_path, "-t", "filter", "-A", chain, *args, "-j", "ACCEPT"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=5, check=False)
    except Exception as e:
        logger.debug(f"firewall ACCEPT failed {cmd}: {e}")
        return False
    return True


def ensure_firewall_ports(
    spec: Dict[str, Any],
    extra_tcp_ports: Optional[Sequence[int]] = None,
    ensure_gre: bool = True,
) -> Dict[str, Any]:
    """
    Open a tunnel's ports in the host firewall automatically.

    - Listen ports → TCP + UDP ACCEPT; control ports → TCP ACCEPT.
    - Uses ufw-user-input when UFW is active (else INPUT), plus `ufw allow`
      when the ufw tool exists (covers IPv6 too).
    - Optionally allows GRE (proto 47 from known peers + inner IPs) so
      GRE-backed forwards work without manual firewall steps.

    Never raises: every failure is logged and skipped. Safe to call on
    every tunnel apply / restore; existing rules are detected, not duped.
    """
    result: Dict[str, Any] = {"tcp": [], "udp": [], "gre": [], "backends": []}
    try:
        ports = collect_tunnel_ports(spec)
        tcp_ports = list(ports["tcp"])
        for extra in extra_tcp_ports or []:
            port = _valid_port(extra)
            if port and port not in tcp_ports:
                tcp_ports.append(port)
        tcp_ports = sorted(tcp_ports)[:_MAX_AUTO_PORTS]
        udp_ports = ports["udp"]
        result["tcp"] = tcp_ports
        result["udp"] = udp_ports

        gre_peers = discover_gre_peers() if ensure_gre else []
        result["gre"] = [g.get("iface") for g in gre_peers if g.get("iface")]

        if _ufw_active():
            result["backends"].append("ufw")
            for port in tcp_ports:
                _ufw_allow(["allow", f"{port}/tcp", "comment", "smite-tunnel"])
            for port in udp_ports:
                _ufw_allow(["allow", f"{port}/udp", "comment", "smite-tunnel"])
            for gre in gre_peers:
                remote = (gre.get("remote") or "").strip()
                peer_inner = (gre.get("peer_inner") or "").strip()
                if remote:
                    _ufw_allow(["allow", "proto", "gre", "from", remote, "comment", "smite-gre"])
                if peer_inner:
                    _ufw_allow(["allow", "from", peer_inner, "comment", "smite-gre"])

        bins = _filter_bins()
        if bins:
            result["backends"].append("iptables")
            for bin_path in bins:
                chain = _user_chain(bin_path)
                is_v6 = "ip6tables" in bin_path
                for port in tcp_ports:
                    _accept_ensure(bin_path, chain, ["-p", "tcp", "--dport", str(port)])
                for port in udp_ports:
                    _accept_ensure(bin_path, chain, ["-p", "udp", "--dport", str(port)])
                for gre in gre_peers:
                    remote = (gre.get("remote") or "").strip()
                    peer_inner = (gre.get("peer_inner") or "").strip()
                    if remote and not is_v6:
                        _accept_ensure(bin_path, chain, ["-p", "47", "-s", remote])
                    if peer_inner:
                        _accept_ensure(bin_path, chain, ["-s", peer_inner])

        logger.info(
            f"Firewall auto-open: tcp={tcp_ports} udp={udp_ports} "
            f"gre={result['gre']} via={result['backends'] or 'none'}"
        )
    except Exception as e:
        logger.debug(f"ensure_firewall_ports skipped: {e}")
    return result
