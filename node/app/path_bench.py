"""Short TCP throughput checks between Smite nodes.

Temporary listeners and processes are removed when the check finishes.
Ports used by the live tunnels are refused.
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
DENY_PORTS = {22, 53, 80, 443, 8000, 8080, 8888, 23534, 7000, 7835}
_lock = threading.Lock()
_servers: Dict[int, Dict[str, Any]] = {}
_procs: Dict[str, subprocess.Popen] = {}


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed during transfer")
        buf += chunk
    return bytes(buf)


def _handle_client(conn: socket.socket) -> None:
    try:
        header = _read_exact(conn, 8)
        cmd, size = struct.unpack("!4sI", header)
        if size <= 0 or size > MAX_BYTES:
            return
        if cmd == b"DOWN":
            left = size
            block = b"S" * 65536
            while left:
                piece = block if left >= len(block) else block[:left]
                conn.sendall(piece)
                left -= len(piece)
        elif cmd == b"UPLD":
            _read_exact(conn, size)
            conn.sendall(struct.pack("!I", size))
    except Exception as exc:
        logger.debug("bench client ended: %s", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve(port: int, stop: threading.Event, sock: socket.socket) -> None:
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, _addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        conn.settimeout(30)
        worker = threading.Thread(target=_handle_client, args=(conn,), daemon=True)
        worker.start()
    try:
        sock.close()
    except Exception:
        pass


def start_listener(port: int) -> Dict[str, Any]:
    port = int(port)
    if port in DENY_PORTS or port < 1024 or port > 65535:
        return {"ok": False, "error": f"Port {port} is reserved and was not used"}
    with _lock:
        if port in _servers:
            return {"ok": True, "port": port, "already": True}
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            sock.listen(8)
        except OSError as exc:
            sock.close()
            return {"ok": False, "error": f"Port {port} is busy ({exc})"}
        stop = threading.Event()
        thread = threading.Thread(target=_serve, args=(port, stop, sock), daemon=True)
        thread.start()
        _servers[port] = {"stop": stop, "thread": thread}
    return {"ok": True, "port": port}


def stop_listener(port: int) -> Dict[str, Any]:
    with _lock:
        item = _servers.pop(int(port), None)
    if not item:
        return {"ok": True, "stopped": False}
    item["stop"].set()
    return {"ok": True, "stopped": True}


def stop_all() -> None:
    with _lock:
        ports = list(_servers)
        procs = list(_procs.items())
        _procs.clear()
    for port in ports:
        stop_listener(port)
    for _name, proc in procs:
        _kill(proc)


def _mbps(nbytes: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return round((nbytes * 8) / seconds / 1_000_000, 2)


def measure(host: str, port: int, nbytes: int) -> Dict[str, Any]:
    host = (host or "").strip()
    port = int(port)
    nbytes = max(256 * 1024, min(int(nbytes or (2 * 1024 * 1024)), MAX_BYTES))
    if not host:
        return {"ok": False, "error": "Target host is empty"}

    def once(cmd: bytes) -> float:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(20)
        try:
            sock.connect((host, port))
            started = time.monotonic()
            sock.sendall(struct.pack("!4sI", cmd, nbytes))
            if cmd == b"DOWN":
                _read_exact(sock, nbytes)
            else:
                sock.sendall(b"U" * nbytes)
                ack = _read_exact(sock, 4)
                got = struct.unpack("!I", ack)[0]
                if got != nbytes:
                    raise ConnectionError(f"upload ack {got} != {nbytes}")
            return time.monotonic() - started
        finally:
            sock.close()

    try:
        down_s = once(b"DOWN")
        up_s = once(b"UPLD")
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)}
    return {
        "ok": True,
        "host": host,
        "port": port,
        "bytes": nbytes,
        "download_mbps": _mbps(nbytes, down_s),
        "upload_mbps": _mbps(nbytes, up_s),
        "download_seconds": round(down_s, 3),
        "upload_seconds": round(up_s, 3),
    }


def _kill(proc: Optional[subprocess.Popen]) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def measure_gost(listen_port: int, target_host: str, target_port: int, nbytes: int) -> Dict[str, Any]:
    listen_port = int(listen_port)
    if listen_port in DENY_PORTS:
        return {"ok": False, "error": f"Refusing to bind GOST on reserved port {listen_port}"}
    binary = "/usr/local/bin/gost"
    if not os.path.isfile(binary):
        return {"ok": False, "error": "gost binary is not installed on this node"}
    cmd = [binary, "-L", f"tcp://127.0.0.1:{listen_port}/{target_host}:{int(target_port)}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _lock:
        _procs["gost"] = proc
    try:
        time.sleep(0.4)
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[:300]
            return {"ok": False, "error": f"gost exited: {err or proc.returncode}"}
        result = measure("127.0.0.1", listen_port, nbytes)
        result["via"] = f"{target_host}:{target_port}"
        return result
    finally:
        _kill(proc)
        with _lock:
            _procs.pop("gost", None)


def start_frps(bind_port: int, token: str) -> Dict[str, Any]:
    bind_port = int(bind_port)
    if bind_port in DENY_PORTS:
        return {"ok": False, "error": f"Refusing FRP bind on reserved port {bind_port}"}
    binary = "/usr/local/bin/frps"
    if not os.path.isfile(binary):
        return {"ok": False, "error": "frps is not installed on this node"}
    path = Path("/tmp/smite-bench-frps.toml")
    path.write_text(f'bindPort = {bind_port}\nauth.token = "{token}"\n', encoding="utf-8")
    proc = subprocess.Popen([binary, "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _lock:
        _procs["frps"] = proc
    time.sleep(0.4)
    if proc.poll() is not None:
        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[:300]
        return {"ok": False, "error": f"frps exited: {err or proc.returncode}"}
    return {"ok": True, "bind_port": bind_port}


def start_frpc(server_addr: str, server_port: int, token: str, local_port: int, remote_port: int) -> Dict[str, Any]:
    binary = "/usr/local/bin/frpc"
    if not os.path.isfile(binary):
        return {"ok": False, "error": "frpc is not installed on this node"}
    path = Path("/tmp/smite-bench-frpc.toml")
    path.write_text(
        "\n".join([
            f'serverAddr = "{server_addr}"',
            f"serverPort = {int(server_port)}",
            f'auth.token = "{token}"',
            "",
            "[[proxies]]",
            'name = "smitebench"',
            'type = "tcp"',
            'localIP = "127.0.0.1"',
            f"localPort = {int(local_port)}",
            f"remotePort = {int(remote_port)}",
            "",
        ]),
        encoding="utf-8",
    )
    proc = subprocess.Popen([binary, "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _lock:
        _procs["frpc"] = proc
    time.sleep(0.8)
    if proc.poll() is not None:
        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[:300]
        return {"ok": False, "error": f"frpc exited: {err or proc.returncode}"}
    return {"ok": True}


def stop_named(name: str) -> None:
    with _lock:
        proc = _procs.pop(name, None)
    _kill(proc)
