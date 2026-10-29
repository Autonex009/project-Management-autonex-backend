"""Unit tests for Office Wi-Fi / IP-Restricted Check-in Feature."""
import ipaddress
import pytest
from starlette.requests import Request
from app.utils.client_ip import get_client_ip, parse_ip_whitelist, is_ip_allowed
from app.api.checkins import get_cached_checkin_ip_policy, invalidate_checkin_ip_cache, _checkin_ip_cache
from app.db.database import engine
from app.models.company_settings import CompanySetting


@pytest.fixture(autouse=True, scope="module")
def setup_company_settings_table():
    """Ensure company_settings table exists in fallback SQLite/test engine."""
    CompanySetting.__table__.create(bind=engine, checkfirst=True)
    yield


def _build_mock_request(headers=None, client_host="192.168.1.50"):
    """Build a mock Starlette Request with specified headers and client host."""
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/checkins",
        "headers": raw_headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_get_client_ip_header_precedence():
    # 1. CF-Connecting-IP takes precedence
    req1 = _build_mock_request(
        headers={
            "CF-Connecting-IP": "103.54.189.22",
            "X-Forwarded-For": "49.36.12.1, 10.0.0.1",
            "X-Real-IP": "49.36.12.1",
        }
    )
    assert get_client_ip(req1) == "103.54.189.22"

    # 2. X-Real-IP when no Cloudflare
    req2 = _build_mock_request(
        headers={
            "X-Real-IP": "103.54.189.22",
            "X-Forwarded-For": "103.54.189.22, 10.0.0.1",
        }
    )
    assert get_client_ip(req2) == "103.54.189.22"

    # 3. X-Forwarded-For leftmost hop
    req3 = _build_mock_request(
        headers={
            "X-Forwarded-For": " 103.54.189.22 , 10.0.0.1 ",
        }
    )
    assert get_client_ip(req3) == "103.54.189.22"

    # 4. Fallback to client.host
    req4 = _build_mock_request(headers={}, client_host="103.54.189.22")
    assert get_client_ip(req4) == "103.54.189.22"


def test_parse_ip_whitelist():
    raw_whitelist = """
    103.54.189.22
    10.0.0.0/24, 172.16.0.0/16
    malformed_entry_should_not_crash
    103.54.189.22/33
    """
    networks = parse_ip_whitelist(raw_whitelist)
    assert len(networks) == 3
    assert ipaddress.ip_network("103.54.189.22/32") in networks
    assert ipaddress.ip_network("10.0.0.0/24") in networks
    assert ipaddress.ip_network("172.16.0.0/16") in networks

    # Empty whitelist
    assert parse_ip_whitelist("") == []
    assert parse_ip_whitelist(None) == []


def test_is_ip_allowed_matching():
    networks = parse_ip_whitelist("103.54.189.22, 192.168.1.0/24")

    # Allowed exact match
    assert is_ip_allowed("103.54.189.22", networks) is True

    # Allowed CIDR subnet match
    assert is_ip_allowed("192.168.1.100", networks) is True

    # Blocked external IP
    assert is_ip_allowed("49.36.12.1", networks) is False
    assert is_ip_allowed("8.8.8.8", networks) is False

    # Strictly enforce whitelist: unwhitelisted loopback or external IPs are blocked
    assert is_ip_allowed("127.0.0.1", networks) is False
    assert is_ip_allowed("::1", networks) is False

    # Whitelisted loopback or IP with port is allowed
    dev_networks = parse_ip_whitelist("127.0.0.1, ::1")
    assert is_ip_allowed("127.0.0.1", dev_networks) is True
    assert is_ip_allowed("127.0.0.1:8000", dev_networks) is True
    assert is_ip_allowed("::1", dev_networks) is True
    assert is_ip_allowed("103.54.189.22:443", networks) is True

    # Fail-open when whitelist is empty
    assert is_ip_allowed("49.36.12.1", []) is True
    assert is_ip_allowed("127.0.0.1", []) is True

    # Malformed client IP does not crash
    assert is_ip_allowed("invalid.ip.str", networks) is False


def test_ram_cache_invalidation():
    invalidate_checkin_ip_cache()
    assert _checkin_ip_cache["expiry"] == 0.0
    assert _checkin_ip_cache["version"] == 0


def test_cache_immutability():
    """Verify that returned networks tuple is immutable and cannot corrupt shared RAM."""
    networks, is_enabled = get_cached_checkin_ip_policy()
    assert isinstance(networks, tuple)
    with pytest.raises(AttributeError):
        # Tuple has no append; cannot mutate in-memory state
        networks.append(ipaddress.ip_network("1.1.1.1/32"))


def test_concurrent_cache_access():
    """Test 20 threads simultaneously hitting get_cached_checkin_ip_policy (Thundering Herd test)."""
    import concurrent.futures

    invalidate_checkin_ip_cache()
    results = []

    def _worker():
        nets, enabled = get_cached_checkin_ip_policy()
        return len(nets), enabled

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(_worker) for _ in range(20)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    assert len(results) == 20
    first_res = results[0]
    for r in results:
        assert r == first_res, "All concurrent threads must receive identical consistent cache state"

