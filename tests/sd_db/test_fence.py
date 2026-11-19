"""Hub failover slice 2, the fence (sd:4044, design D5 in system's `docs/work/2026-10-10-hub-failover/`).

A machine holds the hub role only while its tailnet name matches the fleet
setting `sd.hub_name`. A returning old hub still has its database and no
`hub.json`, so without the fence it serves, runs lanes and pushes as the hub.
Each caller reads four answers: `holder` and `off` (no setting: a fleet that
never named its hub) go on; `elsewhere` (another node holds the name) and
`unknown` (the setting or Tailscale does not answer) refuse.

`tailscale` is the harness stub on PATH; no test reaches the real daemon.
"""

from __future__ import annotations

import functools
import io
import ipaddress
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sd_db import (
    database,
    hub,
    initialise,
    remote,
    serve,
    ship,
    tailnet,
    upsert_repo,
    writes,
)
from sd_db.testing import Stubs

from . import support

HUB_NAME = "hub.example.test"
REPOSITORY = "example/one"
REMOTE = "git@github.com:example/one.git"


def set_during(case, path, value: str | None) -> None:
    """Patch `hub._this_name` to set `sd.hub_name` to `value` in `path` while Tailscale answers."""
    name = hub._this_name

    def setting() -> str:
        raw = sqlite3.connect(path)
        try:
            raw.execute("INSERT INTO state (kind, key, body, timestamp) "
                        "VALUES ('setting', ?, json_object('value', ?), '2026')", (hub.HUB_NAME, value))
            raw.commit()
        finally:
            raw.close()
        return name()

    case.enterContext(mock.patch.object(hub, "_this_name", setting))


class StubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stubs = Stubs(self.root / "stubs", names=("tailscale",))
        self.enterContext(mock.patch.dict(os.environ, self.stubs.environment()))
        self.enterContext(mock.patch.object(hub, "_name_failed", None, create=True))
        # An offline node is read again after each pause; in-process tests do not wait (pack sd:7281).
        self.enterContext(mock.patch.object(tailnet, "ONLINE_PAUSES", (0.0, 0.0), create=True))
        self.path = self.root / "sd.db"
        initialise(self.path)

    def name_hub(self, value: str | None) -> None:
        connection = database.connect(self.path)
        try:
            writes.set_setting(connection, "sd.hub_name", value)
        finally:
            connection.close()

    def node(self, dnsname: str | None = HUB_NAME + ".", **state) -> None:
        if dnsname is not None:
            state["dnsname"] = dnsname
        self.stubs.state("tailscale", {"ips": ["100.64.0.10"], **state})

    def asked(self) -> list[list[str]]:
        return [call.argv for call in self.stubs.calls("tailscale")]


