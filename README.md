# wren-cam

All-in-one bird cam for a Raspberry Pi 5. One Python process serves:

- **Live MJPEG stream(s)** — point a browser at the stream URL.
- **A built-in web UI** — adjust camera and recording settings, browse and play recordings.
- **Motion-triggered recording** — clips written to disk as MP4.

Unlike hootcam (which split streaming, motion detection, and UI across machines), wren-cam runs everything on the Pi.

## URLs

After it's running on the Pi at `pi.local:8080`:

| What                    | URL                                          |
|-------------------------|----------------------------------------------|
| Web UI                  | `http://pi.local:8080/`                      |
| Live MJPEG stream cam 0 | `http://pi.local:8080/stream/0`              |
| Live MJPEG stream cam 1 | `http://pi.local:8080/stream/1` *(2-cam only)* |
| Single JPEG snapshot    | `http://pi.local:8080/snapshot/0.jpg`        |
| JSON status             | `http://pi.local:8080/api/status`            |

Open `/stream/0` directly in any browser to watch live.

## Install on Pi 5

```bash
# System deps
sudo apt update
sudo apt install -y python3-picamera2 python3-libcamera python3-venv ffmpeg

# Clone / copy this repo
cd ~ && git clone <your-repo> wren-cam
cd wren-cam

# venv with --system-site-packages so picamera2 is visible
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a config
cp config.example.json config.json
$EDITOR config.json   # set cameras, recordings_dir, etc.

# Run it
python -m wren_cam
```

Then open `http://<pi-ip>:8080/` in a browser.

## Run as a systemd service

```bash
sudo cp wren-cam.service /etc/systemd/system/wren-cam.service
sudo systemctl daemon-reload
sudo systemctl enable --now wren-cam
journalctl -u wren-cam -f
```

The unit assumes the repo lives at `/home/pi/wren-cam` with a `.venv/` inside it. Adjust the unit if your paths differ.

## Configuration

Config lives in `config.json` (path overridable with `WREN_CAM_CONFIG`). Changes made in the web UI are saved back to this file.

### Top-level

| Key              | Default          | Notes                                                  |
|------------------|------------------|--------------------------------------------------------|
| `host`           | `0.0.0.0`        | Bind address                                           |
| `port`           | `8080`           | HTTP port                                              |
| `recordings_dir` | `./recordings`   | Where motion clips are written                         |
| `stream_quality` | `80`             | JPEG quality for MJPEG (1-100)                         |
| `stream_maxrate` | `15`             | Max framerate served to viewers                        |
| `cameras`        | one entry, id 0  | List of camera configs (add a second entry for cam 1)  |

### Per-camera

| Key                  | Default       | Notes                                                                 |
|----------------------|---------------|-----------------------------------------------------------------------|
| `id`                 | `0`           | Pi CSI port: `0` or `1`                                               |
| `name`               | `Wren Cam`    | Display name in the UI                                                |
| `width` / `height`   | `1280` / `720`| Resolution                                                            |
| `framerate`          | `15`          | Capture fps                                                           |
| `autofocus`          | `continuous`  | `continuous` or `manual` (requires Pi Camera Module 3)                |
| `lens_position`      | `0.0`         | Manual focus; 0 ≈ infinity, ~0.5 ≈ 50 cm                              |
| `rotate_180`         | `false`       | Flip video 180° for an upside-down camera                             |
| `motion_enabled`     | `true`        | Toggle motion-triggered recording                                     |
| `motion_threshold`   | `1500`        | Changed pixels needed to declare motion                               |
| `noise_level`        | `32`          | Per-pixel intensity delta filtered as noise                           |
| `event_gap_seconds`  | `30`          | Seconds of no motion before clip closes                               |
| `max_clip_seconds`   | `300`         | Hard cap on a single clip (0 = no cap)                                |

To add a second camera, append another entry with `"id": 1`.

## How motion detection works

Each frame is converted to grayscale and compared to the previous reference frame. Any per-pixel intensity delta below `noise_level` is ignored. If the number of remaining changed pixels exceeds `motion_threshold`, a recording starts. The clip keeps going until `event_gap_seconds` of stillness — or `max_clip_seconds`, whichever comes first.

Tuning:

- **Too many false positives** (clips of wind/clouds): raise `motion_threshold` or `noise_level`.
- **Missing real motion**: lower `motion_threshold`.
- **Clips end too eagerly** (chops the bird in half): raise `event_gap_seconds`.

## Recordings

Clips are written to `recordings_dir` as `cam<id>-YYYYMMDD-HHMMSS.mp4` (H.264, ultrafast preset, faststart). The UI lists, plays, downloads, and deletes them. No database — the UI just lists `*.mp4` from disk.

## Notes

- **One process, two cameras:** Starting cam 1 is staggered 5 seconds after cam 0 to avoid libcamera contention. If cam 1 still fails to start, lower its resolution or framerate first.
- **Recording uses ffmpeg** piped raw frames. The Pi 5 CPU handles 1080p15 fine; for 1080p30 on both cameras you may need to drop fps or resolution.
- **No auth.** This is intended for a trusted home network. Put it behind a reverse proxy with auth if you expose it.
