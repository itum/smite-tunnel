"""In-memory path-bench jobs with live logs and a single-run lock."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_active_job_id: Optional[str] = None
_jobs: Dict[str, Dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _jobs.get(job_id)


def active_job() -> Optional[Dict[str, Any]]:
    if _active_job_id and _active_job_id in _jobs:
        return _jobs[_active_job_id]
    return None


def _log(job: Dict[str, Any], message: str, level: str = "info") -> None:
    line = {"ts": _now(), "level": level, "message": message}
    job["logs"].append(line)
    logger.info("[path-bench %s] %s", job["id"], message)


async def _send(client, node_id: str, endpoint: str, data: dict, timeout: float, alt_bases: Optional[List[str]] = None):
    """Try primary NodeClient, then optional direct base URLs (e.g. GRE inner)."""
    import httpx

    resp = await client.send_to_node(node_id, endpoint, data, timeout=timeout)
    if isinstance(resp, dict) and resp.get("status") != "error":
        return resp
    msg = str((resp or {}).get("message") or "")
    if "Network error" not in msg and "Connection" not in msg:
        return resp
    for base in alt_bases or []:
        url = f"{base.rstrip('/')}{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), verify=False) as http:
                r = await http.post(url, json=data)
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            last = {"status": "error", "message": f"Network error: {exc}"}
            continue
    return resp if isinstance(resp, dict) else {"status": "error", "message": "Network error"}


async def start_job(iran, foreign, body_ids: dict) -> Dict[str, Any]:
    global _active_job_id
    async with _lock:
        if _active_job_id and _jobs.get(_active_job_id, {}).get("state") == "running":
            current = _jobs[_active_job_id]
            raise RuntimeError(
                f"A path test is already running (job {current['id'][:8]}). "
                "Wait for it to finish, or close the other tab that started it."
            )
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "state": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "iran_node_id": iran.id,
            "foreign_node_id": foreign.id,
            "logs": [],
            "result": None,
            "error": None,
        }
        _jobs[job_id] = job
        _active_job_id = job_id
        asyncio.create_task(_run_job(job_id, iran, foreign))
        return {"job_id": job_id, "state": "running"}


async def _run_job(job_id: str, iran, foreign) -> None:
    global _active_job_id
    from app.node_client import NodeClient

    job = _jobs[job_id]
    client = NodeClient()
    opened_foreign: List[int] = []
    opened_iran: List[int] = []
    rows: List[dict] = []
    foreign_alts: List[str] = []
    iran_alts: List[str] = []

    def row(label, core, tunnel_type, port, resp):
        resp = resp or {}
        if resp.get("ok") and resp.get("download_mbps") is not None and resp.get("upload_mbps") is not None:
            return {
                "label": label,
                "core": core,
                "type": tunnel_type,
                "port": port,
                "ok": True,
                "download_mbps": resp["download_mbps"],
                "upload_mbps": resp["upload_mbps"],
                "bytes": resp.get("bytes"),
            }
        return {
            "label": label,
            "core": core,
            "type": tunnel_type,
            "port": port,
            "ok": False,
            "error": resp.get("error") or resp.get("message") or "Measurement failed",
        }

    try:
        iran_ip = (iran.node_metadata or {}).get("ip_address")
        foreign_ip = (foreign.node_metadata or {}).get("ip_address")
        iran_peers = (iran.node_metadata or {}).get("gre_peers") or []
        peer = iran_peers[0] if iran_peers else {}
        foreign_inner = peer.get("peer_inner")
        iran_inner = peer.get("local_inner")
        if foreign_inner:
            foreign_alts.append(f"http://{foreign_inner}:8888")
        if iran_inner:
            iran_alts.append(f"http://{iran_inner}:8888")

        _log(job, f"Starting path test: {iran.name} ({iran_ip}) ↔ {foreign.name} ({foreign_ip})")
        _log(job, "Existing tunnels will not be changed.")

        _log(job, "Checking Iran node agent…")
        iran_ping = await _send(client, iran.id, "/api/agent/bench/stop-all", {}, 5, iran_alts)
        if isinstance(iran_ping, dict) and iran_ping.get("status") == "error" and "Network" in str(iran_ping.get("message")):
            raise RuntimeError(f"Iran node is unreachable ({iran_ip}:8888). Bring the Iran agent online.")
        _log(job, "Iran node agent is reachable.")

        _log(job, "Checking foreign node agent…")
        foreign_ping = await _send(client, foreign.id, "/api/agent/bench/stop-all", {}, 6, foreign_alts)
        foreign_up = not (
            isinstance(foreign_ping, dict)
            and foreign_ping.get("status") == "error"
            and ("Network error" in str(foreign_ping.get("message") or "") or "Connection" in str(foreign_ping.get("message") or ""))
        )
        if not foreign_up:
            # stop-all may 404 if agent old — treat 404 as online-but-outdated
            msg = str((foreign_ping or {}).get("message") or "")
            if "404" in msg or "Not Found" in msg:
                foreign_up = True
                _log(job, "Foreign agent answered but is missing bench endpoints. Need agent update.", "warn")
            else:
                last_seen = getattr(foreign, "last_seen", None)
                _log(
                    job,
                    f"Foreign agent unreachable at {foreign_ip}:8888"
                    + (f" and GRE {foreign_inner}:8888" if foreign_inner else "")
                    + (f" (last seen {last_seen})" if last_seen else ""),
                    "error",
                )
                _log(
                    job,
                    "The foreign VPS itself is not answering SSH/HTTP/GRE from Iran. "
                    "Reboot it from your cloud console (Hetzner), wait until smite-node is healthy, then retry.",
                    "error",
                )
                raise RuntimeError(
                    "Foreign node is offline (no answer on public IP or GRE). "
                    "Reboot the foreign server from the cloud console, start smite-node on port 8888, "
                    "then run the test again. Existing tunnels were not changed."
                )
        else:
            _log(job, "Foreign node agent is reachable.")

        candidates = [2053, 2096, 8443, 39443, 39120]
        _log(job, f"Opening temporary TCP listeners on foreign ports: {', '.join(map(str, candidates))}")
        for port in candidates:
            started = await _send(
                client, foreign.id, "/api/agent/bench/listen", {"port": port}, 8, foreign_alts
            )
            if isinstance(started, dict) and started.get("ok"):
                opened_foreign.append(port)
                _log(job, f"Foreign listening on {port}")
            else:
                err = (started or {}).get("error") or (started or {}).get("message") or "rejected"
                _log(job, f"Foreign port {port} not opened: {err}", "warn")
                if "404" in str(err) or "Not Found" in str(err):
                    raise RuntimeError(
                        "Foreign node is online but missing the path-test agent update "
                        "(bench/listen). Update foreign smite-node, then retry. Nothing was changed."
                    )

        measure_host = foreign_ip
        best_port = None

        if opened_foreign:
            _log(job, "Measuring Iran → foreign direct TCP upload/download…")
            for port in opened_foreign:
                _log(job, f"Direct TCP test to {measure_host}:{port} (2 MiB)…")
                measured = await _send(
                    client,
                    iran.id,
                    "/api/agent/bench/measure",
                    {"host": measure_host, "port": port, "nbytes": 2 * 1024 * 1024},
                    45,
                    iran_alts,
                )
                r = row(f"Direct TCP port {port}", "direct", "tcp", port, measured)
                rows.append(r)
                if r["ok"]:
                    _log(job, f"Port {port}: up {r['upload_mbps']} Mbps / down {r['download_mbps']} Mbps")
                else:
                    _log(job, f"Port {port} failed: {r.get('error')}", "warn")
            ok_ports = [r for r in rows if r.get("ok") and r["core"] == "direct"]
            if ok_ports:
                best_port_row = max(ok_ports, key=lambda x: min(x["upload_mbps"], x["download_mbps"]))
                best_port = int(best_port_row["port"])
                _log(job, f"Best direct port: {best_port}")
        else:
            _log(job, "No inbound foreign ports opened. Falling back to Iran listener + foreign measure…", "warn")
            for port in candidates:
                started = await _send(client, iran.id, "/api/agent/bench/listen", {"port": port}, 8, iran_alts)
                if isinstance(started, dict) and started.get("ok"):
                    opened_iran.append(port)
                    _log(job, f"Iran listening on {port}")
            if not opened_iran:
                raise RuntimeError(
                    "Could not open a temporary test port on either node. "
                    "Check firewall and agent updates. Nothing was changed."
                )
            dial_hosts = [h for h in (iran_inner, iran_ip) if h]
            for port in opened_iran:
                for host in dial_hosts:
                    _log(job, f"Foreign → {host}:{port} direct TCP (2 MiB)…")
                    measured = await _send(
                        client,
                        foreign.id,
                        "/api/agent/bench/measure",
                        {"host": host, "port": port, "nbytes": 2 * 1024 * 1024},
                        45,
                        foreign_alts,
                    )
                    r = row(f"Direct TCP (foreign→{host}:{port})", "direct", "tcp", port, measured)
                    rows.append(r)
                    if r["ok"]:
                        _log(job, f"OK via {host}:{port} — up {r['upload_mbps']} / down {r['download_mbps']} Mbps")
                        best_port = port
                        measure_host = host
                        break
                    _log(job, f"Failed via {host}:{port}: {r.get('error')}", "warn")
                if best_port:
                    break

        if not best_port:
            raise RuntimeError(
                "TCP transfer between the servers failed on every test port. "
                "Existing tunnels were not changed."
            )

        gost_target = foreign_inner or foreign_ip
        if foreign_inner and opened_foreign:
            _log(job, f"Measuring over GRE to {foreign_inner}:{best_port}…")
            gre = await _send(
                client,
                iran.id,
                "/api/agent/bench/measure",
                {"host": foreign_inner, "port": best_port, "nbytes": 4 * 1024 * 1024},
                45,
                iran_alts,
            )
            r = row(f"GRE TCP to {foreign_inner}", "gre", "tcp", best_port, gre)
            rows.append(r)
            if r["ok"]:
                _log(job, f"GRE: up {r['upload_mbps']} / down {r['download_mbps']} Mbps")
            else:
                _log(job, f"GRE failed: {r.get('error')}", "warn")

        if opened_foreign:
            _log(job, f"Testing GOST TCP Iran→{gost_target}:{best_port}…")
            gost = await _send(
                client,
                iran.id,
                "/api/agent/bench/gost",
                {
                    "listen_port": 39112,
                    "target_host": gost_target,
                    "target_port": best_port,
                    "nbytes": 4 * 1024 * 1024,
                },
                50,
                iran_alts,
            )
            r = row("GOST TCP", "gost", "tcp", best_port, gost)
            rows.append(r)
            if r["ok"]:
                _log(job, f"GOST: up {r['upload_mbps']} / down {r['download_mbps']} Mbps")
            else:
                _log(job, f"GOST failed: {r.get('error')}", "warn")

            token = "smite-bench"
            _log(job, "Starting temporary FRP server on Iran (bind 39110)…")
            frps = await _send(
                client, iran.id, "/api/agent/bench/frps", {"bind_port": 39110, "token": token}, 15, iran_alts
            )
            if isinstance(frps, dict) and frps.get("ok"):
                dial = iran_inner or iran_ip
                _log(job, f"Starting temporary FRP client on foreign → {dial}:39110…")
                frpc = await _send(
                    client,
                    foreign.id,
                    "/api/agent/bench/frpc",
                    {
                        "server_addr": dial,
                        "server_port": 39110,
                        "token": token,
                        "local_port": best_port,
                        "remote_port": 39111,
                    },
                    20,
                    foreign_alts,
                )
                if isinstance(frpc, dict) and frpc.get("ok"):
                    _log(job, "Measuring through FRP TCP…")
                    frp = await _send(
                        client,
                        iran.id,
                        "/api/agent/bench/measure",
                        {"host": "127.0.0.1", "port": 39111, "nbytes": 4 * 1024 * 1024},
                        50,
                        iran_alts,
                    )
                    r = row("FRP TCP", "frp", "tcp", best_port, frp)
                    rows.append(r)
                    if r["ok"]:
                        _log(job, f"FRP: up {r['upload_mbps']} / down {r['download_mbps']} Mbps")
                    else:
                        _log(job, f"FRP measure failed: {r.get('error')}", "warn")
                else:
                    rows.append(row("FRP TCP", "frp", "tcp", best_port, frpc if isinstance(frpc, dict) else {}))
                    _log(job, f"FRP client failed: {(frpc or {}).get('error') or (frpc or {}).get('message')}", "warn")
            else:
                rows.append(row("FRP TCP", "frp", "tcp", best_port, frps if isinstance(frps, dict) else {}))
                _log(job, f"FRP server failed: {(frps or {}).get('error') or (frps or {}).get('message')}", "warn")
        else:
            _log(job, "Skipping GOST/FRP core compare (no foreign inbound listener).", "warn")

        tunnels = [r for r in rows if r.get("ok") and r["core"] in {"gost", "frp"}]
        directs = [r for r in rows if r.get("ok") and r["core"] in {"direct", "gre"}]
        best = None
        if tunnels:
            best = max(tunnels, key=lambda x: min(x["upload_mbps"], x["download_mbps"]))
            message = (
                f"Best tunnel is {best['core'].upper()} {best['type'].upper()} on port {best['port']}. "
                f"Upload {best['upload_mbps']} Mbps, download {best['download_mbps']} Mbps. "
                f"Fastest direct port was {best_port}. Nothing was changed."
            )
        elif directs:
            best = max(directs, key=lambda x: min(x["upload_mbps"], x["download_mbps"]))
            message = (
                f"Best path is {best['label']}. "
                f"Upload {best['upload_mbps']} Mbps, download {best['download_mbps']} Mbps. "
                f"Core tunnels (GOST/FRP) did not complete. Nothing was changed."
            )
        else:
            raise RuntimeError("No successful transfer completed. Nothing was changed.")

        _log(job, message)
        job["result"] = {
            "status": "ok",
            "changed": False,
            "iran_node_id": iran.id,
            "foreign_node_id": foreign.id,
            "best": best,
            "best_port": best_port,
            "rows": rows,
            "message": message,
        }
        job["state"] = "done"
        _log(job, "Cleanup temporary listeners and processes…")
    except Exception as exc:
        job["state"] = "error"
        job["error"] = str(exc)
        _log(job, str(exc), "error")
        logger.exception("path-bench job %s failed", job_id)
    finally:
        try:
            await _send(client, iran.id, "/api/agent/bench/stop-all", {}, 8, [])
        except Exception:
            pass
        try:
            await _send(client, foreign.id, "/api/agent/bench/stop-all", {}, 5, foreign_alts)
        except Exception:
            pass
        for port in opened_foreign:
            try:
                await _send(client, foreign.id, "/api/agent/bench/stop", {"port": port}, 5, [])
            except Exception:
                pass
        for port in opened_iran:
            try:
                await _send(client, iran.id, "/api/agent/bench/stop", {"port": port}, 5, [])
            except Exception:
                pass
        _log(job, "Finished.")
        async with _lock:
            if _active_job_id == job_id:
                _active_job_id = None
