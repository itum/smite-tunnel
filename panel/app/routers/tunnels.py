"""Tunnels API endpoints"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from datetime import datetime, timedelta
from pydantic import BaseModel, field_validator
import asyncio
import logging
import time

from app.database import get_db
from app.models import Tunnel, Node
from app.node_client import NodeClient


router = APIRouter()
logger = logging.getLogger(__name__)


def prepare_frp_spec_for_node(spec: dict, node: Node, request: Request) -> dict:
    """Prepare FRP spec for node by determining correct server_addr from node metadata"""
    spec_for_node = spec.copy()
    bind_port = spec_for_node.get("bind_port", 7000)
    token = spec_for_node.get("token")
    
    panel_address = node.node_metadata.get("panel_address", "")
    panel_host = None
    
    if panel_address:
        if "://" in panel_address:
            panel_address = panel_address.split("://", 1)[1]
        if ":" in panel_address:
            panel_host = panel_address.split(":")[0]
        else:
            panel_host = panel_address
    
    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1", "0.0.0.0"]:
        panel_host = spec_for_node.get("panel_host")
        if panel_host:
            if "://" in panel_host:
                panel_host = panel_host.split("://", 1)[1]
            if ":" in panel_host:
                panel_host = panel_host.split(":")[0]
    
    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1", "0.0.0.0"]:
        forwarded_host = request.headers.get("X-Forwarded-Host")
        if forwarded_host:
            panel_host = forwarded_host.split(":")[0] if ":" in forwarded_host else forwarded_host
    
    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1", "0.0.0.0"]:
        request_host = request.url.hostname if request.url else None
        if request_host and request_host not in ["localhost", "127.0.0.1", "::1", "0.0.0.0", ""]:
            panel_host = request_host
    
    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1", "0.0.0.0"]:
        import os
        panel_public_ip = os.getenv("PANEL_PUBLIC_IP") or os.getenv("PANEL_IP")
        if panel_public_ip and panel_public_ip not in ["localhost", "127.0.0.1", "::1", "0.0.0.0", ""]:
            panel_host = panel_public_ip
    
    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1", "0.0.0.0", ""]:
        error_details = {
            "node_id": node.id,
            "node_name": node.name,
            "node_metadata_panel_address": panel_address,
            "node_metadata_keys": list(node.node_metadata.keys()),
            "request_hostname": request.url.hostname if request.url else None,
            "x_forwarded_host": request.headers.get("X-Forwarded-Host"),
            "env_panel_public_ip": os.getenv("PANEL_PUBLIC_IP"),
            "env_panel_ip": os.getenv("PANEL_IP"),
        }
        error_msg = f"Cannot determine panel address for FRP tunnel. Details: {error_details}. Please ensure node has correct PANEL_ADDRESS configured (node should register with panel_address in metadata) or set PANEL_PUBLIC_IP environment variable on panel."
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    from app.utils import is_valid_ipv6_address
    if is_valid_ipv6_address(panel_host):
        server_addr = f"[{panel_host}]"
    else:
        server_addr = panel_host
    
    spec_for_node["server_addr"] = server_addr
    spec_for_node["server_port"] = int(bind_port)
    if token:
        spec_for_node["token"] = token
    
    logger.info(f"FRP spec prepared: server_addr={server_addr}, server_port={bind_port}, token={'set' if token else 'none'}, panel_host={panel_host} (from node panel_address: {panel_address})")
    return spec_for_node


def iran_host_for_foreign_client(iran_node: Node, foreign_node: Node | None = None) -> str:
    """Prefer GRE inner IP when foreign reverse clients dial Iran."""
    from app.utils import resolve_iran_control_target

    iran_md = iran_node.node_metadata or {}
    foreign_md = (foreign_node.node_metadata or {}) if foreign_node else {}
    target = resolve_iran_control_target(
        iran_public_ip=iran_md.get("ip_address"),
        foreign_public_ip=foreign_md.get("ip_address"),
        iran_gre_peers=iran_md.get("gre_peers") or [],
        foreign_gre_peers=foreign_md.get("gre_peers") or [],
    )
    if target.get("via") == "gre":
        logger.info(
            "Reverse tunnel control via GRE: foreign→%s (mtu=%s, iface=%s)",
            target.get("host"),
            target.get("mtu"),
            target.get("iface"),
        )
    return target["host"]


class TunnelCreate(BaseModel):
    name: str
    core: str
    type: str
    node_id: str | None = None
    foreign_node_id: str | None = None  # For reverse tunnels: foreign node (server side)
    iran_node_id: str | None = None  # For reverse tunnels: iran node (client side)
    spec: dict


class TunnelUpdate(BaseModel):
    name: str | None = None
    spec: dict | None = None
    type: str | None = None
    core: str | None = None


_EDIT_CORE_TYPES = {
    "gost": {"tcp", "udp", "grpc", "tcpmux"},
    "rathole": {"tcp", "ws"},
    "backhaul": {"tcp", "udp", "ws", "wsmux", "tcpmux"},
    "chisel": {"tcp", "chisel"},
    "frp": {"tcp", "udp"},
    "wstunnel": {"tcp", "udp"},
    "bore": {"tcp"},
}


def _bound_ports(spec: dict | None) -> set[int]:
    found: set[int] = set()
    for raw in parse_ports_from_spec(spec or {}):
        if isinstance(raw, int):
            found.add(raw)
        elif isinstance(raw, str) and raw.isdigit():
            found.add(int(raw))
        elif isinstance(raw, str) and "=" in raw:
            left = raw.split("=", 1)[0]
            if ":" in left:
                left = left.rsplit(":", 1)[-1]
            if left.isdigit():
                found.add(int(left))
    for key in ("listen_port", "remote_port", "control_port", "bind_port", "server_port"):
        val = (spec or {}).get(key)
        if val is not None and str(val).isdigit():
            found.add(int(val))
    return found


async def _stop_panel_helpers(tunnel, request: Request) -> None:
    """Stop panel-side helpers for this tunnel id. Safe if a helper was never started."""
    gost = getattr(request.app.state, "gost_forwarder", None)
    if gost:
        try:
            gost.stop_forward(tunnel.id)
        except Exception:
            pass
    for attr in ("rathole_server_manager", "backhaul_manager", "chisel_server_manager", "frp_server_manager"):
        manager = getattr(request.app.state, attr, None)
        if not manager:
            continue
        try:
            manager.stop_server(tunnel.id)
        except Exception:
            pass


async def _remove_tunnel_from_nodes(tunnel, db: AsyncSession) -> None:
    from app.node_client import NodeClient

    node_ids = {tunnel.node_id, tunnel.iran_node_id, tunnel.foreign_node_id}
    client = NodeClient()
    for node_id in node_ids:
        if not node_id:
            continue
        try:
            await client.send_to_node(
                node_id=node_id,
                endpoint="/api/agent/tunnels/remove",
                data={"tunnel_id": tunnel.id},
            )
        except Exception as exc:
            logger.warning("Could not remove tunnel %s from node %s: %s", tunnel.id, node_id, exc)


def _ensure_edit_secrets(tunnel) -> None:
    from app.utils import generate_bore_secret, generate_chisel_auth, generate_token, generate_wstunnel_secret

    spec = dict(tunnel.spec or {})
    if tunnel.core == "frp" and not spec.get("token"):
        spec["token"] = generate_token()
    if tunnel.core == "rathole" and not spec.get("token"):
        spec["token"] = generate_token()
    if tunnel.core == "wstunnel" and not spec.get("secret"):
        spec["secret"] = generate_wstunnel_secret()
    if tunnel.core == "chisel" and not spec.get("auth"):
        spec["auth"] = generate_chisel_auth()
    if tunnel.core == "bore" and not spec.get("secret"):
        spec["secret"] = generate_bore_secret()
    if tunnel.core == "gost":
        spec.pop("forward_to", None)
        spec["type"] = tunnel.type
    tunnel.spec = spec


class TunnelResponse(BaseModel):
    id: str
    name: str
    core: str
    type: str
    node_id: str
    foreign_node_id: str | None = None
    iran_node_id: str | None = None
    spec: dict
    status: str
    error_message: str | None = None
    revision: int
    used_mb: float = 0.0
    quota_mb: float = 0.0
    created_at: datetime
    updated_at: datetime

    @field_validator("error_message", mode="before")
    @classmethod
    def _clean_error_message(cls, value):
        from app.utils import humanize_error
        return humanize_error(value) if value else value
    
    class Config:
        from_attributes = True


def parse_ports_from_spec(spec: dict) -> list:
    """Parse ports from spec - supports both comma-separated string and list formats"""
    ports = spec.get("ports", [])
    if isinstance(ports, str):
        # Comma-separated string: "8080,8081,8082"
        ports = [int(p.strip()) for p in ports.split(",") if p.strip().isdigit()]
    elif isinstance(ports, list) and ports:
        # List of numbers or strings
        ports = [int(p) if isinstance(p, (int, str)) and str(p).isdigit() else p for p in ports]
    return ports if ports else []


def _control_port_from_spec(core: str, spec: dict) -> int | None:
    """Derive the Iran-side control/bind port used by reverse cores."""
    if not spec:
        return None
    for key in ("control_port", "bind_port", "server_port"):
        val = spec.get(key)
        if val is not None and str(val).isdigit():
            return int(val)
    ports = parse_ports_from_spec(spec)
    listen = spec.get("listen_port") or spec.get("remote_port") or (ports[0] if ports else None)
    if listen is None:
        return None
    try:
        listen_i = int(listen)
    except (TypeError, ValueError):
        return None
    if core in ("wstunnel", "chisel"):
        return listen_i + 10000
    if core == "bore":
        from app.utils import BORE_CONTROL_PORT
        return int(BORE_CONTROL_PORT)
    return listen_i


def _port_conflict_key(core: str, spec: dict, iran_node_id: str | None, foreign_node_id: str | None) -> tuple:
    ports = sorted(
        int(p) for p in parse_ports_from_spec(spec or {})
        if isinstance(p, int) or (isinstance(p, str) and str(p).isdigit())
    )
    listen = (spec or {}).get("listen_port") or (ports[0] if ports else None)
    try:
        listen_i = int(listen) if listen is not None else None
    except (TypeError, ValueError):
        listen_i = None
    control = _control_port_from_spec(core, spec or {})
    return (core, iran_node_id or "", foreign_node_id or "", tuple(ports), listen_i, control)


# Serialize create requests that would bind the same ports (double-click / race).
_create_locks: dict[tuple, asyncio.Lock] = {}
_create_locks_guard = asyncio.Lock()


@router.post("", response_model=TunnelResponse)
async def create_tunnel(tunnel: TunnelCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """Create a new tunnel and auto-apply it"""
    from app.node_client import NodeClient
    
    logger.info(f"Creating tunnel: name={tunnel.name}, type={tunnel.type}, core={tunnel.core}, node_id={tunnel.node_id}")
    
    if tunnel.spec and tunnel.core == "backhaul":
        ports_received = tunnel.spec.get("ports", [])
        logger.info(f"Backhaul tunnel creation: received ports from frontend: {ports_received} (type: {type(ports_received)}, length: {len(ports_received) if isinstance(ports_received, list) else 'N/A'})")
    
    if tunnel.spec and tunnel.core != "backhaul":
        ports = parse_ports_from_spec(tunnel.spec)
        if ports:
            tunnel.spec["ports"] = ports
    
    is_reverse_tunnel = tunnel.core in {"rathole", "backhaul", "chisel", "frp", "wstunnel", "bore"}
    # GOST Iran→Foreign forwarding also needs both node roles persisted (and GRE preference).
    is_dual_node_tunnel = is_reverse_tunnel or tunnel.core == "gost"
    foreign_node = None
    iran_node = None
    
    if is_dual_node_tunnel:
        foreign_node_id_val = tunnel.foreign_node_id if tunnel.foreign_node_id and (not isinstance(tunnel.foreign_node_id, str) or tunnel.foreign_node_id.strip()) else None
        if foreign_node_id_val:
            result = await db.execute(select(Node).where(Node.id == foreign_node_id_val))
            foreign_node = result.scalar_one_or_none()
            if not foreign_node:
                raise HTTPException(status_code=404, detail=f"Foreign node {foreign_node_id_val} not found")
            if foreign_node.node_metadata.get("role") != "foreign":
                raise HTTPException(status_code=400, detail=f"Node {foreign_node_id_val} is not a foreign node")
        
        iran_node_id_val = tunnel.iran_node_id if tunnel.iran_node_id and (not isinstance(tunnel.iran_node_id, str) or tunnel.iran_node_id.strip()) else None
        if iran_node_id_val:
            result = await db.execute(select(Node).where(Node.id == iran_node_id_val))
            iran_node = result.scalar_one_or_none()
            if not iran_node:
                raise HTTPException(status_code=404, detail=f"Iran node {iran_node_id_val} not found")
            if iran_node.node_metadata.get("role") != "iran":
                raise HTTPException(status_code=400, detail=f"Node {iran_node_id_val} is not an iran node")
        
        node_id_val = tunnel.node_id if tunnel.node_id and (not isinstance(tunnel.node_id, str) or tunnel.node_id.strip()) else None
        if node_id_val and not (foreign_node and iran_node):
            result = await db.execute(select(Node).where(Node.id == node_id_val))
            provided_node = result.scalar_one_or_none()
            if not provided_node:
                raise HTTPException(status_code=404, detail="Node not found")
            
            node_role = provided_node.node_metadata.get("role", "iran")
            if node_role == "foreign":
                foreign_node = provided_node
                result = await db.execute(select(Node))
                all_nodes = result.scalars().all()
                iran_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "iran"]
                if iran_nodes:
                    iran_node = iran_nodes[0]
                else:
                    raise HTTPException(status_code=400, detail="No iran node found. Please specify iran_node_id or register an iran node.")
            else:
                iran_node = provided_node
                result = await db.execute(select(Node))
                all_nodes = result.scalars().all()
                foreign_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "foreign"]
                if foreign_nodes:
                    foreign_node = foreign_nodes[0]
                else:
                    raise HTTPException(status_code=400, detail="No foreign node found. Please specify foreign_node_id or register a foreign node.")

        # Auto-pick missing peer for GOST when only one side was provided.
        if tunnel.core == "gost" and (iran_node or foreign_node) and not (iran_node and foreign_node):
            result = await db.execute(select(Node))
            all_nodes = result.scalars().all()
            if not iran_node:
                iran_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "iran"]
                if iran_nodes:
                    iran_node = iran_nodes[0]
            if not foreign_node:
                foreign_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "foreign"]
                if foreign_nodes:
                    foreign_node = foreign_nodes[0]
        
        if is_reverse_tunnel and (not foreign_node or not iran_node):
            raise HTTPException(status_code=400, detail=f"Both foreign and iran nodes are required for {tunnel.core.title()} tunnels. Provide foreign_node_id and iran_node_id, or provide node_id and we'll find the matching node.")
        if tunnel.core == "gost" and not iran_node:
            raise HTTPException(status_code=400, detail="GOST tunnels require an Iran node (iran_node_id/node_id).")
        
        node = iran_node
    else:
        node = None
        if tunnel.node_id or tunnel.iran_node_id:
            node_id_to_check = tunnel.iran_node_id or tunnel.node_id
            result = await db.execute(select(Node).where(Node.id == node_id_to_check))
            node = result.scalar_one_or_none()
    
    tunnel_node_id = (iran_node.id if iran_node else None) or tunnel.iran_node_id or tunnel.node_id or ""
    
    foreign_node_id_to_store = foreign_node.id if foreign_node else None
    iran_node_id_to_store = iran_node.id if iran_node else None

    conflict_key = _port_conflict_key(
        tunnel.core,
        tunnel.spec or {},
        iran_node_id_to_store,
        foreign_node_id_to_store,
    )
    async with _create_locks_guard:
        lock = _create_locks.get(conflict_key)
        if lock is None:
            lock = asyncio.Lock()
            _create_locks[conflict_key] = lock

    async with lock:
        # Reject duplicate create (double-click / parallel requests) for same ports.
        result = await db.execute(
            select(Tunnel).where(
                Tunnel.core == tunnel.core,
                Tunnel.status.in_(["pending", "active"]),
            )
        )
        existing_tunnels = result.scalars().all()
        for existing in existing_tunnels:
            existing_iran = existing.iran_node_id or existing.node_id
            existing_foreign = existing.foreign_node_id
            if _port_conflict_key(
                existing.core,
                existing.spec or {},
                existing_iran,
                existing_foreign,
            ) == conflict_key:
                logger.warning(
                    "Duplicate tunnel create blocked: core=%s ports=%s existing=%s status=%s",
                    tunnel.core,
                    conflict_key,
                    existing.id,
                    existing.status,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"These ports are already used by tunnel \"{existing.name or existing.id}\" "
                        f"({existing.core}, status {existing.status}). "
                        "Delete or edit that tunnel instead of creating another one."
                    ),
                )

        db_tunnel = Tunnel(
            name=tunnel.name,
            core=tunnel.core,
            type=tunnel.type,
            node_id=tunnel_node_id,
            foreign_node_id=foreign_node_id_to_store,
            iran_node_id=iran_node_id_to_store,
            spec=tunnel.spec,
            status="pending"
        )
        db.add(db_tunnel)
        await db.commit()
        await db.refresh(db_tunnel)
    
    try:
        needs_gost_forwarding = db_tunnel.type in ["tcp", "udp", "ws", "grpc", "tcpmux"] and db_tunnel.core == "gost" and not is_reverse_tunnel
        needs_rathole_server = False
        needs_backhaul_server = False
        needs_chisel_server = False
        needs_frp_server = False
        needs_node_apply = db_tunnel.core in {"rathole", "backhaul", "chisel", "frp", "wstunnel", "bore"}
        
        logger.info(
            "Tunnel %s: gost=%s, rathole=%s, backhaul=%s, chisel=%s, frp=%s, wstunnel=%s, bore=%s",
            db_tunnel.id,
            needs_gost_forwarding,
            needs_rathole_server,
            needs_backhaul_server,
            needs_chisel_server,
            needs_frp_server,
            db_tunnel.core == "wstunnel",
            db_tunnel.core == "bore",
        )
        
        if is_reverse_tunnel and foreign_node and iran_node:
            client = NodeClient()
            
            server_spec = db_tunnel.spec.copy() if db_tunnel.spec else {}
            server_spec["mode"] = "server"
            
            if "ports" in db_tunnel.spec and "ports" not in server_spec:
                server_spec["ports"] = db_tunnel.spec.get("ports", [])
            
            client_spec = db_tunnel.spec.copy() if db_tunnel.spec else {}
            client_spec["mode"] = "client"
            
            if db_tunnel.core == "rathole":
                transport = server_spec.get("transport") or server_spec.get("type") or "tcp"
                token = server_spec.get("token")
                if not token:
                    from app.utils import generate_token
                    token = generate_token()
                    server_spec["token"] = token
                    db_tunnel.spec["token"] = token
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(db_tunnel, "spec")
                
                ports = parse_ports_from_spec(db_tunnel.spec)
                if not ports:
                    proxy_port = server_spec.get("remote_port") or server_spec.get("listen_port")
                    if proxy_port:
                        ports = [int(proxy_port) if isinstance(proxy_port, (int, str)) and str(proxy_port).isdigit() else proxy_port]
                
                if not ports:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Rathole requires ports"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                
                remote_addr = server_spec.get("remote_addr", "0.0.0.0:23333")
                from app.utils import parse_address_port
                _, control_port, _ = parse_address_port(remote_addr)
                if not control_port:
                    import hashlib
                    port_hash = int(hashlib.md5(db_tunnel.id.encode()).hexdigest()[:8], 16)
                    control_port = 23333 + (port_hash % 1000)
                server_spec["bind_addr"] = f"0.0.0.0:{control_port}"
                server_spec["ports"] = ports
                server_spec["transport"] = transport
                server_spec["type"] = transport
                if "websocket_tls" in server_spec:
                    server_spec["websocket_tls"] = server_spec["websocket_tls"]
                elif "tls" in server_spec:
                    server_spec["websocket_tls"] = server_spec["tls"]
                
                if not iran_node.node_metadata.get("ip_address"):
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Iran node has no IP address"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                transport_lower = transport.lower()
                if transport_lower in ("websocket", "ws"):
                    use_tls = bool(server_spec.get("websocket_tls") or server_spec.get("tls"))
                    protocol = "wss://" if use_tls else "ws://"
                    client_spec["remote_addr"] = f"{protocol}{iran_node_ip}:{control_port}"
                else:
                    client_spec["remote_addr"] = f"{iran_node_ip}:{control_port}"
                client_spec["transport"] = transport
                client_spec["type"] = transport
                client_spec["token"] = token
                client_spec["ports"] = ports  # Pass ports to client
                if "websocket_tls" in server_spec:
                    client_spec["websocket_tls"] = server_spec["websocket_tls"]
                elif "tls" in server_spec:
                    client_spec["websocket_tls"] = server_spec["tls"]
                
            elif db_tunnel.core == "chisel":
                ports = parse_ports_from_spec(db_tunnel.spec)
                if not ports:
                    listen_port = server_spec.get("listen_port") or server_spec.get("remote_port")
                    if listen_port:
                        ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]
                
                if not ports:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Chisel requires ports"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                
                if not iran_node.node_metadata.get("ip_address"):
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Iran node has no IP address"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                import hashlib
                port_hash = int(hashlib.md5(db_tunnel.id.encode()).hexdigest()[:8], 16)
                first_port = int(ports[0]) if isinstance(ports[0], (int, str)) and str(ports[0]).isdigit() else ports[0]
                server_control_port = server_spec.get("control_port") or (int(first_port) + 10000 + (port_hash % 1000))
                server_spec["server_port"] = server_control_port
                server_spec["reverse_port"] = first_port
                from app.utils import generate_chisel_auth, normalize_chisel_auth
                auth = normalize_chisel_auth(server_spec.get("auth"))
                if not auth:
                    auth = generate_chisel_auth()
                if server_spec.get("auth") != auth:
                    server_spec["auth"] = auth
                    db_tunnel.spec["auth"] = auth
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(db_tunnel, "spec")
                server_spec["auth"] = auth
                fingerprint = server_spec.get("fingerprint")
                if fingerprint:
                    server_spec["fingerprint"] = fingerprint
                
                client_spec["server_url"] = f"http://{iran_node_ip}:{server_control_port}"
                client_spec["ports"] = ports
                client_spec["auth"] = auth
                if fingerprint:
                    client_spec["fingerprint"] = fingerprint
                
            elif db_tunnel.core == "wstunnel":
                ports = parse_ports_from_spec(db_tunnel.spec)
                if not ports:
                    listen_port = server_spec.get("listen_port") or server_spec.get("remote_port")
                    if listen_port:
                        ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]

                if not ports:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Wstunnel requires ports"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel

                if not iran_node.node_metadata.get("ip_address"):
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Iran node has no IP address"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)

                import hashlib
                port_hash = int(hashlib.md5(db_tunnel.id.encode()).hexdigest()[:8], 16)
                first_port = int(ports[0]) if isinstance(ports[0], (int, str)) and str(ports[0]).isdigit() else ports[0]
                server_control_port = server_spec.get("control_port") or (int(first_port) + 10000 + (port_hash % 1000))
                server_spec["server_port"] = int(server_control_port)
                server_spec["control_port"] = int(server_control_port)
                server_spec["reverse_port"] = first_port
                server_spec["ports"] = ports

                from app.utils import generate_wstunnel_secret, build_wstunnel_server_url
                secret = (server_spec.get("secret") or db_tunnel.spec.get("secret") or "").strip()
                if not secret:
                    secret = generate_wstunnel_secret()
                    server_spec["secret"] = secret
                    db_tunnel.spec["secret"] = secret
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(db_tunnel, "spec")
                server_spec["secret"] = secret
                db_tunnel.spec["control_port"] = int(server_control_port)
                from sqlalchemy.orm.attributes import flag_modified as flag_modified_ws
                flag_modified_ws(db_tunnel, "spec")

                tunnel_type = (db_tunnel.type or "tcp").lower()
                if tunnel_type not in ("tcp", "udp"):
                    tunnel_type = "tcp"
                server_spec["type"] = tunnel_type
                use_tls = bool(server_spec.get("use_tls", False))

                client_spec["server_url"] = build_wstunnel_server_url(
                    iran_node_ip, int(server_control_port), secret, use_tls=use_tls
                )
                client_spec["ports"] = ports
                client_spec["secret"] = secret
                client_spec["type"] = tunnel_type
                client_spec["local_addr"] = server_spec.get("local_addr") or f"127.0.0.1:{first_port}"

            elif db_tunnel.core == "bore":
                ports = parse_ports_from_spec(db_tunnel.spec)
                if not ports:
                    listen_port = server_spec.get("listen_port") or server_spec.get("remote_port")
                    if listen_port:
                        ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]

                if not ports:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Bore requires ports"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel

                if not iran_node.node_metadata.get("ip_address"):
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Iran node has no IP address"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)

                from app.utils import BORE_CONTROL_PORT, generate_bore_secret
                from sqlalchemy.orm.attributes import flag_modified

                secret = (server_spec.get("secret") or db_tunnel.spec.get("secret") or "").strip()
                if not secret:
                    # Reuse secret from another active bore tunnel on same Iran node (fixed control port 7835).
                    existing_q = await db.execute(
                        select(Tunnel).where(
                            Tunnel.core == "bore",
                            Tunnel.status == "active",
                            Tunnel.id != db_tunnel.id,
                        )
                    )
                    for existing in existing_q.scalars().all():
                        same_iran = (existing.iran_node_id or existing.node_id) == iran_node.id
                        if same_iran and existing.spec and existing.spec.get("secret"):
                            secret = existing.spec["secret"]
                            break
                if not secret:
                    secret = generate_bore_secret()

                server_spec["secret"] = secret
                server_spec["control_port"] = BORE_CONTROL_PORT
                server_spec["ports"] = ports
                server_spec["type"] = "tcp"
                db_tunnel.spec["secret"] = secret
                db_tunnel.spec["control_port"] = BORE_CONTROL_PORT
                flag_modified(db_tunnel, "spec")

                client_spec["server_addr"] = iran_node_ip
                client_spec["secret"] = secret
                client_spec["control_port"] = BORE_CONTROL_PORT
                client_spec["type"] = "tcp"
                client_spec["local_host"] = server_spec.get("local_host") or "127.0.0.1"
                client_spec["ports"] = [{"local": int(p), "remote": int(p)} for p in ports]

            elif db_tunnel.core == "frp":
                import hashlib
                port_hash = int(hashlib.md5(db_tunnel.id.encode()).hexdigest()[:8], 16)
                bind_port = server_spec.get("bind_port") or (7000 + (port_hash % 1000))
                token = server_spec.get("token")
                if not token:
                    from app.utils import generate_token
                    token = generate_token()
                    server_spec["token"] = token
                    db_tunnel.spec["token"] = token
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(db_tunnel, "spec")
                server_spec["bind_port"] = bind_port
                server_spec["token"] = token
                
                if not iran_node.node_metadata.get("ip_address"):
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Iran node has no IP address"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                client_spec["server_addr"] = iran_node_ip
                client_spec["server_port"] = bind_port
                client_spec["token"] = token
                tunnel_type = db_tunnel.type.lower() if db_tunnel.type else "tcp"
                if tunnel_type not in ["tcp", "udp"]:
                    tunnel_type = "tcp"  # Default to tcp if invalid
                client_spec["type"] = tunnel_type
                # Foreign FRP client must dial the local service on the foreign node
                # (e.g. xray on 127.0.0.1), never the Iran node IP.
                local_ip = client_spec.get("local_ip") or db_tunnel.spec.get("local_ip") or "127.0.0.1"
                client_spec["local_ip"] = local_ip
                
                ports = parse_ports_from_spec(db_tunnel.spec)
                if ports:
                    client_spec["ports"] = [{"local": int(p), "remote": int(p)} for p in ports]
                else:
                    local_port = client_spec.get("local_port")
                    if not local_port:
                        local_port = db_tunnel.spec.get("listen_port") or db_tunnel.spec.get("remote_port") or bind_port
                    client_spec["local_port"] = local_port
                    if "remote_port" not in client_spec:
                        client_spec["remote_port"] = db_tunnel.spec.get("remote_port") or db_tunnel.spec.get("listen_port") or bind_port
                
            elif db_tunnel.core == "backhaul":
                transport = server_spec.get("transport") or server_spec.get("type") or "tcp"
                import hashlib
                port_hash = int(hashlib.md5(db_tunnel.id.encode()).hexdigest()[:8], 16)
                control_port = server_spec.get("control_port") or server_spec.get("listen_port") or (3080 + (port_hash % 1000))
                target_host = server_spec.get("target_host", "127.0.0.1")
                token = server_spec.get("token")
                if not token:
                    from app.utils import generate_token
                    token = generate_token()
                    server_spec["token"] = token
                    db_tunnel.spec["token"] = token
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(db_tunnel, "spec")
                
                ports = server_spec.get("ports", [])
                if not ports:
                    ports = db_tunnel.spec.get("ports", [])
                logger.info(f"Backhaul tunnel {db_tunnel.id}: received ports from server_spec: {server_spec.get('ports')}, from db_tunnel.spec: {db_tunnel.spec.get('ports')}, final: {ports} (type: {type(ports)}, length: {len(ports) if isinstance(ports, list) else 'N/A'})")
                
                if not ports or (isinstance(ports, list) and len(ports) == 0):
                    public_port = server_spec.get("public_port") or server_spec.get("remote_port") or server_spec.get("listen_port")
                    target_port = server_spec.get("target_port") or public_port
                    if not public_port:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Backhaul requires ports array or public_port/remote_port"
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                    if target_port:
                        target_addr = f"{target_host}:{target_port}"
                        ports = [f"{public_port}={target_addr}"]
                    else:
                        ports = [str(public_port)]
                else:
                    if isinstance(ports, list) and ports:
                        processed_ports = []
                        for p in ports:
                            if not p:
                                continue
                            if isinstance(p, str):
                                if '=' in p:
                                    processed_ports.append(p)
                                elif p.isdigit():
                                    processed_ports.append(f"{p}={target_host}:{p}")
                                else:
                                    processed_ports.append(p)
                            elif isinstance(p, int):
                                processed_ports.append(f"{p}={target_host}:{p}")
                            elif isinstance(p, dict):
                                local = p.get("local") or p.get("listen_port") or p.get("public_port")
                                tgt_host = p.get("target_host") or target_host
                                tgt_port = p.get("target_port") or p.get("remote_port") or local
                                if local:
                                    processed_ports.append(f"{local}={tgt_host}:{tgt_port}")
                            else:
                                processed_ports.append(str(p))
                        ports = processed_ports
                
                logger.info(f"Backhaul tunnel {db_tunnel.id}: processed ports: {ports} (count: {len(ports)})")
                
                bind_ip = server_spec.get("bind_ip") or server_spec.get("listen_ip") or "0.0.0.0"
                server_spec["bind_addr"] = f"{bind_ip}:{control_port}"
                server_spec["transport"] = transport
                server_spec["type"] = transport
                server_spec["ports"] = ports
                server_spec["mode"] = "server"
                server_spec["token"] = token
                
                # CRITICAL: Update the database spec with processed ports so they're preserved
                if "ports" not in db_tunnel.spec:
                    db_tunnel.spec["ports"] = []
                db_tunnel.spec["ports"] = ports.copy() if isinstance(ports, list) else ports
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(db_tunnel, "spec")
                await db.commit()
                await db.refresh(db_tunnel)
                logger.info(f"Backhaul tunnel {db_tunnel.id}: saved ports to database: {db_tunnel.spec.get('ports')} (count: {len(db_tunnel.spec.get('ports', []))})")
                
                if not iran_node.node_metadata.get("ip_address"):
                    db_tunnel.status = "error"
                    db_tunnel.error_message = "Iran node has no IP address"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
                iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                transport_lower = transport.lower()
                if transport_lower in ("ws", "wsmux"):
                    use_tls = bool(server_spec.get("tls_cert") or server_spec.get("server_options", {}).get("tls_cert"))
                    protocol = "wss://" if use_tls else "ws://"
                    client_spec["remote_addr"] = f"{protocol}{iran_node_ip}:{control_port}"
                else:
                    client_spec["remote_addr"] = f"{iran_node_ip}:{control_port}"
                client_spec["transport"] = transport
                client_spec["type"] = transport
                client_spec["mode"] = "client"  # Ensure mode is set
                if token:
                    client_spec["token"] = token
            
            if not iran_node.node_metadata.get("api_address"):
                iran_node.node_metadata["api_address"] = f"http://{iran_node.node_metadata.get('ip_address', iran_node.fingerprint)}:{iran_node.node_metadata.get('api_port', 8888)}"
                await db.commit()
            
            logger.info(f"Applying server config to iran node {iran_node.id} for tunnel {db_tunnel.id}")
            server_response = await client.send_to_node(
                node_id=iran_node.id,
                endpoint="/api/agent/tunnels/apply",
                data={
                    "tunnel_id": db_tunnel.id,
                    "core": db_tunnel.core,
                    "type": db_tunnel.type,
                    "spec": server_spec
                }
            )
            
            if server_response.get("status") == "error":
                db_tunnel.status = "error"
                error_msg = server_response.get("message", "Unknown error from iran node")
                db_tunnel.error_message = f"Iran node error: {error_msg}"
                logger.error(f"Tunnel {db_tunnel.id}: Iran node error: {error_msg}")
                await db.commit()
                await db.refresh(db_tunnel)
                return db_tunnel
            
            if not foreign_node.node_metadata.get("api_address"):
                foreign_node.node_metadata["api_address"] = f"http://{foreign_node.node_metadata.get('ip_address', foreign_node.fingerprint)}:{foreign_node.node_metadata.get('api_port', 8888)}"
                await db.commit()
            
            logger.info(f"Applying client config to foreign node {foreign_node.id} for tunnel {db_tunnel.id}")
            client_response = await client.send_to_node(
                node_id=foreign_node.id,
                endpoint="/api/agent/tunnels/apply",
                data={
                    "tunnel_id": db_tunnel.id,
                    "core": db_tunnel.core,
                    "type": db_tunnel.type,
                    "spec": client_spec
                }
            )
            
            if client_response.get("status") == "error":
                db_tunnel.status = "error"
                error_msg = client_response.get("message", "Unknown error from foreign node")
                db_tunnel.error_message = f"Foreign node error: {error_msg}"
                logger.error(f"Tunnel {db_tunnel.id}: Foreign node error: {error_msg}")
                try:
                    await client.send_to_node(
                        node_id=iran_node.id,
                        endpoint="/api/agent/tunnels/remove",
                        data={"tunnel_id": db_tunnel.id}
                    )
                except:
                    pass
                await db.commit()
                await db.refresh(db_tunnel)
                return db_tunnel
            
            if server_response.get("status") == "success" and client_response.get("status") == "success":
                db_tunnel.status = "active"
                logger.info(f"Tunnel {db_tunnel.id} successfully applied to both nodes")
            else:
                db_tunnel.status = "error"
                db_tunnel.error_message = "Failed to apply tunnel to one or both nodes"
                logger.error(f"Tunnel {db_tunnel.id}: Failed to apply to nodes")
            
            await db.commit()
            await db.refresh(db_tunnel)
            return db_tunnel
        
        
        if needs_node_apply and not is_reverse_tunnel:
            remote_addr = db_tunnel.spec.get("remote_addr")
            token = db_tunnel.spec.get("token")
            proxy_port = db_tunnel.spec.get("remote_port") or db_tunnel.spec.get("listen_port")
            use_ipv6 = db_tunnel.spec.get("use_ipv6", False)
            
            if remote_addr:
                from app.utils import parse_address_port
                _, rathole_port, _ = parse_address_port(remote_addr)
                try:
                    if rathole_port and int(rathole_port) == 8000:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Rathole server cannot use port 8000 (panel API port). Use a different port like 23333."
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                except (ValueError, TypeError):
                    pass
            
            if remote_addr and token and proxy_port and hasattr(request.app.state, 'rathole_server_manager'):
                try:
                    logger.info(f"Starting Rathole server for tunnel {db_tunnel.id}: remote_addr={remote_addr}, token={token}, proxy_port={proxy_port}, use_ipv6={use_ipv6}")
                    request.app.state.rathole_server_manager.start_server(
                        tunnel_id=db_tunnel.id,
                        remote_addr=remote_addr,
                        token=token,
                        proxy_port=int(proxy_port),
                        use_ipv6=bool(use_ipv6)
                    )
                    logger.info(f"Successfully started Rathole server for tunnel {db_tunnel.id}")
                    rathole_started = True
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Failed to start Rathole server for tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Rathole server error: {error_msg}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
            else:
                missing = []
                if not remote_addr:
                    missing.append("remote_addr")
                if not token:
                    missing.append("token")
                if not proxy_port:
                    missing.append("proxy_port")
                if not hasattr(request.app.state, 'rathole_server_manager'):
                    missing.append("rathole_server_manager")
                logger.warning(f"Tunnel {db_tunnel.id}: Missing required fields for Rathole server: {missing}")
                if not remote_addr or not token or not proxy_port:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Missing required fields for Rathole: {missing}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
        
        if needs_chisel_server:
            listen_port = db_tunnel.spec.get("listen_port") or db_tunnel.spec.get("remote_port") or db_tunnel.spec.get("server_port")
            from app.utils import normalize_chisel_auth
            auth = normalize_chisel_auth(db_tunnel.spec.get("auth"))
            fingerprint = db_tunnel.spec.get("fingerprint")
            use_ipv6 = db_tunnel.spec.get("use_ipv6", False)
            
            if listen_port:
                from app.utils import parse_address_port
                try:
                    if int(listen_port) == 8000:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Chisel server cannot use port 8000 (panel API port). Use a different port."
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                except (ValueError, TypeError):
                    pass
            
            if listen_port and hasattr(request.app.state, 'chisel_server_manager'):
                try:
                    server_control_port = db_tunnel.spec.get("control_port")
                    if server_control_port:
                        server_control_port = int(server_control_port)
                    else:
                        server_control_port = int(listen_port) + 10000
                    logger.info(f"Starting Chisel server for tunnel {db_tunnel.id}: server_control_port={server_control_port}, reverse_port={listen_port}, auth={auth is not None}, fingerprint={fingerprint is not None}, use_ipv6={use_ipv6}")
                    request.app.state.chisel_server_manager.start_server(
                        tunnel_id=db_tunnel.id,
                        server_port=server_control_port,
                        auth=auth,
                        fingerprint=fingerprint,
                        use_ipv6=bool(use_ipv6)
                    )
                    time.sleep(1.0)
                    if not request.app.state.chisel_server_manager.is_running(db_tunnel.id):
                        raise RuntimeError("Chisel server process started but is not running")
                    chisel_started = True
                    logger.info(f"Successfully started Chisel server for tunnel {db_tunnel.id}")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Failed to start Chisel server for tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Chisel server error: {error_msg}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
            else:
                missing = []
                if not listen_port:
                    missing.append("listen_port")
                if not hasattr(request.app.state, 'chisel_server_manager'):
                    missing.append("chisel_server_manager")
                logger.warning(f"Tunnel {db_tunnel.id}: Missing required fields for Chisel server: {missing}")
                if not listen_port:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Missing required fields for Chisel: {missing}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
        
        if needs_frp_server:
            bind_port = db_tunnel.spec.get("bind_port", 7000)
            token = db_tunnel.spec.get("token")
            
            if bind_port:
                from app.utils import parse_address_port
                try:
                    if int(bind_port) == 8000:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "FRP server cannot use port 8000 (panel API port). Use a different port like 7000."
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                except (ValueError, TypeError):
                    pass
            
            if bind_port and hasattr(request.app.state, 'frp_server_manager'):
                try:
                    logger.info(f"Starting FRP server for tunnel {db_tunnel.id}: bind_port={bind_port}, token={'set' if token else 'none'}")
                    request.app.state.frp_server_manager.start_server(
                        tunnel_id=db_tunnel.id,
                        bind_port=int(bind_port),
                        token=token
                    )
                    time.sleep(1.0)
                    if not request.app.state.frp_server_manager.is_running(db_tunnel.id):
                        raise RuntimeError("FRP server process started but is not running")
                    frp_started = True
                    logger.info(f"Successfully started FRP server for tunnel {db_tunnel.id}")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Failed to start FRP server for tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"FRP server error: {error_msg}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
            else:
                missing = []
                if not bind_port:
                    missing.append("bind_port")
                if not hasattr(request.app.state, 'frp_server_manager'):
                    missing.append("frp_server_manager")
                logger.warning(f"Tunnel {db_tunnel.id}: Missing required fields for FRP server: {missing}")
                if not bind_port:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Missing required fields for FRP: {missing}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
        
        if needs_node_apply:
            if not node:
                raise HTTPException(status_code=400, detail=f"Node is required for {db_tunnel.core.title()} tunnels")
            
            client = NodeClient()
            if not node.node_metadata.get("api_address"):
                node.node_metadata["api_address"] = f"http://{node.node_metadata.get('ip_address', node.fingerprint)}:{node.node_metadata.get('api_port', 8888)}"
                await db.commit()
            
            spec_for_node = db_tunnel.spec.copy() if db_tunnel.spec else {}
            
            if needs_chisel_server:
                listen_port = spec_for_node.get("listen_port") or spec_for_node.get("remote_port") or spec_for_node.get("server_port")
                use_ipv6 = spec_for_node.get("use_ipv6", False)
                if listen_port:
                    server_control_port = spec_for_node.get("control_port")
                    if server_control_port:
                        server_control_port = int(server_control_port)
                    else:
                        server_control_port = int(listen_port) + 10000
                    reverse_port = int(listen_port)
                    
                    panel_host = spec_for_node.get("panel_host")
                    
                    if not panel_host:
                        panel_address = node.node_metadata.get("panel_address", "")
                        if panel_address:
                            if "://" in panel_address:
                                panel_address = panel_address.split("://", 1)[1]
                            if ":" in panel_address:
                                panel_host = panel_address.split(":")[0]
                            else:
                                panel_host = panel_address
                    
                    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1"]:
                        panel_host = request.url.hostname
                        if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1"]:
                            forwarded_host = request.headers.get("X-Forwarded-Host")
                            if forwarded_host:
                                panel_host = forwarded_host.split(":")[0] if ":" in forwarded_host else forwarded_host
                    
                    if not panel_host or panel_host in ["localhost", "127.0.0.1", "::1"]:
                        logger.warning(f"Chisel tunnel {db_tunnel.id}: Could not determine panel host, using request hostname: {request.url.hostname}. Node may not be able to connect if this is localhost.")
                        panel_host = request.url.hostname or "localhost"
                    
                    from app.utils import is_valid_ipv6_address
                    if is_valid_ipv6_address(panel_host):
                        server_url = f"http://[{panel_host}]:{server_control_port}"
                    else:
                        server_url = f"http://{panel_host}:{server_control_port}"
                    spec_for_node["server_url"] = server_url
                    spec_for_node["reverse_port"] = reverse_port
                    spec_for_node["remote_port"] = int(listen_port)
                    logger.info(f"Chisel tunnel {db_tunnel.id}: server_url={server_url}, server_control_port={server_control_port}, reverse_port={reverse_port}, use_ipv6={use_ipv6}, panel_host={panel_host}")
            
            if needs_frp_server:
                logger.info(f"Preparing FRP spec for tunnel {db_tunnel.id}, original spec server_addr: {spec_for_node.get('server_addr', 'NOT SET')}")
                try:
                    spec_for_node = prepare_frp_spec_for_node(spec_for_node, node, request)
                    final_server_addr = spec_for_node.get('server_addr', 'NOT SET')
                    logger.info(f"FRP spec prepared for tunnel {db_tunnel.id}: server_addr={final_server_addr}, server_port={spec_for_node.get('server_port')}")
                    if final_server_addr in ["0.0.0.0", "NOT SET", ""]:
                        raise ValueError(f"FRP server_addr is invalid: {final_server_addr}")
                except Exception as e:
                    error_msg = f"Failed to prepare FRP spec: {str(e)}"
                    logger.error(f"Tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"FRP configuration error: {error_msg}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
            
            logger.info(f"Applying tunnel {db_tunnel.id} to node {node.id}, spec keys: {list(spec_for_node.keys())}, server_addr: {spec_for_node.get('server_addr', 'NOT SET')}, full spec: {spec_for_node}")
            response = await client.send_to_node(
                node_id=node.id,
                endpoint="/api/agent/tunnels/apply",
                data={
                    "tunnel_id": db_tunnel.id,
                    "core": db_tunnel.core,
                    "type": db_tunnel.type,
                    "spec": spec_for_node
                }
            )
            
            if response.get("status") == "error":
                db_tunnel.status = "error"
                error_msg = response.get("message", "Unknown error from node")
                db_tunnel.error_message = f"Node error: {error_msg}"
                logger.error(f"Tunnel {db_tunnel.id}: {error_msg}")
                if needs_rathole_server and hasattr(request.app.state, 'rathole_server_manager'):
                    try:
                        request.app.state.rathole_server_manager.stop_server(db_tunnel.id)
                    except:
                        pass
                if needs_backhaul_server and hasattr(request.app.state, "backhaul_manager"):
                    try:
                        request.app.state.backhaul_manager.stop_server(db_tunnel.id)
                    except Exception:
                        pass
                if needs_chisel_server and hasattr(request.app.state, 'chisel_server_manager'):
                    try:
                        request.app.state.chisel_server_manager.stop_server(db_tunnel.id)
                    except Exception:
                        pass
                if needs_frp_server and hasattr(request.app.state, 'frp_server_manager'):
                    try:
                        request.app.state.frp_server_manager.stop_server(db_tunnel.id)
                    except Exception:
                        pass
                await db.commit()
                await db.refresh(db_tunnel)
                return db_tunnel
            
            if response.get("status") != "success":
                db_tunnel.status = "error"
                db_tunnel.error_message = "Failed to apply tunnel to node. Check node connection."
                logger.error(f"Tunnel {db_tunnel.id}: Failed to apply to node")
                if needs_rathole_server and hasattr(request.app.state, 'rathole_server_manager'):
                    try:
                        request.app.state.rathole_server_manager.stop_server(db_tunnel.id)
                    except:
                        pass
                if needs_backhaul_server and hasattr(request.app.state, "backhaul_manager"):
                    try:
                        request.app.state.backhaul_manager.stop_server(db_tunnel.id)
                    except Exception:
                        pass
                if needs_chisel_server and hasattr(request.app.state, 'chisel_server_manager'):
                    try:
                        request.app.state.chisel_server_manager.stop_server(db_tunnel.id)
                    except Exception:
                        pass
                if needs_frp_server and hasattr(request.app.state, 'frp_server_manager'):
                    try:
                        request.app.state.frp_server_manager.stop_server(db_tunnel.id)
                    except Exception:
                        pass
                await db.commit()
                await db.refresh(db_tunnel)
                return db_tunnel
        
        db_tunnel.status = "active"
        
        try:
            if needs_gost_forwarding:
                # Prefer persisted IDs; fall back to request body for older clients.
                iran_node_id_val = (
                    db_tunnel.iran_node_id
                    or db_tunnel.node_id
                    or (tunnel.iran_node_id if tunnel.iran_node_id and str(tunnel.iran_node_id).strip() else None)
                    or (tunnel.node_id if tunnel.node_id and str(tunnel.node_id).strip() else None)
                )
                foreign_node_id_val = (
                    db_tunnel.foreign_node_id
                    or (tunnel.foreign_node_id if tunnel.foreign_node_id and str(tunnel.foreign_node_id).strip() else None)
                )

                if iran_node_id_val and not foreign_node_id_val:
                    # Recover foreign node automatically for GOST.
                    result = await db.execute(select(Node))
                    foreign_nodes = [n for n in result.scalars().all() if n.node_metadata and n.node_metadata.get("role") == "foreign"]
                    if foreign_nodes:
                        foreign_node_id_val = foreign_nodes[0].id
                        db_tunnel.foreign_node_id = foreign_node_id_val
                
                if iran_node_id_val and foreign_node_id_val:
                    result = await db.execute(select(Node).where(Node.id == iran_node_id_val))
                    iran_node = result.scalar_one_or_none()
                    result = await db.execute(select(Node).where(Node.id == foreign_node_id_val))
                    foreign_node = result.scalar_one_or_none()
                    
                    if not iran_node:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Iran node not found"
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                    
                    if not foreign_node:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Foreign server not found"
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel

                    # Persist roles on the tunnel row (fixes null iran/foreign IDs for GOST).
                    db_tunnel.iran_node_id = iran_node.id
                    db_tunnel.foreign_node_id = foreign_node.id
                    if not db_tunnel.node_id:
                        db_tunnel.node_id = iran_node.id
                    
                    foreign_ip = foreign_node.node_metadata.get("ip_address")
                    if not foreign_ip:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Foreign server has no IP address"
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                    
                    ports = parse_ports_from_spec(db_tunnel.spec)
                    if not ports:
                        listen_port = db_tunnel.spec.get("listen_port") or db_tunnel.spec.get("remote_port")
                        if listen_port:
                            ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]
                    
                    if not ports:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "GOST requires ports"
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                    
                    use_ipv6 = db_tunnel.spec.get("use_ipv6", False)
                    from app.utils import resolve_gost_forward_target
                    from sqlalchemy.orm.attributes import flag_modified
                    target = resolve_gost_forward_target(
                        foreign_public_ip=foreign_ip,
                        iran_gre_peers=(iran_node.node_metadata or {}).get("gre_peers") or [],
                        foreign_gre_peers=(foreign_node.node_metadata or {}).get("gre_peers") or [],
                        iran_public_ip=(iran_node.node_metadata or {}).get("ip_address"),
                        explicit_remote_ip=db_tunnel.spec.get("remote_ip"),
                    )
                    remote_ip = target["remote_ip"]
                    db_tunnel.spec["remote_ip"] = remote_ip
                    db_tunnel.spec["public_ip"] = target.get("public_ip") or foreign_ip
                    db_tunnel.spec["forward_via"] = target.get("via")
                    db_tunnel.spec["forward_mtu"] = target.get("mtu")
                    db_tunnel.spec["tcp_mss"] = target.get("mss")
                    if target.get("iface"):
                        db_tunnel.spec["forward_iface"] = target["iface"]
                    flag_modified(db_tunnel, "spec")
                    
                    gost_spec = {
                        "ports": ports,
                        "remote_ip": remote_ip,
                        "public_ip": target.get("public_ip") or foreign_ip,
                        "forward_via": target.get("via"),
                        "forward_mtu": target.get("mtu"),
                        "tcp_mss": target.get("mss"),
                        "type": db_tunnel.type,
                        "use_ipv6": use_ipv6
                    }
                    
                    client = NodeClient()
                    if not iran_node.node_metadata.get("api_address"):
                        iran_node.node_metadata["api_address"] = f"http://{iran_node.node_metadata.get('ip_address', iran_node.fingerprint)}:{iran_node.node_metadata.get('api_port', 8888)}"
                        await db.commit()
                    
                    logger.info(
                        f"Applying GOST forwarding to Iran node {iran_node.id} for tunnel {db_tunnel.id}: "
                        f"{db_tunnel.type} ports={ports} -> {remote_ip} via={target.get('via')} mtu={target.get('mtu')}"
                    )
                    response = await client.send_to_node(
                        node_id=iran_node.id,
                        endpoint="/api/agent/tunnels/apply",
                        data={
                            "tunnel_id": db_tunnel.id,
                            "core": "gost",
                            "type": db_tunnel.type,
                            "spec": gost_spec
                        }
                    )
                    
                    if response.get("status") != "success":
                        error_msg = response.get("message", "Unknown error from Iran node")
                        db_tunnel.status = "error"
                        db_tunnel.error_message = f"Iran node error: {error_msg}"
                        logger.error(f"Tunnel {db_tunnel.id}: Iran node error: {error_msg}")
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                    
                    logger.info(f"Successfully applied GOST forwarding to Iran node for tunnel {db_tunnel.id}")
                else:
                    ports = parse_ports_from_spec(db_tunnel.spec)
                    if not ports:
                        listen_port = db_tunnel.spec.get("listen_port")
                        if listen_port:
                            ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]
                    
                    forward_to = db_tunnel.spec.get("forward_to")
                    remote_ip = db_tunnel.spec.get("remote_ip", "127.0.0.1")
                    use_ipv6 = db_tunnel.spec.get("use_ipv6", False)
                    
                    if not ports:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "GOST requires ports"
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                    
                    if ports and hasattr(request.app.state, 'gost_forwarder'):
                        try:
                            for port in ports:
                                port_num = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else port
                                if not forward_to:
                                    from app.utils import format_address_port
                                    forward_to_port = format_address_port(remote_ip, port_num)
                                else:
                                    forward_to_port = forward_to
                                
                                tunnel_id_for_port = f"{db_tunnel.id}_{port_num}" if len(ports) > 1 else db_tunnel.id
                                logger.info(f"Starting gost forwarding on panel for tunnel {db_tunnel.id}: {db_tunnel.type}://:{port_num} -> {forward_to_port}, use_ipv6={use_ipv6}")
                                request.app.state.gost_forwarder.start_forward(
                                    tunnel_id=tunnel_id_for_port,
                                    local_port=port_num,
                                    forward_to=forward_to_port,
                                    tunnel_type=db_tunnel.type,
                                    use_ipv6=bool(use_ipv6)
                                )
                            
                            time.sleep(2)
                            logger.info(f"Successfully started gost forwarding on panel for tunnel {db_tunnel.id} with {len(ports)} ports")
                        except Exception as e:
                            error_msg = str(e)
                            logger.error(f"Failed to start gost forwarding on panel for tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                            db_tunnel.status = "error"
                            db_tunnel.error_message = f"Gost forwarding error: {error_msg}"
                            await db.commit()
                            await db.refresh(db_tunnel)
                            return db_tunnel
                    else:
                        missing = []
                        if not ports:
                            missing.append("ports")
                        if not forward_to and not remote_ip:
                            missing.append("forward_to")
                        if not hasattr(request.app.state, 'gost_forwarder'):
                            missing.append("gost_forwarder")
                        logger.warning(f"Tunnel {db_tunnel.id}: Missing required fields: {missing}")
                        if not forward_to:
                            error_msg = "forward_to is required for gost tunnels"
                            db_tunnel.status = "error"
                            db_tunnel.error_message = error_msg
            
        except Exception as e:
            logger.error(f"Exception in forwarding setup for tunnel {db_tunnel.id}: {e}", exc_info=True)
        
        await db.commit()
        await db.refresh(db_tunnel)
    except Exception as e:
        logger.error(f"Exception in tunnel creation for {db_tunnel.id}: {e}", exc_info=True)
        error_msg = str(e)
        db_tunnel.status = "error"
        db_tunnel.error_message = f"Tunnel creation error: {error_msg}"
        try:
            if needs_rathole_server and hasattr(request.app.state, "rathole_server_manager"):
                request.app.state.rathole_server_manager.stop_server(db_tunnel.id)
        except Exception:
            pass
        try:
            if needs_backhaul_server and hasattr(request.app.state, "backhaul_manager"):
                request.app.state.backhaul_manager.stop_server(db_tunnel.id)
        except Exception:
            pass
        await db.commit()
        await db.refresh(db_tunnel)
    
    return db_tunnel


@router.get("", response_model=List[TunnelResponse])
async def list_tunnels(db: AsyncSession = Depends(get_db)):
    """List all tunnels"""
    result = await db.execute(select(Tunnel))
    tunnels = result.scalars().all()
    return tunnels


def _ports_for_live(tunnel) -> list[int]:
    ports = parse_ports_from_spec(tunnel.spec or {})
    cleaned: list[int] = []
    for item in ports:
        if isinstance(item, int):
            cleaned.append(item)
        elif isinstance(item, str):
            text = item.split("=", 1)[0]
            if ":" in text:
                text = text.rsplit(":", 1)[-1]
            if text.isdigit():
                cleaned.append(int(text))
        elif isinstance(item, dict):
            for key in ("local", "remote", "listen_port", "public_port"):
                val = item.get(key)
                if val is not None and str(val).isdigit():
                    cleaned.append(int(val))
                    break
    if not cleaned:
        for key in ("listen_port", "remote_port", "public_port"):
            val = (tunnel.spec or {}).get(key)
            if val is not None and str(val).isdigit():
                cleaned.append(int(val))
    # unique preserve order
    seen = set()
    out = []
    for p in cleaned:
        if p not in seen and 1 <= p <= 65535:
            seen.add(p)
            out.append(p)
    return out


@router.get("/live-status")
async def tunnels_live_status(db: AsyncSession = Depends(get_db)):
    """
    Probe Iran nodes for real per-port listen state.
    Does not invent 'active' — only reports what is actually listening.
    """
    from app.node_client import NodeClient

    result = await db.execute(select(Tunnel))
    tunnels = result.scalars().all()
    client = NodeClient()

    async def one(tunnel):
        iran_id = tunnel.iran_node_id or tunnel.node_id
        ports = _ports_for_live(tunnel)
        control = _control_port_from_spec(tunnel.core, tunnel.spec or {})
        if not iran_id:
            return tunnel.id, {
                "status": "error",
                "process_running": False,
                "ports": [{"port": p, "status": "error", "error": "No Iran node on this tunnel"} for p in ports],
                "error": "No Iran node on this tunnel",
            }
        resp = await client.send_to_node(
            iran_id,
            "/api/agent/tunnels/live",
            {
                "tunnel_id": tunnel.id,
                "core": tunnel.core,
                "ports": ports,
                "control_port": control,
            },
            timeout=6,
        )
        if not isinstance(resp, dict) or resp.get("ok") is not True:
            msg = (resp or {}).get("message") or (resp or {}).get("error") or "Iran node did not return live status"
            if "Network error" in str(msg) or "Connection" in str(msg):
                msg = "Iran node is unreachable"
            return tunnel.id, {
                "status": "offline",
                "process_running": False,
                "ports": [{"port": p, "status": "offline", "error": msg} for p in ports] or [
                    {"port": 0, "status": "offline", "error": msg}
                ],
                "error": msg,
            }
        # Keep DB status honest when we have a clear probe result.
        live_status = resp.get("status")
        changed = False
        if live_status == "live" and tunnel.status != "active":
            tunnel.status = "active"
            tunnel.error_message = None
            changed = True
        elif live_status == "error" and tunnel.status != "error":
            tunnel.status = "error"
            tunnel.error_message = resp.get("error") or "Port check failed"
            changed = True
        elif live_status == "offline" and tunnel.status == "active":
            tunnel.status = "error"
            tunnel.error_message = resp.get("error") or "Ports are offline"
            changed = True
        return tunnel.id, {
            "status": resp.get("status") or "offline",
            "process_running": bool(resp.get("process_running")),
            "ports": resp.get("ports") or [],
            "control": resp.get("control"),
            "error": resp.get("error"),
            "_changed": changed,
        }

    pairs = await asyncio.gather(*[one(t) for t in tunnels], return_exceptions=True)
    payload = {}
    dirty = False
    for item in pairs:
        if isinstance(item, Exception):
            logger.warning("live-status item failed: %s", item)
            continue
        tid, body = item
        if body.pop("_changed", False):
            dirty = True
        payload[tid] = body
    if dirty:
        try:
            await db.commit()
        except Exception:
            await db.rollback()
    return {"status": "ok", "tunnels": payload}


@router.get("/{tunnel_id}", response_model=TunnelResponse)
async def get_tunnel(tunnel_id: str, db: AsyncSession = Depends(get_db)):
    """Get tunnel by ID"""
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    return tunnel


@router.put("/{tunnel_id}", response_model=TunnelResponse)
async def update_tunnel(
    tunnel_id: str,
    tunnel_update: TunnelUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Update a tunnel and re-apply if spec changed"""
    from app.node_client import NodeClient
    
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    new_core = (tunnel_update.core or tunnel.core or "").strip().lower()
    new_type = (tunnel_update.type or tunnel.type or "tcp").strip().lower()
    if new_core not in _EDIT_CORE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported core '{new_core}'")
    if new_type not in _EDIT_CORE_TYPES[new_core]:
        allowed = ", ".join(sorted(_EDIT_CORE_TYPES[new_core]))
        raise HTTPException(
            status_code=400,
            detail=f"Type '{new_type}' is not valid for {new_core}. Allowed types: {allowed}",
        )

    next_spec = dict(tunnel_update.spec if tunnel_update.spec is not None else (tunnel.spec or {}))
    if new_core == "gost":
        next_spec.pop("forward_to", None)
        next_spec["type"] = new_type

    wanted_ports = _bound_ports(next_spec)
    previous_ports = _bound_ports(tunnel.spec)
    check_ports = wanted_ports if new_core != tunnel.core else (wanted_ports - previous_ports)
    if check_ports:
        others = (await db.execute(select(Tunnel).where(Tunnel.id != tunnel.id))).scalars().all()
        iran_id = tunnel.iran_node_id or tunnel.node_id
        for other in others:
            other_iran = other.iran_node_id or other.node_id
            if iran_id and other_iran and other_iran != iran_id:
                continue
            overlap = check_ports & _bound_ports(other.spec or {})
            if overlap:
                ports_txt = ", ".join(str(p) for p in sorted(overlap))
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Port {ports_txt} is already used by tunnel \"{other.name or other.id}\" "
                        f"({other.core}, status {other.status}). "
                        "Choose a different port or edit that tunnel."
                    ),
                )

    core_changed = new_core != tunnel.core
    type_changed = new_type != tunnel.type
    spec_changed = next_spec != (tunnel.spec or {})

    if tunnel_update.name is not None:
        tunnel.name = tunnel_update.name
    tunnel.core = new_core
    tunnel.type = new_type
    tunnel.spec = next_spec
    _ensure_edit_secrets(tunnel)

    tunnel.revision += 1
    tunnel.updated_at = datetime.utcnow()

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(tunnel, "spec")
    await db.commit()
    await db.refresh(tunnel)

    if spec_changed or type_changed or core_changed:
        await _stop_panel_helpers(tunnel, request)
        if core_changed:
            await _remove_tunnel_from_nodes(tunnel, db)
        try:
            await apply_tunnel(tunnel.id, request, db)
        except HTTPException as exc:
            await db.refresh(tunnel)
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            if tunnel.status != "error":
                tunnel.status = "error"
                tunnel.error_message = detail
                await db.commit()
            raise HTTPException(status_code=exc.status_code, detail=detail)
        except Exception as exc:
            logger.error("Failed to re-apply tunnel %s: %s", tunnel.id, exc, exc_info=True)
            tunnel.status = "error"
            tunnel.error_message = f"Re-apply error: {exc}"
            await db.commit()
            await db.refresh(tunnel)
            raise HTTPException(status_code=500, detail=tunnel.error_message)
        await db.refresh(tunnel)

    return tunnel


