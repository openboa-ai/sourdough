"""Local Unix domain socket control plane for the resident CompanyDaemon."""

from .client import ControlPlaneClient
from .protocol import (
    CONTROL_PLANE_VERSION,
    WORK_TIMELINE_V1,
    WORK_TIMELINE_V2,
    IpcRefused,
    PeerCredentials,
    default_socket_path,
    extract_peer_credentials,
    verify_socket_path,
)
from .server import CompanyDaemon, DaemonInfo
from .web_bridge import ClientWebBridge, WebBridgeInfo

__all__ = [
    "CONTROL_PLANE_VERSION",
    "WORK_TIMELINE_V1",
    "WORK_TIMELINE_V2",
    "ClientWebBridge",
    "CompanyDaemon",
    "ControlPlaneClient",
    "DaemonInfo",
    "IpcRefused",
    "PeerCredentials",
    "WebBridgeInfo",
    "default_socket_path",
    "extract_peer_credentials",
    "verify_socket_path",
]
