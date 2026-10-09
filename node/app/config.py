"""Application configuration"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    node_api_port: int = 8888
    node_name: str = "node-1"
    node_role: str = "iran"  # "iran" or "foreign"
    
    panel_ca_path: str = "/etc/smite-node/ca.crt"
    panel_address: str = "panel.example.com:443"
    panel_api_port: int = 8000

    # Optional GRE peer (other server public IP). Auto-created on node start.
    gre_peer_ip: str = ""
    gre_local_ip: str = ""
    gre_local_inner: str = ""
    gre_peer_inner: str = ""
    gre_network: str = "172.17.1.0/30"
    gre_iface: str = "smite-gre"
    gre_mtu: int = 1472
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = Settings()

