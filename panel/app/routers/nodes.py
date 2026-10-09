"""Nodes API endpoints"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from datetime import datetime
from pydantic import BaseModel
import httpx
import logging

from app.database import get_db
from app.models import Node, Settings
from app.node_client import NodeClient

logger = logging.getLogger(__name__)

router = APIRouter()


class NodeCreate(BaseModel):
    name: str
    ip_address: str
    api_port: int = 8888
    metadata: dict = {}


class NodeResponse(BaseModel):
    id: str
    name: str
    fingerprint: str
    status: str
    registered_at: datetime
    last_seen: datetime
    metadata: dict
    
    class Config:
        from_attributes = True
    


@router.post("", response_model=NodeResponse)
async def create_node(node: NodeCreate, db: AsyncSession = Depends(get_db)):
    """Register a new node"""
    import hashlib
    
    fingerprint_data = f"{node.ip_address}:{node.api_port}".encode()
    fingerprint = hashlib.sha256(fingerprint_data).hexdigest()[:16]
    
    result = await db.execute(select(Node).where(Node.fingerprint == fingerprint))
    existing = result.scalar_one_or_none()
    
    metadata = node.metadata.copy() if node.metadata else {}
    metadata["api_address"] = f"http://{node.ip_address}:{node.api_port}"
    metadata["ip_address"] = node.ip_address
    metadata["api_port"] = node.api_port
    
    incoming_role = node.metadata.get("role", "iran") if node.metadata else "iran"
    if incoming_role not in ["iran", "foreign"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid role '{incoming_role}'. Role must be either 'iran' or 'foreign'"
        )
    metadata["role"] = incoming_role
    
    if existing:
        existing_role = existing.node_metadata.get("role", "iran") if existing.node_metadata else "iran"
        if existing_role != incoming_role:
            raise HTTPException(
                status_code=409,
                detail=f"Node with this fingerprint already exists with role '{existing_role}'. "
                       f"Cannot register as '{incoming_role}'. "
                       f"Each node must have a consistent role."
            )
        
        existing.last_seen = datetime.utcnow()
        existing.status = "active"
        # Replace JSON blob so SQLAlchemy persists nested updates (gre_peers, etc.).
        from sqlalchemy.orm.attributes import flag_modified
        merged = dict(existing.node_metadata or {})
        merged.update(metadata)
        merged["role"] = existing_role
        existing.node_metadata = merged
        flag_modified(existing, "node_metadata")
        await db.commit()
        await db.refresh(existing)
        
        response_metadata = existing.node_metadata.copy() if existing.node_metadata else {}
        
        result = await db.execute(select(Settings).where(Settings.key == "frp"))
        frp_setting = result.scalar_one_or_none()
        if frp_setting and frp_setting.value and frp_setting.value.get("enabled"):
            panel_host = node.metadata.get("panel_address", "").split(":")[0] if node.metadata else ""
            if not panel_host or panel_host == "panel.example.com":
                import socket
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    panel_host = s.getsockname()[0]
                    s.close()
                except:
                    panel_host = "127.0.0.1"
            
            response_metadata["frp_config"] = {
                "enabled": True,
                "server_addr": panel_host,
                "server_port": frp_setting.value.get("port", 7000),
                "token": frp_setting.value.get("token")
            }
        
        return NodeResponse(
            id=existing.id,
            name=existing.name,
            fingerprint=existing.fingerprint,
            status=existing.status,
            registered_at=existing.registered_at,
            last_seen=existing.last_seen,
            metadata=response_metadata
        )
    
    db_node = Node(
        name=node.name,
        fingerprint=fingerprint,
        status="active",
        node_metadata=metadata
    )
    db.add(db_node)
    await db.commit()
    await db.refresh(db_node)
    
    response_metadata = db_node.node_metadata.copy() if db_node.node_metadata else {}
    
    result = await db.execute(select(Settings).where(Settings.key == "frp"))
    frp_setting = result.scalar_one_or_none()
    if frp_setting and frp_setting.value and frp_setting.value.get("enabled"):
        panel_host = node.metadata.get("panel_address", "").split(":")[0] if node.metadata else ""
        if not panel_host or panel_host == "panel.example.com":
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                panel_host = s.getsockname()[0]
                s.close()
            except:
                panel_host = "127.0.0.1"
        
        response_metadata["frp_config"] = {
            "enabled": True,
            "server_addr": panel_host,
            "server_port": frp_setting.value.get("port", 7000),
            "token": frp_setting.value.get("token")
        }
    
    return NodeResponse(
        id=db_node.id,
        name=db_node.name,
        fingerprint=db_node.fingerprint,
        status=db_node.status,
        registered_at=db_node.registered_at,
        last_seen=db_node.last_seen,
        metadata=response_metadata
    )


@router.get("", response_model=List[NodeResponse])
async def list_nodes(db: AsyncSession = Depends(get_db)):
    """List all nodes with connection state"""
    import asyncio
    result = await db.execute(select(Node))
    nodes = result.scalars().all()
    
    client = NodeClient()
    node_responses = []
    
    async def check_node_status(node):
        connection_status = "failed"
        try:
            response = await client.get_tunnel_status(node.id, "")
            if response and response.get("status") == "ok":
                connection_status = "connected"
            else:
                error_msg = response.get("message", "Node disconnected") if response else "Node not responding"
                if "timeout" in error_msg.lower() or "connection" in error_msg.lower():
                    if node.node_metadata and node.node_metadata.get("frp_connected"):
                        connection_status = "connected"
                    else:
                        connection_status = "reconnecting"
                else:
                    connection_status = "failed"
        except httpx.ConnectError:
            if node.node_metadata and node.node_metadata.get("frp_connected"):
                connection_status = "connected"
            else:
                connection_status = "connecting"
        except httpx.TimeoutException:
            if node.node_metadata and node.node_metadata.get("frp_connected"):
                connection_status = "connected"
            else:
                connection_status = "reconnecting"
        except Exception:
            if node.node_metadata and node.node_metadata.get("frp_connected"):
                connection_status = "connected"
            else:
                connection_status = "failed"
        
        metadata = node.node_metadata.copy() if node.node_metadata else {}
        metadata["connection_status"] = connection_status
        
        return NodeResponse(
            id=node.id,
            name=node.name,
            fingerprint=node.fingerprint,
            status=node.status,
            registered_at=node.registered_at,
            last_seen=node.last_seen,
            metadata=metadata
        )
    
    tasks = [check_node_status(node) for node in nodes]
    node_responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    results = []
    for i, response in enumerate(node_responses):
        if isinstance(response, Exception):
            node = nodes[i]
            metadata = node.node_metadata.copy() if node.node_metadata else {}
            metadata["connection_status"] = "failed"
            results.append(NodeResponse(
                id=node.id,
                name=node.name,
                fingerprint=node.fingerprint,
                status=node.status,
                registered_at=node.registered_at,
                last_seen=node.last_seen,
                metadata=metadata
            ))
        else:
            results.append(response)
    
    return results


class GreSetupRequest(BaseModel):
    iran_node_id: str | None = None
    foreign_node_id: str | None = None
    network: str = "172.17.1.0/30"
    mtu: int = 1472
    iface: str = "smite-gre"


@router.post("/gre/setup")
async def setup_gre_between_nodes(body: GreSetupRequest | None = None, db: AsyncSession = Depends(get_db)):
    """
    Create GRE on both Iran and foreign nodes (auto underlay NIC/IP on each side).
    Panel then receives gre_peers on the next node re-register and uses them for tunnels.
    """
    import ipaddress
    from sqlalchemy.orm.attributes import flag_modified

    body = body or GreSetupRequest()
    result = await db.execute(select(Node))
    all_nodes = result.scalars().all()
    iran = None
    foreign = None
    if body.iran_node_id:
        iran = next((n for n in all_nodes if n.id == body.iran_node_id), None)
    if body.foreign_node_id:
        foreign = next((n for n in all_nodes if n.id == body.foreign_node_id), None)
    if not iran:
        iran = next(
            (n for n in all_nodes if (n.node_metadata or {}).get("role") == "iran"),
            None,
        )
    if not foreign:
        foreign = next(
            (n for n in all_nodes if (n.node_metadata or {}).get("role") == "foreign"),
            None,
        )
    if not iran or not foreign:
        raise HTTPException(
            status_code=400,
            detail="Need one Iran node and one foreign node registered before GRE setup",
        )

    iran_ip = (iran.node_metadata or {}).get("ip_address")
    foreign_ip = (foreign.node_metadata or {}).get("ip_address")
    if not iran_ip or not foreign_ip:
        raise HTTPException(status_code=400, detail="Both nodes must have public ip_address in metadata")

    net = ipaddress.ip_network(body.network, strict=False)
    hosts = [str(h) for h in net.hosts()]
    if len(hosts) < 2:
        raise HTTPException(status_code=400, detail=f"Invalid GRE network {body.network}")
    foreign_inner, iran_inner = hosts[0], hosts[1]

    client = NodeClient()
    iran_payload = {
        "remote_public_ip": foreign_ip,
        "role": "iran",
        "local_public_ip": iran_ip,
        "local_inner": iran_inner,
        "peer_inner": foreign_inner,
        "network": str(net),
        "iface": body.iface,
        "mtu": body.mtu,
    }
    foreign_payload = {
        "remote_public_ip": iran_ip,
        "role": "foreign",
        "local_public_ip": foreign_ip,
        "local_inner": foreign_inner,
        "peer_inner": iran_inner,
        "network": str(net),
        "iface": body.iface,
        "mtu": body.mtu,
    }

    iran_resp = await client.send_to_node(iran.id, "/api/agent/network/gre", iran_payload)
    foreign_resp = await client.send_to_node(foreign.id, "/api/agent/network/gre", foreign_payload)

    # Persist gre_peers hints immediately (nodes will refresh on re-register too).
    for node, peer_public, local_inner, peer_inner, resp in (
        (iran, foreign_ip, iran_inner, foreign_inner, iran_resp),
        (foreign, iran_ip, foreign_inner, iran_inner, foreign_resp),
    ):
        md = dict(node.node_metadata or {})
        gre = (resp or {}).get("gre") or {}
        md["gre_peers"] = [
            {
                "iface": gre.get("iface") or body.iface,
                "local": gre.get("local_public") or md.get("ip_address"),
                "remote": peer_public,
                "local_inner": gre.get("local_inner") or local_inner,
                "peer_inner": gre.get("peer_inner") or peer_inner,
                "mtu": gre.get("mtu") or body.mtu,
            }
        ]
        node.node_metadata = md
        flag_modified(node, "node_metadata")
    await db.commit()

    ok = (iran_resp or {}).get("status") == "ok" and (foreign_resp or {}).get("status") == "ok"
    return {
        "status": "ok" if ok else "partial",
        "network": str(net),
        "iran": {"node_id": iran.id, "inner": iran_inner, "response": iran_resp},
        "foreign": {"node_id": foreign.id, "inner": foreign_inner, "response": foreign_resp},
        "message": "GRE configured; tunnels will prefer GRE automatically",
    }


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str, db: AsyncSession = Depends(get_db)):
    """Get node by ID"""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return NodeResponse(
        id=node.id,
        name=node.name,
        fingerprint=node.fingerprint,
        status=node.status,
        registered_at=node.registered_at,
        last_seen=node.last_seen,
        metadata=node.node_metadata or {}
    )


@router.put("/{node_id}/frp-status")
async def update_frp_status(node_id: str, frp_status: dict, db: AsyncSession = Depends(get_db)):
    """Update node FRP connection status"""
    from sqlalchemy.orm.attributes import flag_modified
    
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    if not node.node_metadata:
        node.node_metadata = {}
    
    if frp_status.get("connected") and frp_status.get("remote_port"):
        node.node_metadata["frp_remote_port"] = frp_status.get("remote_port")
        node.node_metadata["frp_connected"] = True
        logger.info(f"[FRP] Node {node_id} FRP status updated: remote_port={frp_status.get('remote_port')}")
    else:
        node.node_metadata["frp_connected"] = False
        node.node_metadata.pop("frp_remote_port", None)
        logger.info(f"[FRP] Node {node_id} FRP status cleared")
    
    # Mark JSON column as modified so SQLAlchemy detects the change
    flag_modified(node, "node_metadata")
    
    await db.commit()
    await db.refresh(node)
    return {"status": "success"}


@router.delete("/{node_id}")
async def delete_node(node_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a node"""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    await db.delete(node)
    await db.commit()
    return {"status": "deleted"}

