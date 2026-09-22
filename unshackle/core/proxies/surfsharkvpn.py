import json
import random
import re
from typing import Optional
from urllib.parse import quote

import requests

from unshackle.core.proxies.proxy import Proxy

CLUSTER_SUFFIX = ".prod.surfshark.com"
# Cluster slugs from the API, e.g. us-bos, in-del, uk-lon-st003.
SLUG_RE = re.compile(r"^[a-z]{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
COUNTRY_RE = re.compile(r"^[a-z]+$")
# Surfshark lists the UK as GB; hostnames still use uk-lon, uk-man, …
CODE_ALIASES = {"uk": "gb", "gb": "gb"}


def _as_str(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _hostname(value: object) -> Optional[str]:
    """Turn a server_map pin or query into a cluster hostname.

    Accepts ``us-dal``, ``us-dal.prod.surfshark.com``, or a full host. Numeric
    IDs from the old NordVPN-style config are not valid Surfshark hosts.
    """
    text = _as_str(value).lower().rstrip(".")
    if not text or text.isdigit():
        return None
    if text.endswith(CLUSTER_SUFFIX):
        return text
    if SLUG_RE.match(text):
        return f"{text}{CLUSTER_SUFFIX}"
    return None


class SurfsharkVPN(Proxy):
    def __init__(
        self,
        username: str,
        password: str,
        server_map: Optional[dict] = None,
        servers: Optional[dict] = None,
        **_: object,
    ):
        """
        Proxy provider that uses SurfsharkVPN Service Credentials.

        These are Service Credentials from
        https://my.surfshark.com/vpn/manual-setup/main/openvpn
        not the email/password used to log in to the website.

        Same URI shape as unshackle-dl: ``https://user:pass@{connectionName}:443``.
        ``server_map`` (also accepted as ``servers``) pins a query to a cluster
        hostname, e.g. ``us: us-dal`` or ``us: us-dal.prod.surfshark.com``.
        Extra yaml keys are ignored so a leftover ``enabled:`` does not fail load.
        """
        username = _as_str(username)
        password = _as_str(password)
        if not username:
            raise ValueError("No Username was provided to the SurfsharkVPN Proxy Service.")
        if not password:
            raise ValueError("No Password was provided to the SurfsharkVPN Proxy Service.")
        if "@" in username:
            raise ValueError(
                "The Username and Password must be SurfsharkVPN Service Credentials, not your Login Credentials. "
                "The Service Credentials can be found here: https://my.surfshark.com/vpn/manual-setup/main/openvpn"
            )

        raw_map = server_map if server_map is not None else servers
        if raw_map is not None and not isinstance(raw_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping a region to a hostname, not '{raw_map!r}'.")
        self.username = username
        self.password = password
        self.server_map = {str(k).lower().strip(): v for k, v in (raw_map or {}).items()}
        self.last_host: Optional[str] = None
        self.last_name: Optional[str] = None
        self.last_city: Optional[str] = None

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(set(x.get("country") for x in self.countries if x.get("country")))
        servers = sum(1 for x in self.countries if x.get("connectionName"))

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def last_connection_display(self) -> Optional[str]:
        """Short ``(Country - City): slug`` so the console does not wrap the proxy URI."""
        if not self.last_host:
            return None
        host = self.last_host[: -len(CLUSTER_SUFFIX)] if self.last_host.endswith(CLUSTER_SUFFIX) else self.last_host
        label = self.last_name or ""
        if self.last_city:
            label = f"{label} - {self.last_city}" if label else self.last_city
        if label:
            return f"({label}): {host}"
        return f": {host}"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTPS proxy URI for a Surfshark cluster.

        Same forms as unshackle-dl:
        - Country code: ``us``, ``gb`` / ``uk``, ``in``
        - City: ``us:seattle``
        - Cluster slug or host: ``us-bos``, ``in-mum.prod.surfshark.com``
        """
        query = _as_str(query).lower()
        if not query:
            raise ValueError("The query provided is unsupported and unrecognized: ''")

        pinned = _hostname(self.server_map.get(query))
        if pinned:
            return self._uri(pinned)

        direct = _hostname(query)
        if direct:
            return self._uri(direct)

        city = None
        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip() or None

        code = CODE_ALIASES.get(query, query)
        pinned = _hostname(self.server_map.get(f"{code}:{city}" if city else code)) or (
            None if city else _hostname(self.server_map.get(query))
        )
        if pinned:
            return self._uri(pinned)

        if not COUNTRY_RE.match(code):
            raise ValueError(f"The query provided is unsupported and unrecognized: {query}")

        country = self.get_country(by_code=code)
        if not country:
            return None

        hostname = self.get_random_server(country["countryCode"], city)
        if not hostname:
            raise ValueError(
                f"The SurfsharkVPN Country {query} currently has no random servers. "
                "Try again later. If the issue persists, double-check the query."
            )
        return self._uri(hostname)

    def _remember(self, hostname: str) -> None:
        self.last_host = hostname
        cluster = self._cluster(hostname)
        self.last_name = (cluster or {}).get("country") or None
        self.last_city = (cluster or {}).get("location") or (cluster or {}).get("city") or None

    def _uri(self, hostname: str) -> str:
        self._remember(hostname)
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"https://{user}:{password}@{hostname}:443"

    def _cluster(self, hostname: str) -> Optional[dict]:
        wanted = hostname.lower()
        for cluster in self.countries:
            if str(cluster.get("connectionName") or "").lower() == wanted:
                return cluster
        return None

    def get_country(self, by_code: Optional[str] = None) -> Optional[dict]:
        """Find the first cluster for a country code (GB also matches UK)."""
        if not by_code:
            raise ValueError("At least one search query must be made.")
        wanted = CODE_ALIASES.get(by_code.lower(), by_code.lower())
        aliases = {wanted}
        if wanted == "gb":
            aliases.add("uk")
        for country in self.countries:
            code = str(country.get("countryCode") or "").lower()
            if code in aliases:
                return country
        return None

    def get_random_server(self, country_id: str, city: Optional[str] = None):
        """
        Get a random ``connectionName`` for a country, optionally filtered by city.

        Prefers ``type: generic`` clusters (the same pool the Surfshark app uses as
        HTTPS proxies). Dedicated ``static`` hostnames are a fallback.

        Args:
            country_id: The country code (e.g. ``US``, ``GB``)
            city: Optional city name to filter by (case-insensitive)
        """
        wanted = CODE_ALIASES.get(country_id.lower(), country_id.lower())
        aliases = {wanted}
        if wanted == "gb":
            aliases.add("uk")
        servers = [x for x in self.countries if str(x.get("countryCode") or "").lower() in aliases]

        if city:
            city_lower = city.lower()
            city_servers = [
                x
                for x in servers
                if x.get("location", "").lower() == city_lower or x.get("city", "").lower() == city_lower
            ]
            if city_servers:
                servers = city_servers
            else:
                raise ValueError(
                    f"No servers found in city '{city}' for country '{country_id}'. "
                    "Try a different city or check the city name spelling."
                )

        if not servers:
            raise ValueError(f"Could not get random server for country '{country_id}': no servers found.")

        generic = [x for x in servers if str(x.get("type") or "generic").lower() == "generic"]
        pool = generic or servers
        connection_names = [x["connectionName"] for x in pool if x.get("connectionName")]
        if not connection_names:
            raise ValueError(
                f"Could not get random server for country '{country_id}': no servers with connectionName found."
            )

        return random.choice(connection_names)

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available clusters and their metadata."""
        res = requests.get(
            url="https://api.surfshark.com/v3/server/clusters/all",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of SurfsharkVPN countries [{res.status_code}]")

        try:
            data = res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of SurfsharkVPN countries, not JSON data.")
        if not isinstance(data, list):
            raise ValueError("Could not decode list of SurfsharkVPN countries, not a JSON list.")
        return data
