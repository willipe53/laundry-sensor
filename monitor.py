"""Audio sampling, log-mel feature extraction, classification, and state machine.

This module is a plain-Python library imported by laundry_server.py. It has no
FastAPI or async dependencies so it can be tested standalone:

    python -m monitor sample          # one-shot classify from the live mic
    python -m monitor classify FILE   # classify a raw S16_LE 16kHz file
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/laundry-sensor")
STATE_FILE = STATE_DIR / "state.json"
CALIBRATION_FILE = STATE_DIR / "calibration.json"

VALID_STATES = {"BOTH_STOPPED", "WASHER_ONLY", "DRYER_ONLY", "BOTH_RUNNING"}


# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------

def capture_sample(seconds: float = 5.0, device: str = "plughw:0") -> np.ndarray:
    """Record from ALSA and return float32 PCM in [-1, 1] at 16 kHz.

    Robustness notes:
      * stdin=DEVNULL — some arecord builds misbehave if stdin is a TTY.
      * timeout is `seconds + 10` to bound the worst case if PCM open hangs;
        on TimeoutExpired the child is killed by subprocess.run.
    """
    cmd = [
        "arecord", "-D", device, "-c1", "-r", "16000",
        "-f", "S16_LE", "-d", str(int(seconds)), "-t", "raw", "-q", "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=seconds + 10,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"arecord timed out after {exc.timeout}s (mic likely wedged)") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"arecord failed ({proc.returncode}): {proc.stderr.decode(errors='replace')}")
    raw = np.frombuffer(proc.stdout, dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Mel filterbank (built once at startup)
# ---------------------------------------------------------------------------

def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + f / 700.0)

def _mel_to_hz(m):
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

def build_mel_filterbank(sr: int, n_fft: int, n_mel: int,
                         fmin: float, fmax: float) -> np.ndarray:
    mel_pts = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mel + 2)
    bin_pts = np.floor((n_fft + 1) * _mel_to_hz(mel_pts) / sr).astype(int)
    fb = np.zeros((n_mel, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mel):
        lo, center, hi = bin_pts[i], bin_pts[i + 1], bin_pts[i + 2]
        if center > lo:
            fb[i, lo:center] = np.linspace(0, 1, center - lo, endpoint=False)
        if hi > center:
            fb[i, center:hi] = np.linspace(1, 0, hi - center, endpoint=False)
    return fb


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def log_mel_mean(pcm: np.ndarray, n_fft: int, hop: int,
                 mel_fb: np.ndarray) -> np.ndarray:
    """Compute mean log-mel energy vector (n_mel,) in dB from float32 PCM."""
    win = np.hanning(n_fft).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(pcm, n_fft)[::hop] * win
    spec = np.fft.rfft(frames, axis=1)
    power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
    log_mel = 10.0 * np.log10(power @ mel_fb.T + 1e-10)
    return log_mel.mean(axis=0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(vec: np.ndarray, signatures: dict,
             max_dist: float = 8.0) -> tuple[str, float, dict]:
    """Weighted L2 nearest-neighbor against signature vectors.

    Returns (best_state, best_distance, {state: distance}).
    If best_distance > max_dist, best_state is "UNKNOWN".
    """
    distances = {}
    best_state, best_dist = "UNKNOWN", float("inf")
    for name, sig in signatures["states"].items():
        mu = np.array(sig["mean_db"], dtype=np.float32)
        std = np.array(sig["std_db"], dtype=np.float32)
        w = 1.0 / (std ** 2 + 1.0)
        d = float(np.sqrt(((vec - mu) ** 2 * w).sum() / w.sum()))
        distances[name] = round(d, 3)
        if d < best_dist:
            best_state, best_dist = name, d
    if best_dist > max_dist:
        best_state = "UNKNOWN"
    return best_state, round(best_dist, 3), distances


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

@dataclass
class TransitionEvent:
    kind: str
    old_state: str
    new_state: str


@dataclass
class StateMachine:
    current_state: str = "UNKNOWN"
    candidate_state: Optional[str] = None
    candidate_count: int = 0
    state_changed_at: float = field(default_factory=time.time)
    debounce_samples: int = 2

    # Rolling log of recent classifications
    log: list = field(default_factory=list)
    max_log: int = 200

    def step(self, raw_label: str, vec: np.ndarray,
             dist: float, all_dist: dict) -> list[TransitionEvent]:
        """Process one classification. Returns list of transition events (0-1)."""
        now = time.time()
        entry = {
            "time": now,
            "label": raw_label,
            "distance": dist,
            "distances": all_dist,
        }

        events = []

        if raw_label == self.current_state:
            self.candidate_state = None
            self.candidate_count = 0
        elif raw_label == self.candidate_state:
            self.candidate_count += 1
            if self.candidate_count >= self.debounce_samples:
                old = self.current_state
                self.current_state = raw_label
                self.state_changed_at = now
                self.candidate_state = None
                self.candidate_count = 0
                entry["transition"] = {"from": old, "to": raw_label}
                if old != "UNKNOWN":
                    kind = "CYCLES_COMPLETE" if raw_label == "BOTH_STOPPED" else "CYCLES_IN_PROGRESS"
                    events.append(TransitionEvent(kind=kind, old_state=old, new_state=raw_label))
        else:
            self.candidate_state = raw_label
            self.candidate_count = 1

        self.log.append(entry)
        if len(self.log) > self.max_log:
            self.log = self.log[-self.max_log:]

        self._persist()
        return events

    def _persist(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "current_state": self.current_state,
                "candidate_state": self.candidate_state,
                "candidate_count": self.candidate_count,
                "state_changed_at": self.state_changed_at,
            }
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except OSError as e:
            logger.warning("Could not persist state: %s", e)

    @classmethod
    def load(cls, debounce_samples: int = 2) -> "StateMachine":
        sm = cls(debounce_samples=debounce_samples)
        if STATE_FILE.is_file():
            try:
                data = json.loads(STATE_FILE.read_text())
                sm.current_state = data.get("current_state", "UNKNOWN")
                sm.candidate_state = data.get("candidate_state")
                sm.candidate_count = data.get("candidate_count", 0)
                sm.state_changed_at = data.get("state_changed_at", time.time())
                logger.info("Restored state: %s", sm.current_state)
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                logger.warning("Could not load persisted state: %s", e)
        return sm


# ---------------------------------------------------------------------------
# Signature retraining (EMA blend)
# ---------------------------------------------------------------------------

def blend_signature(signatures: dict, state: str, vec: np.ndarray,
                    alpha: float = 0.1) -> None:
    """Blend a new observation into a state's signature using EMA.

    Updates signatures["states"][state] mean_db and std_db in place.
    """
    if state not in signatures["states"]:
        raise ValueError(f"Unknown state: {state}")
    sig = signatures["states"][state]
    old_mean = np.array(sig["mean_db"], dtype=np.float32)
    old_std = np.array(sig["std_db"], dtype=np.float32)

    new_mean = (1.0 - alpha) * old_mean + alpha * vec
    new_std = (1.0 - alpha) * old_std + alpha * np.abs(vec - new_mean)

    sig["mean_db"] = [round(float(v), 2) for v in new_mean]
    sig["std_db"] = [round(float(v), 2) for v in new_std]


def save_signatures(signatures: dict, path: Path) -> None:
    """Write updated signatures dict to disk."""
    path.write_text(json.dumps(signatures, indent=2))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def load_calibration() -> Optional[np.ndarray]:
    if CALIBRATION_FILE.is_file():
        try:
            data = json.loads(CALIBRATION_FILE.read_text())
            return np.array(data["offset_db"], dtype=np.float32)
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return None


def save_calibration(offset_db: np.ndarray):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "offset_db": [round(float(v), 4) for v in offset_db],
        "created_at": time.time(),
    }
    CALIBRATION_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# CLI for debugging
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    sigs = json.loads(Path(__file__).with_name("signatures.json").read_text())
    mel_fb = build_mel_filterbank(
        sigs["sample_rate"], sigs["n_fft"], sigs["n_mel"],
        sigs["fmin"], sigs["fmax"])

    if len(sys.argv) > 1 and sys.argv[1] == "sample":
        pcm = capture_sample(sigs.get("window_seconds", 5.0))
        vec = log_mel_mean(pcm, sigs["n_fft"], sigs["hop"], mel_fb)
        cal = load_calibration()
        if cal is not None:
            vec -= cal
        label, dist, dists = classify(vec, sigs)
        print(f"State: {label}  (distance: {dist})")
        for s, d in sorted(dists.items(), key=lambda x: x[1]):
            print(f"  {s}: {d}")
    else:
        print("Usage: python -m monitor sample")
