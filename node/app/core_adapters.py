"""Core adapters for different tunnel types"""
from typing import Protocol, Dict, Any, Optional, List
import subprocess
import os
import psutil
import time
import logging
from pathlib import Path
import shutil

logger = logging.getLogger(__name__)
def parse_address_port(address_str: str):
    """Parse address:port string, returns (host, port, is_ipv6)"""
    import re
    import ipaddress
    
    if not address_str:
        return ("", None, False)
    
    address_str = address_str.strip()
    
    ipv6_bracket_match = re.match(r'^\[([^\]]+)\](?::(\d+))?$', address_str)
    if ipv6_bracket_match:
        host = ipv6_bracket_match.group(1)
        port_str = ipv6_bracket_match.group(2)
        port = int(port_str) if port_str else None
        return (host, port, True)
    
    try:
        ipaddress.IPv6Address(address_str)
        return (address_str, None, True)
    except (ValueError, ipaddress.AddressValueError):
        pass
    
    if ":" in address_str:
        parts = address_str.rsplit(":", 1)
        if len(parts) == 2:
            host_part = parts[0]
            port_str = parts[1]
            
            try:
                ipaddress.IPv6Address(host_part)
                return (host_part, int(port_str), True)
            except (ValueError, ipaddress.AddressValueError):
                try:
                    port = int(port_str)
                    return (host_part, port, False)
                except ValueError:
                    return (address_str, None, False)
    
    return (address_str, None, False)


class CoreAdapter(Protocol):
    """Protocol for core adapters"""
    name: str
    
    def apply(self, tunnel_id: str, spec: Dict[str, Any]) -> None:
        """Apply tunnel configuration"""
        ...
    
    def remove(self, tunnel_id: str) -> None:
        """Remove tunnel"""
        ...
    
    def status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get tunnel status"""
        ...


class RatholeAdapter:
    """Rathole reverse tunnel adapter"""
    name = "rathole"
    
    def __init__(self):
        self.config_dir = Path("/etc/smite-node/rathole")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes = {}
    
    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        """Apply Rathole tunnel - supports both server and client modes"""
        if tunnel_id in self.processes:
            logger.info(f"Rathole tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)
        
        mode = spec.get('mode', 'client')
        
        transport = (spec.get('transport') or spec.get('type') or 'tcp').lower()
        use_websocket = transport == 'websocket' or transport == 'ws'
        websocket_tls = spec.get('websocket_tls', False) or spec.get('tls', False)
        
        if mode == 'server':
            bind_addr = spec.get('bind_addr', '0.0.0.0:23333')
            token = spec.get('token', '').strip()
            
            ports = spec.get('ports', [])
            if not ports:
                proxy_port = spec.get('proxy_port') or spec.get('remote_port') or spec.get('listen_port')
                if proxy_port:
                    ports = [int(proxy_port) if isinstance(proxy_port, (int, str)) and str(proxy_port).isdigit() else proxy_port]
            
            if not token:
                raise ValueError("Rathole server requires 'token' in spec")
            if not ports:
                raise ValueError("Rathole server requires 'ports' array or 'proxy_port'/'remote_port' in spec")
            
            bind_host, bind_port, is_ipv6 = parse_address_port(bind_addr)
            if not bind_port:
                bind_host = "0.0.0.0"
                bind_port = 23333
            
            config = f"""[server]
bind_addr = "{bind_host}:{bind_port}"
default_token = "{token}"
"""
            
            if use_websocket:
                config += f"""
[server.transport]
type = "websocket"

[server.transport.websocket]
"""
                if websocket_tls:
                    config += "tls = true\n"
            
            for i, port in enumerate(ports):
                port_num = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else port
                service_name = f"{tunnel_id}_{i}" if len(ports) > 1 else tunnel_id
                config += f"""
[server.services.{service_name}]
bind_addr = "0.0.0.0:{port_num}"
"""
            
            config_path = self.config_dir / f"{tunnel_id}.toml"
            with open(config_path, "w") as f:
                f.write(config)
            
            try:
                proc = subprocess.Popen(
                    ["/usr/local/bin/rathole", "-s", str(config_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
            except FileNotFoundError:
                proc = subprocess.Popen(
                    ["rathole", "-s", str(config_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
        else:
            remote_addr = spec.get('remote_addr', '').strip()
            token = spec.get('token', '').strip()
            
            # Support multiple ports
            ports = spec.get('ports', [])
            if not ports:
                # Fallback to single port for backward compatibility
                local_addr = spec.get('local_addr', '127.0.0.1:8080')
                # Extract port from local_addr
                _, local_port, _ = parse_address_port(local_addr)
                if local_port:
                    ports = [local_port]
                else:
                    ports = [8080]
            
            if not remote_addr:
                raise ValueError("Rathole client requires 'remote_addr' (foreign server address) in spec")
            if not token:
                raise ValueError("Rathole client requires 'token' in spec")
            
            if remote_addr.startswith('ws://'):
                remote_addr = remote_addr[5:]
            elif remote_addr.startswith('wss://'):
                remote_addr = remote_addr[6:]
                websocket_tls = True
            
            config = f"""[client]
remote_addr = "{remote_addr}"
default_token = "{token}"
"""
            
            if use_websocket:
                config += f"""
[client.transport]
type = "websocket"

[client.transport.websocket]
"""
                if websocket_tls:
                    config += "tls = true\n"
            
            # Create multiple service sections for multiple ports
            for i, port in enumerate(ports):
                port_num = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else port
                service_name = f"{tunnel_id}_{i}" if len(ports) > 1 else tunnel_id
                local_addr = f"127.0.0.1:{port_num}"
                config += f"""
[client.services.{service_name}]
local_addr = "{local_addr}"
"""
            
            config_path = self.config_dir / f"{tunnel_id}.toml"
            with open(config_path, "w") as f:
                f.write(config)
            
            try:
                proc = subprocess.Popen(
                    ["/usr/local/bin/rathole", "-c", str(config_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
            except FileNotFoundError:
                proc = subprocess.Popen(
                    ["rathole", "-c", str(config_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
        
        self.processes[tunnel_id] = proc
        time.sleep(0.5)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else "Unknown error"
            raise RuntimeError(f"rathole failed to start: {stderr}")
    
    def remove(self, tunnel_id: str):
        """Remove Rathole tunnel"""
        config_path = self.config_dir / f"{tunnel_id}.toml"
        
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except:
                pass
            del self.processes[tunnel_id]
        
        try:
            subprocess.run(["pkill", "-f", f"rathole.*{tunnel_id}"], check=False, timeout=3)
        except:
            pass
            
        if config_path.exists():
            config_path.unlink()
    
    def status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get status"""
        config_path = self.config_dir / f"{tunnel_id}.toml"
        is_running = False
        
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            is_running = proc.poll() is None
        
        return {
            "active": config_path.exists() and is_running,
            "type": "rathole",
            "config_exists": config_path.exists(),
            "process_running": is_running
        }


