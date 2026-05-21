#!/usr/bin/env python3
"""Laundry sensor web interface — audio recorder and playback."""

import asyncio
import io
import json
import os
import struct
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
import uvicorn

app = FastAPI(title="Laundry Sensor")

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

    if _recording_proc is not None and _recording_proc.returncode is None:
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

    if _recording_proc is None or _recording_proc.returncode is not None:
        raise HTTPException(409, "Not recording")

    _recording_proc.terminate()
    await _recording_proc.wait()
    stopped_file = _recording_file
    _recording_proc = None
    _recording_file = None
    _recording_start = None
    return {"status": "stopped", "filename": stopped_file}


@app.get("/status")
async def recording_status():
    if _recording_proc is not None and _recording_proc.returncode is None:
        elapsed = time.monotonic() - _recording_start if _recording_start else 0
        return {"recording": True, "filename": _recording_file, "elapsed": round(elapsed, 1)}
    return {"recording": False}


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
    if _recording_proc is None or _recording_proc.returncode is not None:
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


INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Laundry Sensor</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
      padding: 1.5rem 1rem;
    }
    h1 {
      font-size: 1.4rem;
      font-weight: 600;
      margin-bottom: 1rem;
      color: #94a3b8;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }
    .card {
      background: #1e293b;
      border-radius: 12px;
      padding: 1.5rem;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
      width: 100%;
      max-width: 500px;
      margin-bottom: 1rem;
    }
    .status {
      text-align: center;
      font-size: 1rem;
      margin-bottom: 1rem;
      min-height: 1.5em;
    }
    .status.idle { color: #64748b; }
    .status.recording { color: #f87171; }
    .controls {
      display: flex;
      gap: 0.75rem;
      justify-content: center;
    }
    button {
      padding: 0.6rem 1.5rem;
      border: 1px solid #334155;
      border-radius: 8px;
      background: #1e293b;
      color: #cbd5e1;
      font-size: 0.95rem;
      cursor: pointer;
      transition: background 0.15s, opacity 0.15s;
    }
    button:hover:not(:disabled) { background: #334155; }
    button:disabled { opacity: 0.35; cursor: default; }
    button.rec { border-color: #991b1b; color: #fca5a5; }
    button.rec:hover:not(:disabled) { background: #450a0a; }
    button.stop { border-color: #854d0e; color: #fde68a; }
    button.stop:hover:not(:disabled) { background: #451a03; }
    h2 {
      font-size: 1rem;
      font-weight: 500;
      color: #94a3b8;
      margin-bottom: 0.75rem;
    }
    .empty { color: #475569; font-size: 0.85rem; }
    .file {
      padding: 0.75rem 0;
      border-bottom: 1px solid #334155;
    }
    .file:last-child { border-bottom: none; }
    .file-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.4rem;
    }
    .file-name {
      font-size: 0.85rem;
      color: #cbd5e1;
      word-break: break-all;
    }
    .file-meta {
      font-size: 0.75rem;
      color: #64748b;
    }
    .file-actions {
      display: flex;
      gap: 0.5rem;
      flex-shrink: 0;
    }
    .file-actions button {
      padding: 0.35rem 0.5rem;
      font-size: 0.8rem;
      border-radius: 6px;
      display: flex;
      align-items: center;
    }
    .file-actions .del { border-color: #991b1b; color: #fca5a5; }
    .file-actions .del:hover { background: #450a0a; }
    .file-actions svg { display: block; }
    audio {
      width: 100%;
      height: 36px;
      margin-top: 0.4rem;
      border-radius: 6px;
    }
    .notes-card { display: none; }
    .notes-card.active { display: block; }
    .note-input {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }
    .note-input input {
      flex: 1;
      padding: 0.5rem 0.75rem;
      border: 1px solid #334155;
      border-radius: 8px;
      background: #0f172a;
      color: #e2e8f0;
      font-size: 0.9rem;
      outline: none;
    }
    .note-input input:focus { border-color: #475569; }
    .presets {
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
      margin-bottom: 0.75rem;
    }
    .presets button {
      padding: 0.3rem 0.7rem;
      font-size: 0.75rem;
      border-color: #1e40af;
      color: #93c5fd;
    }
    .presets button:hover { background: #1e3a5f; }
    .notes-log {
      max-height: 200px;
      overflow-y: auto;
      font-size: 0.8rem;
    }
    .note-entry {
      padding: 0.3rem 0;
      border-bottom: 1px solid #1e293b;
      color: #94a3b8;
    }
    .note-entry span { color: #64748b; margin-right: 0.5rem; }
    .file-notes {
      font-size: 0.75rem;
      color: #64748b;
      margin-top: 0.3rem;
    }
    .file-notes .note-item {
      padding: 0.15rem 0;
    }
    .file-notes .note-item span { color: #475569; margin-right: 0.4rem; }
  </style>
</head>
<body>
  <h1>Laundry Sensor</h1>
  <div class="card">
    <div id="status" class="status idle">Idle</div>
    <div class="controls">
      <button class="rec" id="btnRec" onclick="doRecord()"><svg width="10" height="10" viewBox="0 0 10 10" style="margin-right:6px;vertical-align:middle"><circle cx="5" cy="5" r="5" fill="#ef4444"/></svg>Record</button>
      <button class="stop" id="btnStop" onclick="doStop()" disabled><svg width="10" height="10" viewBox="0 0 10 10" style="margin-right:6px;vertical-align:middle"><rect width="10" height="10" rx="1" fill="#9ca3af"/></svg>Stop</button>
    </div>
  </div>
  <div class="card notes-card" id="notesCard">
    <h2>Notes</h2>
    <div class="presets">
      <button onclick="addPreset('BOTH_STOPPED')">Both Stopped</button>
      <button onclick="addPreset('WASHER_ONLY')">Washer Only</button>
      <button onclick="addPreset('DRYER_ONLY')">Dryer Only</button>
      <button onclick="addPreset('BOTH_RUNNING')">Both Running</button>
    </div>
    <div class="note-input">
      <input type="text" id="noteText" placeholder="Add a note..." onkeydown="if(event.key==='Enter')addNote()">
      <button onclick="addNote()">Add</button>
    </div>
    <div class="notes-log" id="notesLog"></div>
  </div>
  <div class="card">
    <h2>Recordings</h2>
    <div id="list"><p class="empty">Loading...</p></div>
  </div>

  <script>
    let polling = null;

    async function doRecord() {
      const r = await fetch('/record', {method: 'POST'});
      if (r.ok) startPolling();
      else alert((await r.json()).detail || 'Failed to start');
      refreshAll();
    }

    async function doStop() {
      await fetch('/stop', {method: 'POST'});
      stopPolling();
      refreshAll();
    }

    function startPolling() {
      stopPolling();
      polling = setInterval(refreshStatus, 1000);
    }

    function stopPolling() {
      if (polling) { clearInterval(polling); polling = null; }
    }

    function fmtDuration(s) {
      if (s == null) return '';
      const m = Math.floor(s / 60);
      const sec = Math.floor(s % 60);
      return m + ':' + String(sec).padStart(2, '0');
    }

    function fmtSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / 1048576).toFixed(1) + ' MB';
    }

    async function addNote() {
      const input = document.getElementById('noteText');
      const text = input.value.trim();
      if (!text) return;
      const r = await fetch('/note', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text})
      });
      if (r.ok) {
        input.value = '';
        refreshNotes();
      } else {
        alert((await r.json()).detail || 'Failed');
      }
    }

    function addPreset(label) {
      document.getElementById('noteText').value = label;
      addNote();
    }

    async function refreshNotes() {
      const sr = await fetch('/status');
      const st = await sr.json();
      if (!st.recording) return;
      const r = await fetch('/recordings/' + st.filename + '/notes');
      const d = await r.json();
      const el = document.getElementById('notesLog');
      if (d.notes.length === 0) {
        el.innerHTML = '';
        return;
      }
      el.innerHTML = d.notes.map(n =>
        '<div class="note-entry"><span>' + fmtDuration(n.elapsed) + '</span>' + n.note + '</div>'
      ).join('');
      el.scrollTop = el.scrollHeight;
    }

    async function refreshStatus() {
      const r = await fetch('/status');
      const d = await r.json();
      const el = document.getElementById('status');
      const btnRec = document.getElementById('btnRec');
      const btnStop = document.getElementById('btnStop');
      const notesCard = document.getElementById('notesCard');
      if (d.recording) {
        el.className = 'status recording';
        el.textContent = 'Recording ' + fmtDuration(d.elapsed) + ' \\u2014 ' + d.filename;
        btnRec.disabled = true;
        btnStop.disabled = false;
        notesCard.className = 'card notes-card active';
      } else {
        el.className = 'status idle';
        el.textContent = 'Idle';
        btnRec.disabled = false;
        btnStop.disabled = true;
        notesCard.className = 'card notes-card';
        stopPolling();
      }
    }

    async function refreshList() {
      const r = await fetch('/recordings');
      const files = await r.json();
      const el = document.getElementById('list');
      if (files.length === 0) {
        el.innerHTML = '<p class="empty">No recordings yet</p>';
        return;
      }
      el.innerHTML = files.map(f => `
        <div class="file">
          <div class="file-header">
            <div>
              <div class="file-name">${f.name}</div>
              <div class="file-meta">${fmtSize(f.size)}${f.duration != null ? ' \\u00b7 ' + fmtDuration(f.duration) : ''}${f.notes_count ? ' \\u00b7 ' + f.notes_count + ' note' + (f.notes_count > 1 ? 's' : '') : ''}</div>
            </div>
            <div class="file-actions">
              <a href="/recordings/${f.name}/zip"><button title="Download zip"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button></a>
              <button class="del" onclick="doDelete('${f.name}')" title="Delete"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg></button>
            </div>
          </div>
          <audio controls preload="none" src="/recordings/${f.name}"></audio>
        </div>
      `).join('');
    }

    async function doDelete(name) {
      if (!confirm('Delete ' + name + '?')) return;
      await fetch('/recordings/' + name, {method: 'DELETE'});
      refreshList();
    }

    function refreshAll() {
      refreshStatus().then(() => refreshNotes());
      refreshList();
    }

    refreshAll();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=443,
        ssl_certfile="/etc/laundry-sensor/cert.pem",
        ssl_keyfile="/etc/laundry-sensor/key.pem",
    )
