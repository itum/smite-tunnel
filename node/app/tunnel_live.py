"""Real per-port liveness for tunnels running on this node."""
from __future__ import annotations

import logging
import socket
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


def _listening_tcp_ports() -> Set[int]:
    ports: Set[int] = set()
    try:
        proc = subprocess.run(
            ["ss", "-lntH"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            if ":" not in local:
                continue
            port_s = local.rsplit(":", 1)[-1]
            if port_s.isdigit():
                ports.add(int(port_s))
    except Exception as exc:
        logger.debug("ss listen probe failed: %s", exc)
    return ports


def _tcp_accepts(port: int, timeout: float = 0.4) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def normalize_ports(raw: Iterable[Any]) -> List[int]:
    found: List[int] = []
    for item in raw or []:
        if isinstance(item, int):
            found.append(item)
        elif isinstance(item, str):
            text = item.strip()
            if "=" in text:
                text = text.split("=", 1)[0]
            if ":" in text:
                text = text.rsplit(":", 1)[-1]
            if text.isdigit():
                found.append(int(text))
        elif isinstance(item, dict):
            for key in ("local", "remote", "listen_port", "public_port", "port"):
                val = item.get(key)
                if val is not None and str(val).isdigit():
                    found.append(int(val))
                    break
    # unique, keep order
    seen = set()
    ordered: List[int] = []
    for port in found:
        if 1 <= port <= 65535 and port not in seen:
            seen.add(port)
            ordered.append(port)
    return ordered


def check_ports(ports: List[int]) -> List[Dict[str, Any]]:
    listening = _listening_tcp_ports()
    rows: List[Dict[str, Any]] = []
    for port in ports:
        if port in listening or _tcp_accepts(port):
            rows.append({"port": port, "status": "live", "error": None})
        else:
            rows.append({
                "port": port,
                "status": "offline",
                "error": f"Nothing is listening on port {port}",
            })
    return rows


def live_status(
    tunnel_id: str,
    core: str,
    ports: List[int],
    control_port: Optional[int],
    adapter_manager,
) -> Dict[str, Any]:
    process_running = False
    process_error = None
    try:
        if tunnel_id in getattr(adapter_manager, "active_tunnels", {}):
            adapter = adapter_manager.active_tunnels[tunnel_id]
            st = adapter.status(tunnel_id) or {}
            process_running = bool(st.get("process_running") or st.get("active"))
            if not process_running:
                process_error = "Tunnel process is not running on this node"
        else:
            # Still allow port checks: process map may be empty after restart restore race
            cfg = getattr(adapter_manager, "tunnel_configs", {}).get(tunnel_id)
            if cfg:
                process_error = "Tunnel is configured but the process is not active"
            else:
                process_error = "Tunnel is not loaded on this node"
    except Exception as exc:
        process_error = str(exc)

    port_rows = check_ports(ports)
    control_row = None
    if control_port:
        control_checks = check_ports([int(control_port)])
        control_row = control_checks[0] if control_checks else None

    live_ports = [r for r in port_rows if r["status"] == "live"]
    offline_ports = [r for r in port_rows if r["status"] != "live"]

    if not ports and not process_running:
        overall = "offline"
        error = process_error or "No ports to check"
    elif process_running and live_ports and not offline_ports:
        overall = "live"
        error = None
    elif live_ports and not process_running:
        # Port is open but our process map says down — still report live if accept works
        overall = "live"
        error = process_error
    elif not live_ports and process_running:
        overall = "error"
        error = "Process is running but public ports are not accepting connections"
    elif not live_ports:
        overall = "offline"
        error = process_error or (offline_ports[0]["error"] if offline_ports else "Ports are offline")
    else:
        overall = "error"
        error = "Some ports are offline"
        # partial: mark overall error but keep per-port truth

    if control_row and control_row["status"] != "live" and overall == "live" and core in {
        "frp", "chisel", "wstunnel", "rathole", "backhaul", "bore"
    }:
        # Data ports live but control dead is unusual; keep data truth, note control
        error = error or control_row.get("error")

    return {
        "ok": True,
        "tunnel_id": tunnel_id,
        "core": core,
        "status": overall,
        "process_running": process_running,
        "ports": port_rows,
        "control": control_row,
        "error": error,
    }
