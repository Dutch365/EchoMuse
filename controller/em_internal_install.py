"""
Who may call the internal wake-word install endpoint.

One add-on on the Home Assistant internal network, holding one shared
secret. Nothing else. The peer address is the TCP peer: a forwarding
header is not a peer. Ingress arrives from Supervisor's gateway and is
refused here even though that address sits inside the same subnet.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os

import em_ingressauth

HA_NET = ipaddress.ip_network("172.30.32.0/23")
SECRET_HEADER = "X-EM-Install-Secret"
SECRET_ENV = "INTERNAL_INSTALL_SECRET"


def peer_in_ha_net(remote: str | None) -> bool:
    """True when `remote` is an IPv4 address inside 172.30.32.0/23."""
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return isinstance(ip, ipaddress.IPv4Address) and ip in HA_NET


def via_ingress(remote: str | None, ingress_path: str | None) -> bool:
    """True when this request came through Home Assistant ingress."""
    if ingress_path and ingress_path.strip():
        return True
    return remote == em_ingressauth.INGRESS_GATEWAY_IP


def configured_secret() -> str:
    """The add-on option, or empty when the endpoint is disabled."""
    return os.environ.get(SECRET_ENV, "")


def secret_configured(value: str | None) -> bool:
    return bool((value or "").strip())


def secret_matches(presented: str, expected: str) -> bool:
    """Constant-time compare. Lengths differ without branching on the bytes."""
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)
