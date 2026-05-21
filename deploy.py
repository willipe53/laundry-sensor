#!/usr/bin/env python3
"""Deploy laundry-sensor (audio recorder) to the Pi Zero 2W over SSH."""

import subprocess
import sys
import tempfile
from pathlib import Path

PI = "willipe@laundry"
REMOTE_TMP = "/tmp/laundry-sensor"
INSTALL_DIR = "/opt/laundry-sensor"
CERT_DIR = "/etc/laundry-sensor"

APP_FILES = [
    "laundry_server.py",
    "requirements.txt",
    "laundry-sensor.service",
]

REPO_DIR = Path(__file__).resolve().parent

REMOTE_SETUP = f"""\
#!/usr/bin/env bash
set -e

echo "[2/5] Installing app and dependencies..."
sudo mkdir -p {INSTALL_DIR}
sudo cp {REMOTE_TMP}/laundry_server.py {REMOTE_TMP}/requirements.txt {INSTALL_DIR}/
if [ ! -d {INSTALL_DIR}/venv ]; then
    echo "  Creating Python venv..."
    sudo python3 -m venv {INSTALL_DIR}/venv
fi
echo "  Installing pip packages..."
sudo {INSTALL_DIR}/venv/bin/pip install --quiet -r {INSTALL_DIR}/requirements.txt

echo ""
echo "[3/5] Creating recordings directory..."
sudo mkdir -p /home/willipe/recordings
sudo chown willipe:willipe /home/willipe/recordings

echo ""
echo "[4/5] Ensuring TLS certificate..."
sudo mkdir -p {CERT_DIR}
if [ ! -f {CERT_DIR}/cert.pem ]; then
    echo "  Generating self-signed cert..."
    sudo openssl req -x509 -newkey rsa:2048 -nodes \\
        -keyout {CERT_DIR}/key.pem -out {CERT_DIR}/cert.pem \\
        -days 3650 -subj /CN=laundry
    sudo chmod 600 {CERT_DIR}/key.pem
else
    echo "  Certificate already exists, skipping."
fi

echo ""
echo "[5/5] Installing systemd service..."
sudo cp {REMOTE_TMP}/laundry-sensor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable laundry-sensor.service
sudo systemctl restart laundry-sensor.service

rm -rf {REMOTE_TMP}

echo ""
echo "=== Done ==="
echo ""
sudo systemctl status laundry-sensor.service --no-pager || true
echo ""
echo "Open https://laundry in your browser."
"""


def run(cmd, **kwargs):
    print(f"  $ {cmd}")
    subprocess.run(cmd, shell=True, check=True, **kwargs)


def main():
    print("=== Laundry Sensor — deploy ===\n")

    # Write the remote setup script to a temp file so we can SCP it
    setup_script = Path(tempfile.mktemp(suffix=".sh"))
    setup_script.write_text(REMOTE_SETUP)

    try:
        # 1. SCP app files + setup script to the Pi
        print("[1/5] Copying files to Pi...")
        run(f"ssh {PI} 'mkdir -p {REMOTE_TMP}'")
        sources = " ".join(str(REPO_DIR / f) for f in APP_FILES)
        run(f"scp {sources} {setup_script} {PI}:{REMOTE_TMP}/")

        # 2-4. Run setup with TTY so sudo can prompt for password
        print()
        remote_script = f"{REMOTE_TMP}/{setup_script.name}"
        subprocess.run(["ssh", "-t", PI, f"bash {remote_script}"], check=True)
    finally:
        setup_script.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\nDeploy failed: {e}", file=sys.stderr)
        sys.exit(1)
