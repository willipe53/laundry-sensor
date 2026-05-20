#!/usr/bin/env python3
"""Laundry sensor web interface — live MJPEG camera stream."""

import asyncio

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

app = FastAPI(title="Laundry Sensor")

RPICAM_CMD = [
    "rpicam-vid",
    "--nopreview",
    "--codec", "mjpeg",
    "--width", "640",
    "--height", "480",
    "--framerate", "15",
    "--quality", "70",
    "-t", "0",
    "-o", "-",
]


async def mjpeg_frames():
    """Yield JPEG frames from rpicam-vid as multipart chunks."""
    proc = await asyncio.create_subprocess_exec(
        *RPICAM_CMD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                soi = buf.find(b"\xff\xd8")
                if soi == -1:
                    buf = b""
                    break
                eoi = buf.find(b"\xff\xd9", soi + 2)
                if eoi == -1:
                    buf = buf[soi:]
                    break
                frame = buf[soi : eoi + 2]
                buf = buf[eoi + 2:]
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
    finally:
        proc.terminate()
        await proc.wait()


@app.get("/stream")
async def video_stream():
    return StreamingResponse(
        mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


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
    .feed {
      background: #1e293b;
      border-radius: 12px;
      padding: 8px;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
    }
    .feed img {
      display: block;
      width: 100%;
      max-width: 640px;
      border-radius: 8px;
      background: #000;
    }
    .bar {
      margin-top: 1.5rem;
      display: flex;
      gap: 0.75rem;
      flex-wrap: wrap;
      justify-content: center;
    }
    .bar button {
      padding: 0.5rem 1.25rem;
      border: 1px solid #334155;
      border-radius: 8px;
      background: #1e293b;
      color: #cbd5e1;
      font-size: 0.9rem;
      cursor: pointer;
      transition: background 0.15s;
    }
    .bar button:hover { background: #334155; }
    .status {
      margin-top: 1rem;
      font-size: 0.8rem;
      color: #475569;
    }
  </style>
</head>
<body>
  <h1>Laundry Sensor</h1>
  <div class="feed">
    <img src="/stream" alt="Live camera feed">
  </div>
  <div class="bar">
    <button disabled title="Coming soon">Record Video</button>
    <button disabled title="Coming soon">Record Audio</button>
    <button disabled title="Coming soon">Downloads</button>
  </div>
  <p class="status">Live &middot; 640&times;480 @ 15 fps</p>
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
