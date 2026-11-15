"""Refresh-rate measurement and the saved-cloud reader."""

import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from basestation.core import export                         # noqa: E402
from basestation.core.quality import choose_level, parse_levels   # noqa: E402
from basestation.core.rate import RateMeter, format_cloud, format_hz   # noqa: E402
from basestation.net.workers import NetworkStats            # noqa: E402


class RateMeterTests(unittest.TestCase):
    def feed(self, meter, times):
        for t in times:
            meter.tick(t)

    def test_unknown_until_two_events(self):
        m = RateMeter()
        self.assertIsNone(m.rate(now=0.0))
        m.tick(0.0)
        self.assertIsNone(m.rate(now=0.1))

    def test_steady_rate_is_exact_at_any_speed(self):
        for hz in (0.4, 1.0, 4.8, 10.0, 30.0):
            m = RateMeter()
            self.feed(m, [i / hz for i in range(10)])
            self.assertAlmostEqual(m.rate(now=9 / hz), hz, places=6, msg=f"{hz} Hz")

    def test_does_not_flicker_between_zero_and_one_at_one_hertz(self):
        """The old counter-per-second showed 0 or 1 depending on when you looked."""
        m = RateMeter()
        self.feed(m, [float(i) for i in range(8)])
        for look in (7.0, 7.3, 7.6, 7.95):
            self.assertAlmostEqual(m.rate(now=look), 1.0, delta=0.15)

    def test_decays_toward_zero_when_frames_stop(self):
        m = RateMeter()
        self.feed(m, [i * 0.1 for i in range(10)])              # 10 Hz, last at 0.9
        live = m.rate(now=0.95)
        self.assertGreater(live, 9.0)
        later = [m.rate(now=0.9 + gap) for gap in (0.5, 1.0, 2.0, 5.0)]
        self.assertEqual(later, sorted(later, reverse=True))      # only ever falls
        self.assertLess(later[-1], 0.25)
        self.assertEqual(m.rate(now=60.0), 0.0)                   # fully stale

    def test_old_burst_does_not_inflate_the_rate(self):
        m = RateMeter()
        self.feed(m, [0.0, 0.01, 0.02, 0.03])                     # a burst, then silence
        self.assertEqual(m.rate(now=30.0), 0.0)

    def test_reset_forgets_everything(self):
        m = RateMeter()
        self.feed(m, [0.0, 1.0, 2.0])
        m.reset()
        self.assertIsNone(m.rate(now=2.0))
        self.assertIsNone(m.age(now=2.0))

    def test_simultaneous_events_do_not_divide_by_zero(self):
        m = RateMeter()
        self.feed(m, [1.0, 1.0, 1.0])
        self.assertIsNone(m.rate(now=1.0))


class FormatTests(unittest.TestCase):
    def test_hz_formatting(self):
        self.assertEqual(format_hz(None), "-")
        self.assertEqual(format_hz(float("nan")), "-")
        self.assertEqual(format_hz(0), "0 Hz")
        self.assertEqual(format_hz(0.4), "0.40 Hz")
        self.assertEqual(format_hz(4.83), "4.8 Hz")
        self.assertEqual(format_hz(12.4), "12 Hz")

    def test_cloud_text_always_carries_the_rate(self):
        self.assertEqual(format_cloud(123456, 4.83), "123,456 points @ 4.8 Hz")
        self.assertEqual(format_cloud(10, None, "pts"), "10 pts @ -")


class NetworkStatsHzTests(unittest.TestCase):
    def test_snapshot_reports_hz_and_draw_rate(self):
        s = NetworkStats()
        self.assertIsNone(s.snapshot()["hz"])
        self.assertEqual(s.snapshot()["fps"], 0.0)
        s.reset()
        for _ in range(3):
            s.record_frame(1000, 50, 0.01)
            s.record_render()
        snap = s.snapshot()
        self.assertEqual(snap["frames_received"], 3)
        self.assertIn("render_hz", snap)

    def test_reset_clears_the_rates(self):
        s = NetworkStats()
        for _ in range(3):
            s.record_frame(1000, 50, 0.01)
        s.reset()
        self.assertIsNone(s.snapshot()["hz"])


