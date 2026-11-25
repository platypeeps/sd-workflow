"""A satellite dials its hub through a SOCKS5 `ALL_PROXY` (sd:10533).

A sandbox that allows the hub's host and port only through its proxy (the
Codex network proxy) sets `ALL_PROXY`; the hub link is raw TCP, so without
this it ignored the proxy and the sandbox refused the direct connect.
"""

from __future__ import annotations

import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sd_db import remote

from .test_wire import Served

PROXY_VARS = ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
#: The hub these tests name: not loopback, resolved to the test hub.
HUB = "hub.example.test"


def _exactly(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _pipe(source: socket.socket, sink: socket.socket, seen: bytearray | None = None) -> None:
    try:
        while chunk := source.recv(65536):
            if seen is not None:
                seen += chunk
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for end in (source, sink):
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class FakeSocks5:
    """A SOCKS5 server on loopback: records each CONNECT, then relays or refuses."""

    def __init__(self, refuse: int = 0, greeting: bytes = b"\x05\x00",
                 unreachable: tuple[str, ...] = ()) -> None:
        self.refuse = refuse
        self.greeting = greeting
        self.unreachable = unreachable
        self.requests: list[tuple[int, str, int]] = []
        #: Every byte the client sent past the CONNECT, as the proxy relayed it.
        self.relayed = bytearray()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.listener.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        try:
            version, count = _exactly(conn, 2)
            methods = _exactly(conn, count)
            assert version == 5 and 0 in methods
            conn.sendall(self.greeting)
            if self.greeting != b"\x05\x00":
                conn.close()
                return
            _, command, _, kind = _exactly(conn, 4)
            assert command == 1
            if kind == 1:
                host = socket.inet_ntop(socket.AF_INET, _exactly(conn, 4))
            elif kind == 4:
                host = socket.inet_ntop(socket.AF_INET6, _exactly(conn, 16))
            else:
                host = _exactly(conn, _exactly(conn, 1)[0]).decode("ascii")
            (port,) = struct.unpack(">H", _exactly(conn, 2))
            self.requests.append((kind, host, port))
            bound = b"\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", 1)
            refuse = 4 if host in self.unreachable else self.refuse
            if refuse:
                conn.sendall(bytes([5, refuse, 0]) + bound)
                conn.close()
                return
            # Every hub this fake reaches is the loopback test hub.
            upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
            # A domain-name bound address, so the client reads every ATYP's length.
            conn.sendall(b"\x05\x00\x00\x03\x09localhost" + struct.pack(">H", 1))
            threading.Thread(target=_pipe, args=(upstream, conn), daemon=True).start()
            _pipe(conn, upstream, self.relayed)
        except (OSError, AssertionError, ConnectionError):
            conn.close()


