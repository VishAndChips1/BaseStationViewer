"""Refresh-rate measurement and the saved-cloud reader."""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from basestation.core import export                         # noqa: E402
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