class TheRole(StubCase):
    def role(self) -> hub.Role:
        return hub.role_at(self.path)

    def test_the_named_node_holds_the_role(self):
        self.name_hub(HUB_NAME)
        self.node("Hub.Example.Test.")
        found = self.role()
        self.assertEqual((found.state, found.holder), ("holder", HUB_NAME))

    def test_a_bare_machine_name_matches_the_first_label(self):
        self.name_hub("hub")
        self.node()
        self.assertEqual(self.role().state, "holder")
        self.node("hub-2.example.test.")
        self.assertEqual(self.role().state, "elsewhere")

    def test_another_node_is_elsewhere_and_named(self):
        self.name_hub(HUB_NAME)
        self.node("old-hub.example.test.")
        found = self.role()
        self.assertEqual(found.state, "elsewhere")
        self.assertIn(f"sd.hub_name names {HUB_NAME}, and this node is old-hub.example.test", found.reason)

    def test_no_setting_is_off_and_asks_tailscale_nothing(self):
        self.node("old-hub.example.test.")
        found = self.role()
        self.assertEqual((found.state, found.refuses), ("off", False))
        self.assertIn("sd.hub_name is unset, so the fence is off", found.reason)
        self.name_hub(HUB_NAME)
        self.name_hub(None)
        self.assertEqual(self.role().state, "off", "a cleared row reads unset")
        self.assertEqual(self.asked(), [])

    def test_a_tailscale_that_does_not_answer_is_unknown(self):
        self.name_hub(HUB_NAME)
        for state in ({"backend": "Stopped"}, {"backend": "NeedsLogin"}, {"dnsname": ""}):
            with self.subTest(state=state):
                hub._name_failed = None
                self.node(**state)
                found = self.role()
                self.assertEqual(found.state, "unknown")
                self.assertIn("Tailscale does not name this node", found.reason)

    def test_a_node_cut_from_the_control_plane_is_unknown(self):
        """Third review: `DNSName` comes from the cached netmap, so offline it can be a name the console took away."""
        self.name_hub(HUB_NAME)
        self.node(online=False)
        found = self.role()
        self.assertEqual(found.state, "unknown", found.reason)
        self.assertIn("not connected to the Tailscale control plane", found.reason)

    def test_a_database_that_does_not_open_is_unknown_as_of_now(self):
        """No input was read, so the answer ages from its arrival (`observed` is `None`, pack sd:7120)."""
        found = hub.role_at(self.root / "missing.db")
        self.assertEqual(found.state, "unknown")
        self.assertIn("did not open", found.reason)
        self.assertIsNone(found.observed)

    def test_an_unreadable_setting_row_is_unknown(self):
        connection = database.connect(self.path)
        connection.execute("INSERT INTO state (kind, key, body, timestamp) VALUES ('setting', ?, 'nope', '2026')",
                           (hub.HUB_NAME,))
        connection.close()
        self.node()
        found = self.role()
        self.assertEqual(found.state, "unknown")
        self.assertIn("does not read", found.reason)

    def test_a_database_that_does_not_open_is_unknown(self):
        self.assertEqual(hub.role_at(self.root / "missing.db").state, "unknown")

    def test_a_rename_refuses_the_next_check(self):
        """Codex pass: a held name admitted a renamed node for 30 s; a name is read at every check."""
        self.name_hub(HUB_NAME)
        self.node()
        self.assertEqual(self.role().state, "holder")
        self.node("old-hub.example.test.")
        self.assertEqual(self.role().state, "elsewhere")

    def test_a_failed_read_refuses_again_without_asking(self):
        self.name_hub(HUB_NAME)
        self.node(backend="Stopped")
        self.assertEqual(self.role().state, "unknown")
        self.node()
        for _ in range(5):
            self.assertEqual(self.role().state, "unknown")
        self.assertEqual(len(self.asked()), 1, "a stalled daemon costs a lane run one timeout")
        with mock.patch.object(hub.time, "monotonic", return_value=hub.time.monotonic() + hub.NAME_SECONDS + 1):
            self.assertEqual(self.role().state, "holder")