class ThroughAProxy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.served = Served(Path(tmp.name))
        self.addCleanup(self.served.stop)
        clean = mock.patch.dict(os.environ)
        clean.start()
        self.addCleanup(clean.stop)
        for name in PROXY_VARS:
            os.environ.pop(name, None)
        # A loopback hub is always dialled directly, so these tests name a
        # hub that is not loopback and resolve it to the test hub.
        real = socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            return real("127.0.0.1" if host == HUB else host, *args, **kwargs)

        resolving = mock.patch.object(remote.socket, "getaddrinfo", side_effect=resolve)
        resolving.start()
        self.addCleanup(resolving.stop)

    def proxy(self, **options) -> FakeSocks5:
        fake = FakeSocks5(**options)
        self.addCleanup(fake.close)
        return fake

    def dial(self, host: str = HUB, token=None) -> remote.Connection:
        return remote.connect(host, self.served.port, self.served.database,
                              token=self.served.token if token is None else token,
                              timeout=5, answer_timeout=10)

    def reach_tokenless(self, host: str = HUB) -> None:
        """A session with no token reaches the hub, which answers its token refusal.

        The loopback test hub admits only by token, and a token is never sent
        through a proxy; its refusal is the proof the frames crossed both ways.
        """
        with self.assertRaises(remote.RemoteError) as raised:
            self.dial(host, token="")
        self.assertNotIsInstance(raised.exception, remote.HubUnreachable)
        self.assertIn("lacks this server's token", str(raised.exception))

    def read_one(self, host: str = HUB) -> int:
        connection = self.dial(host)
        try:
            return connection.execute("SELECT 1").fetchone()[0]
        finally:
            connection.close()

    def test_a_socks5h_all_proxy_carries_the_hub_session_by_name(self):
        fake = self.proxy()
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        self.reach_tokenless()
        self.assertEqual(fake.requests, [(3, HUB, self.served.port)])

    def test_the_lower_case_name_and_an_ip_hub_send_the_address(self):
        fake = self.proxy()
        os.environ["all_proxy"] = f"socks5://127.0.0.1:{fake.port}"
        self.reach_tokenless("192.0.2.10")
        self.assertEqual(fake.requests, [(1, "192.0.2.10", self.served.port)])

    def test_a_loopback_hub_is_dialled_directly_past_any_proxy(self):
        for value in ("socks5://x", "socks5h://127.0.0.1:1"):
            for host in ("127.0.0.1", "localhost", "LOCALHOST"):
                with self.subTest(value=value, host=host):
                    os.environ["ALL_PROXY"] = value
                    self.assertEqual(self.read_one(host), 1)
        self.assertIsNone(remote.socks_proxy("::1"))

    def test_the_owner_only_token_never_crosses_a_proxy(self):
        fake = self.proxy()
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        with self.assertRaises(remote.HubUnreachable) as raised:
            self.dial()
        self.assertIn("owner-only token", raised.exception.reason)
        self.assertIn("NO_PROXY", raised.exception.reason)
        self.assertNotIn(self.served.token.encode(), bytes(fake.relayed))

    def test_a_token_held_by_a_callable_never_crosses_a_proxy(self):
        fake = self.proxy()
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        with self.assertRaises(remote.HubUnreachable):
            self.dial(token=lambda: self.served.token)
        self.assertNotIn(self.served.token.encode(), bytes(fake.relayed))

    def test_socks5_tries_each_address_the_name_resolves_to(self):
        fake = self.proxy(unreachable=("192.0.2.1",))
        os.environ["ALL_PROXY"] = f"socks5://127.0.0.1:{fake.port}"
        real = socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            if host != HUB:
                return real(host, *args, **kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, args[0]))
                    for address in ("192.0.2.1", "192.0.2.1", "192.0.2.3")]

        with mock.patch.object(remote.socket, "getaddrinfo", side_effect=resolve):
            self.reach_tokenless()
        port = self.served.port
        self.assertEqual(fake.requests, [(1, "192.0.2.1", port), (1, "192.0.2.3", port)])

    def test_socks5_raises_the_last_refusal_when_no_address_answers(self):
        fake = self.proxy(unreachable=("192.0.2.1", "192.0.2.2"))
        os.environ["ALL_PROXY"] = f"socks5://127.0.0.1:{fake.port}"
        real = socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            if host != HUB:
                return real(host, *args, **kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 1)) for a in ("192.0.2.1", "192.0.2.2")]

        with mock.patch.object(remote.socket, "getaddrinfo", side_effect=resolve):
            with self.assertRaises(remote.HubUnreachable) as raised:
                self.dial(token="")
        self.assertIn("host unreachable", raised.exception.reason)
        self.assertEqual(len(fake.requests), 2)

    def test_a_proxy_password_is_never_echoed(self):
        os.environ["ALL_PROXY"] = "socks5h://operator:s3cret-word@127.0.0.1:1080"
        with self.assertRaises(remote.HubUnreachable) as raised:
            self.dial()
        self.assertNotIn("s3cret-word", str(raised.exception))
        self.assertNotIn("operator", str(raised.exception))
        self.assertIn("ALL_PROXY", raised.exception.reason)

    def test_a_refused_connect_names_the_proxy_and_its_reason(self):
        fake = self.proxy(refuse=2)
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        with self.assertRaises(remote.HubUnreachable) as raised:
            self.dial()
        self.assertIn(f"SOCKS5 proxy 127.0.0.1:{fake.port}", raised.exception.reason)
        self.assertIn("not allowed by ruleset", raised.exception.reason)

    def test_a_proxy_that_wants_a_password_is_refused_by_name(self):
        fake = self.proxy(greeting=b"\x05\xff")
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        with self.assertRaises(remote.HubUnreachable) as raised:
            self.dial()
        self.assertIn("accepts no unauthenticated session", raised.exception.reason)

    def test_no_proxy_naming_the_hub_dials_it_directly(self):
        fake = self.proxy()
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        os.environ["NO_PROXY"] = "other.test,example.test"
        self.assertEqual(self.read_one(), 1)
        self.assertEqual(fake.requests, [])

    def test_no_proxy_matches_by_name_not_by_substring(self):
        fake = self.proxy()
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        os.environ["NO_PROXY"] = "ample.test"
        self.reach_tokenless()
        self.assertEqual(fake.requests, [(3, HUB, self.served.port)])

    def test_no_proxy_entries_match_whole_labels(self):
        cases = [("hub.example.test", "*", True), ("hub.example.test", ".example.test", True),
                 ("HUB.example.test.", "hub.example.test", True), ("hub.example.test", "ple.test", False),
                 ("hub.example.test", "", False), ("example.test", "hub.example.test", False)]
        for host, entries, expected in cases:
            with self.subTest(host=host, entries=entries):
                self.assertEqual(remote._bypassed(host, entries), expected)

    def test_an_http_all_proxy_is_left_alone(self):
        os.environ["ALL_PROXY"] = f"http://127.0.0.1:{socket_closed_port()}"
        self.assertEqual(self.read_one(), 1)

    def test_a_socks_proxy_with_no_port_is_refused_by_name(self):
        for value in ("socks5h://127.0.0.1", "socks5h://:1080", "socks5h://127.0.0.1:x",
                      "socks5h://user:pw@127.0.0.1:1080", "socks5h://[::1:1080",
                      "socks5h://[zz]:1080"):
            with self.subTest(value=value):
                os.environ["ALL_PROXY"] = value
                with self.assertRaises(remote.HubUnreachable) as raised:
                    self.dial()
                self.assertIn("ALL_PROXY", raised.exception.reason)

    def test_a_long_or_non_ascii_hub_name_is_refused_not_sent(self):
        fake = self.proxy()
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{fake.port}"
        for host in ("a" * 256, "hüb\n.example.test"):
            with self.subTest(host=host[:12]):
                with self.assertRaises(remote.HubUnreachable):
                    self.dial(host)
        self.assertEqual(fake.requests, [])

    def test_a_dead_proxy_is_unreachable_not_a_direct_dial(self):
        os.environ["ALL_PROXY"] = f"socks5h://127.0.0.1:{socket_closed_port()}"
        with self.assertRaises(remote.HubUnreachable) as raised:
            self.dial()
        self.assertIn("SOCKS5 proxy", raised.exception.reason)


def socket_closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
