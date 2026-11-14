"""Build sensor_side/pi_setup.sh: a single self-contained installer for the Pi.

The installer embeds sensor_side/pitfusion_control.py, so the Pi needs nothing
but this one file (or one curl). Rerun after changing the daemon:

    python tools/build_pi_installer.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "tools" / "pi_setup.template.sh"
DAEMON = ROOT / "sensor_side" / "pitfusion_control.py"
OUT = ROOT / "sensor_side" / "pi_setup.sh"
PLACEHOLDER = "@@DAEMON@@"
HEREDOC_END = "PITFUSION_CONTROL_PY_EOF"


def build() -> bytes:
    daemon = DAEMON.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    if HEREDOC_END in daemon:
        raise SystemExit(f"daemon source contains the heredoc terminator {HEREDOC_END!r}")
    template = TEMPLATE.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise SystemExit("template must contain exactly one @@DAEMON@@ placeholder")
    return template.replace(PLACEHOLDER, daemon.rstrip("\n")).encode("utf-8")   # LF: this runs on Linux


if __name__ == "__main__":
    OUT.write_bytes(build())
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size:,} bytes)")
