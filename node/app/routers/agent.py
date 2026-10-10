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


class MtuProbeRequest(BaseModel):
    target: str


class BenchListen(BaseModel):
    port: int


class BenchMeasure(BaseModel):
    host: str
    port: int
    nbytes: int = 2 * 1024 * 1024


class BenchGost(BaseModel):
    listen_port: int
    target_host: str
    target_port: int
    nbytes: int = 4 * 1024 * 1024


class BenchFrps(BaseModel):
    bind_port: int
    token: str


class BenchFrpc(BaseModel):
    server_addr: str
    server_port: int
    token: str
    local_port: int
    remote_port: int


class TunnelLiveRequest(BaseModel):
    tunnel_id: str
    core: str = "gost"
    ports: list = []
    control_port: Optional[int] = None


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


@router.post("/tunnels/live")
async def tunnel_live(data: TunnelLiveRequest, request: Request):
    """Real listen/process check for each tunnel port. Not a cached DB flag."""
    from app.tunnel_live import live_status, normalize_ports

    adapter_manager = request.app.state.adapter_manager
    ports = normalize_ports(data.ports or [])
    return live_status(
        tunnel_id=data.tunnel_id,
        core=(data.core or "").lower(),
        ports=ports,
        control_port=data.control_port,
        adapter_manager=adapter_manager,
    )


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


@router.post("/network/mtu-probe")
async def probe_path_mtu(data: MtuProbeRequest):
    """Measure path MTU to a peer. Does not change GRE or tunnels."""
    try:
        from app.gre_setup import discover_underlay_mtu
        return discover_underlay_mtu(data.target)
    except Exception as e:
        logger.error(f"MTU probe failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bench/listen")
async def bench_listen(data: BenchListen):
    from app.path_bench import start_listener
    return start_listener(data.port)


@router.post("/bench/stop")
async def bench_stop(data: BenchListen):
    from app.path_bench import stop_listener
    return stop_listener(data.port)


@router.post("/bench/stop-all")
async def bench_stop_all():
    from app.path_bench import stop_all
    stop_all()
    return {"ok": True}


@router.post("/bench/measure")
async def bench_measure(data: BenchMeasure):
    from app.path_bench import measure
    return measure(data.host, data.port, data.nbytes)


@router.post("/bench/gost")
async def bench_gost(data: BenchGost):
    from app.path_bench import measure_gost
    return measure_gost(data.listen_port, data.target_host, data.target_port, data.nbytes)


@router.post("/bench/frps")
async def bench_frps(data: BenchFrps):
    from app.path_bench import start_frps
    return start_frps(data.bind_port, data.token)


@router.post("/bench/frpc")
async def bench_frpc(data: BenchFrpc):
    from app.path_bench import start_frpc
    return start_frpc(data.server_addr, data.server_port, data.token, data.local_port, data.remote_port)


@router.post("/bench/stop-proc")
async def bench_stop_proc(data: dict):
    from app.path_bench import stop_named
    stop_named(str(data.get("name") or ""))
    return {"ok": True}


@router.get("/network/gre")
async def get_gre_status():
    """GRE discovery + saved config + underlay NIC."""
    try:
        from app.gre_setup import gre_status
        return {"status": "ok", **gre_status()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

