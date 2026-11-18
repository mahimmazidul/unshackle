import json
import logging
import random
import re
from typing import Optional
from urllib.parse import quote

import requests
from unidecode import unidecode

from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import COUNTRY_CODE_ALIASES

log = logging.getLogger("proxies.surfsharkvpn")

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
        self.server_map: dict[str, str] = {}
        for region, server in (raw_map or {}).items():
            server = _as_str(server)
            if not server or server.isdigit():
                # A warning, not an error: one stale entry must not disable the whole proxy provider.
                log.warning(
                    "SurfsharkVPN: ignoring server_map entry '%s: %s'. Use a server name such as 'us-dal'. "
                    "SurfsharkVPN no longer uses numeric server IDs.",
                    region,
                    server,
                )
                continue
            country, sep, city = str(region).lower().strip().partition(":")
            self.server_map[CODE_ALIASES.get(country, country) + sep + city] = server

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(set(x.get("country") for x in self.countries if x.get("country")))
        servers = sum(1 for x in self.countries if x.get("connectionName"))

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

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

        direct = self._known_cluster(query)
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
            # A city or slug this provider does not serve: let the next provider try it.
            return None

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

    def _known_cluster(self, query: str) -> Optional[str]:
        """The full hostname of a cluster slug the API lists, else None.

        ``us-bos`` is a cluster; ``us-central-150`` and ``us150`` are not, so they fall through
        to the next proxy provider instead of being turned into a hostname that cannot resolve.
        """
        hostname = _hostname(query)
        if not hostname:
            return None
        if self._cluster(hostname) or self._cluster(hostname.split(".", 1)[0]):
            return hostname
        return None

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
            wanted = self.fold(city)
            located = [(self.fold(x.get("location") or x.get("city") or ""), x) for x in servers]
            city_servers = [x for folded, x in located if folded == wanted] or [
                x for folded, x in located if wanted and folded.startswith(wanted)
            ]
            if city_servers:
                servers = city_servers
            else:
                raise ValueError(
                    f"No servers found in city '{city}'. Try a different city or check the city name spelling."
                )

        if not servers:
            raise ValueError(f"Could not get random server for country '{country_id}': no servers found.")

        generic = [x for x in servers if str(x.get("type") or "generic").lower() == "generic"]
        pool = generic or servers
        connection_names = [x["connectionName"] for x in pool if x.get("connectionName")]
        # A cluster without a connectionName cannot be dialled; skip it rather than crash.
        if not connection_names:
            raise ValueError(
                f"Could not get random server for country '{country_id}': no servers with connectionName found."
            )

        return random.choice(connection_names)

    @staticmethod
    def fold(text: Optional[str]) -> str:
        """Reduce a place name to lowercase ASCII letters and digits."""
        return re.sub(r"[^a-z0-9]", "", unidecode(text or "").lower())

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
