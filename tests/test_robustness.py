"""Edge cases and failure modes: hostile input, flaky servers, odd addresses.

Each test pins a defect found in review (named in its docstring), so it cannot
quietly come back. Run with:  python -m unittest discover -s tests
"""

import gzip
import http.server
import json
import random
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sensor_side"))

from basestation.core import control, diagnostics as dg, discovery, netutil, processing as pr, protocol
from basestation.core.protocol import MalformedFrameError, parse_frame


# ------------------------------------------------------------------ helpers

class Server:
    """A throwaway local HTTP server whose behaviour each test supplies."""

    def __init__(self, respond):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                respond(self)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                respond(self)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def send(h, body: bytes, code=200, headers=None):
    h.send_response(code)
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def truncated(h, claim=100_000, actual=10):
    """Promise a long body, deliver a few bytes, hang up (=> IncompleteRead)."""
    h.send_response(200)
    h.send_header("Content-Length", str(claim))
    h.end_headers()
    h.wfile.write(b"x" * actual)
    h.wfile.flush()
    h.close_connection = True


def legacy_frame(n: int, seed: int = 0, nan_rows: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    a = np.zeros(n, dtype=protocol.POINT_DTYPE)
    a["xyz"] = rng.uniform(-5, 5, (n, 3))
    a["rgb"] = rng.uniform(0, 1, (n, 3))
    a["thermal_k"] = 300.0
    if nan_rows:
        a["xyz"][:nan_rows] = np.nan
    return struct.pack("<I", n) + a.tobytes()


def rig_frame(n=200, seed=1, gzip_it=True):
    rng = np.random.default_rng(seed)
    xyz = rng.uniform(-8, 8, (n, 3)).astype(np.float32)
    rgb = rng.uniform(0, 1, (n, 3)).astype(np.float32)
    t = rng.uniform(280, 320, n).astype(np.float32)
    t[::7] = np.nan
    return xyz, rgb, t, protocol.encode_rig_frame(xyz, rgb, t, seq=5, gzip_it=gzip_it)


# ------------------------------------------------------------------ protocol

class ProtocolTests(unittest.TestCase):
    def test_rig_format_round_trips(self):
        """The real rig sends gzip + int16-quantized points; the app used to reject it."""
        xyz, rgb, t, body = rig_frame()
        f = parse_frame(body, 0, 0.0, 0.0)
        self.assertEqual(f.num_points, 200)
        scale = float((xyz.max(0) - xyz.min(0)).max()) / 32000.0
        self.assertLess(float(np.abs(f.xyz - xyz).max()), scale)          # sub-millimetre
        self.assertLess(float(np.abs(f.rgb - rgb).max()), 1 / 255 + 1e-6)
        known = ~np.isnan(t)
        self.assertTrue(np.array_equal(np.isnan(f.temp_c), ~known))        # sentinel -> NaN
        self.assertLess(float(np.abs(f.temp_c[known] - (t[known] - 273.15)).max()), 0.01)

    def test_gzip_wrapped_legacy_frame(self):
        self.assertEqual(parse_frame(gzip.compress(legacy_frame(40)), 0, 0, 0).num_points, 40)

    def test_empty_frames_in_both_layouts(self):
        self.assertEqual(parse_frame(legacy_frame(0), 0, 0, 0).num_points, 0)
        self.assertEqual(parse_frame(protocol.encode_rig_frame(
            np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32), np.zeros(0, np.float32)),
            0, 0, 0).num_points, 0)

    def test_layouts_can_never_be_confused(self):
        # 4 + 28N == 24 + 11N has no integer solution, so length alone decides.
        self.assertFalse(any(4 + 28 * n == 24 + 11 * n for n in range(0, 100_000)))

    def test_non_finite_points_are_dropped_and_arrays_stay_aligned(self):
        """NaN/inf coordinates (lidar 'no return') broke bounds, camera and voxel keys."""
        body = bytearray(legacy_frame(100, nan_rows=10))
        # make a further point infinite
        struct.pack_into("<f", body, 4 + 28 * 20, float("inf"))
        f = parse_frame(bytes(body), 0, 0, 0)
        self.assertEqual(f.num_points, 89)
        self.assertTrue(np.isfinite(f.xyz).all())
        self.assertEqual((len(f.rgb), len(f.temp_c)), (89, 89))

    def test_bad_rig_header_is_rejected(self):
        body = bytearray(protocol.encode_rig_frame(
            np.random.rand(5, 3).astype(np.float32), np.random.rand(5, 3).astype(np.float32),
            np.full(5, 300, np.float32)))
        struct.pack_into("<f", body, 20, 0.0)                              # scale = 0
        with self.assertRaises(MalformedFrameError):
            parse_frame(bytes(body), 0, 0, 0)

    def test_gzip_failures_are_reported_not_raised_raw(self):
        good = gzip.compress(legacy_frame(500))
        for label, blob in (("truncated", good[: len(good) // 2]),
                            ("corrupt", good[:12] + b"\xff" * 20 + good[32:])):
            with self.subTest(label), self.assertRaises(MalformedFrameError):
                parse_frame(blob, 0, 0, 0)

    def test_gzip_bomb_is_refused(self):
        bomb = gzip.compress(b"\0" * 20_000_000)
        with self.assertRaises(MalformedFrameError):
            protocol.gunzip(bomb, limit=1_000_000)

    def test_junk_bodies_raise_malformed(self):
        for blob in (b"", b"abc", b"<html>404</html>", struct.pack("<I", 4_000_000_000) + b"x",
                     legacy_frame(10)[:-3], legacy_frame(10) + b"zz"):
            with self.subTest(len=len(blob)), self.assertRaises(MalformedFrameError):
                parse_frame(blob, 0, 0, 0)


# ------------------------------------------------------------------ addresses

class NetutilTests(unittest.TestCase):
    def test_accepts_the_ways_people_type_an_address(self):
        cases = {
            "192.168.1.5": ("192.168.1.5", None),
            "  192.168.1.5  ": ("192.168.1.5", None),
            "192.168.1.5:8080": ("192.168.1.5", 8080),
            "http://192.168.1.5:8080/": ("192.168.1.5", 8080),
            "http://192.168.1.5": ("192.168.1.5", None),
            "https://rover.local/latest.bin": ("rover.local", None),
            "ROVER.local": ("rover.local", None),
            "[fe80::1]:8080": ("fe80::1", 8080),
            "::1": ("::1", None),
            "": ("", None),
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(netutil.parse_host_input(text), expected)

    def test_rejects_things_that_cannot_be_hosts(self):
        """'http://http://x' used to be built into a URL and blow up inside the thread."""
        for text in ("a b", "http://", "1.2.3.4:99999", "bad_host!", "@@", "1.2.3.4:abc", "[::1"):
            with self.subTest(text), self.assertRaises(ValueError):
                netutil.parse_host_input(text)

    def test_ipv6_urls_get_brackets(self):
        self.assertEqual(netutil.http_url("fe80::1", 8080, "/x"), "http://[fe80::1]:8080/x")
        self.assertEqual(netutil.http_url("10.0.0.2", 8080, "/x"), "http://10.0.0.2:8080/x")


# ------------------------------------------------------------------ processing

class ProcessingRobustnessTests(unittest.TestCase):
    def test_far_apart_points_are_never_merged(self):
        """The old key clamp collapsed everything past ~2M cells into one voxel."""
        xyz = np.array([[1e7, 0, 0], [2e7, 0, 0], [3e7, 0, 0]], np.float32)
        kept = pr.Pipeline(voxel_enabled=True, voxel_size=0.005).apply(xyz, np.full(3, 30, np.float32))
        self.assertEqual(len(kept), 3)

    def test_neighbour_counts_survive_huge_extents(self):
        pts = [[i * 0.01, 0, 0] for i in range(5)] + [[1e7 + i * 0.01, 0, 0] for i in range(5)]
        counts = pr.neighbor_counts(np.array(pts, np.float32), 0.05)
        self.assertTrue((counts >= 3).all(), counts)

    def test_voxel_grid_is_anchored_so_it_does_not_shimmer_between_frames(self):
        base = np.random.default_rng(3).uniform(0, 4, (3000, 3)).astype(np.float32)
        shifted = np.vstack([base, [[-50, -50, -50]]]).astype(np.float32)   # a new far point moves the min
        a = pr.Pipeline(voxel_enabled=True, voxel_size=0.5).apply(base, np.zeros(len(base), np.float32))
        b = pr.Pipeline(voxel_enabled=True, voxel_size=0.5).apply(shifted, np.zeros(len(shifted), np.float32))
        self.assertEqual(len(a), len(b) - 1)

    def test_non_finite_points_never_reach_the_filters(self):
        xyz = np.random.default_rng(1).uniform(-5, 5, (2000, 3)).astype(np.float32)
        xyz[:50] = np.nan
        xyz[50:60] = np.inf
        t = np.full(2000, 30, np.float32)
        for p in (pr.Pipeline(voxel_enabled=True), pr.Pipeline(isolated_enabled=True),
                  pr.Pipeline(range_enabled=True)):
            idx = p.apply(xyz, t)
            self.assertTrue(np.isfinite(xyz[idx]).all())

    def test_swapped_min_max_are_treated_as_a_range_not_as_nothing(self):
        xyz = np.random.default_rng(2).uniform(-5, 5, (500, 3)).astype(np.float32)
        t = np.random.default_rng(2).uniform(10, 90, 500).astype(np.float32)
        thermal = pr.Pipeline(thermal_enabled=True, thermal_min=60, thermal_max=20).apply(xyz, t)
        self.assertGreater(len(thermal), 0)
        rng_ = pr.Pipeline(range_enabled=True, range_min=6, range_max=2).apply(xyz, t)
        self.assertGreater(len(rng_), 0)
        box = pr.Pipeline(crop=pr.CropBox(True, (3, -3, 3, -3, 3, -3), True)).apply(xyz, t)
        self.assertGreater(len(box), 0)

    def test_bounds_ignore_non_finite_points(self):
        b = pr.bounds_of(np.array([[np.nan, 0, 0], [1, 2, 3], [np.inf, 0, 0]], np.float32))
        self.assertEqual(b, (1.0, 1.0, 2.0, 2.0, 3.0, 3.0))
        self.assertIsNone(pr.bounds_of(np.full((3, 3), np.nan, np.float32)))
        self.assertIsNone(pr.bounds_of(np.zeros((0, 3), np.float32)))

    def test_tiny_and_degenerate_clouds(self):
        for n in (0, 1, 2):
            xyz = np.random.default_rng(n).uniform(-1, 1, (n, 3)).astype(np.float32)
            p = pr.Pipeline(range_enabled=True, thermal_enabled=True, isolated_enabled=True,
                            voxel_enabled=True, crop=pr.CropBox(True, (-1, 1, -1, 1, -1, 1)))
            p.apply(xyz, np.full(n, 30, np.float32))                     # must not raise
        same = np.zeros((300, 3), np.float32)
        self.assertEqual(len(pr.Pipeline(voxel_enabled=True).apply(same, np.zeros(300, np.float32))), 1)


# ------------------------------------------------------------------ diagnostics / control clients

class ClientRobustnessTests(unittest.TestCase):
    def test_nan_and_inf_are_not_measurements(self):
        """A NaN fps compared False against every threshold and graded as healthy."""
        item = dg.parse_report({"sensors": {"c": {"state": "streaming", "fps": float("nan"),
                                                   "target_fps": 15, "last_frame_age_s": float("inf")}}}).sensors[0]
        self.assertIsNone(item.fps)
        self.assertIsNone(item.age_s)

    def test_random_documents_never_crash_the_parser(self):
        rnd = random.Random(0)
        keys = ["state", "connected", "fps", "target_fps", "last_frame_age_s", "message", "status", "name",
                "sensors", "pipelines", "system", "cpu_pct", "temp_c", "under_voltage_now", "unit", "error"]

        def doc(depth=0):
            r = rnd.random()
            if depth > 3 or r < 0.3:
                return rnd.choice([None, True, 0, -1, 1e308, float("nan"), float("inf"), "", "x" * 50, 0.5, "running"])
            if r < 0.6:
                return [doc(depth + 1) for _ in range(rnd.randint(0, 4))]
            return {rnd.choice(keys): doc(depth + 1) for _ in range(rnd.randint(0, 6))}

        for _ in range(3000):
            d = doc()
            if isinstance(d, dict):
                dg.parse_report(d).overall
            else:
                with self.assertRaises(ValueError):
                    dg.parse_report(d)

    def test_diagnostics_fetch_turns_every_bad_server_into_a_result(self):
        cases = {
            "dropped mid-body": (truncated, "unreachable"),
            "html": (lambda h: send(h, b"<html>hi</html>"), "bad"),
            "huge": (lambda h: send(h, b"[" + b"1," * 1_200_000 + b"1]"), "bad"),
            "deeply nested": (lambda h: send(h, b"[" * 200_000), "bad"),
            "404": (lambda h: send(h, b"", 404), "unsupported"),
            "500": (lambda h: send(h, b"", 500), "unreachable"),
        }
        for label, (respond, expected) in cases.items():
            srv = Server(respond)
            try:
                with self.subTest(label):
                    self.assertEqual(dg.fetch("127.0.0.1", srv.port, timeout=3).kind, expected)
            finally:
                srv.close()

    def test_control_client_never_raises(self):
        srv = Server(truncated)
        try:
            ok, msg = control.send_action("127.0.0.1", srv.port, "stop", "t", timeout=3)
            self.assertFalse(ok)
            self.assertEqual(control.get_status("127.0.0.1", srv.port, timeout=3).kind, "unreachable")
        finally:
            srv.close()
        srv = Server(lambda h: send(h, b"[1, 2]"))
        try:
            self.assertEqual(control.get_status("127.0.0.1", srv.port).kind, "no_daemon")
            self.assertFalse(control.send_action("127.0.0.1", srv.port, "stop", "t")[0])
        finally:
            srv.close()

    def test_unknown_pipeline_state_is_not_shown_as_off(self):
        """'off' invites a click on Start for a pipeline nobody actually knows the state of."""
        for raw in ("unknown", "", "maintenance", None):
            self.assertEqual(control.ui_state(raw), control.UNKNOWN)
        self.assertEqual(control.ui_state("inactive"), control.OFF)


# ------------------------------------------------------------------ discovery probe

class ProbeTests(unittest.TestCase):
    def probe(self, respond):
        srv = Server(respond)
        try:
            return discovery.probe("127.0.0.1", srv.port, timeout=2)
        finally:
            srv.close()

    def test_recognises_both_layouts(self):
        self.assertEqual(self.probe(lambda h: send(h, legacy_frame(123))), 123)
        rig = rig_frame(321)[3]
        self.assertEqual(self.probe(lambda h: send(h, rig, headers={"Content-Encoding": "gzip"})), 321)

    def test_rejects_look_alikes(self):
        """Scanning must not list unrelated web servers."""
        junk = [lambda h: send(h, b"<html>router</html>"),
                lambda h: send(h, b"", 404),
                lambda h: send(h, b"", 503),
                lambda h: send(h, b"\x1f\x8b" + b"garbage" * 5),
                lambda h: send(h, legacy_frame(50)[:-5]),
                truncated]
        for i, respond in enumerate(junk):
            with self.subTest(i):
                self.assertIsNone(self.probe(respond))

    def test_closed_port_is_not_a_sender(self):
        self.assertIsNone(discovery.probe("127.0.0.1", 1, timeout=0.3))


# ------------------------------------------------------------------ the polling thread

try:
    from PySide6.QtCore import QCoreApplication, Qt
    from basestation.net.workers import NetworkStats, PollWorker
    HAVE_QT = True
except ImportError:                                   # pragma: no cover
    HAVE_QT = False


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PollWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def start(self, respond, interval=0.02, timeout=3.0):
        srv = Server(respond)
        self.addCleanup(srv.close)
        stats = NetworkStats()
        w = PollWorker(f"http://127.0.0.1:{srv.port}/latest.bin", interval, stats, timeout=timeout)
        self.states, self.ready = [], []
        w.state_changed.connect(lambda s, d: self.states.append((s, d)), Qt.DirectConnection)
        w.frame_ready.connect(lambda: self.ready.append(time.time()), Qt.DirectConnection)
        w.start()
        self.addCleanup(lambda: (w.stop(), w.wait(3000)))
        return w, stats, srv

    def test_a_slow_gui_gets_only_the_newest_frame_and_memory_does_not_pile_up(self):
        """Frames used to queue without limit when the GUI was slower than the network."""
        counter = {"n": 0}

        def respond(h):
            counter["n"] += 1
            send(h, legacy_frame(10 + counter["n"], seed=counter["n"]))

        w, stats, _ = self.start(respond)
        self.assertTrue(wait_for(lambda: counter["n"] >= 25))          # GUI "busy" meanwhile
        self.assertLessEqual(len(self.ready), 1)                       # one wake-up, not 25
        newest = w.take_frame()
        self.assertIsNotNone(newest)
        self.assertGreater(newest.num_points, 20)                      # the latest, not the first
        self.assertIsNone(w.take_frame())                              # mailbox is now empty

    def test_dropped_connection_mid_body_does_not_kill_the_thread(self):
        """http.client.IncompleteRead is not an OSError: it escaped and ended the thread silently."""
        calls = {"n": 0}

        def respond(h):
            calls["n"] += 1
            if calls["n"] <= 3:
                truncated(h)
            else:
                send(h, legacy_frame(30))

        w, stats, _ = self.start(respond)
        self.assertTrue(wait_for(lambda: any(s == "live" for s, _ in self.states)))
        self.assertIn("error", [s for s, _ in self.states])
        self.assertTrue(w.isRunning())
        self.assertGreaterEqual(stats.snapshot()["frames_failed"], 3)

    def test_unusable_address_is_an_error_state_not_a_dead_thread(self):
        stats = NetworkStats()
        w = PollWorker("http://[::1/latest.bin", 0.02, stats, timeout=1.0)     # invalid IPv6 literal: fails at once
        states = []
        w.state_changed.connect(lambda s, d: states.append(s), Qt.DirectConnection)
        w.start()
        self.assertTrue(wait_for(lambda: "error" in states))
        self.assertTrue(w.isRunning())
        w.stop()
        self.assertTrue(w.wait(3000))

    def test_not_modified_is_neither_a_failure_nor_a_frame(self):
        """The rig answers 304 for an unchanged frame; that must not look like an error."""
        body = legacy_frame(40)

        def respond(h):
            if h.headers.get("If-None-Match") == '"v1"':
                h.send_response(304)
                h.send_header("ETag", '"v1"')
                h.end_headers()
            else:
                send(h, body, headers={"ETag": '"v1"'})

        w, stats, _ = self.start(respond)
        self.assertTrue(wait_for(lambda: stats.snapshot()["frames_received"] >= 1))
        time.sleep(0.4)                                               # many more polls -> 304s
        snap = stats.snapshot()
        self.assertEqual(snap["frames_received"], 1)
        self.assertEqual(snap["frames_failed"], 0)
        self.assertEqual(self.states[-1][0], "live")

    def test_rig_format_end_to_end(self):
        xyz, rgb, t, body = rig_frame(150)
        w, stats, _ = self.start(lambda h: send(h, body, headers={"Content-Encoding": "gzip"}))
        self.assertTrue(wait_for(lambda: self.ready))
        self.assertEqual(w.take_frame().num_points, 150)

    def test_stop_returns_promptly_even_mid_download(self):
        """Disconnect used to freeze the window for the whole network timeout."""
        hit = threading.Event()

        def stall(h):
            h.send_response(200)
            h.send_header("Content-Length", "50000000")
            h.end_headers()
            h.wfile.write(b"x" * 1000)
            h.wfile.flush()
            hit.set()
            time.sleep(8)

        w, stats, _ = self.start(stall, timeout=10.0)
        self.assertTrue(hit.wait(5))
        started = time.time()
        w.stop()
        self.assertTrue(w.wait(3000), "worker still running after stop()")
        self.assertLess(time.time() - started, 2.5)

    def test_failures_back_off_instead_of_hammering(self):
        calls = []
        w, stats, _ = self.start(lambda h: (calls.append(time.time()), send(h, b"", 500)), interval=0.02)
        time.sleep(1.2)
        self.assertLess(len(calls), 20, len(calls))                    # 20 ms interval would be ~60


if __name__ == "__main__":
    unittest.main()
