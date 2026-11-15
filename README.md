# Ground Station 3D

Desktop base station for a mine / landfill survey rig. It pulls the rover's live
RGB + thermal point cloud over WiFi, lets you crop, measure and filter it, and
can check on and power the sensor PC. Built on Qt (PySide6) and PyVista/VTK.

![Ground Station 3D, dark theme: measuring from the top of a mound to the bottom of a pit](docs/screenshot-dark.png)

<details>
<summary>Light theme</summary>

![Ground Station 3D, light theme, with the crop box](docs/screenshot-light.png)

</details>

## Features

- **Live 3D cloud** with RGB or thermal colouring, scale grid, origin and orientation axes, and a Home button for the default view
- **Crop** with a draggable 3D box or exact limits; **Measure** distance, slope, path length and plan-view area, with the temperature at each point
- **Process** every frame live: distance range, temperature band, stray-point removal, voxel thinning (saves and recordings use the result)
- **Sensor diagnostics**: camera / thermal / lidar connection and fps, pipeline health, and Pi vitals including under-voltage
- **Power button**: start, stop or restart the sensor pipeline, or power the Pi off (with a warning that its power button is then needed to turn it back on)
- **Scan for senders** on the local network, **record** frames on an interval, **save** clouds and screenshots
- **Light, Dark or System** appearance (toggle in the title bar)

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python -m basestation           # or double-click "Run Base Station.bat"
```

Press **Scan for senders**, pick the rover, then **Connect**. Python 3.10+.

No rig? Run a stand-in and connect to `127.0.0.1`:

```bash
python tools/mock_rover_server.py --scene terrain --wire rig --token demo
```

It serves a cloud (`--wire rig` uses the rig's real gzip format), simulated diagnostics, and the real control daemon over a fake systemd.

## Sensor PC

The rig is [PitFusion3D](https://github.com/VishAndChips1/PitFusion3D) (ROS 2 on a Raspberry Pi). Diagnostics and power control need a small addition there; see [`sensor_side/README.md`](sensor_side/README.md): Power control is one `curl` command on the Pi (`sensor_side/pi_setup.sh`). Without it, everything else still works.

## Layout

```text
basestation/
  app.py        main window, tools, inspector
  core/         no-Qt logic: protocol, processing, diagnostics, control client, discovery
  ui/           theme (light/dark), widgets, icons, diagnostics window
  net/          Qt threads: polling, scanning, commands
  assets/       app icon
sensor_side/    Pi-side diagnostics + control daemon, and the patch for PitFusion3D
tools/          mock rover server
tests/          python -m unittest discover -s tests
data/           created at run time: recent hosts, crash log, captures (git-ignored)
```

## Protocol

`GET http://<rover>:<port>/latest.bin` returns one frame. Two layouts are understood and told apart by length alone; a gzip wrapper is detected and removed first:

- **float32**: `uint32 N`, then `N x 7 float32` (`x y z r g b thermal`; metres, RGB 0-1, thermal in Kelvin or NaN)
- **rig** (what [PitFusion3D](https://github.com/VishAndChips1/PitFusion3D) sends, normally gzipped): `uint32 N, uint32 seq, float32 ox oy oz scale`, then `N x { int16 xyz, uint8 rgb, int16 thermal }` (xyz = q * scale + offset; thermal = Kelvin x 100, `-32768` = none)

Points with NaN/infinite coordinates are dropped on parse. The rig answers `304` for an unchanged frame, which the app honours (an `ETag` is sent back), so a 5 Hz poll of a 1 Hz cloud costs one download per second. Only `basestation/core/protocol.py` parses frames, so a format change touches one file.

Known rig-side limit: thermal is stored as Kelvin x 100 in an `int16`, which tops out at 327.67 K (54.5 C); anything hotter reads as 54.5 C.

Trusted LAN only: no authentication or encryption on the point-cloud feed.
