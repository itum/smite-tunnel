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
