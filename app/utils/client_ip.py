"""app/utils/client_ip.py — Secure client IP resolution & subnet validation."""
import ipaddress
import logging
from typing import Optional, List, Union
from fastapi import Request

logger = logging.getLogger(__name__)

def _clean_ip(ip_str: str) -> str:
    """Clean IP string by stripping whitespace, bracket encapsulation, and port suffixes."""
    if not ip_str:
        return ""
    ip_str = ip_str.strip()
    # IPv6 with port: [2001:db8::1]:8080 or [::1]
    if ip_str.startswith("["):
        end_idx = ip_str.find("]")
        if end_idx != -1:
            return ip_str[1:end_idx]
    # IPv4 with port: 192.168.1.1:8080 (single colon, no brackets)
    if ":" in ip_str and ip_str.count(":") == 1:
        return ip_str.split(":")[0].strip()
    return ip_str


def get_client_ip(request: Request) -> str:
    """Extract real client IP safely, accounting for reverse proxies."""
    # 1. Cloudflare edge header
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return _clean_ip(cf_ip)

    # 2. X-Real-IP (Set by reverse proxy)
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return _clean_ip(real_ip)

    # 3. X-Forwarded-For: Extract leftmost IP
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return _clean_ip(parts[0])

    # 4. Fallback to direct socket connection
    if request.client and request.client.host:
        return _clean_ip(request.client.host)

    return "0.0.0.0"


def parse_ip_whitelist(raw_text: Optional[str]) -> List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
    """Parse newline or comma-separated IP/CIDR strings into validated IP networks."""
    if not raw_text or not raw_text.strip():
        return []

    networks = []
    entries = [e.strip() for line in raw_text.splitlines() for e in line.split(",") if e.strip()]

    for entry in entries:
        cleaned_entry = _clean_ip(entry)
        if not cleaned_entry:
            continue
        try:
            # strict=False allows host bits set in CIDRs (e.g. 103.54.189.22/28)
            net = ipaddress.ip_network(cleaned_entry, strict=False)
            networks.append(net)
        except ValueError:
            logger.warning(f"Invalid IP/CIDR entry in checkin whitelist: {entry}")
            continue

    return networks


def is_ip_allowed(client_ip_str: str, allowed_networks: List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]) -> bool:
    """Check if client IP matches allowed networks."""
    if not allowed_networks:
        # Fail-Open: If no whitelist is configured, do not block employees
        return True

    cleaned_ip = _clean_ip(client_ip_str)
    try:
        client_ip = ipaddress.ip_address(cleaned_ip)
    except ValueError:
        logger.warning(f"Malformed client IP received: {client_ip_str}")
        return False

    # Validate against pre-compiled subnets
    for net in allowed_networks:
        try:
            if client_ip in net:
                return True
        except TypeError:
            continue

    return False
