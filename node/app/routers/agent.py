"""Agent API endpoints"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
import logging

router = APIRouter()
logger = logging.getLogger(__name__)



class TunnelApply(BaseModel):
    tunnel_id: str
    core: str
    type: str
    spec: Dict[str, Any]


class TunnelRemove(BaseModel):
    tunnel_id: str


class GreEnsure(BaseModel):
    remote_public_ip: str
    role: str = "iran"
    local_public_ip: Optional[str] = None
    local_inner: Optional[str] = None
    peer_inner: Optional[str] = None
    network: str = "172.17.1.0/30"
    iface: str = "smite-gre"
    mtu: int = 1472


@router.post("/tunnels/apply")
async def apply_tunnel(data: TunnelApply, request: Request):
    """Apply tunnel configuration"""
    logger = logging.getLogger(__name__)
    adapter_manager = request.app.state.adapter_manager
    
    logger.info(f"Applying tunnel {data.tunnel_id}: core={data.core}, type={data.type}")
    try:
        await adapter_manager.apply_tunnel(
            tunnel_id=data.tunnel_id,
            tunnel_core=data.core,
            spec=data.spec
        )
        logger.info(f"Tunnel {data.tunnel_id} applied successfully")
        return {"status": "success", "message": "Tunnel applied"}
    except Exception as e:
        logger.error(f"Failed to apply tunnel {data.tunnel_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tunnels/remove")
async def remove_tunnel(data: TunnelRemove, request: Request):
    """Remove tunnel"""
    adapter_manager = request.app.state.adapter_manager
    
    try:
        await adapter_manager.remove_tunnel(data.tunnel_id)
        return {"status": "success", "message": "Tunnel removed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tunnels/status")
async def get_tunnel_status(tunnel_id: str, request: Request):
    """Get tunnel status"""
    adapter_manager = request.app.state.adapter_manager
    
    try:
        status = await adapter_manager.get_tunnel_status(tunnel_id)
        return {"status": "success", "data": status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_status(request: Request):
    """Get node status"""
    adapter_manager = request.app.state.adapter_manager
    
    return {
        "status": "ok",
        "active_tunnels": len(adapter_manager.active_tunnels),
        "tunnels": list(adapter_manager.active_tunnels.keys())
    }


@router.post("/network/gre")
async def ensure_gre_tunnel(data: GreEnsure):
    """Create or refresh GRE toward the peer public IP (auto-detects local NIC/IP)."""
    try:
        from app.gre_setup import ensure_gre
        from app.network_optimize import discover_gre_peers
        result = ensure_gre(
            remote_public_ip=data.remote_public_ip,
            role=data.role,
            local_public_ip=data.local_public_ip,
            local_inner=data.local_inner,
            peer_inner=data.peer_inner,
            network=data.network,
            iface=data.iface,
            mtu=data.mtu,
        )
        result["peers"] = discover_gre_peers()
        return result
    except Exception as e:
        logger.error(f"GRE ensure failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/network/gre")
async def get_gre_status():
    """GRE discovery + saved config + underlay NIC."""
    try:
        from app.gre_setup import gre_status
        return {"status": "ok", **gre_status()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

