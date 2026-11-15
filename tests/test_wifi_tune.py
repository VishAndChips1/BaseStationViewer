"""sensor_side/pi_wifi_tune.sh, run for real under bash with stand-in commands.

`sudo`, `iw`, `ip`, `ping`, `systemctl` are small logging scripts, so every change
the script would make to a Pi is recorded and asserted without touching anything.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")


def posix(path: Path) -> str:
    cyg = shutil.which("cygpath")
    if cyg:
        return subprocess.run([cyg, "-u", str(path)], capture_output=True, text=True).stdout.strip()
    return str(path)


STUBS = {
    "id": '#!/usr/bin/env bash\ncase "$1" in -u) echo "${STUB_UID:-1000}";; *) echo pi;; esac\n',
    "ip": '#!/usr/bin/env bash\necho "default via 192.168.1.1 dev wlan0 proto dhcp"\n',
    "ping": '#!/usr/bin/env bash\n'
            'if [ -f "$STUB_STATE/off" ]; then echo "rtt min/avg/max/mdev = 1.1/3.2/9.8/2.0 ms";\n'
            'else echo "rtt min/avg/max/mdev = 40.0/530.4/1096.0/300.0 ms"; fi\n',
    "iw": ('#!/usr/bin/env bash\n'
           'echo "iw $*" >> "$STUB_LOG"\n'
           'case "$*" in\n'
           '  "dev") printf "phy#0\\n\\tInterface ${STUB_IFACE:-wlan0}\\n";;\n'
           '  *"get power_save") if [ -f "$STUB_STATE/off" ]; then echo "Power save: off"; else echo "Power save: on"; fi;;\n'
           '  *" info") printf "Interface wlan0\\n\\ttype ${STUB_TYPE:-managed}\\n";;\n'
           '  *" link") printf "Connected to aa:bb\\n\\tsignal: -58 dBm\\n\\ttx bitrate: 173.3 MBit/s\\n";;\n'
           '  *"set power_save off") [ -n "${STUB_IW_FAIL:-}" ] && exit 1; touch "$STUB_STATE/off";;\n'
           'esac\n'),
    "systemctl": ('#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n'
                  'if [ "$1" = "is-active" ]; then [ "${STUB_NM:-1}" = "1" ]; exit $?; fi\n'),
    "sudo": ('#!/usr/bin/env bash\necho "sudo $*" >> "$STUB_LOG"\n'
             'case "$1" in\n'
             '  tee) cat > "$STUB_STATE/written_$(basename "$2")";;\n'
             '  -n|-v) exit 0;;\n'
             '  *) "$@";;\n'
             'esac\n'),
}


@unittest.skipUnless(BASH, "bash not available")
class WifiTuneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin, self.state = self.tmp / "bin", self.tmp / "state"
        self.bin.mkdir()
        self.state.mkdir()
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_bytes(body.encode())
            f.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")

    def run_script(self, **extra):
        env = dict(os.environ)
        env.update({"STUB_LOG": posix(self.log), "STUB_STATE": posix(self.state),
                    "PATH": posix(self.bin) + ":" + env["PATH"]})
        env.update({k: str(v) for k, v in extra.items()})
        return subprocess.run([BASH, posix(ROOT / "sensor_side" / "pi_wifi_tune.sh")],
                              capture_output=True, text=True, env=env, timeout=60)

    def written(self, name):
        return (self.state / f"written_{name}").read_text()

    def test_turns_power_save_off_now_and_keeps_it_off_with_networkmanager(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("iw dev wlan0 set power_save off", self.log.read_text())
        conf = self.written("99-pitfusion3d-wifi.conf")
        self.assertIn("[connection]", conf)
        self.assertIn("wifi.powersave = 2", conf)

    def test_shows_before_and_after_so_the_effect_is_visible(self):
        r = self.run_script()
        self.assertIn("Power save: on", r.stdout)
        self.assertIn("Power save: off", r.stdout)
        self.assertIn("worst 1096 ms", r.stdout)               # before
        self.assertIn("worst 10 ms", r.stdout)                 # after
        self.assertIn("signal: -58 dBm", r.stdout)

    def test_without_networkmanager_a_boot_service_is_installed_instead(self):
        r = self.run_script(STUB_NM=0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        unit = self.written("pitfusion3d-wifi.service")
        self.assertIn("set power_save off", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("systemctl enable pitfusion3d-wifi.service", self.log.read_text())

    def test_changes_nothing_but_wifi_power_save(self):
        """It must not touch the control service, the pipeline, or anything else."""
        self.run_script()
        calls = self.log.read_text()
        for forbidden in ("pitfusion3d-control", "pitfusion3d.service", "poweroff", "rpi-eeprom",
                          "restart", "stop", "rm "):
            self.assertNotIn(forbidden, calls, forbidden)
        self.assertEqual(sorted(p.name for p in self.state.iterdir() if p.name.startswith("written_")),
                         ["written_99-pitfusion3d-wifi.conf"])

    def test_refuses_to_run_as_root_and_changes_nothing(self):
        r = self.run_script(STUB_UID=0)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not as root", r.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_a_failed_change_is_reported_and_nothing_is_made_permanent(self):
        r = self.run_script(STUB_IW_FAIL=1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not change power-save", r.stdout + r.stderr)
        self.assertFalse(any(p.name.startswith("written_") for p in self.state.iterdir()))

    def test_picks_the_interface_it_finds_and_honours_an_override(self):
        self.run_script(STUB_IFACE="wlan1")
        self.assertIn("iw dev wlan1 set power_save off", self.log.read_text())

    def test_on_the_hotspot_it_explains_and_changes_nothing(self):
        r = self.run_script(STUB_TYPE="AP")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hotspot", r.stdout)
        self.assertNotIn("set power_save", self.log.read_text())
        self.assertFalse(any(p.name.startswith("written_") for p in self.state.iterdir()))

    def test_speaks_before_doing_any_work(self):
        r = self.run_script()
        self.assertTrue(r.stdout.lstrip().startswith("PitFusion3D WiFi tuning - starting"))


if __name__ == "__main__":
    unittest.main()
