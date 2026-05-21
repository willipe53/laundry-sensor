# Laundry Sensor — Audio Capture Setup

Dedicated capture device to record washer and dryer audio for
downstream AI analysis of discrete sound signatures.

## Hardware

| Component | Details |
|-----------|---------|
| SBC | Raspberry Pi Zero 2W (Debian Trixie Lite, aarch64) |
| Microphone | INMP441 I2S MEMS omnidirectional mic (soldered to GPIO) |
| Power | 5V USB wall charger via micro-USB port |
| Network | SSH + HTTPS web UI, hostname `laundry` (mDNS → `laundry.local`) |

## Wiring

### INMP441 Microphone → Pi Zero GPIO

| Mic Pin | Pi Zero Pin | GPIO Function |
|---------|-------------|---------------|
| VDD | Pin 1 (3.3V) | Power |
| GND | Pin 6 (GND) | Ground |
| SCK | Pin 12 (GPIO 18) | PCM_CLK |
| WS | Pin 35 (GPIO 19) | PCM_FS |
| SD | Pin 38 (GPIO 20) | PCM_DIN |
| L/R | Pin 6 (GND) | Left channel select |

### Enclosure

3D-printed two-piece enclosure (front and back). Mesh files in repo:

- `laun_frt.3mf` — front panel
- `laun_bk.3mf` — back panel

## Boot Configuration (I2S Microphone)

Added to `/boot/firmware/config.txt`:

```
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
```

The `googlevoicehat-soundcard` overlay is a generic I2S mic driver — not
specific to Google hardware. It configures the I2S interface that the
INMP441 needs. Reboot required after changes.

## Verified Device Mapping

| Resource | Interface |
|----------|-----------|
| Audio capture | ALSA via `arecord` (I2S, `googlevoicehat-soundcard`) |

## Web Interface

A FastAPI server (`laundry_server.py`) provides an audio recorder UI
over HTTPS on port 443. It runs as a systemd service that starts on boot.

Features:
- Start/stop recording from the browser
- Browse and play back all saved recordings
- Download or delete recordings

### Deploy from your Mac

```bash
cd ~/github/laundry-sensor
python deploy.py
```

`deploy.py` SCPs the app files to the Pi and SSHes in to:

1. Copy files to `/opt/laundry-sensor` and create a Python venv
2. Install FastAPI + uvicorn
3. Create `/home/willipe/recordings/` for audio files
4. Generate a self-signed TLS certificate in `/etc/laundry-sensor/` (first run only)
5. Install, enable, and restart the `laundry-sensor` systemd service

Subsequent deploys after code changes are the same single command.
Open **https://laundry** in your browser.

### Service management

```bash
sudo systemctl status laundry-sensor
sudo systemctl restart laundry-sensor
sudo journalctl -u laundry-sensor -f
```

## Pre-installed Software

```
alsa-utils
python3 + venv
```

## Quick Tests

### Microphone — record 5 seconds of audio

```bash
arecord -D plughw:0 -c1 -r 48000 -f S32_LE -t wav -d 5 ~/test.wav
```

### Copy test files to laptop

```bash
scp willipe@laundry:~/test.wav .
```

## Capture Plan

The goal is to record one complete laundry load to capture three distinct
audio environments for later analysis:

1. **Washer running alone** (~45 min)
2. **Washer + dryer running together** (overlap period)
3. **Dryer running alone** (~45 min)

Start the capture before loading the washer. The resulting recording will
be segmented or analyzed in the next phase to isolate sound signatures
for each appliance state.

## Storage Estimates

Recordings are 48 kHz mono 32-bit WAV.

| Stream | Approximate bitrate | Per hour |
|--------|---------------------|----------|
| 48 kHz mono WAV audio | ~1.5 Mbit/s | ~0.7 GB |

A full washer + dryer cycle (~2 hours) will use roughly 1.4 GB.

## Status

- [x] Pi Zero 2W imaged with Trixie Lite
- [x] INMP441 microphone soldered to GPIO and tested (I2S)
- [x] I2S driver configured (`googlevoicehat-soundcard` overlay)
- [x] 3D-printed enclosure designed (`laun_frt.3mf`, `laun_bk.3mf`)
- [x] Web interface — audio recorder with playback
- [ ] Mount in laundry room
- [ ] First full capture session
