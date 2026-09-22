"""Surfshark cluster resolution against the current clusters API."""

from __future__ import annotations

from unittest.mock import patch

import pytest

try:
    from unshackle.core.proxies.resolve import provider_block, provider_matches
    from unshackle.core.proxies.surfsharkvpn import SurfsharkVPN, _hostname
except ModuleNotFoundError:  # incomplete sandbox install; load files without proxies/__init__
    import importlib.util
    import sys
    import types
    from pathlib import Path

    proxies = Path(__file__).resolve().parents[2] / "unshackle" / "core" / "proxies"
    pkg = sys.modules.get("unshackle.core.proxies")
    if pkg is None or not getattr(pkg, "__path__", None):
        pkg = types.ModuleType("unshackle.core.proxies")
        pkg.__path__ = [str(proxies)]
        sys.modules["unshackle.core.proxies"] = pkg

    def _load(name: str):
        full = f"unshackle.core.proxies.{name}"
        if full in sys.modules and hasattr(sys.modules[full], "__file__"):
            return sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, proxies / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    _load("proxy")
    _ss = _load("surfsharkvpn")
    _rs = _load("resolve")
    SurfsharkVPN = _ss.SurfsharkVPN
    _hostname = _ss._hostname
    provider_block = _rs.provider_block
    provider_matches = _rs.provider_matches

CLUSTERS = [
    {
        "country": "United States",
        "countryCode": "US",
        "location": "Dallas",
        "city": "Dallas",
        "connectionName": "us-dal.prod.surfshark.com",
    },
    {
        "country": "United States",
        "countryCode": "US",
        "location": "Boston",
        "city": "Boston",
        "connectionName": "us-bos.prod.surfshark.com",
    },
    {
        "country": "India",
        "countryCode": "IN",
        "location": "Mumbai",
        "city": "Mumbai",
        "connectionName": "in-mum.prod.surfshark.com",
    },
    {
        "country": "United Kingdom",
        "countryCode": "GB",
        "location": "London",
        "city": "London",
        "connectionName": "uk-lon.prod.surfshark.com",
    },
]


@pytest.fixture
def provider():
    with patch.object(SurfsharkVPN, "get_countries", return_value=CLUSTERS):
        yield SurfsharkVPN("svcuser", "svcpass")


def test_hostname_from_slug_and_full_host():
    assert _hostname("us-dal") == "us-dal.prod.surfshark.com"
    assert _hostname("us-dal.prod.surfshark.com") == "us-dal.prod.surfshark.com"
    assert _hostname("uk-lon-st003") == "uk-lon-st003.prod.surfshark.com"
    assert _hostname(1234) is None
    assert _hostname("1234") is None


def test_yaml_ints_and_extra_keys_are_accepted():
    with patch.object(SurfsharkVPN, "get_countries", return_value=CLUSTERS):
        p = SurfsharkVPN(username=12345, password=67890, enabled=True, servers={"us": "us-dal"})
    assert p.username == "12345"
    assert p.password == "67890"
    uri = p.get_proxy("us")
    assert uri.endswith("@us-dal.prod.surfshark.com:443")


def test_email_login_is_rejected():
    with pytest.raises(ValueError, match="Service Credentials"):
        SurfsharkVPN("user@example.com", "hunter2")


def test_country_query_uses_connection_name(provider):
    uri = provider.get_proxy("in")
    assert uri == "https://svcuser:svcpass@in-mum.prod.surfshark.com:443"


def test_slug_query_skips_api_lookup(provider):
    uri = provider.get_proxy("us-bos")
    assert uri.endswith("@us-bos.prod.surfshark.com:443")


def test_city_query(provider):
    uri = provider.get_proxy("us:boston")
    assert uri.endswith("@us-bos.prod.surfshark.com:443")


def test_uk_alias_uses_gb_clusters(provider):
    uri = provider.get_proxy("uk")
    assert uri.endswith("@uk-lon.prod.surfshark.com:443")
    assert provider.get_proxy("gb").endswith("@uk-lon.prod.surfshark.com:443")


def test_numeric_server_map_is_ignored(provider):
    provider.server_map = {"us": 1234}
    uri = provider.get_proxy("us")
    assert "1234" not in uri
    assert uri.endswith(".prod.surfshark.com:443")


def test_server_map_pin(provider):
    provider.server_map = {"us": "us-dal"}
    assert provider.get_proxy("us").endswith("@us-dal.prod.surfshark.com:443")


def test_provider_block_alias_and_match():
    cfg = {"Surfshark": {"username": "a", "password": "b"}}
    assert provider_block(cfg, "surfsharkvpn", "surfshark") == {"username": "a", "password": "b"}

    stub = type("SurfsharkVPN", (), {})()
    assert provider_matches(stub, "surfshark")
    assert provider_matches(stub, "surfsharkvpn")
    assert not provider_matches(stub, "nordvpn")