class TheLaneHost(StubCase):
    """`hosts_lane` on this machine's own database: the fence decides."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(ship, "this_host", return_value="build-2"))
        self.enterContext(mock.patch.object(ship, "served_by", return_value=None))

    def hosts(self, lane_host: str | None = None) -> bool:
        connection = database.connect(self.path)
        try:
            upsert_repo(connection, "/srv/one", remote=REMOTE)
            connection.execute("UPDATE repo SET lane_host = ? WHERE path = '/srv/one'", (lane_host,))
            return ship.hosts_lane(connection, self.path, REPOSITORY)
        finally:
            connection.close()

    def test_the_holder_runs_its_lanes(self):
        self.name_hub(HUB_NAME)
        self.node()
        self.assertTrue(self.hosts())
        self.assertTrue(self.hosts("build-2"))

    def test_a_demoted_hub_runs_no_lane_and_names_the_holder(self):
        self.name_hub(HUB_NAME)
        self.node("old-hub.example.test.")
        for lane_host in (None, "build-2"):
            with self.subTest(lane_host=lane_host), self.assertRaises(ship.LaneElsewhere) as caught:
                self.hosts(lane_host)
            self.assertEqual(caught.exception.code, "lane_elsewhere")
            self.assertIn(f"runs on the hub, {HUB_NAME}, not on this machine", str(caught.exception))
            self.assertNotIn("lane-host", str(caught.exception), "moving the lane here is no remedy")

    def test_no_setting_runs_the_lanes_as_before(self):
        self.node("old-hub.example.test.")
        self.assertTrue(self.hosts())
        self.assertEqual(self.asked(), [])

    def test_unknown_runs_no_lane(self):
        self.name_hub(HUB_NAME)
        self.node(backend="Stopped")
        with self.assertRaisesRegex(ship.LaneUnknown, "Tailscale does not name this node"):
            self.hosts()

    def test_a_partitioned_hub_runs_no_lane(self):
        """The lane host check, `still_hosted` and the ship lock all read the fence here."""
        self.name_hub(HUB_NAME)
        self.node(online=False)
        with self.assertRaisesRegex(ship.LaneUnknown, "not connected to the Tailscale control plane"):
            self.hosts()

    def test_a_lane_elsewhere_never_asks_the_fence(self):
        self.assertFalse(self.hosts("build-3"))
        self.assertEqual(self.asked(), [])

    def test_the_ship_lock_refuses_on_a_demoted_hub_and_locks_nothing(self):
        self.name_hub(HUB_NAME)
        self.node("old-hub.example.test.")
        with self.assertRaises(ship.LaneElsewhere):
            self.hosts()
        with self.assertRaises(ship.LaneElsewhere):
            with ship.repository_lock(self.path, REPOSITORY):
                self.fail("the lock was taken")
        self.assertFalse((self.root / ship.LOCK_DIRECTORY).exists())


class TheServer(StubCase):
    """`sd-db serve` without `--loopback` at start; `--loopback` leaves the machine for nobody."""

    def run_serve(self, *arguments) -> subprocess.CompletedProcess:
        home = self.root / "home"
        environment = {**os.environ, "PYTHONPATH": str(support.LIB), "HOME": str(home),
                       "XDG_DATA_HOME": str(home / ".local" / "share")}
        return subprocess.run(support.sd_db_argv("serve", *arguments, "--database", str(self.path)),
                              capture_output=True, text=True, env=environment, cwd=support.LIB, timeout=60)

    def test_a_demoted_hub_does_not_listen_and_names_the_holder(self):
        self.name_hub(HUB_NAME)
        self.node("old-hub.example.test.", ips=["100.100.100.100"])
        done = self.run_serve("--port", "0")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(f"this machine is not the hub: sd.hub_name names {HUB_NAME}", done.stderr)
        self.assertNotIn("cannot listen", done.stderr)
        self.assertEqual(sorted(p.name for p in self.root.glob("sd.db.serve.*")), [])

    def test_unknown_does_not_listen(self):
        self.name_hub(HUB_NAME)
        self.node(dnsname="", ips=["100.100.100.100"])
        done = self.run_serve("--port", "0")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("this machine is not the hub: Tailscale does not name this node", done.stderr)

    def test_a_partitioned_hub_does_not_listen(self):
        """Startup read the name from `this_node`'s status, past the fence's own reader."""
        self.name_hub(HUB_NAME)
        self.node(ips=["100.100.100.100"], online=False)
        done = self.run_serve("--port", "0")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("not connected to the Tailscale control plane", done.stderr)

    def test_no_setting_goes_on_to_bind(self):
        self.node(ips=["100.100.100.100"])
        done = self.run_serve("--port", "0")
        self.assertIn("cannot listen on 100.100.100.100:0", done.stderr)

    def test_the_holder_goes_on_to_bind(self):
        # 100.100.100.100 is Tailscale's resolver and never a node's, so the bind fails past the fence.
        self.name_hub(HUB_NAME)
        self.node(ips=["100.100.100.100"])
        done = self.run_serve("--port", "0")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("cannot listen on 100.100.100.100:0", done.stderr)
        self.assertEqual(self.asked(), [["status", "--json"]] * 2, "one for the node, one for the fence's name")

    def test_loopback_is_not_fenced(self):
        self.node("old-hub.example.test.")
        refused: list[str] = []
        with mock.patch.object(serve, "Server", side_effect=OSError("stop here")):
            code = serve.serve(0, self.path, stream=mock.Mock(write=refused.append), loopback=True)
        self.assertEqual(code, 1)
        self.assertIn("cannot listen on 127.0.0.1:0", "".join(refused))
        self.assertEqual(self.asked(), [])


class TheLiveServer(StubCase):
    """A server that started as the hub and is demoted while it runs (the 2026-10-14 review of sd:4044).

    An old hub that slept, or lost its network, keeps its process; the
    console renames it meanwhile. The check at start does not see that, so
    every session frame asks the fence again, within `serve.FENCE_SECONDS`
    and never across a wall-clock jump (a sleep).
    """

    OPERATOR = "operator@example.test"
    HUB_IP = "100.64.0.10"
    SATELLITE = "100.64.0.20"

    def setUp(self):
        super().setUp()
        local = database.connect(self.path)
        local.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        local.close()
        self.name_hub(HUB_NAME)
        self.rename(HUB_NAME + ".")
        self.stream = io.StringIO()
        node = tailnet.Node(frozenset({ipaddress.ip_address(self.HUB_IP)}), self.OPERATOR,
                            ipaddress.IPv4Address("127.0.0.1"))
        self.server = serve.Server(0, self.path, serve.Log(self.stream), "", self.root / "unused.token", node)
        self.addCleanup(self.server.server_close)

    def rename(self, dnsname: str, **state) -> None:
        whois = {self.SATELLITE: {"Node": {"Addresses": [f"{self.SATELLITE}/32"], "Tags": []},
                                  "UserProfile": {"LoginName": self.OPERATOR}}}
        self.node(dnsname, ips=[self.HUB_IP], login=self.OPERATOR, whois=whois, **state)

    def rows(self) -> int:
        watch = database.connect(self.path, write=False)
        try:
            return watch.execute("SELECT count(*) FROM probe").fetchone()[0]
        finally:
            watch.close()

    def session(self, *frames, between=None, after: int = 0) -> list:
        """Open, then each frame; `between` runs after answer `after` (0 is the open's). `None` once the hub hangs up."""
        ours, theirs = socket.socketpair()
        ours.settimeout(30)

        def handle():
            try:
                serve.Session(theirs, (self.SATELLITE, 45678), self.server)
            finally:
                theirs.close()

        handler = threading.Thread(target=handle, daemon=True)
        handler.start()
        answers: list = []
        opening = remote.request("open", path=None, write=True, create=False, busy_timeout=5000,
                                 token="", **remote.handshake())
        try:
            for index, frame in enumerate((opening, *frames)):
                try:
                    remote.send_frame(ours, frame)
                    answers.append(remote.read_frame(ours))
                except (EOFError, OSError):
                    answers.append(None)
                    break
                if index == after and between is not None:
                    between()
        finally:
            ours.close()
            handler.join(30)
        self.assertFalse(handler.is_alive(), "the session handler did not end")
        return answers

    def test_a_rename_after_start_refuses_the_next_open(self):
        self.enterContext(mock.patch.object(serve, "FENCE_SECONDS", 0.0))
        self.assertTrue(self.session(remote.request("close"))[0]["ok"])
        self.rename("old-hub.example.test.")
        answers = self.session(remote.request("execute", sql="INSERT INTO probe (name) VALUES ('stale')", params=None))
        self.assertFalse(answers[0]["ok"], answers)
        self.assertIn(f"this machine is not the hub: sd.hub_name names {HUB_NAME}", answers[0]["error"]["text"])
        self.assertEqual(answers[1:], [None], "a fenced server answered a second frame")
        self.assertEqual(self.rows(), 0)

    def test_the_fence_state_crosses_the_wire(self):
        """pack sd:9543: a satellite tells a fence that could not answer from a demotion by `state`."""
        self.enterContext(mock.patch.object(serve, "FENCE_SECONDS", 0.0))
        for dnsname, state in (("", "unknown"), ("old-hub.example.test.", "elsewhere")):
            with self.subTest(state=state):
                self.rename(dnsname)
                hub._name_failed = None
                answers = self.session(remote.request("close"))
                self.assertFalse(answers[0]["ok"], answers)
                rebuilt = remote.rebuild_error(answers[0]["error"])
                if not isinstance(rebuilt, hub.Fenced):
                    self.fail(f"rebuilt as {rebuilt!r}")
                self.assertEqual(rebuilt.state, state, answers[0]["error"])
                self.assertIn("this machine is not the hub", str(rebuilt))

    def test_a_partition_after_start_refuses_the_next_frame(self):
        """Third review: the old hub keeps its cached name while cut from the control plane."""
        self.enterContext(mock.patch.object(serve, "FENCE_SECONDS", 0.0))
        self.assertTrue(self.session(remote.request("close"))[0]["ok"])
        self.rename(HUB_NAME + ".", online=False)
        answers = self.session(remote.request("execute", sql="INSERT INTO probe (name) VALUES ('stale')", params=None,
                                              rid=remote.new_request_id()))
        self.assertFalse(answers[0]["ok"], answers)
        self.assertIn("not connected to the Tailscale control plane", answers[0]["error"]["text"])
        self.assertEqual(answers[1:], [None])
        self.assertEqual(self.rows(), 0)

    def test_a_rename_mid_session_refuses_the_write_and_rolls_back(self):
        self.enterContext(mock.patch.object(serve, "FENCE_SECONDS", 0.0))
        rid = remote.new_request_id()
        answers = self.session(
            remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=rid),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('before')", params=None),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('after')", params=None),
            remote.request("execute", sql="COMMIT", params=None, rid=rid),
            between=None)
        self.assertTrue(all(a and a["ok"] for a in answers), answers)
        self.assertEqual(self.rows(), 2)

        def demote():
            self.rename("old-hub.example.test.")
        second = remote.new_request_id()
        answers = self.session(
            remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=second),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('stale')", params=None),
            remote.request("execute", sql="COMMIT", params=None, rid=second),
            remote.request("close"),
            between=demote, after=2)
        self.assertTrue(all(a["ok"] for a in answers[:3]), answers)
        self.assertFalse(answers[3]["ok"], answers)
        self.assertIn("this machine is not the hub", answers[3]["error"]["text"])
        self.assertEqual(answers[4:], [None], "a fenced server answered a second frame")
        self.assertEqual(self.rows(), 2, "a demoted hub wrote a row")
        self.assertIn("no longer the hub", self.stream.getvalue())

    def test_the_answer_stands_a_few_seconds_but_never_across_a_sleep(self):
        clock = {"monotonic": 1000.0, "time": 2_000_000_000.0}
        self.enterContext(mock.patch.object(serve.time, "monotonic", lambda: clock["monotonic"]))
        self.enterContext(mock.patch.object(serve.time, "time", lambda: clock["time"]))
        self.assertIsNone(self.server.fence())
        self.rename("old-hub.example.test.")
        clock["monotonic"] += 1
        clock["time"] += 1
        self.assertIsNone(self.server.fence(), "asked again inside the window")
        self.assertEqual(self.asked().count(["status", "--json"]), 1)
        # A sleep: the wall clock moves an hour, the monotonic clock does not.
        clock["time"] += 3600
        self.assertIn(f"sd.hub_name names {HUB_NAME}", getattr(self.server.fence(), "reason", ""))
        clock["monotonic"] += serve.FENCE_SECONDS + 1
        clock["time"] += serve.FENCE_SECONDS + 1
        self.rename(HUB_NAME + ".")
        self.assertIsNone(self.server.fence(), "a refusal outlived its window")

    def test_a_frame_reads_the_hub_name_after_tailscale_answers(self):
        """pack sd:7120: the serve fence's answer ages from its oldest input, so the name is read after the call."""
        set_during(self, self.path, "old-hub.example.test")
        self.assertIn("sd.hub_name names old-hub.example.test", getattr(self.server.fence(), "reason", ""))

    def test_a_commit_past_the_window_asks_again(self):
        """codex pass: a frame passed on a cached answer, then the hub was renamed; the commit asks again
        once that answer is `FENCE_SECONDS` old (operator ruling on pack sd:7042, note #39256)."""
        rid = remote.new_request_id()

        def demote():
            self.rename("old-hub.example.test.")
            self.server.fenced = hub.Held()
        answers = self.session(
            remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=rid),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('stale')", params=None),
            remote.request("execute", sql="COMMIT", params=None, rid=rid),
            remote.request("close"),
            between=demote, after=2)
        self.assertTrue(all(a["ok"] for a in answers[:3]), answers)
        self.assertFalse(answers[3]["ok"], answers)
        self.assertIn("this machine is not the hub", answers[3]["error"]["text"])
        self.assertEqual(answers[4:], [None])
        self.assertEqual(self.rows(), 0, "a demoted hub committed a row")

    def test_a_commit_inside_the_window_asks_tailscale_nothing(self):
        """pack sd:7042, note #39256: the commit trusts the answer its frames trust, so it spawns no
        Tailscale call."""
        rid = remote.new_request_id()
        answers = self.session(
            remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=rid),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('one')", params=None),
            remote.request("execute", sql="COMMIT", params=None, rid=rid))
        self.assertTrue(all(a and a["ok"] for a in answers), answers)
        self.assertEqual(self.rows(), 1)
        self.assertEqual(self.asked().count(["status", "--json"]), 1, "one ask for the session, none for its commit")

    def test_a_write_begin_that_asks_again_and_finds_the_rename_refuses_before_it_opens(self):
        """pack sd:7042: the write BEGIN's own ask refuses as a frame's does, before BEGIN runs."""
        ask = self.server.fence

        def fence(**kwargs):
            if kwargs.get("within") is not None:
                self.rename("old-hub.example.test.")
                self.server.fenced = hub.Held()
            return ask(**kwargs)

        self.enterContext(mock.patch.object(self.server, "fence", fence))
        rid = remote.new_request_id()
        answers = self.session(
            remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=rid),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('stale')", params=None))
        self.assertTrue(answers[0]["ok"], answers)
        self.assertFalse(answers[1]["ok"], answers)
        self.assertIn("this machine is not the hub", answers[1]["error"]["text"])
        self.assertEqual(answers[2:], [None], "a fenced server answered a second frame")
        self.assertEqual(self.rows(), 0)

    def test_a_write_begin_on_a_slow_tailscale_holds_no_other_frame(self):
        """pack sd:7042: a frame with a held answer never waits while another session's ask stalls.

        The write BEGIN asks past half the window and stalls in Tailscale;
        a frame inside the window reads the held answer meanwhile. Events and
        a frozen clock, not a 0.5 s sleep and a wall-clock bound: the commit
        that test timed asked nothing (pack sd:7123).
        """
        clock = {"monotonic": 1000.0, "time": 2_000_000_000.0}
        self.enterContext(mock.patch.object(serve.time, "monotonic", lambda: clock["monotonic"]))
        self.enterContext(mock.patch.object(serve.time, "time", lambda: clock["time"]))
        self.assertIsNone(self.server.fence())
        clock["monotonic"] += serve.FENCE_SECONDS * 3 / 4
        clock["time"] += serve.FENCE_SECONDS * 3 / 4
        asking, release = threading.Event(), threading.Event()
        name = hub._this_name

        def stalled() -> str:
            asking.set()
            release.wait(30)
            return name()

        self.enterContext(mock.patch.object(hub, "_this_name", stalled))
        rid = remote.new_request_id()
        answers: list = []
        writer = threading.Thread(target=lambda: answers.extend(self.session(
            remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=rid),
            remote.request("execute", sql="INSERT INTO probe (name) VALUES ('one')", params=None),
            remote.request("execute", sql="COMMIT", params=None, rid=rid))))
        fenced: list = []
        reader = threading.Thread(target=lambda: fenced.append(self.server.fence()))
        writer.start()
        try:
            self.assertTrue(asking.wait(30), "the write BEGIN did not ask Tailscale")
            reader.start()
            # A frame that waits on the ask never ends while the ask stalls.
            reader.join(10)
            self.assertFalse(reader.is_alive(), "a frame waited on another session's Tailscale call")
        finally:
            release.set()
            writer.join(30)
            if reader.ident is not None:
                reader.join(30)
        self.assertEqual(fenced, [None])
        self.assertTrue(all(a and a["ok"] for a in answers), answers)
        self.assertEqual(self.rows(), 1)

    def test_a_write_outside_a_transaction_asks_again_past_the_window(self):
        """codex pass: a lone write with an id is its own transaction, whose COMMIT asks once the
        held answer is `FENCE_SECONDS` old (note #39256)."""
        def demote():
            self.rename("old-hub.example.test.")
            self.server.fenced = hub.Held()
        answers = self.session(remote.request("execute", sql="INSERT INTO probe (name) VALUES ('stale')", params=None,
                                              rid=remote.new_request_id()),
                               remote.request("close"), between=demote)
        self.assertFalse(answers[1]["ok"], answers)
        self.assertIn("this machine is not the hub", answers[1]["error"]["text"])
        self.assertEqual(answers[2:], [None])
        self.assertEqual(self.rows(), 0, "a demoted hub wrote a row")

    def test_a_write_without_a_request_id_is_refused_so_no_answer_precedes_its_commit(self):
        """Second review: such a write commits when it ends, and a batch commits each write, all after
        the one fresh answer; a demotion meanwhile wrote the rest."""
        ask = self.server.fence

        def fence(**kwargs):
            refusal = ask(**kwargs)
            self.rename("old-hub.example.test.")  # demoted right after each answer
            return refusal

        self.enterContext(mock.patch.object(self.server, "fence", fence))
        two = [remote.encode_params(["a"]), remote.encode_params(["b"])]
        for frame in (remote.request("execute", sql="INSERT INTO probe (name) VALUES ('a')", params=None),
                      remote.request("executemany", sql="INSERT INTO probe (name) VALUES (?)", params=two),
                      remote.request("executescript", sql="INSERT INTO probe (name) VALUES ('a');"
                                                          "INSERT INTO probe (name) VALUES ('b');")):
            with self.subTest(op=frame["op"]):
                self.rename(HUB_NAME + ".")
                self.server.fenced = hub.Held()
                answers = self.session(frame, remote.request("close"))
                self.assertFalse(answers[1]["ok"], answers)
                self.assertIn("only with a request id", answers[1]["error"]["text"])
                self.assertTrue(answers[2]["ok"], "a refused write is not a fence refusal; the session stays")
                self.assertEqual(self.rows(), 0, "a write committed after its fence answer")
        self.assertTrue(self.session(remote.request("executescript", sql="SELECT 1;"))[1]["ok"], "a read-only script")
        # The remedy: one write transaction, one COMMIT, one fresh answer.
        self.rename(HUB_NAME + ".")
        self.server.fenced = hub.Held()
        rid = remote.new_request_id()
        answers = self.session(remote.request("execute", sql="BEGIN IMMEDIATE", params=None, rid=rid),
                               remote.request("executemany", sql="INSERT INTO probe (name) VALUES (?)", params=two),
                               remote.request("execute", sql="COMMIT", params=None, rid=rid))
        self.assertTrue(all(a and a["ok"] for a in answers), answers)
        self.assertEqual(self.rows(), 2)

    def test_no_setting_serves_as_before_and_asks_tailscale_nothing(self):
        self.name_hub(None)
        self.enterContext(mock.patch.object(serve, "FENCE_SECONDS", 0.0))
        answers = self.session(remote.request("execute", sql="INSERT INTO probe (name) VALUES ('x')", params=None,
                                              rid=remote.new_request_id()))
        self.assertTrue(all(a and a["ok"] for a in answers), answers)
        self.assertEqual(self.asked().count(["status", "--json"]), 0)


