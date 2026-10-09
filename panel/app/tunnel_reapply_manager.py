"""Tunnel auto reapply manager"""
import asyncio
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Settings, Tunnel, Node
from app.node_client import NodeClient
from fastapi import Request

logger = logging.getLogger(__name__)


class TunnelReapplyManager:
    """Manages automatic tunnel reapplication"""
    
    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.enabled = False
        self.interval = 60
        self.interval_unit = "minutes"
        self.request: Optional[Request] = None
    
    async def load_settings(self):
        """Load settings from database"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.key == "tunnel"))
            setting = result.scalar_one_or_none()
            if setting and setting.value:
                self.enabled = setting.value.get("auto_reapply_enabled", False)
                self.interval = setting.value.get("auto_reapply_interval", 60)
                self.interval_unit = setting.value.get("auto_reapply_interval_unit", "minutes")
            else:
                self.enabled = False
                self.interval = 60
                self.interval_unit = "minutes"
    
    async def start(self):
        """Start auto reapply task"""
        await self.stop()
        await self.load_settings()
        
        if self.enabled:
            self.task = asyncio.create_task(self._reapply_loop())
            logger.info(f"Tunnel auto reapply task started: interval={self.interval} {self.interval_unit}")
    
    async def stop(self):
        """Stop auto reapply task"""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
            logger.info("Tunnel auto reapply task stopped")
    
    async def _reapply_loop(self):
        """Background task for automatic tunnel reapplication"""
        try:
            while True:
                await self.load_settings()
                
                if not self.enabled:
                    await asyncio.sleep(60)
                    continue
                
                if self.interval_unit == "hours":
                    sleep_seconds = self.interval * 3600
                else:
                    sleep_seconds = self.interval * 60
                
                await asyncio.sleep(sleep_seconds)
                
                if not self.enabled:
                    continue
                
                try:
                    await self._reapply_all_tunnels()
                except Exception as e:
                    logger.error(f"Error in automatic tunnel reapply: {e}", exc_info=True)
        except asyncio.CancelledError:
            logger.info("Tunnel reapply loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Tunnel reapply loop error: {e}", exc_info=True)
    
    async def _reapply_all_tunnels(self):
        """Reapply all tunnels"""
        from app.routers.tunnels import prepare_frp_spec_for_node
        from app.models import Node
        from fastapi import Request
        from starlette.requests import Request as StarletteRequest
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Tunnel).where(Tunnel.status == "active"))
            tunnels = result.scalars().all()
            
            if not tunnels:
                logger.debug("No active tunnels to reapply")
                return
            
            client = NodeClient()
            applied = 0
            failed = 0
            
            from starlette.requests import Request as StarletteRequest
            from starlette.datastructures import Headers
            
            fake_request = StarletteRequest(
                scope={
                    "type": "http",
                    "method": "POST",
                    "path": "/api/tunnels/reapply",
                    "headers": Headers({}).raw,
                    "query_string": b"",
                }
            )
            
            for tunnel in tunnels:
                try:
                    is_reverse_tunnel = tunnel.core in {"rathole", "backhaul", "chisel", "frp", "wstunnel", "bore"}
                    
                    if is_reverse_tunnel:
                        iran_node_id = tunnel.iran_node_id or tunnel.node_id
                        if not iran_node_id:
                            continue
                            
                        result = await session.execute(select(Node).where(Node.id == iran_node_id))
                        iran_node = result.scalar_one_or_none()
                        if not iran_node:
                            continue
                        
                        result = await session.execute(select(Node))
                        all_nodes = result.scalars().all()
                        foreign_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "foreign"]
                        if not foreign_nodes:
                            continue
                        foreign_node = foreign_nodes[0]
                        
                        spec = tunnel.spec.copy() if tunnel.spec else {}
                        
                        if tunnel.core == "frp":
                            bind_port = spec.get("bind_port", 7000)
                            token = spec.get("token")
                            
                            iran_node_ip = iran_node.node_metadata.get("ip_address")
                            if not iran_node_ip:
                                logger.warning(f"Tunnel {tunnel.id}: Iran node has no IP address, skipping")
                                failed += 1
                                continue
                            
                            spec_for_iran = spec.copy()
                            spec_for_iran["mode"] = "server"
                            spec_for_iran["bind_port"] = bind_port
                            if token:
                                spec_for_iran["token"] = token
                            
                            spec_for_foreign = spec.copy()
                            spec_for_foreign["mode"] = "client"
                            spec_for_foreign["server_addr"] = iran_node_ip
                            spec_for_foreign["server_port"] = bind_port
                            if token:
                                spec_for_foreign["token"] = token
                            tunnel_type = tunnel.type.lower() if tunnel.type else "tcp"
                            if tunnel_type not in ["tcp", "udp"]:
                                tunnel_type = "tcp"
                            spec_for_foreign["type"] = tunnel_type
                            
                            ports = spec.get("ports", [])
                            if not ports:
                                local_port = spec.get("local_port")
                                remote_port = spec.get("remote_port") or spec.get("listen_port")
                                if remote_port and local_port:
                                    spec_for_foreign["ports"] = [{"local": int(local_port), "remote": int(remote_port)}]
                                elif remote_port:
                                    spec_for_foreign["ports"] = [{"local": int(remote_port), "remote": int(remote_port)}]
                                elif local_port:
                                    spec_for_foreign["ports"] = [{"local": int(local_port), "remote": int(local_port)}]
                            else:
                                spec_for_foreign["ports"] = ports
                            
                            server_response = await client.send_to_node(
                                node_id=iran_node.id,
                                endpoint="/api/agent/tunnels/apply",
                                data={
                                    "tunnel_id": tunnel.id,
                                    "core": tunnel.core,
                                    "type": tunnel.type,
                                    "spec": spec_for_iran
                                }
                            )
                            
                            if server_response.get("status") == "error":
                                logger.error(f"Failed to reapply tunnel {tunnel.id} to iran node: {server_response.get('message')}")
                                failed += 1
                                continue
                            
                            client_response = await client.send_to_node(
                                node_id=foreign_node.id,
                                endpoint="/api/agent/tunnels/apply",
                                data={
                                    "tunnel_id": tunnel.id,
                                    "core": tunnel.core,
                                    "type": tunnel.type,
                                    "spec": spec_for_foreign
                                }
                            )
                            
                            if client_response.get("status") == "error":
                                logger.error(f"Failed to reapply tunnel {tunnel.id} to foreign node: {client_response.get('message')}")
                                failed += 1
                                continue
                            
                            if server_response.get("status") == "success" and client_response.get("status") == "success":
                                applied += 1
                                logger.info(f"Successfully reapplied tunnel {tunnel.id} ({tunnel.core})")
                            else:
                                failed += 1
                        else:
                            server_spec = spec.copy()
                            server_spec["mode"] = "server"
                            client_spec = spec.copy()
                            client_spec["mode"] = "client"
                            
                            if tunnel.core == "rathole":
                                transport = server_spec.get("transport") or server_spec.get("type") or "tcp"
                                proxy_port = server_spec.get("remote_port") or server_spec.get("listen_port")
                                token = server_spec.get("token")
                                if not proxy_port or not token:
                                    continue
                                
                                from app.utils import parse_address_port
                                remote_addr = server_spec.get("remote_addr", "0.0.0.0:23333")
                                _, control_port, _ = parse_address_port(remote_addr)
                                if not control_port:
                                    import hashlib
                                    port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                                    control_port = 23333 + (port_hash % 1000)
                                server_spec["mode"] = "server"
                                server_spec["bind_addr"] = f"0.0.0.0:{control_port}"
                                server_spec["proxy_port"] = proxy_port
                                server_spec["transport"] = transport
                                server_spec["token"] = token
                                
                                iran_node_ip = iran_node.node_metadata.get("ip_address")
                                if not iran_node_ip:
                                    continue
                                transport_lower = transport.lower()
                                if transport_lower in ("websocket", "ws"):
                                    use_tls = bool(server_spec.get("websocket_tls") or server_spec.get("tls"))
                                    protocol = "wss://" if use_tls else "ws://"
                                    client_spec["remote_addr"] = f"{protocol}{iran_node_ip}:{control_port}"
                                else:
                                    client_spec["remote_addr"] = f"{iran_node_ip}:{control_port}"
                                client_spec["mode"] = "client"
                                client_spec["transport"] = transport
                                client_spec["token"] = token
                            
                            elif tunnel.core == "backhaul":
                                transport = server_spec.get("transport") or server_spec.get("type") or "tcp"
                                control_port = server_spec.get("control_port") or server_spec.get("public_port") or server_spec.get("listen_port") or 3080
                                public_port = server_spec.get("public_port") or server_spec.get("listen_port") or control_port
                                target_host = server_spec.get("target_host", "127.0.0.1")
                                token = server_spec.get("token")
                                
                                server_spec["bind_addr"] = f"0.0.0.0:{control_port}"
                                server_spec["control_port"] = control_port
                                server_spec["public_port"] = public_port
                                server_spec["listen_port"] = public_port
                                ports = server_spec.get("ports", [])
                                if ports:
                                    server_spec["ports"] = ports
                                if token:
                                    server_spec["token"] = token
                                
                                iran_node_ip = iran_node.node_metadata.get("ip_address")
                                if not iran_node_ip:
                                    continue
                                transport_lower = transport.lower()
                                if transport_lower in ("ws", "wsmux"):
                                    use_tls = bool(server_spec.get("tls_cert") or server_spec.get("server_options", {}).get("tls_cert"))
                                    protocol = "wss://" if use_tls else "ws://"
                                    client_spec["remote_addr"] = f"{protocol}{iran_node_ip}:{control_port}"
                                else:
                                    client_spec["remote_addr"] = f"{iran_node_ip}:{control_port}"
                                client_spec["transport"] = transport
                                if token:
                                    client_spec["token"] = token
                            
                            elif tunnel.core == "chisel":
                                listen_port = server_spec.get("listen_port") or server_spec.get("remote_port")
                                if not listen_port:
                                    continue
                                
                                import hashlib
                                port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                                server_control_port = server_spec.get("control_port") or (int(listen_port) + 10000 + (port_hash % 1000))
                                server_spec["mode"] = "server"
                                server_spec["server_port"] = server_control_port
                                server_spec["reverse_port"] = listen_port
                                
                                iran_node_ip = iran_node.node_metadata.get("ip_address")
                                if not iran_node_ip:
                                    continue
                                from app.utils import is_valid_ipv6_address
                                if is_valid_ipv6_address(iran_node_ip):
                                    client_spec["server_url"] = f"http://[{iran_node_ip}]:{server_control_port}"
                                else:
                                    client_spec["server_url"] = f"http://{iran_node_ip}:{server_control_port}"
                                client_spec["mode"] = "client"
                                client_spec["reverse_port"] = listen_port

                            elif tunnel.core == "wstunnel":
                                ports = server_spec.get("ports") or []
                                if isinstance(ports, str):
                                    ports = [p.strip() for p in ports.split(",") if p.strip()]
                                listen_port = server_spec.get("listen_port") or server_spec.get("remote_port")
                                if not ports and listen_port:
                                    ports = [listen_port]
                                if not ports:
                                    continue
                                iran_node_ip = iran_node.node_metadata.get("ip_address")
                                if not iran_node_ip:
                                    continue
                                import hashlib
                                from app.utils import generate_wstunnel_secret, build_wstunnel_server_url
                                port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                                first_port = int(ports[0]) if str(ports[0]).isdigit() else ports[0]
                                server_control_port = server_spec.get("control_port") or (int(first_port) + 10000 + (port_hash % 1000))
                                secret = (server_spec.get("secret") or "").strip() or generate_wstunnel_secret()
                                tunnel_type = (tunnel.type or "tcp").lower()
                                if tunnel_type not in ("tcp", "udp"):
                                    tunnel_type = "tcp"
                                use_tls = bool(server_spec.get("use_tls", False))
                                server_spec["mode"] = "server"
                                server_spec["server_port"] = int(server_control_port)
                                server_spec["control_port"] = int(server_control_port)
                                server_spec["reverse_port"] = first_port
                                server_spec["ports"] = ports
                                server_spec["secret"] = secret
                                server_spec["type"] = tunnel_type
                                client_spec["mode"] = "client"
                                client_spec["server_url"] = build_wstunnel_server_url(
                                    iran_node_ip, int(server_control_port), secret, use_tls=use_tls
                                )
                                client_spec["ports"] = ports
                                client_spec["secret"] = secret
                                client_spec["type"] = tunnel_type
                                client_spec["local_addr"] = server_spec.get("local_addr") or f"127.0.0.1:{first_port}"

                            elif tunnel.core == "bore":
                                from app.utils import BORE_CONTROL_PORT, generate_bore_secret
                                ports = server_spec.get("ports") or []
                                if isinstance(ports, str):
                                    ports = [p.strip() for p in ports.split(",") if p.strip()]
                                listen_port = server_spec.get("listen_port") or server_spec.get("remote_port")
                                if not ports and listen_port:
                                    ports = [listen_port]
                                if not ports:
                                    continue
                                iran_node_ip = iran_node.node_metadata.get("ip_address")
                                if not iran_node_ip:
                                    continue
                                secret = (server_spec.get("secret") or "").strip() or generate_bore_secret()
                                server_spec["mode"] = "server"
                                server_spec["secret"] = secret
                                server_spec["control_port"] = BORE_CONTROL_PORT
                                server_spec["ports"] = ports
                                server_spec["type"] = "tcp"
                                client_spec["mode"] = "client"
                                client_spec["server_addr"] = iran_node_ip
                                client_spec["secret"] = secret
                                client_spec["control_port"] = BORE_CONTROL_PORT
                                client_spec["type"] = "tcp"
                                client_spec["local_host"] = server_spec.get("local_host") or "127.0.0.1"
                                client_spec["ports"] = [{"local": int(p), "remote": int(p)} for p in ports]
                            
                            server_response = await client.send_to_node(
                                node_id=iran_node.id,
                                endpoint="/api/agent/tunnels/apply",
                                data={
                                    "tunnel_id": tunnel.id,
                                    "core": tunnel.core,
                                    "type": tunnel.type,
                                    "spec": server_spec
                                }
                            )
                            
                            if server_response.get("status") == "error":
                                logger.error(f"Failed to reapply tunnel {tunnel.id} to iran node: {server_response.get('message')}")
                                failed += 1
                                continue
                            
                            client_response = await client.send_to_node(
                                node_id=foreign_node.id,
                                endpoint="/api/agent/tunnels/apply",
                                data={
                                    "tunnel_id": tunnel.id,
                                    "core": tunnel.core,
                                    "type": tunnel.type,
                                    "spec": client_spec
                                }
                            )
                            
                            if client_response.get("status") == "error":
                                logger.error(f"Failed to reapply tunnel {tunnel.id} to foreign node: {client_response.get('message')}")
                                failed += 1
                                continue
                            
                            if server_response.get("status") == "success" and client_response.get("status") == "success":
                                applied += 1
                                logger.info(f"Successfully reapplied tunnel {tunnel.id} ({tunnel.core})")
                            else:
                                failed += 1
                    else:
                        result = await session.execute(select(Node).where(Node.id == tunnel.node_id))
                        node = result.scalar_one_or_none()
                        if not node:
                            continue
                        
                        spec = tunnel.spec.copy() if tunnel.spec else {}
                        
                        if tunnel.core == "gost":
                            from app.utils import resolve_gost_forward_target
                            from sqlalchemy.orm.attributes import flag_modified
                            spec["type"] = tunnel.type
                            # Resolve GRE preference using Iran node + foreign peer metadata.
                            foreign_node = None
                            if tunnel.foreign_node_id:
                                fres = await session.execute(select(Node).where(Node.id == tunnel.foreign_node_id))
                                foreign_node = fres.scalar_one_or_none()
                            if not foreign_node:
                                all_res = await session.execute(select(Node))
                                foreign_nodes = [
                                    n for n in all_res.scalars().all()
                                    if n.node_metadata and n.node_metadata.get("role") == "foreign"
                                ]
                                if foreign_nodes:
                                    foreign_node = foreign_nodes[0]
                            if foreign_node:
                                target = resolve_gost_forward_target(
                                    foreign_public_ip=(foreign_node.node_metadata or {}).get("ip_address"),
                                    iran_gre_peers=(node.node_metadata or {}).get("gre_peers") or [],
                                    foreign_gre_peers=(foreign_node.node_metadata or {}).get("gre_peers") or [],
                                    iran_public_ip=(node.node_metadata or {}).get("ip_address"),
                                    explicit_remote_ip=spec.get("remote_ip"),
                                )
                                spec["remote_ip"] = target["remote_ip"]
                                spec["public_ip"] = target.get("public_ip")
                                spec["forward_via"] = target.get("via")
                                spec["forward_mtu"] = target.get("mtu")
                                spec["tcp_mss"] = target.get("mss")
                                tunnel.spec = {**(tunnel.spec or {}), **{
                                    "remote_ip": target["remote_ip"],
                                    "public_ip": target.get("public_ip"),
                                    "forward_via": target.get("via"),
                                    "forward_mtu": target.get("mtu"),
                                    "tcp_mss": target.get("mss"),
                                }}
                                flag_modified(tunnel, "spec")
                                await session.commit()
                        
                        if tunnel.core == "frp":
                            spec = prepare_frp_spec_for_node(spec, node, fake_request)
                        
                        response = await client.send_to_node(
                            node_id=node.id,
                            endpoint="/api/agent/tunnels/apply",
                            data={
                                "tunnel_id": tunnel.id,
                                "core": tunnel.core,
                                "type": tunnel.type,
                                "spec": spec
                            }
                        )
                        
                        if response.get("status") == "success":
                            applied += 1
                            logger.info(f"Successfully reapplied tunnel {tunnel.id} ({tunnel.core})")
                        else:
                            failed += 1
                            logger.error(f"Failed to reapply tunnel {tunnel.id}: {response.get('message')}")
                except Exception as e:
                    logger.error(f"Error reapplying tunnel {tunnel.id}: {e}", exc_info=True)
                    failed += 1
            
            logger.info(f"Auto reapply completed: {applied} applied, {failed} failed")
    
    def set_request(self, request: Request):
        """Set request object for reapply operations"""
        self.request = request


tunnel_reapply_manager = TunnelReapplyManager()

