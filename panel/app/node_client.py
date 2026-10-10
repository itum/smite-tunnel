"""Client for panel to communicate with nodes"""
import httpx
import ssl
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Node, Settings

logger = logging.getLogger(__name__)


class NodeClient:
    """Client to send requests to nodes via HTTP/HTTPS or FRP"""
    
    def __init__(self):
        self.timeout = httpx.Timeout(30.0)
    
    async def _get_frp_settings(self) -> Optional[Dict[str, Any]]:
        """Get FRP communication settings"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.key == "frp"))
            setting = result.scalar_one_or_none()
            if setting and setting.value and setting.value.get("enabled"):
                return setting.value
        return None
    
    def _candidate_addresses(self, node: Node) -> list[str]:
        """Ordered list of HTTP bases to try for this node (FRP, public, GRE)."""
        md = node.node_metadata or {}
        addrs: list[str] = []
        api_port = int(md.get("api_port") or 8888)

        # Prefer FRP reverse control plane when configured.
        frp_remote_port = md.get("frp_remote_port")
        if frp_remote_port:
            try:
                from app.frp_comm_manager import frp_comm_manager
                if frp_comm_manager.is_running():
                    addrs.append(f"http://127.0.0.1:{int(frp_remote_port)}")
            except Exception:
                pass

        primary = md.get("api_address") or ""
        if primary and not str(primary).startswith("http"):
            primary = f"http://{primary}"
        if primary:
            addrs.append(str(primary).rstrip("/"))

        public_ip = (md.get("ip_address") or "").strip()
        if public_ip:
            addrs.append(f"http://{public_ip}:{api_port}")

        # GRE inner IP — works when public path is blocked but GRE is up.
        for peer in md.get("gre_peers") or []:
            inner = (peer.get("local_inner") or "").strip()
            if inner:
                addrs.append(f"http://{inner}:{api_port}")

        # Dedupe preserve order
        seen = set()
        out = []
        for a in addrs:
            key = a.rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append(key)
        if not out:
            out.append("http://localhost:8888")
        return out

    async def _get_node_address(self, node: Node) -> Tuple[str, bool]:
        """
        Get preferred node address (direct or via FRP)
        Returns: (address, using_frp)
        """
        candidates = self._candidate_addresses(node)
        using_frp = candidates[0].startswith("http://127.0.0.1:")
        logger.info(f"[HTTP] Using {'FRP' if using_frp else 'HTTP'} for node {node.id} at {candidates[0]}")
        return (candidates[0], using_frp)
    
    async def send_to_node(self, node_id: str, endpoint: str, data: Dict[str, Any], timeout: float | None = None) -> Dict[str, Any]:
        """
        Send request to node via HTTPS or FRP
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Node).where(Node.id == node_id))
            node = result.scalar_one_or_none()
            
            if not node:
                return {"status": "error", "message": f"Node {node_id} not found"}
            
            candidates = self._candidate_addresses(node)
            last_error = None

            for base in candidates:
                using_frp = base.startswith("http://127.0.0.1:")
                url = f"{base.rstrip('/')}{endpoint}"
                max_retries = 3 if using_frp else 1
                for attempt in range(max_retries):
                    try:
                        if using_frp and attempt > 0:
                            await asyncio.sleep(1.0)
                        async with httpx.AsyncClient(
                            timeout=httpx.Timeout(timeout) if timeout else self.timeout,
                            verify=False,
                            limits=httpx.Limits(max_keepalive_connections=0 if using_frp else 5),
                        ) as client:
                            response = await client.post(url, json=data)
                            response.raise_for_status()
                            if base != candidates[0]:
                                logger.info(f"Reached node {node_id} via fallback {base}")
                            return response.json()
                    except httpx.HTTPStatusError as e:
                        try:
                            error_detail = e.response.json().get("detail", str(e))
                        except Exception:
                            error_detail = str(e)
                        return {
                            "status": "error",
                            "message": f"Node error (HTTP {e.response.status_code}): {error_detail}",
                        }
                    except httpx.RequestError as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            continue
                        logger.warning(f"Node {node_id} unreachable at {base}: {e}")
                        break
                    except Exception as e:
                        last_error = e
                        break

            return {
                "status": "error",
                "message": (
                    f"Network error: foreign/iran agent unreachable "
                    f"(tried {', '.join(candidates)}). Last error: {last_error}"
                ),
            }
    
    async def get_tunnel_status(self, node_id: str, tunnel_id: str = "") -> Dict[str, Any]:
        """Get tunnel status from node"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Node).where(Node.id == node_id))
            node = result.scalar_one_or_none()
            
            if not node:
                return {"status": "error", "message": f"Node {node_id} not found"}
            
            node_address, using_frp = await self._get_node_address(node)
            url = f"{node_address.rstrip('/')}/api/agent/status"
            
            comm_type = "FRP" if using_frp else "HTTP"
            logger.debug(f"[{comm_type}] Getting tunnel status from node {node_id}")
            
            try:
                timeout = httpx.Timeout(3.0, connect=2.0)
                async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response.json()
            except httpx.RequestError as e:
                return {"status": "error", "message": f"Network error: {str(e)}"}
            except httpx.HTTPStatusError as e:
                try:
                    error_detail = e.response.json().get("detail", str(e))
                except:
                    error_detail = str(e)
                return {"status": "error", "message": f"Node error (HTTP {e.response.status_code}): {error_detail}"}
            except Exception as e:
                return {"status": "error", "message": f"Error: {str(e)}"}
    
    async def apply_tunnel(self, node_id: str, tunnel_data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply tunnel to node"""
        return await self.send_to_node(node_id, "/api/agent/tunnels/apply", tunnel_data)