class TheVerb(StubCase):
    """`sd-db fence`: what shell callers outside the library read (hub-pin, the NAS copy)."""

    def fence(self) -> subprocess.CompletedProcess:
        home = self.root / "home"
        data = database.local_path(home).parent
        data.mkdir(parents=True, exist_ok=True)
        if not database.local_path(home).exists():
            os.link(self.path, database.local_path(home))
        environment = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / ".local" / "share")}
        return subprocess.run(support.sd_db_argv("fence"), capture_output=True, text=True, env=environment,
                              cwd=support.LIB, timeout=60)

    def test_exit_one_only_where_the_fence_refuses(self):
        self.node()
        done = self.fence()
        self.assertEqual((done.returncode, done.stdout.split(":")[1:3]), (0, [" fence", " off"]), done.stderr)
        self.name_hub(HUB_NAME)
        done = self.fence()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("sd-db: fence: holder:", done.stdout)
        self.node("old-hub.example.test.")
        done = self.fence()
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"sd-db: fence: elsewhere: sd.hub_name names {HUB_NAME}", done.stdout)
        self.node(backend="Stopped")
        done = self.fence()
        self.assertEqual(done.returncode, 1)
        self.assertIn("sd-db: fence: unknown: Tailscale does not name this node", done.stdout)
        self.node(online=False)
        done = self.fence()
        self.assertEqual(done.returncode, 1)
        self.assertIn("sd-db: fence: unknown:", done.stdout)
        self.assertIn("not connected to the Tailscale control plane", done.stdout)

    def test_a_satellite_is_elsewhere_and_asks_nobody(self):
        config = self.root / "home" / ".config" / "sd" / "hub.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"hub": "hub.example.test", "port": 8769}')
        done = self.fence()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("sd-db: fence: elsewhere: this machine is a satellite of hub.example.test", done.stdout)
        self.assertEqual(self.asked(), [])