@router.post("/{tunnel_id}/apply")
async def apply_tunnel(tunnel_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Apply tunnel configuration to node(s) - handles both single-node and reverse tunnels"""
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    client = NodeClient()
    
    is_reverse_tunnel = tunnel.core in {"rathole", "backhaul", "chisel", "frp", "wstunnel", "bore"}
    foreign_node = None
    iran_node = None
    
    if is_reverse_tunnel:
        iran_node_id = tunnel.node_id
        result = await db.execute(select(Node).where(Node.id == iran_node_id))
        iran_node = result.scalar_one_or_none()
        if not iran_node:
            raise HTTPException(status_code=404, detail=f"Iran node {iran_node_id} not found")
        
        result = await db.execute(select(Node))
        all_nodes = result.scalars().all()
        foreign_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "foreign"]
        if not foreign_nodes:
            raise HTTPException(status_code=404, detail="No foreign node found. Please ensure at least one node has role='foreign' (set NODE_ROLE=foreign on the foreign node).")
        foreign_node = foreign_nodes[0]
        
        if iran_node.node_metadata.get("role") != "iran":
            raise HTTPException(status_code=400, detail=f"Node {iran_node.id} is not an iran node (role={iran_node.node_metadata.get('role')}). Set NODE_ROLE=iran on the Iran node.")
        if foreign_node.node_metadata.get("role") != "foreign":
            raise HTTPException(status_code=400, detail=f"Node {foreign_node.id} is not a foreign node (role={foreign_node.node_metadata.get('role')}). Set NODE_ROLE=foreign on the foreign node.")
        
        if foreign_node and iran_node:
            try:
                spec = tunnel.spec.copy() if tunnel.spec else {}
                
                if tunnel.core == "backhaul":
                    transport = spec.get("transport", "tcp")
                    control_port = spec.get("control_port") or spec.get("public_port") or spec.get("listen_port") or 3080
                    public_port = spec.get("public_port") or spec.get("listen_port") or control_port
                    target_host = spec.get("target_host", "127.0.0.1")
                    token = spec.get("token")
                    
                    server_spec = spec.copy()
                    server_spec["bind_addr"] = f"0.0.0.0:{control_port}"
                    server_spec["control_port"] = control_port
                    server_spec["public_port"] = public_port
                    server_spec["listen_port"] = public_port
                    
                    # IMPORTANT: Read ports from spec (which is tunnel.spec.copy()) first
                    ports = spec.get("ports", [])
                    if not ports:
                        ports = tunnel.spec.get("ports", [])
                    if ports:
                        server_spec["ports"] = ports
                    logger.info(f"Backhaul tunnel update {tunnel.id}: received ports from spec: {spec.get('ports')}, from tunnel.spec: {tunnel.spec.get('ports')}, final: {ports} (type: {type(ports)}, length: {len(ports) if isinstance(ports, list) else 'N/A'})")
                    
                    if not ports or (isinstance(ports, list) and len(ports) == 0):
                        target_port = spec.get("target_port") or public_port
                        if target_port:
                            target_addr = f"{target_host}:{target_port}"
                            ports = [f"{public_port}={target_addr}"]
                        else:
                            ports = [str(public_port)]
                    else:
                        if isinstance(ports, list) and ports:
                            processed_ports = []
                            for p in ports:
                                if not p:
                                    continue
                                if isinstance(p, str):
                                    if '=' in p:
                                        processed_ports.append(p)
                                    elif p.isdigit():
                                        processed_ports.append(f"{p}={target_host}:{p}")
                                    else:
                                        processed_ports.append(p)
                                elif isinstance(p, int):
                                    processed_ports.append(f"{p}={target_host}:{p}")
                                elif isinstance(p, dict):
                                    local = p.get("local") or p.get("listen_port") or p.get("public_port")
                                    tgt_host = p.get("target_host") or target_host
                                    tgt_port = p.get("target_port") or p.get("remote_port") or local
                                    if local:
                                        processed_ports.append(f"{local}={tgt_host}:{tgt_port}")
                                else:
                                    processed_ports.append(str(p))
                            ports = processed_ports
                    
                    logger.info(f"Backhaul tunnel update {tunnel.id}: processed ports: {ports} (count: {len(ports)})")
                    server_spec["ports"] = ports
                    server_spec["mode"] = "server"  # Ensure mode is set
                    if token:
                        server_spec["token"] = token
                    
                    # CRITICAL: Update the database spec with processed ports so they're preserved
                    if "ports" not in tunnel.spec:
                        tunnel.spec["ports"] = []
                    tunnel.spec["ports"] = ports.copy() if isinstance(ports, list) else ports
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(tunnel, "spec")
                    await db.commit()
                    await db.refresh(tunnel)
                    logger.info(f"Backhaul tunnel update {tunnel.id}: saved ports to database: {tunnel.spec.get('ports')} (count: {len(tunnel.spec.get('ports', []))})")
                    
                    client_spec = spec.copy()
                    if not iran_node.node_metadata.get("ip_address"):
                        tunnel.status = "error"
                        tunnel.error_message = "Iran node has no IP address"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Iran node has no IP address")
                    iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                    
                    transport_lower = transport.lower()
                    if transport_lower in ("ws", "wsmux"):
                        use_tls = bool(server_spec.get("tls_cert") or server_spec.get("server_options", {}).get("tls_cert"))
                        protocol = "wss://" if use_tls else "ws://"
                        client_spec["remote_addr"] = f"{protocol}{iran_node_ip}:{control_port}"
                    else:
                        client_spec["remote_addr"] = f"{iran_node_ip}:{control_port}"
                    client_spec["transport"] = transport
                    client_spec["type"] = transport
                    client_spec["mode"] = "client"  # Ensure mode is set
                    if token:
                        client_spec["token"] = token
                
                if tunnel.core == "frp":
                    bind_port = spec.get("bind_port")
                    if not bind_port:
                        import hashlib
                        port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                        bind_port = 7000 + (port_hash % 1000)
                    
                    token = spec.get("token")
                    if not token:
                        from app.utils import generate_token
                        token = generate_token()
                        spec["token"] = token
                        tunnel.spec["token"] = token
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(tunnel, "spec")
                        await db.commit()
                        await db.refresh(tunnel)
                    
                    if not iran_node.node_metadata.get("ip_address"):
                        tunnel.status = "error"
                        tunnel.error_message = "Iran node has no IP address"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Iran node has no IP address")
                    iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                    
                    server_spec = spec.copy()
                    server_spec["mode"] = "server"
                    server_spec["bind_port"] = bind_port
                    server_spec["token"] = token
                    
                    client_spec = spec.copy()
                    client_spec["mode"] = "client"
                    client_spec["server_addr"] = iran_node_ip
                    client_spec["server_port"] = bind_port
                    client_spec["token"] = token
                    tunnel_type = tunnel.type.lower() if tunnel.type else "tcp"
                    if tunnel_type not in ["tcp", "udp"]:
                        tunnel_type = "tcp"
                    client_spec["type"] = tunnel_type
                    
                    ports = spec.get("ports", [])
                    if not ports:
                        local_port = spec.get("local_port")
                        remote_port = spec.get("remote_port") or spec.get("listen_port")
                        if remote_port and local_port:
                            client_spec["ports"] = [{"local": int(local_port), "remote": int(remote_port)}]
                        elif remote_port:
                            client_spec["ports"] = [{"local": int(remote_port), "remote": int(remote_port)}]
                        elif local_port:
                            client_spec["ports"] = [{"local": int(local_port), "remote": int(local_port)}]
                    else:
                        client_spec["ports"] = ports
                
                elif tunnel.core == "rathole":
                    transport = spec.get("transport") or spec.get("type") or "tcp"
                    proxy_port = spec.get("remote_port") or spec.get("listen_port")
                    token = spec.get("token")
                    
                    if not proxy_port or not token:
                        tunnel.status = "error"
                        tunnel.error_message = "Missing required fields: remote_port/listen_port or token"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Missing required fields: remote_port/listen_port or token")
                    
                    from app.utils import parse_address_port
                    remote_addr = spec.get("remote_addr", "0.0.0.0:23333")
                    _, control_port, _ = parse_address_port(remote_addr)
                    if not control_port:
                        import hashlib
                        port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                        control_port = 23333 + (port_hash % 1000)
                    
                    server_spec = spec.copy()
                    server_spec["mode"] = "server"
                    server_spec["bind_addr"] = f"0.0.0.0:{control_port}"
                    server_spec["proxy_port"] = proxy_port
                    server_spec["transport"] = transport
                    server_spec["token"] = token
                    
                    if not iran_node.node_metadata.get("ip_address"):
                        tunnel.status = "error"
                        tunnel.error_message = "Iran node has no IP address"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Iran node has no IP address")
                    iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                    
                    client_spec = spec.copy()
                    client_spec["mode"] = "client"
                    transport_lower = transport.lower()
                    if transport_lower in ("websocket", "ws"):
                        use_tls = bool(spec.get("websocket_tls") or spec.get("tls"))
                        protocol = "wss://" if use_tls else "ws://"
                        client_spec["remote_addr"] = f"{protocol}{iran_node_ip}:{control_port}"
                    else:
                        client_spec["remote_addr"] = f"{iran_node_ip}:{control_port}"
                    client_spec["transport"] = transport
                    client_spec["token"] = token
                
                elif tunnel.core == "chisel":
                    listen_port = spec.get("listen_port") or spec.get("remote_port")
                    if not listen_port:
                        tunnel.status = "error"
                        tunnel.error_message = "Missing required field: listen_port or remote_port"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Missing required field: listen_port or remote_port")
                    
                    import hashlib
                    port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                    server_control_port = spec.get("control_port") or (int(listen_port) + 10000 + (port_hash % 1000))
                    
                    from app.utils import normalize_chisel_auth, generate_chisel_auth, is_valid_ipv6_address
                    auth = normalize_chisel_auth(spec.get("auth")) or generate_chisel_auth()
                    if tunnel.spec.get("auth") != auth:
                        tunnel.spec["auth"] = auth
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(tunnel, "spec")
                    
                    server_spec = spec.copy()
                    server_spec["mode"] = "server"
                    server_spec["server_port"] = server_control_port
                    server_spec["reverse_port"] = listen_port
                    server_spec["auth"] = auth
                    
                    if not iran_node.node_metadata.get("ip_address"):
                        tunnel.status = "error"
                        tunnel.error_message = "Iran node has no IP address"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Iran node has no IP address")
                    iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)
                    
                    client_spec = spec.copy()
                    client_spec["mode"] = "client"
                    if is_valid_ipv6_address(iran_node_ip):
                        client_spec["server_url"] = f"http://[{iran_node_ip}]:{server_control_port}"
                    else:
                        client_spec["server_url"] = f"http://{iran_node_ip}:{server_control_port}"
                    client_spec["reverse_port"] = listen_port
                    client_spec["auth"] = auth
                
                elif tunnel.core == "wstunnel":
                    ports = parse_ports_from_spec(spec)
                    listen_port = spec.get("listen_port") or spec.get("remote_port")
                    if not ports and listen_port:
                        ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]
                    if not ports:
                        tunnel.status = "error"
                        tunnel.error_message = "Missing required field: ports or listen_port/remote_port"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Missing required field: ports or listen_port/remote_port")

                    import hashlib
                    port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                    first_port = int(ports[0]) if isinstance(ports[0], (int, str)) and str(ports[0]).isdigit() else ports[0]
                    server_control_port = spec.get("control_port") or (int(first_port) + 10000 + (port_hash % 1000))

                    from app.utils import generate_wstunnel_secret, build_wstunnel_server_url
                    secret = (spec.get("secret") or "").strip() or generate_wstunnel_secret()
                    if tunnel.spec.get("secret") != secret:
                        tunnel.spec["secret"] = secret
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(tunnel, "spec")

                    tunnel_type = (tunnel.type or "tcp").lower()
                    if tunnel_type not in ("tcp", "udp"):
                        tunnel_type = "tcp"
                    use_tls = bool(spec.get("use_tls", False))

                    server_spec = spec.copy()
                    server_spec["mode"] = "server"
                    server_spec["server_port"] = int(server_control_port)
                    server_spec["control_port"] = int(server_control_port)
                    server_spec["reverse_port"] = first_port
                    server_spec["ports"] = ports
                    server_spec["secret"] = secret
                    server_spec["type"] = tunnel_type

                    if not iran_node.node_metadata.get("ip_address"):
                        tunnel.status = "error"
                        tunnel.error_message = "Iran node has no IP address"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Iran node has no IP address")
                    iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)

                    client_spec = spec.copy()
                    client_spec["mode"] = "client"
                    client_spec["server_url"] = build_wstunnel_server_url(
                        iran_node_ip, int(server_control_port), secret, use_tls=use_tls
                    )
                    client_spec["ports"] = ports
                    client_spec["secret"] = secret
                    client_spec["type"] = tunnel_type
                    client_spec["local_addr"] = spec.get("local_addr") or f"127.0.0.1:{first_port}"

                elif tunnel.core == "bore":
                    ports = parse_ports_from_spec(spec)
                    listen_port = spec.get("listen_port") or spec.get("remote_port")
                    if not ports and listen_port:
                        ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]
                    if not ports:
                        tunnel.status = "error"
                        tunnel.error_message = "Missing required field: ports or listen_port/remote_port"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Missing required field: ports or listen_port/remote_port")

                    if not iran_node.node_metadata.get("ip_address"):
                        tunnel.status = "error"
                        tunnel.error_message = "Iran node has no IP address"
                        await db.commit()
                        raise HTTPException(status_code=400, detail="Iran node has no IP address")
                    iran_node_ip = iran_host_for_foreign_client(iran_node, foreign_node)

                    from app.utils import BORE_CONTROL_PORT, generate_bore_secret
                    from sqlalchemy.orm.attributes import flag_modified
                    secret = (spec.get("secret") or "").strip()
                    if not secret:
                        existing_q = await db.execute(
                            select(Tunnel).where(
                                Tunnel.core == "bore",
                                Tunnel.status == "active",
                                Tunnel.id != tunnel.id,
                            )
                        )
                        for existing in existing_q.scalars().all():
                            same_iran = (existing.iran_node_id or existing.node_id) == iran_node.id
                            if same_iran and existing.spec and existing.spec.get("secret"):
                                secret = existing.spec["secret"]
                                break
                    if not secret:
                        secret = generate_bore_secret()
                    if tunnel.spec.get("secret") != secret:
                        tunnel.spec["secret"] = secret
                        tunnel.spec["control_port"] = BORE_CONTROL_PORT
                        flag_modified(tunnel, "spec")

                    server_spec = spec.copy()
                    server_spec["mode"] = "server"
                    server_spec["secret"] = secret
                    server_spec["control_port"] = BORE_CONTROL_PORT
                    server_spec["ports"] = ports
                    server_spec["type"] = "tcp"

                    client_spec = spec.copy()
                    client_spec["mode"] = "client"
                    client_spec["server_addr"] = iran_node_ip
                    client_spec["secret"] = secret
                    client_spec["control_port"] = BORE_CONTROL_PORT
                    client_spec["type"] = "tcp"
                    client_spec["local_host"] = spec.get("local_host") or "127.0.0.1"
                    client_spec["ports"] = [{"local": int(p), "remote": int(p)} for p in ports]
                
                if not iran_node.node_metadata.get("api_address"):
                    iran_node.node_metadata["api_address"] = f"http://{iran_node.node_metadata.get('ip_address', iran_node.fingerprint)}:{iran_node.node_metadata.get('api_port', 8888)}"
                    await db.commit()
                
                logger.info(f"Reapplying tunnel {tunnel.id}: applying server config to iran node {iran_node.id}")
                server_response = await client.send_to_node(
                    node_id=iran_node.id,
                    endpoint="/api/agent/tunnels/apply",
                    data={
                        "tunnel_id": tunnel.id,
                        "core": tunnel.core,
                        "type": tunnel.type,
                        "spec": server_spec if tunnel.core in ["backhaul", "frp", "rathole", "chisel", "wstunnel", "bore"] else spec
                    }
                )
                
                if server_response.get("status") == "error":
                    tunnel.status = "error"
                    error_msg = server_response.get("message", "Unknown error from iran node")
                    tunnel.error_message = f"Iran node error: {error_msg}"
                    await db.commit()
                    raise HTTPException(status_code=500, detail=error_msg)
                
                if not foreign_node.node_metadata.get("api_address"):
                    foreign_node.node_metadata["api_address"] = f"http://{foreign_node.node_metadata.get('ip_address', foreign_node.fingerprint)}:{foreign_node.node_metadata.get('api_port', 8888)}"
                    await db.commit()
                
                logger.info(f"Reapplying tunnel {tunnel.id}: applying client config to foreign node {foreign_node.id}")
                client_response = await client.send_to_node(
                    node_id=foreign_node.id,
                    endpoint="/api/agent/tunnels/apply",
                    data={
                        "tunnel_id": tunnel.id,
                        "core": tunnel.core,
                        "type": tunnel.type,
                        "spec": client_spec if tunnel.core in ["backhaul", "frp", "rathole", "chisel", "wstunnel", "bore"] else spec
                    }
                )
                
                if client_response.get("status") == "error":
                    tunnel.status = "error"
                    error_msg = client_response.get("message", "Unknown error from foreign node")
                    tunnel.error_message = f"Foreign node error: {error_msg}"
                    await db.commit()
                    raise HTTPException(status_code=500, detail=error_msg)
                
                if server_response.get("status") == "success" and client_response.get("status") == "success":
                    tunnel.status = "active"
                    tunnel.error_message = None
                    await db.commit()
                    return {"status": "applied", "message": "Tunnel reapplied successfully to both nodes"}
                else:
                    tunnel.status = "error"
                    tunnel.error_message = "Failed to apply tunnel to one or both nodes"
                    await db.commit()
                    raise HTTPException(status_code=500, detail="Failed to apply tunnel to one or both nodes")
            except HTTPException:
                raise
            except Exception as e:
                tunnel.status = "error"
                tunnel.error_message = f"Error: {str(e)}"
                await db.commit()
                raise HTTPException(status_code=500, detail=f"Failed to reapply tunnel: {str(e)}")

    # GOST: Iran node forwards to foreign (prefer GRE inner IP + MSS clamp on node).
    if tunnel.core == "gost":
        from app.utils import resolve_gost_forward_target
        from sqlalchemy.orm.attributes import flag_modified

        iran_id = tunnel.iran_node_id or tunnel.node_id
        result = await db.execute(select(Node).where(Node.id == iran_id))
        iran_node = result.scalar_one_or_none()
        if not iran_node:
            raise HTTPException(status_code=404, detail="Iran node not found for GOST tunnel")

        foreign_node = None
        if tunnel.foreign_node_id:
            result = await db.execute(select(Node).where(Node.id == tunnel.foreign_node_id))
            foreign_node = result.scalar_one_or_none()
        if not foreign_node:
            result = await db.execute(select(Node))
            foreign_nodes = [n for n in result.scalars().all() if n.node_metadata and n.node_metadata.get("role") == "foreign"]
            if foreign_nodes:
                foreign_node = foreign_nodes[0]
                tunnel.foreign_node_id = foreign_node.id

        if not foreign_node:
            raise HTTPException(status_code=404, detail="Foreign node not found for GOST tunnel")

        tunnel.iran_node_id = iran_node.id
        if not tunnel.node_id:
            tunnel.node_id = iran_node.id

        foreign_ip = (foreign_node.node_metadata or {}).get("ip_address")
        if not foreign_ip and not (tunnel.spec or {}).get("remote_ip"):
            raise HTTPException(status_code=400, detail="Foreign node has no IP address")

        target = resolve_gost_forward_target(
            foreign_public_ip=foreign_ip,
            iran_gre_peers=(iran_node.node_metadata or {}).get("gre_peers") or [],
            foreign_gre_peers=(foreign_node.node_metadata or {}).get("gre_peers") or [],
            iran_public_ip=(iran_node.node_metadata or {}).get("ip_address"),
            explicit_remote_ip=(tunnel.spec or {}).get("remote_ip"),
        )
        spec_for_node = (tunnel.spec or {}).copy()
        spec_for_node["type"] = tunnel.type
        spec_for_node["remote_ip"] = target["remote_ip"]
        spec_for_node["public_ip"] = target.get("public_ip") or foreign_ip
        spec_for_node["forward_via"] = target.get("via")
        spec_for_node["forward_mtu"] = target.get("mtu")
        spec_for_node["tcp_mss"] = target.get("mss")
        if target.get("iface"):
            spec_for_node["forward_iface"] = target["iface"]

        tunnel.spec = {**(tunnel.spec or {}), **{
            "remote_ip": target["remote_ip"],
            "public_ip": target.get("public_ip") or foreign_ip,
            "forward_via": target.get("via"),
            "forward_mtu": target.get("mtu"),
            "tcp_mss": target.get("mss"),
        }}
        flag_modified(tunnel, "spec")
        await db.commit()

        if not iran_node.node_metadata.get("api_address"):
            iran_node.node_metadata["api_address"] = (
                f"http://{iran_node.node_metadata.get('ip_address', iran_node.fingerprint)}:"
                f"{iran_node.node_metadata.get('api_port', 8888)}"
            )
            await db.commit()

        response = await client.send_to_node(
            node_id=iran_node.id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": "gost",
                "type": tunnel.type,
                "spec": spec_for_node,
            },
        )
        if response.get("status") == "success":
            tunnel.status = "active"
            tunnel.error_message = None
            await db.commit()
            return {
                "status": "applied",
                "message": f"GOST reapplied via {target.get('via')} to {target['remote_ip']}",
            }
        tunnel.status = "error"
        tunnel.error_message = response.get("message", "Failed to apply GOST tunnel")
        await db.commit()
        raise HTTPException(status_code=500, detail=tunnel.error_message)
    
    result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    try:
        if not node.node_metadata.get("api_address"):
            node.node_metadata["api_address"] = f"http://{node.fingerprint}:8888"
            await db.commit()
        
        spec_for_node = tunnel.spec.copy() if tunnel.spec else {}
        logger.info(f"Reapplying tunnel {tunnel.id} (core={tunnel.core}, type={tunnel.type}): original spec={spec_for_node}")
        
        if tunnel.core == "frp":
            try:
                spec_for_node = prepare_frp_spec_for_node(spec_for_node, node, request)
                logger.info(f"FRP spec prepared for tunnel {tunnel.id}: server_addr={spec_for_node.get('server_addr')}, server_port={spec_for_node.get('server_port')}, full spec={spec_for_node}")
            except Exception as e:
                error_msg = f"Failed to prepare FRP spec: {str(e)}"
                logger.error(f"Tunnel {tunnel.id}: {error_msg}", exc_info=True)
                raise HTTPException(status_code=500, detail=error_msg)
        
        logger.info(f"Sending tunnel {tunnel.id} to node {node.id}: spec={spec_for_node}")
        response = await client.send_to_node(
            node_id=node.id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": tunnel.core,
                "type": tunnel.type,
                "spec": spec_for_node
            }
        )
        
        if response.get("status") == "success":
            tunnel.status = "active"
            tunnel.error_message = None
            await db.commit()
            return {"status": "applied", "message": "Tunnel reapplied successfully"}
        else:
            error_msg = response.get("message", "Failed to apply tunnel")
            tunnel.status = "error"
            tunnel.error_message = error_msg
            await db.commit()
            raise HTTPException(status_code=500, detail=error_msg)
    except HTTPException:
        raise
    except Exception as e:
        tunnel.status = "error"
        tunnel.error_message = f"Error: {str(e)}"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to apply tunnel: {str(e)}")


@router.post("/reapply-all")
async def reapply_all_tunnels(request: Request, db: AsyncSession = Depends(get_db)):
    """Reapply all tunnels"""
    result = await db.execute(select(Tunnel))
    tunnels = result.scalars().all()
    
    if not tunnels:
        return {"status": "success", "message": "No tunnels to reapply", "applied": 0, "failed": 0}
    
    applied = 0
    failed = 0
    errors = []
    
    # Call apply_tunnel for each tunnel
    for tunnel in tunnels:
        try:
            # Call apply_tunnel directly - it's in the same module
            try:
                result_data = await apply_tunnel(tunnel.id, request, db)
                if result_data and result_data.get("status") == "applied":
                    applied += 1
                else:
                    failed += 1
                    errors.append(f"Tunnel {tunnel.name}: Failed to apply")
            except HTTPException as e:
                failed += 1
                errors.append(f"Tunnel {tunnel.name}: {e.detail}")
            except Exception as e:
                failed += 1
                error_msg = str(e)
                errors.append(f"Tunnel {tunnel.name}: {error_msg}")
        except Exception as e:
            logger.error(f"Error reapplying tunnel {tunnel.id}: {e}", exc_info=True)
            failed += 1
            errors.append(f"Tunnel {tunnel.name}: {str(e)}")
    
    return {
        "status": "success",
        "message": f"Reapplied {applied} tunnels, {failed} failed",
        "applied": applied,
        "failed": failed,
        "errors": errors[:10]  # Limit errors to first 10
    }


@router.delete("/{tunnel_id}")
async def delete_tunnel(tunnel_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Delete a tunnel"""
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    needs_gost_forwarding = tunnel.type in ["tcp", "udp", "ws", "grpc"] and tunnel.core == "gost"
    needs_rathole_server = tunnel.core == "rathole"
    needs_backhaul_server = tunnel.core == "backhaul"
    needs_chisel_server = tunnel.core == "chisel"
    needs_frp_server = tunnel.core == "frp"
    
    if needs_gost_forwarding:
        if hasattr(request.app.state, 'gost_forwarder'):
            try:
                request.app.state.gost_forwarder.stop_forward(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop gost forwarding: {e}")
    
    elif needs_rathole_server:
        if hasattr(request.app.state, 'rathole_server_manager'):
            try:
                request.app.state.rathole_server_manager.stop_server(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop Rathole server: {e}")
    elif needs_backhaul_server:
        if hasattr(request.app.state, "backhaul_manager"):
            try:
                request.app.state.backhaul_manager.stop_server(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop Backhaul server: {e}")
    elif needs_chisel_server:
        if hasattr(request.app.state, 'chisel_server_manager'):
            try:
                request.app.state.chisel_server_manager.stop_server(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop Chisel server: {e}")
    elif needs_frp_server:
        if hasattr(request.app.state, 'frp_server_manager'):
            try:
                request.app.state.frp_server_manager.stop_server(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop FRP server: {e}")
    
    if tunnel.status == "active":
        result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
        node = result.scalar_one_or_none()
        if node:
            client = NodeClient()
            try:
                await client.send_to_node(
                    node_id=node.id,
                    endpoint="/api/agent/tunnels/remove",
                    data={"tunnel_id": tunnel.id}
                )
            except:
                pass
    
    await db.delete(tunnel)
    await db.commit()
    return {"status": "deleted"}


