#!/usr/bin/env python3
"""Laundry sensor web interface — audio recorder, playback, and monitor."""

import asyncio
import io
import json
import logging
import os
import socket
import struct
import subprocess
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
import uvicorn

import monitor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration (overridable via env)
# ---------------------------------------------------------------------------

MONITOR_INTERVAL_SEC = int(os.environ.get("MONITOR_INTERVAL_SEC", "60"))
SAMPLE_SECONDS = float(os.environ.get("SAMPLE_SECONDS", "5"))
DEBOUNCE_SAMPLES = int(os.environ.get("DEBOUNCE_SAMPLES", "2"))
MAX_DIST_FOR_CONFIDENCE = float(os.environ.get("MAX_DIST_FOR_CONFIDENCE", "50.0"))
CAL_OFFSET_DB = os.environ.get("CAL_OFFSET_DB", "")
RETRAIN_ALPHA = float(os.environ.get("RETRAIN_ALPHA", "0.1"))
MIN_CYCLE_AGE_SEC = float(os.environ.get("MIN_CYCLE_AGE_SEC", "2700"))
MIN_IDLE_DWELL_SEC = float(os.environ.get("MIN_IDLE_DWELL_SEC", "300"))
# After this many consecutive failed samples, exit so systemd restarts us.
# At the default 5-min interval, 5 failures = ~25 minutes of bad mic state.
MAX_CONSECUTIVE_SAMPLE_FAILURES = int(
    os.environ.get("MAX_CONSECUTIVE_SAMPLE_FAILURES", "5"))
# How often the watchdog/heartbeat task pings systemd. Must be well below
# WatchdogSec in the unit file (currently 600s).
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_INTERVAL_SEC", "60"))
# How long to wait for arecord to exit after SIGTERM in /stop before SIGKILL.
RECORDING_STOP_TIMEOUT_SEC = float(
    os.environ.get("RECORDING_STOP_TIMEOUT_SEC", "10"))

# ---------------------------------------------------------------------------
# Monitor state (loaded at startup)
# ---------------------------------------------------------------------------

_signatures: dict = {}
_mel_fb: np.ndarray | None = None
_state_machine: monitor.StateMachine | None = None
_monitor_enabled: bool = True
_monitor_task: asyncio.Task | None = None
_heartbeat_task: asyncio.Task | None = None
_last_sample_time: float = 0.0
_last_sample_distances: dict = {}
_last_sample_label: str = ""
_last_sample_distance: float = 0.0
_cal_offset: np.ndarray | None = None
_mic_lock: asyncio.Lock | None = None
_consecutive_sample_failures: int = 0
_process_start_time: float = time.time()

HISTORY_LOG = Path(__file__).resolve().parent / "history.log"


