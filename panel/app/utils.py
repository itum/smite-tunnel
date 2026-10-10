"""Utility functions for address parsing and validation"""
import ipaddress
import re
import secrets
import string
from typing import Tuple, Optional


def parse_address_port(address_str: str) -> Tuple[str, Optional[int], bool]:
    """
    Parse an address:port string, handling both IPv4 and IPv6 addresses.
    
    Supports formats:
    - IPv4: "127.0.0.1:8080" -> ("127.0.0.1", 8080, False)
    - IPv6: "[2001:db8::1]:8080" -> ("2001:db8::1", 8080, True)
    - IPv6: "2001:db8::1" -> ("2001:db8::1", None, True)
    - Hostname: "example.com:8080" -> ("example.com", 8080, False)
    
    Args:
        address_str: Address string in format "host:port" or "[ipv6]:port"
        
    Returns:
        Tuple of (host, port, is_ipv6) where port is None if not specified
    """
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


def format_address_port(host: str, port: Optional[int] = None) -> str:
    """
    Format host and port into address:port string, handling IPv6 addresses.
    
    Args:
        host: Host address (IPv4, IPv6, or hostname)
        port: Port number (optional)
        
    Returns:
        Formatted string: "host:port" or "[ipv6]:port" or "host"
    """
    if not host:
        return ""
    
    try:
        ipaddress.IPv6Address(host)
        if port is not None:
            return f"[{host}]:{port}"
        return host
    except (ValueError, ipaddress.AddressValueError):
        if port is not None:
            return f"{host}:{port}"
        return host


def is_valid_ip_address(address: str) -> bool:
    """
    Check if a string is a valid IP address (IPv4 or IPv6).
    
    Args:
        address: String to validate
        
    Returns:
        True if valid IP address, False otherwise
    """
    try:
        ipaddress.ip_address(address)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def is_valid_ipv6_address(address: str) -> bool:
    """
    Check if a string is a valid IPv6 address.
    
    Args:
        address: String to validate
        
    Returns:
        True if valid IPv6 address, False otherwise
    """
    try:
        ipaddress.IPv6Address(address)
        return True
    except (ValueError, ipaddress.AddressValueError):
        return False


def generate_token(length: int = 16) -> str:
    """
    Generate a random secure token.
    
    Args:
        length: Length of the token (default: 16)
        
    Returns:
        Random token string
    """
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def generate_chisel_auth(length: int = 16) -> str:
    """
    Generate a Chisel --auth value in required user:pass form.
    """
    return f"smite:{generate_token(length)}"


def normalize_chisel_auth(auth: Optional[str]) -> Optional[str]:
    """
    Ensure Chisel auth is user:pass. Legacy plain tokens become smite:<token>.
    """
    if not auth:
        return auth
    if ":" in auth:
        return auth
    return f"smite:{auth}"


def generate_wstunnel_secret(length: int = 24) -> str:
    """Generate a URL-path-safe secret for wstunnel --restrict-http-upgrade-path-prefix."""
    return generate_token(length)


def build_wstunnel_server_url(
    host: str,
    port: int,
    secret: str,
    use_tls: bool = False,
) -> str:
    """Build ws(s)://host:port/secret for the wstunnel client."""
    scheme = "wss" if use_tls else "ws"
    if is_valid_ipv6_address(host):
        return f"{scheme}://[{host}]:{port}/{secret}"
    return f"{scheme}://{host}:{port}/{secret}"


# Bore uses a fixed control port (not configurable in upstream CLI).
BORE_CONTROL_PORT = 7835

# Safe TCP MSS for proxied tunnels (GRE MTU 1472 → max MSS 1432; leave margin).
DEFAULT_TUNNEL_TCP_MSS = 1360


