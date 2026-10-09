"""Chisel server management for panel"""
import os
import subprocess
import time
import logging
from pathlib import Path
from typing import Dict, Optional

from app.utils import parse_address_port, format_address_port, normalize_chisel_auth

logger = logging.getLogger(__name__)


class ChiselServerManager:
    """Manages Chisel server processes on the panel"""
    
    def __init__(self):
        self.config_dir = Path("/app/data/chisel")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.active_servers: Dict[str, subprocess.Popen] = {}
        self.server_configs: Dict[str, dict] = {}
    
    def start_server(self, tunnel_id: str, server_port: int, auth: Optional[str] = None, fingerprint: Optional[str] = None, use_ipv6: bool = False) -> bool:
        """
        Start a Chisel server for a tunnel
        
        Args:
            tunnel_id: Unique tunnel identifier
            server_port: Port where server listens for client connections (e.g., 8080)
            auth: Optional authentication string (user:pass)
            fingerprint: Optional server fingerprint for client verification
            use_ipv6: Whether to use IPv6 (default: False for IPv4)
        
        Returns:
            True if server started successfully, False otherwise
        """
        try:
            if tunnel_id in self.active_servers:
                logger.warning(f"Chisel server for tunnel {tunnel_id} already exists, stopping it first")
                self.stop_server(tunnel_id)
            
            host = "0.0.0.0"
            auth = normalize_chisel_auth(auth)
            
            cmd = [
                "/usr/local/bin/chisel",
                "server",
                "--host", host,
                "--port", str(server_port),
                "--reverse"
            ]
            
            if auth:
                cmd.extend(["--auth", auth])
            
            if fingerprint:
                cmd.extend(["--fingerprint", fingerprint])
            
            self.server_configs[tunnel_id] = {
                "server_port": server_port,
                "auth": auth,
                "fingerprint": fingerprint,
                "use_ipv6": use_ipv6
            }
            
            log_file = self.config_dir / f"chisel_{tunnel_id}.log"
            log_f = open(log_file, 'w', buffering=1)
            try:
                log_f.write(f"Starting chisel server for tunnel {tunnel_id}\n")
                log_f.write(f"Config: server_port={server_port}, auth={auth is not None}, fingerprint={fingerprint is not None}\n")
                log_f.write(f"Command: {' '.join(cmd)}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True
                )
            except FileNotFoundError:
                log_f.write(f"Starting chisel server (system binary) for tunnel {tunnel_id}\n")
                log_f.flush()
                proc = subprocess.Popen(
                    ["chisel"] + cmd[1:],  # Use system binary
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config_dir),
                    start_new_session=True
                )
            
            self.active_servers[f"{tunnel_id}_log"] = log_f
            self.active_servers[tunnel_id] = proc
            
            time.sleep(1.0)
            if proc.poll() is not None:
                try:
                    if log_file.exists():
                        with open(log_file, 'r') as f:
                            error_output = f.read()
                    else:
                        error_output = "Log file not found"
                    error_msg = f"chisel server failed to start (exit code: {proc.poll()}): {error_output[-500:] if len(error_output) > 500 else error_output}"
                    logger.error(error_msg)
                except Exception as e:
                    error_msg = f"chisel server failed to start (exit code: {proc.poll()}), could not read log: {e}"
                    logger.error(error_msg)
                finally:
                    del self.active_servers[tunnel_id]
                    if f"{tunnel_id}_log" in self.active_servers:
                        try:
                            self.active_servers[f"{tunnel_id}_log"].close()
                        except:
                            pass
                        del self.active_servers[f"{tunnel_id}_log"]
                    if tunnel_id in self.server_configs:
                        del self.server_configs[tunnel_id]
                raise RuntimeError(error_msg)
            
            try:
                import socket
                max_retries = 3
                port_listening = False
                for attempt in range(max_retries):
                    time.sleep(0.5)  # Give it time to bind
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    result = sock.connect_ex(('127.0.0.1', server_port))
                    sock.close()
                    if result == 0:
                        port_listening = True
                        break
                
                if not port_listening:
                    if proc.poll() is not None:
                        error_msg = f"Chisel server process exited (code: {proc.poll()}) before port verification"
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)
                    else:
                        logger.warning(f"Chisel server port {server_port} not listening after {max_retries} attempts, but process is running. PID: {proc.pid}")
                else:
                    logger.info(f"Chisel server port {server_port} verified as listening")
            except Exception as e:
                if proc.poll() is not None:
                    error_msg = f"Chisel server process died during verification (code: {proc.poll()}): {e}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)
                logger.warning(f"Could not verify chisel server port is listening: {e}")
            
            logger.info(f"Started Chisel server for tunnel {tunnel_id} on port {server_port} (PID: {proc.pid})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start Chisel server for tunnel {tunnel_id}: {e}")
            raise
    
    def stop_server(self, tunnel_id: str):
        """Stop Chisel server for a tunnel"""
        if tunnel_id in self.active_servers:
            proc = self.active_servers[tunnel_id]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception as e:
                logger.warning(f"Error stopping Chisel server for tunnel {tunnel_id}: {e}")
            finally:
                del self.active_servers[tunnel_id]
                log_key = f"{tunnel_id}_log"
                if log_key in self.active_servers:
                    try:
                        self.active_servers[log_key].close()
                    except:
                        pass
                    del self.active_servers[log_key]
            
            logger.info(f"Stopped Chisel server for tunnel {tunnel_id}")
        
        if tunnel_id in self.server_configs:
            del self.server_configs[tunnel_id]
    
    def is_running(self, tunnel_id: str) -> bool:
        """Check if server is running for a tunnel"""
        if tunnel_id not in self.active_servers:
            return False
        proc = self.active_servers[tunnel_id]
        return proc.poll() is None
    
    def get_active_servers(self) -> list:
        """Get list of tunnel IDs with active servers"""
        active = []
        for tunnel_id, proc in list(self.active_servers.items()):
            # Skip log file handles (they have _log suffix)
            if tunnel_id.endswith("_log"):
                continue
            if isinstance(proc, subprocess.Popen):
                if proc.poll() is None:
                    active.append(tunnel_id)
                else:
                    # Process has exited, clean it up
                    del self.active_servers[tunnel_id]
                    log_key = f"{tunnel_id}_log"
                    if log_key in self.active_servers:
                        try:
                            self.active_servers[log_key].close()
                        except:
                            pass
                        del self.active_servers[log_key]
                    if tunnel_id in self.server_configs:
                        del self.server_configs[tunnel_id]
        return active
    
    def cleanup_all(self):
        """Stop all Chisel servers"""
        tunnel_ids = [tid for tid in self.active_servers.keys() if not tid.endswith("_log")]
        for tunnel_id in tunnel_ids:
            self.stop_server(tunnel_id)


chisel_server_manager = ChiselServerManager()