def _sd_notify(message: str) -> None:
    """Send a sd_notify(3) message to systemd if running under Type=notify.

    Silently no-ops if NOTIFY_SOCKET is unset (e.g. dev runs).
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), addr)
    except OSError as exc:
        logger.debug("sd_notify(%r) failed: %s", message, exc)


def _is_recording() -> bool:
    """True iff a user-initiated arecord recording is currently in progress."""
    return _recording_proc is not None and _recording_proc.returncode is None


def _log_observation(state: str):
    """Append state to history.log if it differs from the last recorded state."""
    try:
        last_state = None
        if HISTORY_LOG.is_file():
            text = HISTORY_LOG.read_text()
            lines = text.rstrip("\n").split("\n") if text.strip() else []
            if lines:
                last_line = lines[-1]
                if ": " in last_line:
                    last_state = last_line.rsplit(": ", 1)[-1].strip()
        if state != last_state:
            ts = time.strftime("%a %b %d %H:%M:%S %Z %Y")
            with open(HISTORY_LOG, "a") as f:
                f.write(f"{ts}: {state}\n")
    except OSError as exc:
        logger.warning("Could not write history.log: %s", exc)


def _step_and_track(label, vec, dist, all_d) -> list:
    """Run the state machine step and log state changes."""
    old_state = _state_machine.current_state
    events = _state_machine.step(label, vec, dist, all_d)
    new_state = _state_machine.current_state
    if old_state != new_state:
        _log_observation(new_state)
    return events


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _signatures, _mel_fb, _state_machine, _monitor_task, _cal_offset
    global _monitor_enabled, _mic_lock, _heartbeat_task
    _mic_lock = asyncio.Lock()

    # Belt: make sure no stray arecord from a previous (perhaps crashed) run
    # is still holding the mic. Best-effort, must not block startup.
    await asyncio.to_thread(_cleanup_stray_arecord)

    sig_path = Path(__file__).with_name("signatures.json")
    if sig_path.is_file():
        _signatures = json.loads(sig_path.read_text())
        _mel_fb = monitor.build_mel_filterbank(
            _signatures["sample_rate"], _signatures["n_fft"],
            _signatures["n_mel"], _signatures["fmin"], _signatures["fmax"])
        _state_machine = monitor.StateMachine.load(
            debounce_samples=DEBOUNCE_SAMPLES,
            min_cycle_age_sec=MIN_CYCLE_AGE_SEC,
            min_idle_dwell_sec=MIN_IDLE_DWELL_SEC)
        _cal_offset = monitor.load_calibration()
        if CAL_OFFSET_DB:
            _cal_offset = np.full(_signatures["n_mel"],
                                  float(CAL_OFFSET_DB), dtype=np.float32)
        _monitor_task = asyncio.create_task(_monitor_loop())
        logger.info("Monitor started (interval=%ds, state=%s)",
                    MONITOR_INTERVAL_SEC, _state_machine.current_state)
    else:
        logger.warning("signatures.json not found — monitor disabled")
        _monitor_enabled = False

    # Start watchdog heartbeat regardless of monitor state, then tell systemd
    # we're ready. The heartbeat is what keeps WatchdogSec from killing us.
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    _sd_notify("READY=1")
    _sd_notify(f"STATUS=ready; monitor={'on' if _monitor_enabled else 'off'}")

    yield

    _sd_notify("STOPPING=1")
    for task in (_monitor_task, _heartbeat_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Laundry Sensor", lifespan=lifespan)

RECORDINGS_DIR = Path("/home/willipe/recordings")
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

ARECORD_CMD = [
    "arecord",
    "-D", "plughw:0",
    "-c1",
    "-r", "48000",
    "-f", "S32_LE",
    "-t", "wav",
]

_recording_proc: asyncio.subprocess.Process | None = None
_recording_file: str | None = None
_recording_start: float | None = None


def _wav_duration(path: Path) -> float | None:
    """Read WAV header to compute duration in seconds."""
    try:
        with open(path, "rb") as f:
            f.read(4)  # RIFF
            f.read(4)  # file size
            f.read(4)  # WAVE
            byte_rate = 0
            while True:
                chunk_id = f.read(4)
                if len(chunk_id) < 4:
                    return None
                chunk_size = struct.unpack("<I", f.read(4))[0]
                if chunk_id == b"fmt ":
                    fmt_data = f.read(chunk_size)
                    byte_rate = struct.unpack("<I", fmt_data[8:12])[0]
                elif chunk_id == b"data":
                    if byte_rate:
                        return chunk_size / byte_rate
                    return None
                else:
                    f.seek(chunk_size, 1)
    except Exception:
        return None


@app.post("/record")
async def start_recording():
    global _recording_proc, _recording_file, _recording_start

    if _is_recording():
        raise HTTPException(409, "Already recording")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"laundry_{ts}.wav"
    filepath = RECORDINGS_DIR / filename

    _recording_proc = await asyncio.create_subprocess_exec(
        *ARECORD_CMD, str(filepath),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _recording_file = filename
    _recording_start = time.monotonic()
    return {"status": "recording", "filename": filename}


@app.post("/stop")
async def stop_recording():
    global _recording_proc, _recording_file, _recording_start

    if not _is_recording():
        raise HTTPException(409, "Not recording")

    _recording_proc.terminate()
    try:
        await asyncio.wait_for(_recording_proc.wait(),
                               timeout=RECORDING_STOP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("arecord ignored SIGTERM; sending SIGKILL")
        _recording_proc.kill()
        try:
            await asyncio.wait_for(_recording_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.error("arecord still alive after SIGKILL; giving up wait")
    stopped_file = _recording_file
    _recording_proc = None
    _recording_file = None
    _recording_start = None
    return {"status": "stopped", "filename": stopped_file}


@app.get("/recording/status")
async def recording_status():
    if _is_recording():
        elapsed = time.monotonic() - _recording_start if _recording_start else 0
        return {"recording": True, "filename": _recording_file, "elapsed": round(elapsed, 1)}
    return {"recording": False}


@app.get("/status")
async def laundry_status():
    """Shortcuts-friendly laundry notify status (consumes finished on read)."""
    if _state_machine is None:
        return {"status": "idle"}
    status = _state_machine.notify_status()
    if status == "finished":
        _state_machine.consume_finished()
    return {"status": status}


def _notes_path(wav_filename: str) -> Path:
    return RECORDINGS_DIR / wav_filename.replace(".wav", ".json")


def _load_notes(wav_filename: str) -> dict:
    path = _notes_path(wav_filename)
    if path.is_file():
        return json.loads(path.read_text())
    return {"recording": wav_filename, "notes": []}


def _save_notes(wav_filename: str, data: dict):
    _notes_path(wav_filename).write_text(json.dumps(data, indent=2))


@app.post("/note")
async def add_note(body: dict):
    if not _is_recording():
        raise HTTPException(409, "Not recording")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(400, "Note text is required")

    elapsed = time.monotonic() - _recording_start if _recording_start else 0
    data = _load_notes(_recording_file)
    data["notes"].append({
        "elapsed": round(elapsed, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": text,
    })
    _save_notes(_recording_file, data)
    return {"status": "added", "elapsed": round(elapsed, 1)}


@app.get("/recordings/{filename}/notes")
async def get_notes(filename: str):
    if not filename.endswith(".wav"):
        raise HTTPException(404, "Recording not found")
    return _load_notes(filename)


@app.get("/recordings")
async def list_recordings():
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        st = f.stat()
        notes_data = _load_notes(f.name)
        result.append({
            "name": f.name,
            "size": st.st_size,
            "date": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "duration": _wav_duration(f),
            "notes_count": len(notes_data["notes"]),
        })
    return result


@app.get("/recordings/{filename}")
async def get_recording(filename: str):
    path = RECORDINGS_DIR / filename
    if not path.is_file() or not path.name.endswith(".wav"):
        raise HTTPException(404, "Recording not found")
    return FileResponse(path, media_type="audio/wav", filename=filename)


@app.get("/recordings/{filename}/zip")
async def download_recording_zip(filename: str):
    wav_path = RECORDINGS_DIR / filename
    if not wav_path.is_file() or not filename.endswith(".wav"):
        raise HTTPException(404, "Recording not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(wav_path, filename)
        notes_file = _notes_path(filename)
        if notes_file.is_file():
            zf.write(notes_file, notes_file.name)
    buf.seek(0)

    zip_name = filename.replace(".wav", ".zip")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.delete("/recordings/{filename}")
async def delete_recording(filename: str):
    path = RECORDINGS_DIR / filename
    if not path.is_file() or not path.name.endswith(".wav"):
        raise HTTPException(404, "Recording not found")
    os.remove(path)
    notes = _notes_path(filename)
    if notes.is_file():
        os.remove(notes)
    return {"status": "deleted", "filename": filename}


# ---------------------------------------------------------------------------
# Monitor background loop
# ---------------------------------------------------------------------------

def _cleanup_stray_arecord():
    """Kill any lingering arecord processes that may be holding the device.

    Best-effort; runs in a thread so it can't block the event loop.
    """
    try:
        subprocess.run(["pkill", "-9", "arecord"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=5,
                       check=False)
    except Exception as exc:
        logger.debug("pkill arecord cleanup failed: %s", exc)


async def _heartbeat_loop():
    """Independent heartbeat: pings systemd watchdog regardless of mic state.

    This lets systemd kill+restart us if the asyncio loop itself wedges
    (e.g. blocked on a syscall), while the monitor loop's failure budget
    handles the case where the loop is alive but the mic is broken.
    """
    while True:
        _sd_notify("WATCHDOG=1")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)


async def _monitor_loop():
    global _last_sample_time, _last_sample_distances
    global _last_sample_label, _last_sample_distance
    global _consecutive_sample_failures

    # Short delay for startup, then take the first sample quickly.
    # Subsequent samples use the full interval.
    await asyncio.sleep(10)

    while True:
        sample_attempted = False
        try:
            if _is_recording():
                logger.debug("Monitor tick skipped — recording in progress")
            elif not _monitor_enabled:
                pass
            elif _mel_fb is None or _state_machine is None:
                pass
            else:
                sample_attempted = True
                async with _mic_lock:
                    pcm = await asyncio.to_thread(monitor.capture_sample, SAMPLE_SECONDS)
                vec = monitor.log_mel_mean(pcm, _signatures["n_fft"],
                                           _signatures["hop"], _mel_fb)
                if _cal_offset is not None:
                    vec = vec - _cal_offset

                label, dist, all_d = monitor.classify(
                    vec, _signatures, max_dist=MAX_DIST_FOR_CONFIDENCE)

                _last_sample_time = time.time()
                _last_sample_distances = all_d
                _last_sample_label = label
                _last_sample_distance = dist
                _consecutive_sample_failures = 0

                logger.info("Sample: %s (dist=%.2f) %s", label, dist,
                            " ".join(f"{k}={v:.1f}" for k, v in
                                     sorted(all_d.items(), key=lambda x: x[1])))

                events = _step_and_track(label, vec, dist, all_d)
                for event in events:
                    logger.info("State: %s (%s -> %s)",
                                event.kind, event.old_state, event.new_state)

        except asyncio.CancelledError:
            raise
        except Exception:
            if sample_attempted:
                _consecutive_sample_failures += 1
            logger.exception("Monitor loop error (consecutive_failures=%d)",
                             _consecutive_sample_failures)
            # Best-effort: clear any zombie arecord that may be holding the mic.
            await asyncio.to_thread(_cleanup_stray_arecord)
            if _consecutive_sample_failures >= MAX_CONSECUTIVE_SAMPLE_FAILURES:
                logger.error(
                    "Reached %d consecutive sample failures — exiting so "
                    "systemd restarts the service",
                    _consecutive_sample_failures)
                _sd_notify("STOPPING=1")
                # Use os._exit to skip lifespan teardown (which itself could
                # be blocked); systemd Restart=always will bring us back.
                os._exit(2)

        await asyncio.sleep(MONITOR_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Monitor endpoints
# ---------------------------------------------------------------------------

@app.get("/monitor/status")
async def monitor_status():
    if _state_machine is None:
        return {"enabled": False, "state": "UNAVAILABLE",
                "detail": "signatures.json not loaded"}

    recording = _is_recording()
    if recording:
        mode = "recording"
    elif not _monitor_enabled:
        mode = "disabled"
    else:
        mode = "monitoring"

    return {
        "enabled": _monitor_enabled,
        "mode": mode,
        "state": _state_machine.current_state,
        "state_changed_at": _state_machine.state_changed_at,
        "candidate_state": _state_machine.candidate_state,
        "candidate_count": _state_machine.candidate_count,
        "notify_status": _state_machine.notify_status(),
        "cycle_started_at": _state_machine.cycle_started_at,
        "idle_since": _state_machine.idle_since,
        "pending_finished": _state_machine.pending_finished,
        "last_sample_time": _last_sample_time,
        "last_sample_label": _last_sample_label,
        "last_sample_distance": _last_sample_distance,
        "last_sample_distances": _last_sample_distances,
        "calibrated": _cal_offset is not None,
        "interval_sec": MONITOR_INTERVAL_SEC,
    }


@app.post("/monitor/enable")
async def monitor_enable():
    global _monitor_enabled
    _monitor_enabled = True
    return {"enabled": True}


@app.post("/monitor/disable")
async def monitor_disable():
    global _monitor_enabled
    _monitor_enabled = False
    return {"enabled": False}


@app.post("/monitor/calibrate")
async def monitor_calibrate():
    global _cal_offset
    if _mel_fb is None:
        raise HTTPException(503, "Monitor not initialized")
    if _is_recording():
        raise HTTPException(409, "Cannot calibrate while recording")

    logger.info("Calibration: capturing 30s of quiet audio...")
    async with _mic_lock:
        pcm = await asyncio.to_thread(monitor.capture_sample, 30.0)
    vec = monitor.log_mel_mean(pcm, _signatures["n_fft"],
                               _signatures["hop"], _mel_fb)
    stopped_mean = np.array(_signatures["states"]["BOTH_STOPPED"]["mean_db"],
                            dtype=np.float32)
    offset = vec - stopped_mean
    monitor.save_calibration(offset)
    _cal_offset = offset
    logger.info("Calibration saved (mean offset: %.1f dB)", float(offset.mean()))
    return {"status": "calibrated",
            "mean_offset_db": round(float(offset.mean()), 2)}


@app.post("/monitor/sample")
async def monitor_sample_now():
    global _last_sample_time, _last_sample_distances
    global _last_sample_label, _last_sample_distance

    if _mel_fb is None or _state_machine is None:
        raise HTTPException(503, "Monitor not initialized")
    if _is_recording():
        raise HTTPException(409, "Cannot sample while recording")

    async with _mic_lock:
        pcm = await asyncio.to_thread(monitor.capture_sample, SAMPLE_SECONDS)
    vec = monitor.log_mel_mean(pcm, _signatures["n_fft"],
                               _signatures["hop"], _mel_fb)
    if _cal_offset is not None:
        vec = vec - _cal_offset

    label, dist, all_d = monitor.classify(
        vec, _signatures, max_dist=MAX_DIST_FOR_CONFIDENCE)

    _last_sample_time = time.time()
    _last_sample_distances = all_d
    _last_sample_label = label
    _last_sample_distance = dist

    logger.info("Manual sample: %s (dist=%.2f)", label, dist)

    events = _step_and_track(label, vec, dist, all_d)

    return {
        "label": label,
        "distance": dist,
        "distances": all_d,
        "state": _state_machine.current_state,
        "events": [e.kind for e in events],
    }


@app.post("/monitor/retrain")
async def monitor_retrain(body: dict):
    global _last_sample_time, _last_sample_distances
    global _last_sample_label, _last_sample_distance

    state = body.get("state", "").upper()
    if state not in monitor.VALID_STATES:
        raise HTTPException(400, f"Invalid state: {state}")
    if _mel_fb is None or _state_machine is None:
        raise HTTPException(503, "Monitor not initialized")
    if _is_recording():
        raise HTTPException(409, "Cannot retrain while recording")

    async with _mic_lock:
        pcm = await asyncio.to_thread(monitor.capture_sample, SAMPLE_SECONDS)
    vec = monitor.log_mel_mean(pcm, _signatures["n_fft"],
                               _signatures["hop"], _mel_fb)
    if _cal_offset is not None:
        vec = vec - _cal_offset

    old_label, old_dist, old_dists = monitor.classify(
        vec, _signatures, max_dist=MAX_DIST_FOR_CONFIDENCE)

    monitor.blend_signature(_signatures, state, vec, alpha=RETRAIN_ALPHA)

    sig_path = Path(__file__).with_name("signatures.json")
    monitor.save_signatures(_signatures, sig_path)

    new_label, new_dist, new_dists = monitor.classify(
        vec, _signatures, max_dist=MAX_DIST_FOR_CONFIDENCE)

    _last_sample_time = time.time()
    _last_sample_distances = new_dists
    _last_sample_label = new_label
    _last_sample_distance = new_dist

    logger.info("Retrain: user labeled %s (was %s dist=%.2f, now %s dist=%.2f)",
                state, old_label, old_dist, new_label, new_dist)

    events = _step_and_track(new_label, vec, new_dist, new_dists)

    return {
        "status": "ok",
        "labeled_state": state,
        "before": {"label": old_label, "distance": old_dist, "distances": old_dists},
        "after": {"label": new_label, "distance": new_dist, "distances": new_dists},
        "current_state": _state_machine.current_state,
    }


@app.get("/monitor/log")
async def monitor_log(limit: int = 50):
    if _state_machine is None:
        return []
    entries = _state_machine.log[-limit:]
    return entries


@app.get("/healthz")
async def healthz():
    """Cheap liveness probe.

    Returns ok=False with HTTP 503 if the monitor is enabled but hasn't
    produced a fresh sample within ~3x the sample interval. Suitable for
    a local cron/systemd timer that bounces the service on failure.
    """
    now = time.time()
    last_age = (now - _last_sample_time) if _last_sample_time else None
    # Generous threshold: 3 intervals + sample duration buffer.
    stale_after = 3 * MONITOR_INTERVAL_SEC + 30
    monitor_active = _monitor_enabled and not _is_recording() \
        and _state_machine is not None
    stale = bool(monitor_active and last_age is not None and last_age > stale_after)

    body = {
        "ok": not stale,
        "uptime_sec": round(now - _process_start_time, 1),
        "last_sample_age_sec": round(last_age, 1) if last_age is not None else None,
        "consecutive_sample_failures": _consecutive_sample_failures,
        "recording": _is_recording(),
        "monitor_enabled": _monitor_enabled,
        "state": _state_machine.current_state if _state_machine else None,
    }
    if stale:
        raise HTTPException(status_code=503, detail=body)
    return body


_STATIC_DIR = Path(__file__).resolve().parent


@app.get("/machines.png")
async def machines_image():
    return FileResponse(_STATIC_DIR / "machines.png", media_type="image/png")


@app.get("/clothes.png")
async def clothes_image():
    return FileResponse(_STATIC_DIR / "clothes.png", media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC_DIR / "index.html").read_text()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=443,
        ssl_certfile="/etc/laundry-sensor/cert.pem",
        ssl_keyfile="/etc/laundry-sensor/key.pem",
    )