class BackhaulAdapter:
    """Backhaul reverse tunnel adapter"""
    name = "backhaul"

    CLIENT_OPTION_KEYS = [
        "connection_pool",
        "retry_interval",
        "nodelay",
        "keepalive_period",
        "log_level",
        "pprof",
        "mux_session",
        "mux_version",
        "mux_framesize",
        "mux_recievebuffer",
        "mux_streambuffer",
        "sniffer",
        "web_port",
        "sniffer_log",
        "dial_timeout",
        "aggressive_pool",
        "edge_ip",
        "skip_optz",
        "mss",
        "so_rcvbuf",
        "so_sndbuf",
        "accept_udp",
    ]

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        binary_path: Optional[Path] = None,
    ):
        resolved_config = config_dir or Path(
            os.environ.get("SMITE_BACKHAUL_CLIENT_DIR", "/etc/smite-node/backhaul")
        )
        self.config_dir = Path(resolved_config)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes: Dict[str, subprocess.Popen] = {}
        self.log_handles: Dict[str, Any] = {}
        default_binary = binary_path or Path(
            os.environ.get("BACKHAUL_CLIENT_BINARY", "/usr/local/bin/backhaul")
        )
        self.binary_candidates = [
            Path(default_binary),
            Path("backhaul"),
        ]

    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        """Apply Backhaul tunnel - supports both server and client modes"""
        if tunnel_id in self.processes:
            logger.info(f"Backhaul tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)
        
        mode = spec.get('mode', 'client')
        
        if mode == 'server':
            transport = (spec.get("transport") or spec.get("type") or "tcp").lower()
            if transport not in {"tcp", "udp", "ws", "wsmux", "tcpmux"}:
                raise ValueError(f"Unsupported Backhaul transport '{transport}'")
            
            server_options = dict(spec.get("server_options") or {})
            bind_addr = spec.get("bind_addr")
            if not bind_addr:
                control_port = spec.get("control_port") or spec.get("listen_port") or 3080
                bind_ip = spec.get("bind_ip", "0.0.0.0")
                bind_addr = f"{bind_ip}:{control_port}"
            
            ports = spec.get("ports")
            logger.info(f"Backhaul {mode} tunnel {tunnel_id}: received ports from spec: {ports} (type: {type(ports)})")
            
            if not ports or (isinstance(ports, list) and len(ports) == 0):
                listen_port = spec.get("public_port") or spec.get("listen_port")
                target_addr = spec.get("target_addr")
                if not target_addr:
                    target_host = spec.get("target_host", "127.0.0.1")
                    target_port = spec.get("target_port") or listen_port
                    if target_port:
                        target_addr = f"{target_host}:{target_port}"
                if listen_port and target_addr:
                    ports = [f"{listen_port}={target_addr}"]
                elif listen_port:
                    ports = [str(listen_port)]
                else:
                    ports = []
            
            if isinstance(ports, list):
                processed_ports = []
                for p in ports:
                    if not p:
                        continue
                    if isinstance(p, str):
                        processed_ports.append(p)
                    elif isinstance(p, (int, float)):
                        processed_ports.append(str(p))
                    elif isinstance(p, dict):
                        local = p.get("local") or p.get("listen_port") or p.get("public_port")
                        target_host = p.get("target_host") or spec.get("target_host", "127.0.0.1")
                        target_port = p.get("target_port") or p.get("remote_port") or local
                        if local:
                            processed_ports.append(f"{local}={target_host}:{target_port}")
                    else:
                        processed_ports.append(str(p))
                ports = processed_ports
            else:
                ports = [str(ports)] if ports else []
            
            logger.info(f"Backhaul {mode} tunnel {tunnel_id}: processed ports: {ports} (count: {len(ports)})")
            
            server_config: Dict[str, Any] = {
                "bind_addr": bind_addr,
                "transport": transport,
                "ports": ports,
            }
            
            token = spec.get("token") or server_options.get("token")
            if token:
                server_config["token"] = token
            
            SERVER_OPTION_KEYS = [
                "nodelay", "keepalive_period", "channel_size", "log_level",
                "heartbeat", "mux_con", "accept_udp", "skip_optz",
                "tls_cert", "tls_key", "sniffer", "web_port", "proxy_protocol"
            ]
            for key in SERVER_OPTION_KEYS:
                value = server_options.get(key) or spec.get(key)
                if value is not None and value != "":
                    server_config[key] = value
            
            config_path = self.config_dir / f"{tunnel_id}.toml"
            config_path.write_text(self._render_toml({"server": server_config}), encoding="utf-8")
            
            binary_path = self._resolve_binary_path()
            log_path = self.config_dir / f"backhaul_{tunnel_id}.log"
            log_fh = log_path.open("w", buffering=1)
            log_fh.write(f"Starting Backhaul server for tunnel {tunnel_id}\n")
            log_fh.write(self._render_toml({"server": server_config}))
            log_fh.flush()
            
            try:
                proc = subprocess.Popen(
                    [str(binary_path), "-c", str(config_path)],
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True,
                )
            except Exception:
                log_fh.close()
                raise
        else:
            remote_addr = spec.get("remote_addr") or spec.get("control_addr") or spec.get("bind_addr")
            if not remote_addr:
                raise ValueError("Backhaul client requires 'remote_addr' in spec")

            if remote_addr.startswith('ws://'):
                remote_addr = remote_addr[5:]
            elif remote_addr.startswith('wss://'):
                remote_addr = remote_addr[6:]

            transport = (spec.get("transport") or spec.get("type") or "tcp").lower()
            if transport not in {"tcp", "udp", "ws", "wsmux", "tcpmux"}:
                raise ValueError(f"Unsupported Backhaul transport '{transport}'")
            client_options = dict(spec.get("client_options") or {})

            config_dict: Dict[str, Any] = {
                "remote_addr": remote_addr,
                "transport": transport,
            }

            token = spec.get("token") or client_options.get("token")
            if token:
                config_dict["token"] = token

            for key in self.CLIENT_OPTION_KEYS:
                value = client_options.get(key)
                if value is None or value == "":
                    value = spec.get(key)
                if value is None or value == "":
                    continue
                config_dict[key] = value

            if "connection_pool" not in config_dict:
                config_dict["connection_pool"] = 4
            if "retry_interval" not in config_dict:
                config_dict["retry_interval"] = 3
            if "dial_timeout" not in config_dict:
                config_dict["dial_timeout"] = 10

            if spec.get("accept_udp") and transport in {"tcp", "tcpmux"}:
                config_dict["accept_udp"] = True

            config_path = self.config_dir / f"{tunnel_id}.toml"
            config_path.write_text(self._render_toml({"client": config_dict}), encoding="utf-8")

            binary_path = self._resolve_binary_path()

            log_path = self.config_dir / f"backhaul_{tunnel_id}.log"
            log_fh = log_path.open("w", buffering=1)
            log_fh.write(f"Starting Backhaul client for tunnel {tunnel_id}\n")
            log_fh.write(self._render_toml({"client": config_dict}))
            log_fh.flush()

            try:
                proc = subprocess.Popen(
                    [str(binary_path), "-c", str(config_path)],
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                )
            except Exception:
                log_fh.close()
                raise

        time.sleep(0.5)
        if proc.poll() is not None:
            error_output = ""
            try:
                error_output = log_path.read_text(encoding="utf-8")[-1000:]
            except Exception:
                pass
            log_fh.close()
            raise RuntimeError(f"backhaul failed to start: {error_output}")

        self.processes[tunnel_id] = proc
        self.log_handles[tunnel_id] = log_fh

    def remove(self, tunnel_id: str):
        config_path = self.config_dir / f"{tunnel_id}.toml"
        
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
            del self.processes[tunnel_id]
        if tunnel_id in self.log_handles:
            try:
                self.log_handles[tunnel_id].close()
            except Exception:
                pass
            del self.log_handles[tunnel_id]

        if config_path.exists():
            try:
                config_path.unlink()
            except Exception:
                pass

    def status(self, tunnel_id: str) -> Dict[str, Any]:
        config_path = self.config_dir / f"{tunnel_id}.toml"
        proc = self.processes.get(tunnel_id)
        is_running = proc is not None and proc.poll() is None
        return {
            "active": config_path.exists() and is_running,
            "type": "backhaul",
            "config_exists": config_path.exists(),
            "process_running": is_running,
        }

    def _render_toml(self, data: Dict[str, Dict[str, Any]]) -> str:
        def format_value(value: Any) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, list):
                if not value:
                    return "[]"
                rendered = ",\n  ".join(f"\"{str(item)}\"" for item in value)
                return "[\n  " + rendered + "\n]"
            value_str = str(value).replace("\\", "\\\\").replace('"', '\\"')
            return f"\"{value_str}\""

        lines: List[str] = []
        for section, values in data.items():
            lines.append(f"[{section}]")
            for key, val in values.items():
                if val is None:
                    continue
                lines.append(f"{key} = {format_value(val)}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _resolve_binary_path(self) -> Path:
        for candidate in self.binary_candidates:
            if candidate.exists():
                return candidate

        resolved = shutil.which("backhaul")
        if resolved:
            return Path(resolved)

        raise FileNotFoundError(
            "Backhaul binary not found. Expected at BACKHAUL_CLIENT_BINARY, '/usr/local/bin/backhaul', or in PATH."
        )


def _normalize_chisel_auth(auth: Optional[str]) -> Optional[str]:
    """Ensure Chisel --auth is user:pass (required since Chisel 1.12)."""
    if not auth:
        return auth
    if ":" in auth:
        return auth
    return f"smite:{auth}"


class ChiselAdapter:
    """Chisel reverse tunnel adapter"""
    name = "chisel"
    
    def __init__(self):
        self.config_dir = Path("/etc/smite-node/chisel")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.log_handles = {}
    
    def _resolve_binary_path(self) -> Path:
        """Resolve chisel binary path"""
        env_path = os.environ.get("CHISEL_BINARY")
        if env_path:
            resolved = Path(env_path)
            if resolved.exists() and resolved.is_file():
                return resolved
        
        common_paths = [
            Path("/usr/local/bin/chisel"),
            Path("/usr/bin/chisel"),
            Path("/opt/chisel/chisel"),
        ]
        
        for path in common_paths:
            if path.exists() and path.is_file():
                return path
        
        resolved = shutil.which("chisel")
        if resolved:
            return Path(resolved)
        
        raise FileNotFoundError(
            "Chisel binary not found. Expected at CHISEL_BINARY, '/usr/local/bin/chisel', or in PATH."
        )
    
    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        """Apply Chisel tunnel - supports both server and client modes"""
        if tunnel_id in self.processes:
            logger.info(f"Chisel tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)
        
        mode = spec.get('mode', 'client')
        
        if mode == 'server':
            server_port = spec.get('server_port') or spec.get('control_port') or spec.get('listen_port')
            if not server_port:
                raise ValueError("Chisel server requires 'server_port' or 'control_port' in spec")
            
            reverse_port = spec.get('reverse_port') or spec.get('remote_port') or spec.get('listen_port')
            if not reverse_port:
                raise ValueError("Chisel server requires 'reverse_port' or 'remote_port' in spec")
            
            host = "0.0.0.0"
            binary_path = self._resolve_binary_path()
            cmd = [
                str(binary_path),
                "server",
                "--host", host,
                "--port", str(server_port),
                "--reverse"
            ]
            
            auth = _normalize_chisel_auth(spec.get('auth'))
            if auth:
                cmd.extend(["--auth", auth])
            
            fingerprint = spec.get('fingerprint')
            if fingerprint:
                cmd.extend(["--fingerprint", fingerprint])
            
            log_file = self.config_dir / f"{tunnel_id}.log"
            log_f = open(log_file, 'w', buffering=1)
            try:
                log_f.write(f"Starting chisel server for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.write(f"server_port={server_port}, reverse_port={reverse_port}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True
                )
            except FileNotFoundError:
                log_f.close()
                raise RuntimeError("chisel binary not found. Please install chisel.")
        else:
            server_url = spec.get('server_url', '').strip()
            
            # Support multiple ports
            ports = spec.get('ports', [])
            if not ports:
                # Fallback to single port for backward compatibility
                reverse_port = spec.get('reverse_port') or spec.get('remote_port') or spec.get('listen_port') or spec.get('server_port')
                if reverse_port:
                    ports = [int(reverse_port) if isinstance(reverse_port, (int, str)) and str(reverse_port).isdigit() else reverse_port]
            
            if not server_url:
                raise ValueError("Chisel client requires 'server_url' (foreign server address) in spec")
            if not ports:
                raise ValueError("Chisel client requires 'ports' array or 'reverse_port'/'remote_port'/'listen_port' in spec")
            
            binary_path = self._resolve_binary_path()
            cmd = [
                str(binary_path),
                "client"
            ]
            
            auth = _normalize_chisel_auth(spec.get('auth'))
            if auth:
                cmd.extend(["--auth", auth])
            
            fingerprint = spec.get('fingerprint')
            if fingerprint:
                cmd.extend(["--fingerprint", fingerprint])
            
            cmd.append(server_url)
            
            # Add multiple reverse specs for multiple ports
            for port in ports:
                port_num = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else port
                local_addr = spec.get('local_addr')
                if not local_addr:
                    local_addr = f"127.0.0.1:{port_num}"
                
                host, local_port, is_ipv6 = parse_address_port(local_addr)
                if not local_port:
                    host = "127.0.0.1"
                    local_port = port_num
                
                if is_ipv6:
                    reverse_spec = f"R:{port_num}:[{host}]:{local_port}"
                else:
                    reverse_spec = f"R:{port_num}:{host}:{local_port}"
                cmd.append(reverse_spec)
            
            reverse_specs = [f"R:{port}:127.0.0.1:{port}" for port in ports]
            logger.info(f"Chisel tunnel {tunnel_id}: ports={ports}, server_url={server_url}")
            
            log_file = self.config_dir / f"{tunnel_id}.log"
            log_f = open(log_file, 'w', buffering=1)
            try:
                log_f.write(f"Starting chisel client for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.write(f"server_url={server_url}, reverse_specs={', '.join(reverse_specs)}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True
                )
            except FileNotFoundError:
                log_f.close()
                raise RuntimeError("chisel binary not found. Please install chisel.")
        
        self.log_handles[tunnel_id] = log_f
        self.processes[tunnel_id] = proc
        time.sleep(1.0)  # Give it more time to start
        if proc.poll() is not None:
            stderr = ""
            if log_file.exists():
                with open(log_file, 'r') as f:
                    stderr = f.read()
            if tunnel_id in self.log_handles:
                try:
                    self.log_handles[tunnel_id].close()
                except:
                    pass
                del self.log_handles[tunnel_id]
            raise RuntimeError(f"chisel failed to start: {stderr[-500:] if len(stderr) > 500 else stderr}")
    
    def remove(self, tunnel_id: str):
        """Remove Chisel tunnel"""
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except:
                pass
            del self.processes[tunnel_id]
        
        if tunnel_id in self.log_handles:
            try:
                self.log_handles[tunnel_id].close()
            except:
                pass
            del self.log_handles[tunnel_id]
        
        try:
            subprocess.run(["pkill", "-f", f"chisel.*{tunnel_id}"], check=False, timeout=3)
        except:
            pass
    
    def status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get status"""
        is_running = False
        
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            is_running = proc.poll() is None
        
        return {
            "active": is_running,
            "type": "chisel",
            "process_running": is_running
        }


class FrpAdapter:
    """FRP reverse tunnel adapter"""
    name = "frp"
    
    def __init__(self):
        self.config_dir = Path("/etc/smite-node/frp")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.log_handles = {}
    
    def _resolve_binary_path(self) -> Path:
        """Resolve frpc binary path"""
        env_path = os.environ.get("FRPC_BINARY")
        if env_path:
            resolved = Path(env_path)
            if resolved.exists() and resolved.is_file():
                return resolved
        
        common_paths = [
            Path("/usr/local/bin/frpc"),
            Path("/usr/bin/frpc"),
        ]
        
        for path in common_paths:
            if path.exists() and path.is_file():
                return path
        
        resolved = shutil.which("frpc")
        if resolved:
            return Path(resolved)
        
        raise FileNotFoundError(
            "frpc binary not found. Expected at FRPC_BINARY, '/usr/local/bin/frpc', or in PATH."
        )
    
    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        """Apply FRP tunnel - supports both server and client modes"""
        if tunnel_id in self.processes:
            logger.info(f"FRP tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)
        
        mode = spec.get('mode', 'client')
        
        if mode == 'server':
            bind_port = spec.get('bind_port', 7000)
            token = spec.get('token')
            
            config_file = self.config_dir / f"frps_{tunnel_id}.yaml"
            config_content = f"""bindPort: {bind_port}
"""
            if token:
                config_content += f"""auth:
  method: token
  token: "{token}"
"""
            
            with open(config_file, 'w') as f:
                f.write(config_content)
            
            logger.info(f"FRP server tunnel {tunnel_id}: bind_port={bind_port}, token={'set' if token else 'none'}")
            
            env_path = os.environ.get("FRPS_BINARY")
            if env_path:
                binary_path = Path(env_path)
            else:
                common_paths = [
                    Path("/usr/local/bin/frps"),
                    Path("/usr/bin/frps"),
                ]
                binary_path = None
                for path in common_paths:
                    if path.exists() and path.is_file():
                        binary_path = path
                        break
                if not binary_path:
                    resolved = shutil.which("frps")
                    if resolved:
                        binary_path = Path(resolved)
                    else:
                        raise FileNotFoundError("frps binary not found. Expected at FRPS_BINARY, '/usr/local/bin/frps', or in PATH.")
            
            config_file_abs = config_file.resolve()
            cmd = [
                str(binary_path),
                "-c", str(config_file_abs)
            ]
            
            log_file = self.config_dir / f"{tunnel_id}.log"
            log_f = open(log_file, 'w', buffering=1)
            try:
                log_f.write(f"Starting FRP server for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.write(f"Config: bind_port={bind_port}, token={'set' if token else 'none'}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True
                )
            except FileNotFoundError:
                log_f.close()
                raise RuntimeError("FRP server binary (frps) not found. Please install FRP.")
        else:
            logger.info(f"FRP tunnel {tunnel_id} received spec: {spec}")
            
            server_addr = spec.get('server_addr', '').strip()
            server_port = spec.get('server_port', 7000)
            token = spec.get('token')
            tunnel_type = spec.get('type', 'tcp').lower()
            local_ip = spec.get('local_ip', '127.0.0.1')
            
            ports = spec.get('ports', [])
            if not ports:
                local_port = spec.get('local_port')
                remote_port = spec.get('remote_port') or spec.get('listen_port')
                if remote_port and local_port:
                    ports = [{'local': local_port, 'remote': remote_port}]
                elif remote_port:
                    ports = [{'local': remote_port, 'remote': remote_port}]
                elif local_port:
                    ports = [{'local': local_port, 'remote': local_port}]
            
            logger.info(f"FRP tunnel {tunnel_id} parsed: server_addr='{server_addr}', server_port={server_port}, token={'set' if token else 'none'}, ports={len(ports)}")
            
            if not server_addr:
                raise ValueError("FRP client requires 'server_addr' (foreign server address) in spec")
            if not ports:
                raise ValueError("FRP client requires 'ports' array or 'remote_port'/'listen_port' in spec")
            if tunnel_type not in ['tcp', 'udp']:
                raise ValueError(f"FRP only supports 'tcp' and 'udp' types, got '{tunnel_type}'")
            
            if server_addr.startswith('[') and server_addr.endswith(']'):
                server_addr = server_addr[1:-1]
            
            if not server_addr or server_addr in ["0.0.0.0", "localhost", "127.0.0.1", "::1"]:
                raise ValueError(f"Invalid FRP server_addr: {server_addr}. Must be a valid foreign server IP address or hostname.")
            
            config_file = self.config_dir / f"frpc_{tunnel_id}.yaml"
            config_content = f"""serverAddr: "{server_addr}"
serverPort: {server_port}
"""
            if token:
                config_content += f"""auth:
  method: token
  token: "{token}"
"""
            
            config_content += "\nproxies:\n"
            for i, port_config in enumerate(ports):
                if isinstance(port_config, dict):
                    local_port = port_config.get('local')
                    remote_port = port_config.get('remote')
                else:
                    local_port = remote_port = port_config
                
                proxy_name = f"{tunnel_id}_{i}" if len(ports) > 1 else tunnel_id
                config_content += f"""  - name: {proxy_name}
    type: {tunnel_type}
    localIP: {local_ip}
    localPort: {local_port}
    remotePort: {remote_port}
"""
            
            with open(config_file, 'w') as f:
                f.write(config_content)
            
            logger.info(f"FRP tunnel {tunnel_id}: type={tunnel_type}, local={local_ip}:{local_port}, remote={remote_port}, server={server_addr}:{server_port}")
            
            binary_path = self._resolve_binary_path()
            config_file_abs = config_file.resolve()
            
            cmd = [
                str(binary_path),
                "-c", str(config_file_abs)
            ]
            
            log_file = self.config_dir / f"{tunnel_id}.log"
            log_f = open(log_file, 'w', buffering=1)
            try:
                log_f.write(f"Starting FRP client for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.write(f"Config: type={tunnel_type}, local={local_ip}:{local_port}, remote={remote_port}, server={server_addr}:{server_port}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True,
                    env=os.environ.copy()
                )
            except FileNotFoundError:
                log_f.close()
                raise RuntimeError("FRP binary (frpc) not found. Please install FRP.")
        
        self.log_handles[tunnel_id] = log_f
        self.processes[tunnel_id] = proc
        time.sleep(1.0)
        if proc.poll() is not None:
            stderr = ""
            if log_file.exists():
                with open(log_file, 'r') as f:
                    stderr = f.read()
            if tunnel_id in self.log_handles:
                try:
                    self.log_handles[tunnel_id].close()
                except:
                    pass
                del self.log_handles[tunnel_id]
            raise RuntimeError(f"FRP failed to start: {stderr[-500:] if len(stderr) > 500 else stderr}")
    
    def remove(self, tunnel_id: str):
        """Remove FRP tunnel"""
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except:
                pass
            del self.processes[tunnel_id]
        
        if tunnel_id in self.log_handles:
            try:
                self.log_handles[tunnel_id].close()
            except:
                pass
            del self.log_handles[tunnel_id]
        
        config_file = self.config_dir / f"frpc_{tunnel_id}.yaml"
        if config_file.exists():
            try:
                config_file.unlink()
            except:
                pass
        
        try:
            subprocess.run(["pkill", "-f", f"frpc.*{tunnel_id}"], check=False, timeout=3)
        except:
            pass
    
    def status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get status"""
        is_running = False
        
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            is_running = proc.poll() is None
        
        return {
            "active": is_running,
            "type": "frp",
            "process_running": is_running
        }


class BoreAdapter:
    """
    Bore TCP reverse tunnel (Iran=server, Foreign=client).

    Upstream bore uses a fixed control port 7835, so only one bore server can
    run per node. Multiple tunnels share that server when they use the same secret.
    """
    name = "bore"
    CONTROL_PORT = 7835

    _shared_server_proc = None
    _shared_server_log = None
    _shared_secret = None
    _shared_server_tunnels = set()

    def __init__(self):
        self.config_dir = Path("/etc/smite-node/bore")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # tunnel_id -> list[Popen] for client mode (one process per port)
        self.client_processes: Dict[str, List[subprocess.Popen]] = {}
        self.log_handles: Dict[str, List] = {}

    def _resolve_binary_path(self) -> Path:
        env_path = os.environ.get("BORE_BINARY")
        if env_path:
            resolved = Path(env_path)
            if resolved.exists() and resolved.is_file():
                return resolved
        for path in (Path("/usr/local/bin/bore"), Path("/usr/bin/bore")):
            if path.exists() and path.is_file():
                return path
        resolved = shutil.which("bore")
        if resolved:
            return Path(resolved)
        raise FileNotFoundError(
            "bore binary not found. Expected at BORE_BINARY, '/usr/local/bin/bore', or in PATH."
        )

    def _shared_server_running(self) -> bool:
        proc = BoreAdapter._shared_server_proc
        return proc is not None and proc.poll() is None

    def _start_shared_server(self, secret: str, tunnel_id: str):
        if self._shared_server_running():
            if BoreAdapter._shared_secret != secret:
                raise RuntimeError(
                    "Bore control port 7835 is already in use by another bore server with a different secret. "
                    "Only one bore server can run per node; reuse the existing bore tunnel secret or remove it first."
                )
            BoreAdapter._shared_server_tunnels.add(tunnel_id)
            logger.info(f"Reusing shared bore server for tunnel {tunnel_id}")
            return

        binary_path = self._resolve_binary_path()
        cmd = [
            str(binary_path),
            "server",
            "--secret",
            secret,
            "--bind-addr",
            "0.0.0.0",
            "--min-port",
            "1",
            "--max-port",
            "65535",
        ]
        log_file = self.config_dir / "shared_server.log"
        log_f = open(log_file, "w", buffering=1)
        log_f.write(f"Starting shared bore server for tunnel {tunnel_id}\n")
        log_f.write(f"Command: {' '.join(cmd)}\n")
        log_f.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=str(self.config_dir),
                start_new_session=True,
            )
        except FileNotFoundError:
            log_f.close()
            raise RuntimeError("bore binary not found. Please install bore.")

        time.sleep(0.8)
        if proc.poll() is not None:
            err = ""
            if log_file.exists():
                with open(log_file, "r") as f:
                    err = f.read()
            log_f.close()
            raise RuntimeError(
                f"bore server failed to start: {err[-500:] if len(err) > 500 else err}"
            )

        BoreAdapter._shared_server_proc = proc
        BoreAdapter._shared_server_log = log_f
        BoreAdapter._shared_secret = secret
        BoreAdapter._shared_server_tunnels = {tunnel_id}
        logger.info(f"Started shared bore server (PID {proc.pid}) for tunnel {tunnel_id}")

    def _release_shared_server(self, tunnel_id: str):
        BoreAdapter._shared_server_tunnels.discard(tunnel_id)
        if BoreAdapter._shared_server_tunnels:
            return
        proc = BoreAdapter._shared_server_proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception:
                pass
        if BoreAdapter._shared_server_log is not None:
            try:
                BoreAdapter._shared_server_log.close()
            except Exception:
                pass
        BoreAdapter._shared_server_proc = None
        BoreAdapter._shared_server_log = None
        BoreAdapter._shared_secret = None

    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        if tunnel_id in self.client_processes or tunnel_id in BoreAdapter._shared_server_tunnels:
            logger.info(f"Bore tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)

        mode = spec.get("mode", "client")
        secret = (spec.get("secret") or spec.get("token") or "").strip()
        if not secret:
            raise ValueError("Bore requires 'secret' in spec")

        binary_path = self._resolve_binary_path()

        if mode == "server":
            self._start_shared_server(secret, tunnel_id)
            return

        server_addr = (spec.get("server_addr") or "").strip()
        if not server_addr:
            raise ValueError("Bore client requires 'server_addr' (Iran node IP/host) in spec")
        if server_addr.startswith("[") and server_addr.endswith("]"):
            server_addr = server_addr[1:-1]

        ports = spec.get("ports", [])
        if not ports:
            remote_port = spec.get("remote_port") or spec.get("listen_port")
            local_port = spec.get("local_port") or remote_port
            if remote_port:
                ports = [{"local": local_port or remote_port, "remote": remote_port}]

        if not ports:
            raise ValueError("Bore client requires 'ports' or 'remote_port'/'listen_port' in spec")

        local_host = spec.get("local_host") or "127.0.0.1"
        procs: List[subprocess.Popen] = []
        logs = []
        try:
            for i, port_config in enumerate(ports):
                if isinstance(port_config, dict):
                    local_port = int(port_config.get("local") or port_config.get("remote"))
                    remote_port = int(port_config.get("remote") or local_port)
                else:
                    local_port = remote_port = int(port_config)

                cmd = [
                    str(binary_path),
                    "local",
                    str(local_port),
                    "--to",
                    server_addr,
                    "--port",
                    str(remote_port),
                    "--local-host",
                    local_host,
                    "--secret",
                    secret,
                ]
                log_file = self.config_dir / f"{tunnel_id}_{remote_port}.log"
                log_f = open(log_file, "w", buffering=1)
                log_f.write(f"Starting bore client for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True,
                )
                time.sleep(1.0)
                if proc.poll() is not None:
                    err = ""
                    if log_file.exists():
                        with open(log_file, "r") as f:
                            err = f.read()
                    log_f.close()
                    raise RuntimeError(
                        f"bore client failed for port {remote_port}: {err[-500:] if len(err) > 500 else err}"
                    )
                procs.append(proc)
                logs.append(log_f)
        except Exception:
            for p in procs:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    pass
            for lf in logs:
                try:
                    lf.close()
                except Exception:
                    pass
            raise

        self.client_processes[tunnel_id] = procs
        self.log_handles[tunnel_id] = logs
        logger.info(f"Bore client tunnel {tunnel_id}: {len(procs)} port(s) to {server_addr}")

    def remove(self, tunnel_id: str):
        if tunnel_id in self.client_processes:
            for proc in self.client_processes.pop(tunnel_id, []):
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
        if tunnel_id in self.log_handles:
            for lf in self.log_handles.pop(tunnel_id, []):
                try:
                    lf.close()
                except Exception:
                    pass
        if tunnel_id in BoreAdapter._shared_server_tunnels:
            self._release_shared_server(tunnel_id)

    def status(self, tunnel_id: str) -> Dict[str, Any]:
        if tunnel_id in self.client_processes:
            procs = self.client_processes[tunnel_id]
            is_running = bool(procs) and all(p.poll() is None for p in procs)
            return {"active": is_running, "type": "bore", "process_running": is_running}
        if tunnel_id in BoreAdapter._shared_server_tunnels:
            is_running = self._shared_server_running()
            return {"active": is_running, "type": "bore", "process_running": is_running}
        return {"active": False, "type": "bore", "process_running": False}


class WstunnelAdapter:
    """Wstunnel reverse tunnel over WebSocket/HTTP2 (Iran=server, Foreign=client)"""
    name = "wstunnel"

    def __init__(self):
        self.config_dir = Path("/etc/smite-node/wstunnel")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.log_handles = {}

    def _resolve_binary_path(self) -> Path:
        env_path = os.environ.get("WSTUNNEL_BINARY")
        if env_path:
            resolved = Path(env_path)
            if resolved.exists() and resolved.is_file():
                return resolved

        for path in (Path("/usr/local/bin/wstunnel"), Path("/usr/bin/wstunnel")):
            if path.exists() and path.is_file():
                return path

        resolved = shutil.which("wstunnel")
        if resolved:
            return Path(resolved)

        raise FileNotFoundError(
            "wstunnel binary not found. Expected at WSTUNNEL_BINARY, '/usr/local/bin/wstunnel', or in PATH."
        )

    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        if tunnel_id in self.processes:
            logger.info(f"Wstunnel tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)

        mode = spec.get("mode", "client")
        secret = (spec.get("secret") or spec.get("token") or "").strip()
        if not secret:
            raise ValueError("Wstunnel requires 'secret' (path prefix) in spec")

        binary_path = self._resolve_binary_path()
        tunnel_type = str(spec.get("type", "tcp")).lower()
        if tunnel_type not in ("tcp", "udp"):
            tunnel_type = "tcp"

        if mode == "server":
            server_port = spec.get("server_port") or spec.get("control_port")
            if not server_port:
                raise ValueError("Wstunnel server requires 'server_port' or 'control_port' in spec")

            use_ipv6 = bool(spec.get("use_ipv6", False))
            listen_host = "[::]" if use_ipv6 else "0.0.0.0"
            listen_url = f"ws://{listen_host}:{int(server_port)}"

            # wstunnel v11+: any restriction mode denies reverse unless
            # !ReverseTunnel is explicitly allowed. PathPrefix matching is
            # unreliable for reverse upgrades, so allow reverse for listed ports.
            reverse_ports = []
            for p in (spec.get("ports") or []):
                try:
                    reverse_ports.append(int(p))
                except Exception:
                    pass
            for key in ("reverse_port", "listen_port", "remote_port"):
                if spec.get(key):
                    try:
                        reverse_ports.append(int(spec[key]))
                    except Exception:
                        pass
            reverse_ports = sorted(set(reverse_ports)) or list(range(1, 65536))
            port_lines = "\n".join(f"          - {p}" for p in reverse_ports)
            restrict_path = self.config_dir / f"{tunnel_id}_restrict.yaml"
            restrict_path.write_text(
                "restrictions:\n"
                f'  - name: "smite-{tunnel_id[:8]}"\n'
                "    match:\n"
                "      - !Any\n"
                "    allow:\n"
                "      - !ReverseTunnel\n"
                "        protocol:\n"
                "          - Tcp\n"
                "          - Udp\n"
                "        port:\n"
                f"{port_lines}\n"
                "        cidr:\n"
                "          - 0.0.0.0/0\n"
                "          - ::/0\n",
                encoding="utf-8",
            )

            cmd = [
                str(binary_path),
                "server",
                "--restrict-config",
                str(restrict_path),
                listen_url,
            ]

            log_file = self.config_dir / f"{tunnel_id}.log"
            log_f = open(log_file, "w", buffering=1)
            try:
                log_f.write(f"Starting wstunnel server for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.write(f"server_port={server_port}, secret=set\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True,
                )
            except FileNotFoundError:
                log_f.close()
                raise RuntimeError("wstunnel binary not found. Please install wstunnel.")
        else:
            server_url = (spec.get("server_url") or "").strip()
            if not server_url:
                raise ValueError("Wstunnel client requires 'server_url' in spec")

            ports = spec.get("ports", [])
            if not ports:
                reverse_port = (
                    spec.get("reverse_port")
                    or spec.get("remote_port")
                    or spec.get("listen_port")
                    or spec.get("server_port")
                )
                if reverse_port:
                    ports = [reverse_port]

            if not ports:
                raise ValueError("Wstunnel client requires 'ports' or 'reverse_port'/'listen_port' in spec")

            cmd = [str(binary_path), "client"]
            for port in ports:
                port_num = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else port
                local_addr = spec.get("local_addr")
                if not local_addr:
                    local_addr = f"127.0.0.1:{port_num}"

                host, local_port, is_ipv6 = parse_address_port(local_addr)
                if not local_port:
                    host = "127.0.0.1"
                    local_port = port_num
                    is_ipv6 = False

                if is_ipv6:
                    reverse_spec = f"{tunnel_type}://0.0.0.0:{port_num}:[{host}]:{local_port}"
                else:
                    reverse_spec = f"{tunnel_type}://0.0.0.0:{port_num}:{host}:{local_port}"
                cmd.extend(["-R", reverse_spec])

            cmd.append(server_url)

            log_file = self.config_dir / f"{tunnel_id}.log"
            log_f = open(log_file, "w", buffering=1)
            try:
                log_f.write(f"Starting wstunnel client for tunnel {tunnel_id}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.write(f"server_url={server_url}, ports={ports}, type={tunnel_type}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True,
                )
            except FileNotFoundError:
                log_f.close()
                raise RuntimeError("wstunnel binary not found. Please install wstunnel.")

        self.log_handles[tunnel_id] = log_f
        self.processes[tunnel_id] = proc
        time.sleep(1.0)
        if proc.poll() is not None:
            stderr = ""
            if log_file.exists():
                with open(log_file, "r") as f:
                    stderr = f.read()
            if tunnel_id in self.log_handles:
                try:
                    self.log_handles[tunnel_id].close()
                except Exception:
                    pass
                del self.log_handles[tunnel_id]
            raise RuntimeError(
                f"wstunnel failed to start: {stderr[-500:] if len(stderr) > 500 else stderr}"
            )

    def remove(self, tunnel_id: str):
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception:
                pass
            del self.processes[tunnel_id]

        if tunnel_id in self.log_handles:
            try:
                self.log_handles[tunnel_id].close()
            except Exception:
                pass
            del self.log_handles[tunnel_id]

        try:
            subprocess.run(["pkill", "-f", f"wstunnel.*{tunnel_id}"], check=False, timeout=3)
        except Exception:
            pass

    def status(self, tunnel_id: str) -> Dict[str, Any]:
        is_running = False
        if tunnel_id in self.processes:
            is_running = self.processes[tunnel_id].poll() is None
        return {
            "active": is_running,
            "type": "wstunnel",
            "process_running": is_running,
        }


class GostAdapter:
    """GOST forwarding adapter - forwards from Iran node to Foreign server"""
    name = "gost"
    
    def __init__(self):
        self.config_dir = Path("/etc/smite-node/gost")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.log_handles = {}
    
    def _resolve_binary_path(self) -> Path:
        """Resolve gost binary path"""
        env_path = os.environ.get("GOST_BINARY")
        if env_path:
            resolved = Path(env_path)
            if resolved.exists() and resolved.is_file():
                return resolved
        
        common_paths = [
            Path("/usr/local/bin/gost"),
            Path("/usr/bin/gost"),
        ]
        
        for path in common_paths:
            if path.exists() and path.is_file():
                return path
        
        resolved = shutil.which("gost")
        if resolved:
            return Path(resolved)
        
        raise FileNotFoundError(
            "GOST binary not found. Expected at GOST_BINARY, '/usr/local/bin/gost', or in PATH."
        )
    
    def apply(self, tunnel_id: str, spec: Dict[str, Any]):
        """Apply GOST forwarding - Iran node forwards to Foreign server"""
        if tunnel_id in self.processes:
            logger.info(f"GOST tunnel {tunnel_id} already exists, removing it first")
            self.remove(tunnel_id)
        
        ports = spec.get('ports', [])
        if not ports:
            listen_port = spec.get('listen_port') or spec.get('remote_port')
            if listen_port:
                ports = [int(listen_port) if isinstance(listen_port, (int, str)) and str(listen_port).isdigit() else listen_port]
        
        forward_to = spec.get('forward_to')
        remote_ip = spec.get('remote_ip', '127.0.0.1')
        
        if not ports:
            raise ValueError("GOST requires 'ports' array or 'listen_port'/'remote_port' in spec")
        if not forward_to and not remote_ip:
            raise ValueError("GOST requires 'forward_to' or 'remote_ip' in spec")

        # Prefer GRE inner IP when panel didn't already resolve it but public+GRE exist.
        public_ip = (spec.get("public_ip") or spec.get("foreign_public_ip") or "").strip()
        if public_ip and remote_ip in (public_ip, "", None):
            try:
                from app.network_optimize import pick_gre_forward_ip
                gre = pick_gre_forward_ip(public_ip)
                if gre and gre.get("peer_inner"):
                    remote_ip = gre["peer_inner"]
                    spec = {**spec, "remote_ip": remote_ip, "forward_via": "gre",
                            "forward_mtu": gre.get("mtu"), "forward_iface": gre.get("iface")}
                    logger.info(
                        f"GOST {tunnel_id}: using GRE peer {remote_ip} "
                        f"(iface={gre.get('iface')}, mtu={gre.get('mtu')}) for {public_ip}"
                    )
            except Exception as e:
                logger.warning(f"GOST GRE prefer skipped: {e}")
        
        tunnel_type = spec.get('type', 'tcp').lower()
        use_ipv6 = spec.get('use_ipv6', False)

        # MSS/MTU clamp so large HTTPS (X/Twitter etc.) works through the proxy path.
        try:
            from app.network_optimize import ensure_mtu_optimizations, mss_for_mtu
            path_mtu = spec.get("forward_mtu") or spec.get("mss_mtu") or (1472 if spec.get("forward_via") == "gre" else 1500)
            listen_ports = []
            for p in ports:
                try:
                    listen_ports.append(int(p) if not isinstance(p, int) else p)
                except Exception:
                    pass
            ensure_mtu_optimizations(
                listen_ports=listen_ports,
                path_mtu=int(path_mtu),
                mss=spec.get("tcp_mss") or mss_for_mtu(int(path_mtu)),
            )
        except Exception as e:
            logger.warning(f"GOST MTU optimize skipped: {e}")
        
        binary_path = self._resolve_binary_path()
        cmd = [str(binary_path)]
        
        for port in ports:
            port_num = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else port
            
            if forward_to:
                forward_host, forward_port, forward_is_ipv6 = parse_address_port(forward_to)
                if forward_port is None:
                    forward_port = port_num
            else:
                forward_host = remote_ip
                forward_port = port_num
                forward_is_ipv6 = use_ipv6
            
            if forward_is_ipv6:
                target_addr = f"[{forward_host}]:{forward_port}"
            else:
                target_addr = f"{forward_host}:{forward_port}"
            
            if use_ipv6:
                listen_addr = f"[::]:{port_num}"
            else:
                listen_addr = f"0.0.0.0:{port_num}"
            
            if tunnel_type == "tcp":
                cmd.append(f"-L=tcp://{listen_addr}/{target_addr}")
            elif tunnel_type == "udp":
                cmd.append(f"-L=udp://{listen_addr}/{target_addr}")
            elif tunnel_type == "ws":
                import socket
                try:
                    if use_ipv6:
                        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                        s.connect(("2001:4860:4860::8888", 80))
                        bind_ip = s.getsockname()[0]
                    else:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.connect(("8.8.8.8", 80))
                        bind_ip = s.getsockname()[0]
                    s.close()
                except Exception:
                    bind_ip = "[::]" if use_ipv6 else "0.0.0.0"
                cmd.append(f"-L=ws://{bind_ip}:{port_num}/tcp://{target_addr}")
            elif tunnel_type == "grpc":
                cmd.append(f"-L=grpc://{listen_addr}/{target_addr}")
            elif tunnel_type == "tcpmux":
                cmd.append(f"-L=tcpmux://{listen_addr}/{target_addr}")
            else:
                raise ValueError(f"Unsupported GOST tunnel type: {tunnel_type}")
        
        log_file = self.config_dir / f"{tunnel_id}.log"
        log_f = open(log_file, 'w', buffering=1)
        try:
            log_f.write(f"Starting GOST forwarding for tunnel {tunnel_id}\n")
            log_f.write(f"Command: {' '.join(cmd)}\n")
            log_f.write(f"Forwarding: {tunnel_type}://{listen_addr} -> {target_addr}\n")
            log_f.flush()
            
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=str(self.config_dir),
                start_new_session=True,
                close_fds=False
            )
        except Exception as e:
            log_f.close()
            raise RuntimeError(f"Failed to start GOST: {e}")
        
        self.log_handles[tunnel_id] = log_f
        self.processes[tunnel_id] = proc
        
        time.sleep(1.5)
        if proc.poll() is not None:
            stderr = ""
            if log_file.exists():
                with open(log_file, 'r') as f:
                    stderr = f.read()
            if tunnel_id in self.log_handles:
                try:
                    self.log_handles[tunnel_id].close()
                except:
                    pass
                del self.log_handles[tunnel_id]
            raise RuntimeError(f"GOST failed to start: {stderr[-500:] if len(stderr) > 500 else stderr}")
        
        logger.info(f"GOST forwarding started for tunnel {tunnel_id}: {tunnel_type}://{listen_addr} -> {target_addr}")
    
    def remove(self, tunnel_id: str):
        """Remove GOST tunnel"""
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except:
                pass
            del self.processes[tunnel_id]
        
        if tunnel_id in self.log_handles:
            try:
                self.log_handles[tunnel_id].close()
            except:
                pass
            del self.log_handles[tunnel_id]
        
        try:
            subprocess.run(["pkill", "-f", f"gost.*{tunnel_id}"], check=False, timeout=3, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except:
            pass
    
    def status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get status"""
        is_running = False
        
        if tunnel_id in self.processes:
            proc = self.processes[tunnel_id]
            is_running = proc.poll() is None
        
        return {
            "active": is_running,
            "type": "gost",
            "process_running": is_running
        }


class AdapterManager:
    """Manager for core adapters"""
    
    def __init__(self):
        self.adapters: Dict[str, CoreAdapter] = {
            "rathole": RatholeAdapter(),
            "backhaul": BackhaulAdapter(),
            "chisel": ChiselAdapter(),
            "frp": FrpAdapter(),
            "wstunnel": WstunnelAdapter(),
            "bore": BoreAdapter(),
            "gost": GostAdapter(),
        }
        self.active_tunnels: Dict[str, CoreAdapter] = {}
        self.config_dir = Path("/var/lib/smite-node")
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Tunnel persistence directory: {self.config_dir} (exists: {self.config_dir.exists()}, writable: {self.config_dir.is_dir()})")
        except Exception as e:
            logger.error(f"Failed to create tunnel persistence directory {self.config_dir}: {e}")
            raise
        self.tunnels_file = self.config_dir / "tunnels.json"
        self.tunnel_configs: Dict[str, Dict[str, Any]] = {}
        logger.info(f"Tunnel persistence file: {self.tunnels_file}")
    
    def get_adapter(self, tunnel_core: str) -> Optional[CoreAdapter]:
        """Get adapter for tunnel core"""
        return self.adapters.get(tunnel_core)
    
    def _load_tunnels(self):
        """Load persisted tunnel configurations"""
        import json
        if self.tunnels_file.exists():
            try:
                file_size = self.tunnels_file.stat().st_size
                logger.info(f"Found tunnel config file at {self.tunnels_file} (size: {file_size} bytes)")
                
                if file_size == 0:
                    logger.warning(f"Tunnel config file {self.tunnels_file} is empty")
                    self.tunnel_configs = {}
                    return
                
                with open(self.tunnels_file, 'r') as f:
                    content = f.read()
                    if not content.strip():
                        logger.warning(f"Tunnel config file {self.tunnels_file} contains only whitespace")
                        self.tunnel_configs = {}
                        return
                    
                    self.tunnel_configs = json.loads(content)
                
                logger.info(f"Loaded {len(self.tunnel_configs)} persisted tunnel configurations from {self.tunnels_file}")
                for tunnel_id, config in self.tunnel_configs.items():
                    core = config.get("core", "unknown")
                    mode = config.get("spec", {}).get("mode", "N/A")
                    logger.info(f"  - Tunnel {tunnel_id}: core={core}, mode={mode}")
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse tunnel configurations JSON from {self.tunnels_file}: {e}", exc_info=True)
                self.tunnel_configs = {}
            except Exception as e:
                logger.error(f"Failed to load tunnel configurations from {self.tunnels_file}: {e}", exc_info=True)
                self.tunnel_configs = {}
        else:
            logger.info(f"No tunnel configurations file found at {self.tunnels_file} (this is normal for new nodes)")
            self.tunnel_configs = {}
    
    def _save_tunnels(self):
        """Save tunnel configurations to disk"""
        import json
        import os
        try:
            logger.info(f"Saving {len(self.tunnel_configs)} tunnel configurations to {self.tunnels_file}")
            
            temp_file = self.tunnels_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(self.tunnel_configs, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            
            temp_file.replace(self.tunnels_file)
            
            if self.tunnels_file.exists():
                file_size = self.tunnels_file.stat().st_size
                logger.info(f"Successfully saved tunnel configurations to {self.tunnels_file} (size: {file_size} bytes, tunnels: {list(self.tunnel_configs.keys())})")
            else:
                logger.error(f"File {self.tunnels_file} was not created after write operation")
        except Exception as e:
            logger.error(f"Failed to save tunnel configurations to {self.tunnels_file}: {e}", exc_info=True)
    
    async def restore_tunnels(self):
        """Restore all persisted tunnels on startup"""
        import logging
        logger = logging.getLogger(__name__)
        
        logger.info(f"Starting tunnel restoration from {self.tunnels_file}")
        logger.info(f"Config directory exists: {self.config_dir.exists()}, writable: {os.access(self.config_dir, os.W_OK) if self.config_dir.exists() else False}")
        logger.info(f"Tunnels file exists: {self.tunnels_file.exists()}")
        
        self._load_tunnels()
        
        if not self.tunnel_configs:
            logger.info("No persisted tunnels to restore")
            return
        
        logger.info(f"Restoring {len(self.tunnel_configs)} persisted tunnels...")
        restored = 0
        failed = 0
        
        for tunnel_id, config in self.tunnel_configs.items():
            try:
                tunnel_core = config.get("core")
                spec = config.get("spec", {})
                
                if not tunnel_core:
                    logger.warning(f"Tunnel {tunnel_id}: Missing core, skipping")
                    failed += 1
                    continue
                
                if not spec:
                    logger.warning(f"Tunnel {tunnel_id}: Empty spec, skipping")
                    failed += 1
                    continue
                
                adapter = self.get_adapter(tunnel_core)
                if not adapter:
                    logger.warning(f"Tunnel {tunnel_id}: Unknown core {tunnel_core}, skipping")
                    failed += 1
                    continue
                
                mode = spec.get('mode', 'N/A')
                logger.info(f"Restoring tunnel {tunnel_id}: core={tunnel_core}, mode={mode}, spec_keys={list(spec.keys())}")
                
                if tunnel_core in ["rathole", "backhaul", "chisel", "frp", "wstunnel", "bore"] and mode == 'N/A':
                    logger.warning(f"Tunnel {tunnel_id}: Reverse tunnel missing mode field, defaulting to client")
                    spec['mode'] = 'client'
                
                try:
                    adapter.apply(tunnel_id, spec)
                    self.active_tunnels[tunnel_id] = adapter
                    restored += 1
                    logger.info(f"Successfully restored tunnel {tunnel_id} (core={tunnel_core}, mode={spec.get('mode', 'N/A')})")
                except Exception as apply_error:
                    logger.error(f"Failed to apply tunnel {tunnel_id} during restoration: {apply_error}", exc_info=True)
                    failed += 1
            except Exception as e:
                logger.error(f"Failed to restore tunnel {tunnel_id}: {e}", exc_info=True)
                failed += 1
        
        logger.info(f"Tunnel restoration completed: {restored} restored, {failed} failed")
    
    async def apply_tunnel(self, tunnel_id: str, tunnel_core: str, spec: Dict[str, Any]):
        """Apply tunnel using appropriate adapter"""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Applying tunnel {tunnel_id}: core={tunnel_core}")
        
        if tunnel_id in self.active_tunnels:
            logger.info(f"Tunnel {tunnel_id} already exists, removing it first")
            await self.remove_tunnel(tunnel_id)
        
        adapter = self.get_adapter(tunnel_core)
        if not adapter:
            error_msg = f"Unknown tunnel core: {tunnel_core}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.info(f"Using adapter: {adapter.name}, mode={spec.get('mode', 'N/A')}")

        # Apply MSS clamp for any tunnel that exposes listen/remote ports.
        try:
            from app.network_optimize import ensure_mtu_optimizations, mss_for_mtu
            port_candidates = []
            for key in ("ports",):
                raw = spec.get(key) or []
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            for k in ("local", "remote", "listen_port", "public_port"):
                                if item.get(k) is not None:
                                    port_candidates.append(item.get(k))
                        else:
                            port_candidates.append(item)
            for key in ("listen_port", "remote_port", "reverse_port", "public_port", "bind_port", "control_port"):
                if spec.get(key) is not None:
                    port_candidates.append(spec.get(key))
            listen_ports = []
            for p in port_candidates:
                try:
                    listen_ports.append(int(p))
                except Exception:
                    pass
            path_mtu = spec.get("forward_mtu") or (1472 if spec.get("forward_via") == "gre" else None)
            if listen_ports or path_mtu:
                ensure_mtu_optimizations(
                    listen_ports=sorted(set(listen_ports)),
                    path_mtu=int(path_mtu) if path_mtu else None,
                    mss=spec.get("tcp_mss") or (mss_for_mtu(int(path_mtu)) if path_mtu else None),
                )
        except Exception as e:
            logger.debug(f"MTU optimize for {tunnel_id} skipped: {e}")

        # Auto-open tunnel ports in the host firewall (iptables + UFW).
        # Idempotent and never raises; without this a strict firewall
        # (e.g. UFW default-deny) drops tunnel traffic after apply.
        try:
            from app.network_optimize import ensure_firewall_ports
            ensure_firewall_ports(spec)
        except Exception as e:
            logger.debug(f"Firewall auto-open for {tunnel_id} skipped: {e}")

        adapter.apply(tunnel_id, spec)
        self.active_tunnels[tunnel_id] = adapter
        
        self.tunnel_configs[tunnel_id] = {
            "core": tunnel_core,
            "spec": spec.copy()
        }
        logger.info(f"Saving tunnel {tunnel_id} to persistent storage (core={tunnel_core}, mode={spec.get('mode', 'N/A')})")
        self._save_tunnels()
        logger.info(f"Tunnel {tunnel_id} applied and saved successfully (core={tunnel_core}, mode={spec.get('mode', 'N/A')}, total_saved={len(self.tunnel_configs)})")
    
    async def remove_tunnel(self, tunnel_id: str):
        """Remove tunnel"""
        if tunnel_id in self.active_tunnels:
            adapter = self.active_tunnels[tunnel_id]
            adapter.remove(tunnel_id)
            del self.active_tunnels[tunnel_id]
        
        if tunnel_id in self.tunnel_configs:
            del self.tunnel_configs[tunnel_id]
            self._save_tunnels()
    
    async def get_tunnel_status(self, tunnel_id: str) -> Dict[str, Any]:
        """Get tunnel status"""
        if tunnel_id in self.active_tunnels:
            adapter = self.active_tunnels[tunnel_id]
            return adapter.status(tunnel_id)
        return {"active": False}
    
    async def cleanup(self):
        """Cleanup all tunnels"""
        for tunnel_id in list(self.active_tunnels.keys()):
            await self.remove_tunnel(tunnel_id)

