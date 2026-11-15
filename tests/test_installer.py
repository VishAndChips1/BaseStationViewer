"""The Pi installer, run for real under bash with stand-in system commands.

`sudo`, `systemctl`, `ufw`, `loginctl`, `visudo`, `curl`, `id`, `hostname` are
replaced by small logging scripts, so every step the installer takes is
recorded and asserted without touching the machine running the tests.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

BASH = shutil.which("bash")


def posix(path: Path) -> str:
    """Path as bash wants it (MSYS/Git Bash on Windows, unchanged elsewhere)."""
    cyg = shutil.which("cygpath")
    if cyg:
        return subprocess.run([cyg, "-u", str(path)], capture_output=True, text=True).stdout.strip()
    return str(path)


STUBS = {
    "id": '#!/usr/bin/env bash\ncase "$1" in -u) echo "${STUB_UID:-1000}";; -un) echo vish;; *) echo vish;; esac\n',
    "loginctl": '#!/usr/bin/env bash\necho "loginctl $*" >> "$STUB_LOG"\n',
    "systemctl": ('#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n'
                  '[ -n "${STUB_HANG:-}" ] && [ "$2" = "daemon-reload" ] && sleep 60\n'
                  'if [ "$1 $2" = "--user cat" ] && [ "${STUB_NO_PIPELINE:-0}" = "1" ]; then exit 1; fi\n'),
    "ufw": '#!/usr/bin/env bash\necho "ufw $*" >> "$STUB_LOG"\n',
    "visudo": '#!/usr/bin/env bash\necho "visudo $*" >> "$STUB_LOG"\nexit "${STUB_VISUDO_RC:-0}"\n',
    "sudo": ('#!/usr/bin/env bash\n'
             'echo "sudo $*" >> "$STUB_LOG"\n'
             'case "$1" in\n'
             '  ufw) [ "$2" = "status" ] && echo "Status: active";;\n'
             '  tee) cat > "$STUB_LOG.sudoers_rule";;\n'
             '  visudo) exit "${STUB_VISUDO_RC:-0}";;\n'
             'esac\nexit 0\n'),
    "curl": '#!/usr/bin/env bash\necho \'{"pipeline": "active", "installed": true}\'\n',
    "hostname": '#!/usr/bin/env bash\necho "192.168.1.141 "\n',
    "python3": '#!/usr/bin/env bash\nexec python "$@"\n',
}


@unittest.skipUnless(BASH, "bash not available")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_bytes(body.encode())          # LF line endings
            f.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")

    def run_installer(self, **extra):
        env = dict(os.environ)
        env.update({"HOME": posix(self.home), "STUB_LOG": posix(self.log),
                    "PATH": posix(self.bin) + ":" + env["PATH"]})
        env.update({k: str(v) for k, v in extra.items()})
        return subprocess.run([BASH, posix(ROOT / "sensor_side" / "pi_setup.sh")],
                              capture_output=True, text=True, env=env, timeout=60)

    def calls(self):
        return self.log.read_text()

    def test_installs_everything_the_base_station_needs(self):
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        daemon = self.home / ".local/share/pitfusion3d-control/pitfusion_control.py"
        self.assertEqual(daemon.read_bytes().replace(b"\r\n", b"\n"),
                         (ROOT / "sensor_side/pitfusion_control.py").read_bytes().replace(b"\r\n", b"\n"),
                         "the installed daemon must be exactly the repo's daemon")

        token = (self.home / ".config/pitfusion3d/control_token").read_text().strip()
        self.assertGreaterEqual(len(token), 20)
        self.assertIn(token, r.stdout)                         # shown so it can be pasted into the app

        unit = (self.home / ".config/systemd/user/pitfusion3d-control.service").read_text()
        self.assertIn("ExecStart=/usr/bin/python3 ", unit)
        self.assertIn("pitfusion_control.py --port 8081", unit)
        self.assertNotIn("@", unit)                            # no unfilled placeholders
        self.assertNotIn("$", unit)

        calls = self.calls()
        self.assertIn("loginctl enable-linger vish", calls)
        self.assertIn("systemctl --user enable pitfusion3d-control.service", calls)
        self.assertIn("systemctl --user restart pitfusion3d-control.service", calls)
        self.assertIn("ufw allow 8081/tcp", calls)             # the firewall was blocking it
        rule = Path(str(self.log) + ".sudoers_rule").read_text()
        self.assertRegex(rule, r"^vish ALL=\(root\) NOPASSWD: \S*systemctl poweroff\s*$")
        self.assertIn("visudo -cf /etc/sudoers.d/pitfusion3d-poweroff", calls)   # validated, not trusted
        self.assertIn("8081", r.stdout)

    def test_a_different_installed_service_is_backed_up_not_silently_replaced(self):
        """The Pi's own copy can carry features the repo lacks; never lose it."""
        self.run_installer()
        daemon = self.home / ".local/share/pitfusion3d-control/pitfusion_control.py"
        daemon.write_text("# edited on the Pi: has can_sleep and full_poweroff\n")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backups = list(daemon.parent.glob("pitfusion_control.py.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("full_poweroff", backups[0].read_text())
        self.assertIn("kept at", r.stdout + r.stderr)

    def test_rerunning_with_an_identical_service_makes_no_backup(self):
        self.run_installer()
        self.run_installer()
        daemon_dir = self.home / ".local/share/pitfusion3d-control"
        self.assertEqual(list(daemon_dir.glob("pitfusion_control.py.bak-*")), [])
        self.assertEqual(list(daemon_dir.glob("*.new")), [])

    def test_the_sudo_rule_grants_poweroff_and_nothing_else(self):
        self.run_installer()
        rule = Path(str(self.log) + ".sudoers_rule").read_text()
        self.assertNotIn("ALL\n", rule)
        self.assertEqual(rule.count("NOPASSWD"), 1)
        self.assertTrue(rule.strip().endswith("systemctl poweroff"))

    def test_rerun_keeps_the_token_and_updates_in_place(self):
        self.run_installer()
        tok = self.home / ".config/pitfusion3d/control_token"
        first = tok.read_text()
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tok.read_text(), first)
        self.assertIn("Keeping the existing control token", r.stdout)

    def test_an_invalid_sudoers_rule_is_removed_not_left_behind(self):
        r = self.run_installer(STUB_VISUDO_RC=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sudo rm -f /etc/sudoers.d/pitfusion3d-poweroff", self.calls())
        self.assertIn("did not validate", r.stdout + r.stderr)

    def test_warns_but_succeeds_when_the_pipeline_service_is_missing(self):
        r = self.run_installer(STUB_NO_PIPELINE=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pitfusion3d.service is not installed", r.stdout + r.stderr)

    def test_a_hung_systemd_call_becomes_a_visible_error_not_a_frozen_terminal(self):
        """The user's terminal sat silent forever; every blocking step now has a time limit."""
        import time
        started = time.time()
        r = self.run_installer(STUB_HANG=1, PITFUSION_STEP_TIMEOUT=2)
        self.assertLess(time.time() - started, 25)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("failed or timed out", r.stdout + r.stderr)
        self.assertIn("installer - starting", r.stdout)          # it said something straight away

    def test_prints_immediately_before_doing_any_work(self):
        r = self.run_installer()
        self.assertTrue(r.stdout.lstrip().startswith("PitFusion3D control installer - starting"))

    def test_refuses_to_run_as_root(self):
        r = self.run_installer(STUB_UID=0)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not as root", r.stderr)
        self.assertEqual(self.calls(), "")                      # changed nothing

    def test_embedded_daemon_has_not_drifted_from_the_source(self):
        import build_pi_installer
        self.assertEqual((ROOT / "sensor_side/pi_setup.sh").read_bytes(), build_pi_installer.build(),
                         "pi_setup.sh is stale: run  python tools/build_pi_installer.py")


if __name__ == "__main__":
    unittest.main()