class TheOldPeer(unittest.TestCase):
    def test_a_fence_from_a_hub_older_than_the_field_has_no_state(self):
        """pack sd:9543: an older hub sends no `state`; the rebuilt `Fenced` reads `None`, which no caller skips."""
        old = {"module": "sd_db.hub", "name": "Fenced", "args": ["refused: this machine is not the hub: x"],
               "text": "refused: this machine is not the hub: x"}
        rebuilt = remote.rebuild_error(old)
        if not isinstance(rebuilt, hub.Fenced):
            self.fail(f"rebuilt as {rebuilt!r}")
        self.assertIsNone(rebuilt.state)
        self.assertEqual(str(rebuilt), old["text"])


class TheName(StubCase):
    def test_this_name_is_the_magicdns_name_without_its_dot(self):
        self.node("Hub.Example.Test.")
        self.assertEqual(tailnet.this_name(), HUB_NAME)


def status(online=True, backend="Running") -> str:
    return json.dumps({"BackendState": backend, "Self": {"DNSName": HUB_NAME + ".", "Online": online}})


class Reads:
    """A `tailscale status --json` that answers `answers` in turn, the last one ever after."""

    def __init__(self, *answers):
        self.answers, self.argv = list(answers), []

    def __call__(self, argv):
        self.argv.append(argv)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class TheBlip(unittest.TestCase):
    """Pack sd:7281: a control-plane blip of seconds failed a lane merge, a prepare and a backup."""

    def setUp(self):
        self.slept = []

    def test_a_node_back_within_the_window_is_named(self):
        reads = Reads(status(False), status(True))
        with mock.patch.object(tailnet, "ONLINE_PAUSES", (0.0, 0.0), create=True):
            self.assertEqual(tailnet.this_name(reads), HUB_NAME)
        self.assertEqual(len(reads.argv), 2)

    def test_each_pause_comes_before_the_next_read(self):
        reads = Reads(status(False), status(False), status(True))
        self.assertEqual(tailnet.this_name(reads, sleep=self.slept.append), HUB_NAME)
        self.assertEqual(self.slept, list(tailnet.ONLINE_PAUSES[:2]))
        self.assertEqual(len(reads.argv), 3)

    def test_a_node_offline_past_the_window_still_raises(self):
        reads = Reads(status(False))
        with self.assertRaisesRegex(tailnet.TailnetError, "not connected to the Tailscale control plane"):
            tailnet.this_name(reads, sleep=self.slept.append)
        self.assertEqual(self.slept, list(tailnet.ONLINE_PAUSES))
        self.assertEqual(len(reads.argv), len(tailnet.ONLINE_PAUSES) + 1)

    def test_the_window_stays_well_under_the_fence_callers_bound(self):
        """machine-setup bounds `sd-db fence` at 120 s; each read may take `TIMEOUT` too."""
        pauses = tailnet.ONLINE_PAUSES
        self.assertTrue(pauses and all(pause > 0 for pause in pauses), pauses)
        self.assertLessEqual(sum(pauses), 5.0)
        self.assertLess(sum(pauses) + (len(pauses) + 1) * tailnet.TIMEOUT, 120)

    def test_a_stopped_daemon_raises_at_once(self):
        for answer in (status(backend="Stopped"), status(online=False, backend="NeedsLogin"),
                       tailnet.TailnetError("tailscale status failed: Tailscale is stopped."), "not json"):
            with self.subTest(answer=answer):
                reads, self.slept = Reads(answer), []
                with self.assertRaises(tailnet.TailnetError):
                    tailnet.this_name(reads, sleep=self.slept.append)
                self.assertEqual((len(reads.argv), self.slept), (1, []))

    def test_a_daemon_that_stops_during_the_window_raises_its_own_reason(self):
        reads = Reads(status(False), status(backend="Stopped"))
        with self.assertRaisesRegex(tailnet.TailnetError, r"Tailscale is not running \(Stopped\)"):
            tailnet.this_name(reads, sleep=self.slept.append)
        self.assertEqual(len(reads.argv), 2)

    def test_a_blip_is_never_held_by_the_fence(self):
        """`_this_name` holds a failure for `NAME_SECONDS`; a blip inside the window raises none to hold."""
        reads = Reads(status(False), status(True))
        named = functools.partial(tailnet.this_name, reads, sleep=self.slept.append)
        with mock.patch.object(hub, "_name_failed", None), mock.patch.object(hub.tailnet, "this_name", named):
            self.assertEqual(hub._this_name(), HUB_NAME)
            self.assertIsNone(hub._name_failed)
            self.assertEqual(hub._this_name(), HUB_NAME)
        self.assertEqual(len(reads.argv), 3, "the next check asks again")


if __name__ == "__main__":
    unittest.main()
