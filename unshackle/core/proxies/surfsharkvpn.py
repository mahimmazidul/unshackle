import asyncio
import json
import logging
import random
import re
import socket
import ssl
import threading
from functools import cache, partial
from typing import Optional

import certifi
import requests
from urllib.parse import quote

from unshackle.core.proxies.asn import describe, fold, is_ip, mapped_query, select, split_selector
from unshackle.core.proxies.controld import build_query, parse_a_records
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.utilities import COUNTRY_CODE_ALIASES

log = logging.getLogger("proxies.surfsharkvpn")

CLUSTER_SUFFIX = ".prod.surfshark.com"
# Cluster slugs from the API, e.g. us-bos, in-del, uk-lon-st003.
SLUG_RE = re.compile(r"^[a-z]{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
COUNTRY_RE = re.compile(r"^[a-z]+$")


def _as_str(value: object) -> str:
    """YAML can hand us an int or a bool; a service credential is always text."""
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


POOL_QUERIES = 48
PORT = 443
CONNECT_TIMEOUT = 30
CHUNK = 65536
TLS = ssl.create_default_context(cafile=certifi.where())  # the same CA bundle as requests
PINS: dict[tuple[str, str], int] = {}
LOOP: Optional[asyncio.AbstractEventLoop] = None
STATE = threading.Lock()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes one way until the source closes, then close the sink."""
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def tunnel(ip: str, hostname: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Carry one client connection over TLS to the server at `ip`, with its certificate checked against `hostname`."""
    try:
        upstream = await asyncio.wait_for(
            asyncio.open_connection(ip, PORT, ssl=TLS, server_hostname=hostname), CONNECT_TIMEOUT
        )
    except OSError as e:
        log.warning(f"Could not reach {hostname} at {ip}: {e}")
        writer.close()
        return
    await asyncio.gather(pipe(reader, upstream[1]), pipe(upstream[0], writer))


@cache
def nameserver(zone: str) -> str:
    """Get the IPv4 address of a zone's first authoritative nameserver, through Google's DNS-over-HTTPS JSON API."""
    res = requests.get("https://dns.google/resolve", params={"name": zone, "type": "NS"}, timeout=10)
    res.raise_for_status()
    return socket.gethostbyname([x["data"] for x in res.json().get("Answer", []) if x.get("type") == 2][0])


def pinned(ip: str, hostname: str) -> int:
    """
    Get a loopback port that carries each connection over TLS to one server IP, starting it on first use.

    The client still speaks the HTTP proxy protocol with the credentials itself: the port only fixes the IP,
    which a proxy URI cannot do because the certificate names the hostname, not the IP.
    """
    global LOOP
    with STATE:
        if (ip, hostname) not in PINS:
            if LOOP is None:
                LOOP = asyncio.new_event_loop()
                threading.Thread(target=LOOP.run_forever, daemon=True, name="surfshark").start()
            server = asyncio.run_coroutine_threadsafe(
                asyncio.start_server(partial(tunnel, ip, hostname), "127.0.0.1", 0), LOOP
            ).result()
            PINS[ip, hostname] = server.sockets[0].getsockname()[1]
        return PINS[ip, hostname]


class SurfsharkVPN(Proxy):
    def __init__(
        self,
        username: str,
        password: str,
        server_map: Optional[dict] = None,
        servers: Optional[dict] = None,
        **_: object,
    ) -> None:
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
            raise ValueError("No username for the SurfsharkVPN proxy provider")
        if not password:
            raise ValueError("No password for the SurfsharkVPN proxy provider")
        if "@" in username:
            raise ValueError("SurfsharkVPN needs its Service Credentials (not an email login)")

        server_map = server_map if server_map is not None else servers
        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(
                f"Expected server_map to be a dict mapping a region to a server name, not '{server_map!r}'."
            )

        self.username = username
        self.password = password
        self.server_map: dict[str, str] = {}
        for region, server in (server_map or {}).items():
            server = str(server or "").strip()
            if not server or server.isdigit():
                # A warning, not an error: one stale entry must not disable the whole proxy provider.
                log.warning(
                    "SurfsharkVPN: ignoring server_map entry '%s: %s', use a server name such as 'us-dal'",
                    region,
                    server,
                )
                continue
            country, sep, city = str(region).lower().partition(":")
            self.server_map[COUNTRY_CODE_ALIASES.get(country, country) + sep + city] = server

        self.last_server: Optional[tuple[str, str]] = None
        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(set(x.get("country") for x in self.countries if x.get("country")))
        servers = sum(1 for x in self.countries if x.get("connectionName"))

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP(SSL) proxy URI for a SurfsharkVPN server.

        Supports:
        - Country code: "us", "ca", "gb" (or "uk")
        - Specific server: "us-bos" (Boston)
        - City selection: "us:seattle", "us:new-york"
        - Network selection: "fr-bod@datacamp", "fr@-hostroyale" (a leading "-" excludes)
        - One server IP: "fr-bod@195.86.24.103"

        A network or IP selection goes through a loopback port pinned to the server IP (see `pinned`).
        Returns None for a query that names no SurfsharkVPN country or server, so that the
        next proxy provider can answer it. A city with no matching server raises ValueError.
        """
        query, selector = split_selector(_as_str(query).strip().lower())
        if not query:
            raise ValueError("The query provided is unsupported and unrecognized: ''")

        # A pinned cluster wins over everything else, as the fork documented (unless a selector follows).
        pinned_host = _hostname(self.server_map.get(query)) if not selector else None
        if pinned_host:
            return self._uri(pinned_host)

        # A bare cluster slug or a full host only counts when the API lists that cluster.
        slug = _hostname(query) if not selector else None
        if slug:
            if self._cluster(slug) or self._cluster(slug.split(".", 1)[0]):
                return self._uri(slug)
            return None

        city = None

        if ":" in query:
            query, city = query.split(":", maxsplit=1)
            city = city.strip()
        query = COUNTRY_CODE_ALIASES.get(query, query)

        server_map_key = f"{query}:{city}" if city else query
        mapped = self.server_map.get(server_map_key)
        # An @ selector in the map value is a whole query for its region, unless --proxy has its own.
        as_query = mapped_query(server_map_key, mapped) if mapped else None
        if as_query and not selector:
            return self.get_proxy(as_query)
        pinned_server = _hostname(mapped if not as_query else None)
        if pinned_server:
            servers = [pinned_server]
        elif not city and any(
            _as_str(x.get("connectionName")).split(".", 1)[0] == query for x in self.countries
        ):
            servers = [query]
        elif re.fullmatch(r"[a-z]{2}", query):
            wanted = {"gb": ("gb", "uk")}.get(query, (query,))
            in_country = [x for x in self.countries if _as_str(x.get("countryCode")).lower() in wanted]
            if not in_country:
                return None
            servers = [
                x["connectionName"]
                for x in self.get_city_servers(self.prefer_generic(in_country), city)
                if _as_str(x.get("connectionName"))
            ]
            if not servers:
                raise ValueError(
                    f"The SurfsharkVPN Country {query} currently has no random servers. "
                    "Try again later. If the issue persists, double-check the query."
                )
        else:
            return None

        hostnames = [self.get_hostname(x) for x in servers]
        self.last_server = None
        if not selector:
            return f"https://{self.username}:{self.password}@{random.choice(hostnames)}:443"
        if is_ip(selector):
            hostname, ip = random.choice(hostnames), selector
        else:
            hostname, ip = self.get_server_on_network(hostnames, selector)
        self.last_server = (hostname, ip)
        return f"http://{self.username}:{self.password}@127.0.0.1:{pinned(ip, hostname)}"

    def last_connection_display(self) -> Optional[str]:
        """Name the pinned server with its IP and network, so it can be pinned again or excluded with '@-'."""
        return describe(*self.last_server) if self.last_server else None

    def get_server_on_network(self, hostnames: list[str], selector: str) -> tuple[str, str]:
        """Get a random (hostname, IP) that matches the selector (see `asn.select`), trying the clusters in random order."""
        for hostname in random.sample(hostnames, len(hostnames)):
            ips = select({ip: ip for ip in self.get_pool(hostname)}, selector)
            if ips:
                return hostname, random.choice(ips)
        raise ValueError(f"No SurfsharkVPN server matches '@{selector}'.")

    @staticmethod
    def get_pool(hostname: str) -> list[str]:
        """
        Get the server IPs behind a cluster hostname.

        Its nameserver answers with two of them at random, so this sends it many queries at once and keeps every answer.
        """
        ips: set[str] = set()
        try:
            address = nameserver(hostname.split(".", 1)[1])
            query = build_query(hostname)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                for _ in range(POOL_QUERIES):
                    sock.sendto(query, (address, 53))
                try:
                    for _ in range(POOL_QUERIES):
                        ips.update(ip for ip, _ in parse_a_records(sock.recv(4096)))
                except TimeoutError:
                    pass
        except (requests.RequestException, ValueError, IndexError, OSError) as e:
            raise ValueError(f"Could not list the SurfsharkVPN servers of {hostname}: {e}") from None
        if not ips:
            raise ValueError(f"The nameserver of {hostname} did not answer, so its servers could not be listed.")
        return sorted(ips)

    @staticmethod
    def prefer_generic(servers: list[dict]) -> list[dict]:
        """Prefer `type: generic` clusters: the pool the Surfshark app itself uses as HTTPS proxies."""
        generic = [x for x in servers if _as_str(x.get("type") or "generic").lower() == "generic"]
        return generic or servers

    @staticmethod
    def get_hostname(server: object) -> str:
        """Expand a server name such as 'us-dal' to its full hostname.

        A value that is not a cluster name (a numeric ID from an old config) is passed
        through unchanged so the caller's own validation can reject it.
        """
        server = _as_str(server).lower()
        return server if server.endswith(".surfshark.com") else f"{server}.prod.surfshark.com"

    @staticmethod
    def fold(text: Optional[str]) -> str:
        """Reduce a place name to lowercase ASCII letters and digits."""
        return fold(text or "")

    def _cluster(self, hostname: str) -> Optional[dict]:
        """The API's record for a cluster hostname or slug, if it lists one."""
        wanted = _as_str(hostname).lower()
        for cluster in self.countries:
            name = _as_str(cluster.get("connectionName")).lower()
            if name == wanted or name.split(".", 1)[0] == wanted:
                return cluster
        return None

    def _remember(self, hostname: str) -> None:
        """Record the cluster that answered so the console can name it."""
        self.last_host = hostname
        cluster = self._cluster(hostname) or {}
        self.last_name = cluster.get("country") or None
        self.last_city = cluster.get("location") or cluster.get("city") or None

    def _uri(self, hostname: str) -> str:
        """The unshackle-dl URI shape: credentials in the userinfo, port 443."""
        self._remember(hostname)
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"https://{user}:{password}@{hostname}:443"

    def get_city_servers(self, servers: list[dict], city: Optional[str] = None) -> list[dict]:
        """
        Get a country's servers, optionally filtered by city.

        A city matches with case, spaces, hyphens and accents ignored ("new-york" matches "New York"),
        exactly first, then as a prefix ("frankfurt" matches "Frankfurt am Main").
        """
        if city:
            wanted = fold(city)
            located = [(fold(x.get("location") or x.get("city") or ""), x) for x in servers]
            servers = [x for location, x in located if location == wanted] or [
                x for location, x in located if wanted and location.startswith(wanted)
            ]
            if not servers:
                raise ValueError(f"No servers found in city '{city}'")

        return servers

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://api.surfshark.com/v3/server/clusters/all",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of SurfsharkVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of SurfsharkVPN countries, not JSON data.")