class QualityChoiceTests(unittest.TestCase):
    LEVELS = [8000, 20000, 40000, 60000, 80000]
    BPP = 8.3                                       # bytes per point, as measured on the rig

    def test_parse_levels_ignores_junk(self):
        self.assertEqual(parse_levels("8000, 20000,40000"), [8000, 20000, 40000])
        self.assertEqual(parse_levels("abc,,-5,0,9,3,3"), [3, 9])
        self.assertEqual(parse_levels(None), [])
        self.assertEqual(parse_levels("99999999999"), [])

    def test_fast_link_gets_full_detail(self):
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 5_000_000), 80000)

    def test_slow_link_gets_a_thin_cloud(self):
        # 150 KB/s (what the rig's WiFi managed): 20,000 points ~ 1.1 s, 8,000 ~ 0.44 s
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 150_000), 8000)

    def test_hopeless_link_still_returns_the_smallest_not_nothing(self):
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 1_000), 8000)

    def test_steps_down_when_over_budget_and_up_only_with_headroom(self):
        # at 300 KB/s: 20,000 points take 0.55 s, 40,000 take 1.1 s (over the 1 s budget)
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 300_000, current=80000), 20000)
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 300_000, current=40000), 20000)
        # 20,000 fits, and 40,000 would not fit in 60% of the budget: stay put
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 300_000, current=20000), 20000)
        # a clearly faster link (600 KB/s: 40,000 take 0.55 s) earns a step up
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 600_000, current=20000), 40000)

    def test_no_levels_or_no_measurement_is_harmless(self):
        self.assertIsNone(choose_level([], self.BPP, 100_000))
        self.assertEqual(choose_level(self.LEVELS, 0, 100_000, current=20000), 20000)
        self.assertEqual(choose_level(self.LEVELS, self.BPP, 0), 8000)


try:
    from PySide6.QtCore import QCoreApplication, Qt
    from basestation.net.workers import PollWorker
    HAVE_QT = True
except ImportError:                                           # pragma: no cover
    HAVE_QT = False

