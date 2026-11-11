"""Point-cloud writers, and the Pi-side daemon/diagnostics corner cases."""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sensor_side"))

from basestation.core import export
import pitfusion_control as pc
import sensor_diagnostics as sd


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        rng = np.random.default_rng(0)
        self.xyz = rng.uniform(-5, 5, (300, 3)).astype(np.float32)
        self.rgb = rng.integers(0, 256, (300, 3)).astype(np.uint8)

    def path(self, name):
        return os.path.join(self.dir, name)

    def test_ply_header_and_payload(self):
        p = self.path("a.ply")
        export.write_ply(p, self.xyz, self.rgb)
        raw = Path(p).read_bytes()
        head, _, body = raw.partition(b"end_header\n")
        self.assertIn(b"element vertex 300", head)
        rec = np.frombuffer(body, dtype=np.dtype([("p", "<f4", 3), ("c", "u1", 3)]))
        self.assertTrue(np.array_equal(rec["p"], self.xyz))
        self.assertTrue(np.array_equal(rec["c"], self.rgb))

    def test_pcd_header_and_payload(self):
        p = self.path("a.pcd")
        export.write_pcd(p, self.xyz, self.rgb)
        raw = Path(p).read_bytes()
        head, _, body = raw.partition(b"DATA binary\n")
        self.assertIn(b"POINTS 300", head)
        rec = np.frombuffer(body, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<u4")])
        self.assertTrue(np.array_equal(rec["x"], self.xyz[:, 0]))
        packed = (self.rgb[:, 0].astype(np.uint32) << 16) | (self.rgb[:, 1].astype(np.uint32) << 8) | self.rgb[:, 2]
        self.assertTrue(np.array_equal(rec["rgb"], packed))

    def test_files_open_in_an_independent_reader(self):
        try:
            import open3d as o3d
        except Exception:
            self.skipTest("open3d not installed (it is no longer a dependency)")
        for name in ("b.pcd", "b.ply"):
            p = self.path(name)
            export.write_cloud(p, self.xyz, self.rgb)
            cloud = o3d.io.read_point_cloud(p)
            self.assertEqual(len(cloud.points), 300, name)
            self.assertLess(float(np.abs(np.asarray(cloud.points) - self.xyz).max()), 1e-5, name)

    def test_empty_cloud_and_no_colours_and_mismatched_colours(self):
        export.write_ply(self.path("e.ply"), np.zeros((0, 3), np.float32), None)
        export.write_pcd(self.path("e.pcd"), np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint8))
        export.write_ply(self.path("n.ply"), self.xyz, None)
        export.write_pcd(self.path("m.pcd"), self.xyz, self.rgb[:10])      # wrong length: geometry only
        self.assertIn(b"FIELDS x y z\n", Path(self.path("m.pcd")).read_bytes())

    def test_unknown_extension_is_refused_and_failures_leave_no_partial_file(self):
        with self.assertRaises(ValueError):
            export.write_cloud(self.path("x.xyz"), self.xyz, self.rgb)
        bad = os.path.join(self.dir, "no_such_folder", "a.ply")
        with self.assertRaises(OSError):
            export.write_ply(bad, self.xyz, self.rgb)
        self.assertFalse(any(f.endswith(".tmp") for f in os.listdir(self.dir)))


class PiDaemonTests(unittest.TestCase):
    def test_shutdown_permission_is_not_re_checked_on_every_status_poll(self):
        """`sudo -n -l` is a process spawn; the app polls /status every second or two."""
        spawned = []

        def runner(cmd, timeout=20):
            spawned.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "show"]:
                return 0, "ActiveState=active\nSubState=running\nActiveEnterTimestamp=now"
            return 0, ""

        clock = {"t": 1000.0}
        pc.shutil.which = lambda _n: "/usr/bin/systemctl"
        from http.server import ThreadingHTTPServer
        import threading, urllib.request
        handler = pc.make_handler(lambda: "tok", runner=runner, clock=lambda: clock["t"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/status"
            for _ in range(8):
                urllib.request.urlopen(url, timeout=3).read()
            sudo_calls = lambda: sum(1 for c in spawned if c[0] == "sudo")
            self.assertEqual(sudo_calls(), 1)
            clock["t"] += pc.SHUTDOWN_CHECK_TTL + 1                       # a minute later: ask again
            urllib.request.urlopen(url, timeout=3).read()
            self.assertEqual(sudo_calls(), 2)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_idle_connections_time_out(self):
        self.assertGreater(pc.make_handler(lambda: "t").timeout, 0)


class PiDiagnosticsTests(unittest.TestCase):
    def test_one_broken_entry_does_not_break_the_whole_report(self):
        col = sd.DiagnosticsCollector()
        real = col._entry
        col._entry = lambda topic, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")) \
            if topic == "livox/lidar" else real(topic, *a, **k)
        rep = col.report()
        self.assertEqual(rep["sensors"]["lidar"]["state"], "unknown")
        self.assertIn("boom", rep["sensors"]["lidar"]["message"])
        self.assertIn("state", rep["sensors"]["camera"])
        self.assertIn("web_stream", rep["pipelines"])


if __name__ == "__main__":
    unittest.main()