def generate_bore_secret(length: int = 24) -> str:
    """Generate authentication secret for bore server/client."""
    return generate_token(length)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def humanize_error(message: Optional[str]) -> Optional[str]:
    """Turn raw node/core logs into a short message the panel can show."""
    if not message:
        return message
    text = _ANSI_RE.sub("", str(message))
    text = text.replace("\x1b", "")
    text = re.sub(r"\s+", " ", text).strip()

    port = None
    bind = re.search(r"(?:0\.0\.0\.0|\[::\]|127\.0\.0\.1):(\d{2,5})", text)
    if bind:
        port = bind.group(1)

    busy = re.search(r"address already in use|address in use|os error 98|EADDRINUSE", text, re.I)
    if busy:
        if port:
            return (
                f"Port {port} is already in use. Another tunnel or process is listening on it. "
                "Delete the existing tunnel or choose a different port."
            )
        return (
            "This port is already in use. Delete the existing tunnel or choose a different port."
        )

    text = re.sub(r"thread 'main' \(.*?\) panicked at .*", "", text)
    text = re.sub(r"note: run with `RUST_BACKTRACE=1`.*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 420:
        text = text[:420].rstrip() + "…"
    return text or message


def mss_for_mtu(mtu: int, default: int = DEFAULT_TUNNEL_TCP_MSS) -> int:
    """Compute a safe TCP MSS from path MTU (IPv4+TCP headers = 40 bytes)."""
    try:
        mtu_i = int(mtu)
    except (TypeError, ValueError):
        return default
    return max(536, min(default, mtu_i - 40))


def resolve_gost_forward_target(
    foreign_public_ip: Optional[str],
    iran_gre_peers: Optional[list] = None,
    foreign_gre_peers: Optional[list] = None,
    iran_public_ip: Optional[str] = None,
    explicit_remote_ip: Optional[str] = None,
) -> dict:
    """
    Choose the best GOST forward target.

    Prefer GRE inner peer IP when a GRE tunnel exists between Iran and foreign
    nodes (more stable than the lossy public path). Falls back to public IP.
    """
    explicit = (explicit_remote_ip or "").strip() or None
    foreign_public = (foreign_public_ip or "").strip() or None
    iran_public = (iran_public_ip or "").strip() or None

    # Keep deliberate custom targets (not the foreign public IP).
    if explicit and explicit != foreign_public:
        # Still annotate if explicit already is a known GRE inner IP.
        for gre in iran_gre_peers or []:
            if explicit == gre.get("peer_inner"):
                return {
                    "remote_ip": explicit,
                    "via": "gre",
                    "iface": gre.get("iface"),
                    "mtu": gre.get("mtu") or 1472,
                    "mss": mss_for_mtu(gre.get("mtu") or 1472),
                    "public_ip": foreign_public,
                }
        return {
            "remote_ip": explicit,
            "via": "explicit",
            "mtu": 1500,
            "mss": DEFAULT_TUNNEL_TCP_MSS,
            "public_ip": foreign_public,
        }

    for gre in iran_gre_peers or []:
        if foreign_public and gre.get("remote") == foreign_public and gre.get("peer_inner"):
            mtu = gre.get("mtu") or 1472
            return {
                "remote_ip": gre["peer_inner"],
                "via": "gre",
                "iface": gre.get("iface"),
                "mtu": mtu,
                "mss": mss_for_mtu(mtu),
                "public_ip": foreign_public,
            }

    for gre in foreign_gre_peers or []:
        if iran_public and gre.get("remote") == iran_public and gre.get("local_inner"):
            mtu = gre.get("mtu") or 1472
            return {
                "remote_ip": gre["local_inner"],
                "via": "gre",
                "iface": gre.get("iface"),
                "mtu": mtu,
                "mss": mss_for_mtu(mtu),
                "public_ip": foreign_public,
            }

    target = explicit or foreign_public or "127.0.0.1"
    return {
        "remote_ip": target,
        "via": "public",
        "mtu": 1500,
        "mss": DEFAULT_TUNNEL_TCP_MSS,
        "public_ip": foreign_public,
    }


def resolve_iran_control_target(
    iran_public_ip: Optional[str],
    foreign_public_ip: Optional[str] = None,
    iran_gre_peers: Optional[list] = None,
    foreign_gre_peers: Optional[list] = None,
) -> dict:
    """
    Address foreign reverse-tunnel clients should use to reach Iran.

    Prefer GRE inner IP of the Iran side when a GRE tunnel exists between the
    nodes (foreign peer_inner → Iran, or Iran local_inner). Falls back to Iran public IP.
    """
    iran_public = (iran_public_ip or "").strip() or None
    foreign_public = (foreign_public_ip or "").strip() or None

    for gre in foreign_gre_peers or []:
        if iran_public and gre.get("remote") == iran_public and gre.get("peer_inner"):
            mtu = gre.get("mtu") or 1472
            return {
                "host": gre["peer_inner"],
                "via": "gre",
                "iface": gre.get("iface"),
                "mtu": mtu,
                "mss": mss_for_mtu(mtu),
                "public_ip": iran_public,
            }

    for gre in iran_gre_peers or []:
        if foreign_public and gre.get("remote") == foreign_public and gre.get("local_inner"):
            mtu = gre.get("mtu") or 1472
            return {
                "host": gre["local_inner"],
                "via": "gre",
                "iface": gre.get("iface"),
                "mtu": mtu,
                "mss": mss_for_mtu(mtu),
                "public_ip": iran_public,
            }

    return {
        "host": iran_public or "127.0.0.1",
        "via": "public",
        "mtu": 1500,
        "mss": DEFAULT_TUNNEL_TCP_MSS,
        "public_ip": iran_public,
    }


def iran_control_host_for_nodes(
    iran_metadata: Optional[dict] = None,
    foreign_metadata: Optional[dict] = None,
) -> str:
    """Host foreign reverse clients should dial for Iran control plane."""
    iran_md = iran_metadata or {}
    foreign_md = foreign_metadata or {}
    return resolve_iran_control_target(
        iran_public_ip=iran_md.get("ip_address"),
        foreign_public_ip=foreign_md.get("ip_address"),
        iran_gre_peers=iran_md.get("gre_peers") or [],
        foreign_gre_peers=foreign_md.get("gre_peers") or [],
    )["host"]