sys.path.insert(0, str(ROOT / "tests"))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AutoDetailTests(unittest.TestCase):
    """The worker asks for `?n=`, learns the rig's levels, and adapts to the link."""

    LEVELS = "200,500,1000,2000"

    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def start(self, quality, rate_bytes_per_s=None, budget=None):
        import test_robustness as tr
        self.asked = []

        def respond(h):
            query = h.path.partition("?")[2]
            self.asked.append(query)
            want = int(query[2:]) if query.startswith("n=") and query[2:].isdigit() else 2000
            n = min(l for l in (200, 500, 1000, 2000) if l >= want) if want <= 2000 else 2000
            rng = np.random.default_rng(n)
            xyz = rng.uniform(-5, 5, (n, 3)).astype(np.float32)
            body = tr.protocol.encode_rig_frame(
                xyz, rng.random((n, 3)).astype(np.float32), np.full(n, 300.0, np.float32), gzip_it=False)
            if rate_bytes_per_s:
                time.sleep(len(body) / rate_bytes_per_s)
            tr.send(h, body, headers={"X-Quality-Levels": self.LEVELS})

        srv = tr.Server(respond)
        self.addCleanup(srv.close)
        w = PollWorker(f"http://127.0.0.1:{srv.port}/latest.bin", 0.02, NetworkStats(),
                       timeout=5.0, quality=quality)
        if budget:
            w.auto_budget = budget
        self.found = []
        w.levels_found.connect(self.found.append, Qt.DirectConnection)
        w.start()
        self.addCleanup(lambda: (w.stop(), w.wait(3000)))
        return w

    @staticmethod
    def wait_for(cond, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_fixed_level_is_sent_and_levels_are_learned(self):
        w = self.start(500)
        w.set_quality(500)
        self.assertTrue(self.wait_for(lambda: self.found and "n=500" in self.asked))
        self.assertEqual(self.found[0], [200, 500, 1000, 2000])
        self.assertEqual(w.current_level, 500)

    def test_auto_settles_on_full_detail_when_the_link_is_fast(self):
        w = self.start(PollWorker.AUTO)
        self.assertTrue(self.wait_for(lambda: w.current_level == 2000))
        time.sleep(0.5)
        self.assertEqual(w.current_level, 2000)                 # and stays there

    def test_auto_drops_to_a_thin_cloud_on_a_slow_link(self):
        w = self.start(PollWorker.AUTO, rate_bytes_per_s=8000, budget=0.3)
        self.assertTrue(self.wait_for(lambda: w.current_level == 200),
                        f"stuck at {w.current_level}; asked {self.asked[-5:]}")

    def test_auto_begins_at_the_remembered_size_not_a_full_cloud(self):
        """On a weak link the first full-size download was the slowest thing the app did."""
        import test_robustness as tr
        asked = []

        def respond(h):
            asked.append(h.path.partition("?")[2])
            tr.send(h, tr.protocol.encode_rig_frame(
                np.zeros((10, 3), np.float32), np.zeros((10, 3), np.float32),
                np.full(10, 300.0, np.float32)), headers={"X-Quality-Levels": self.LEVELS})

        srv = tr.Server(respond)
        self.addCleanup(srv.close)
        w = PollWorker(f"http://127.0.0.1:{srv.port}/latest.bin", 0.02, NetworkStats(),
                       quality=PollWorker.AUTO, start_level=500)
        w.start()
        self.addCleanup(lambda: (w.stop(), w.wait(3000)))
        self.assertTrue(self.wait_for(lambda: bool(asked)))
        self.assertEqual(asked[0], "n=500")

    def test_reasserting_the_same_mode_does_not_forget_what_auto_learned(self):
        w = self.start(PollWorker.AUTO)
        self.assertTrue(self.wait_for(lambda: w.current_level == 2000))
        w.set_quality(PollWorker.AUTO)
        self.assertEqual(w.current_level, 2000)

    def test_switching_to_a_fixed_level_while_running_takes_effect(self):
        w = self.start(PollWorker.AUTO)
        self.assertTrue(self.wait_for(lambda: bool(self.found)))
        w.set_quality(200)
        self.assertTrue(self.wait_for(lambda: self.asked and self.asked[-1] == "n=200"))


def _tmp(name):
    return os.path.join(tempfile.mkdtemp(), name)


class ReadCloudTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(3)
        self.xyz = (rng.random((500, 3)) * 20 - 10).astype(np.float32)
        self.rgb = rng.integers(0, 256, (500, 3), dtype=np.uint8)

    def test_round_trip_with_colour(self):
        for ext in (".pcd", ".ply"):
            path = _tmp("c" + ext)
            export.write_cloud(path, self.xyz, self.rgb)
            xyz, rgb = export.read_cloud(path)
            np.testing.assert_array_equal(xyz, self.xyz)
            np.testing.assert_array_equal(rgb, self.rgb)

    def test_round_trip_without_colour(self):
        for ext in (".pcd", ".ply"):
            path = _tmp("g" + ext)
            export.write_cloud(path, self.xyz)
            xyz, rgb = export.read_cloud(path)
            np.testing.assert_array_equal(xyz, self.xyz)
            self.assertIsNone(rgb)

    def test_empty_cloud(self):
        path = _tmp("e.pcd")
        export.write_cloud(path, np.empty((0, 3), np.float32))
        xyz, rgb = export.read_cloud(path)
        self.assertEqual(len(xyz), 0)

    def test_ascii_pcd_and_ply(self):
        pcd = _tmp("a.pcd")
        Path(pcd).write_text(
            "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
            "WIDTH 2\nHEIGHT 1\nPOINTS 2\nDATA ascii\n1 2 3\n4 5 6\n")
        xyz, rgb = export.read_cloud(pcd)
        np.testing.assert_array_equal(xyz, [[1, 2, 3], [4, 5, 6]])
        ply = _tmp("a.ply")
        Path(ply).write_text(
            "ply\nformat ascii 1.0\nelement vertex 2\nproperty float x\nproperty float y\n"
            "property float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\n"
            "end_header\n1 2 3 255 0 0\n4 5 6 0 255 0\n")
        xyz, rgb = export.read_cloud(ply)
        np.testing.assert_array_equal(rgb, [[255, 0, 0], [0, 255, 0]])

    def test_non_finite_points_are_dropped_like_live_frames(self):
        bad = self.xyz.copy()
        bad[7] = np.nan
        path = _tmp("n.ply")
        export.write_cloud(path, bad, self.rgb)
        xyz, rgb = export.read_cloud(path)
        self.assertEqual(len(xyz), 499)
        self.assertEqual(len(rgb), 499)
        self.assertTrue(np.isfinite(xyz).all())

    def test_malformed_files_are_refused_with_a_message_not_a_crash(self):
        cases = {
            "trunc.pcd": None, "junk.pcd": b"hello world, not a cloud", "empty.ply": b"",
            "bad.ply": b"ply\nformat binary_big_endian 1.0\nelement vertex 1\n"
                       b"property float x\nproperty float y\nproperty float z\nend_header\n",
            "list.ply": b"ply\nformat ascii 1.0\nelement vertex 1\nproperty list uchar int x\nend_header\n",
            "huge.pcd": b"VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
                        b"WIDTH 9\nHEIGHT 1\nPOINTS 999999999999\nDATA binary\n",
        }
        good = _tmp("good.pcd")
        export.write_cloud(good, self.xyz)
        cases["trunc.pcd"] = Path(good).read_bytes()[:-100]
        for name, body in cases.items():
            path = _tmp(name)
            Path(path).write_bytes(body)
            with self.assertRaises(ValueError, msg=name):
                export.read_cloud(path)

    def test_wrong_extension_and_missing_file(self):
        with self.assertRaises(ValueError):
            export.read_cloud(_tmp("x.txt"))
        with self.assertRaises(OSError):
            export.read_cloud(_tmp("missing.pcd"))


if __name__ == "__main__":
    unittest.main()
