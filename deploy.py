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
    "monitor.py",
    "signatures.json",
    "requirements.txt",
    "laundry-sensor.service",
    "index.html",
    "machines.png",
    "clothes.png",
    "history.log",
    "health-check.sh",
    "99-usb-audio-noautosuspend.rules",
]

REPO_DIR = Path(__file__).resolve().parent

REMOTE_SETUP = f"""\
#!/usr/bin/env bash
set -e

echo "[2/6] Installing app and dependencies..."
sudo mkdir -p {INSTALL_DIR}
sudo cp {REMOTE_TMP}/laundry_server.py {REMOTE_TMP}/monitor.py \\
       {REMOTE_TMP}/signatures.json {REMOTE_TMP}/requirements.txt \\
       {REMOTE_TMP}/index.html {REMOTE_TMP}/machines.png {REMOTE_TMP}/clothes.png \\
       {REMOTE_TMP}/history.log {INSTALL_DIR}/
sudo install -m 755 {REMOTE_TMP}/health-check.sh {INSTALL_DIR}/health-check.sh
if [ ! -d {INSTALL_DIR}/venv ]; then
    echo "  Creating Python venv..."
    sudo python3 -m venv {INSTALL_DIR}/venv
fi
echo "  Installing pip packages..."
sudo {INSTALL_DIR}/venv/bin/pip install --quiet -r {INSTALL_DIR}/requirements.txt

echo ""
echo "[2b/6] Installing USB-audio no-autosuspend udev rule..."
if ! sudo cmp -s {REMOTE_TMP}/99-usb-audio-noautosuspend.rules \\
                 /etc/udev/rules.d/99-usb-audio-noautosuspend.rules 2>/dev/null; then
    sudo cp {REMOTE_TMP}/99-usb-audio-noautosuspend.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb || true
    echo "  Installed/refreshed udev rule."
else
    echo "  udev rule already up to date."
fi

echo ""
echo "[2c/6] Installing health-check cron entry..."
CRON_LINE="* * * * * {INSTALL_DIR}/health-check.sh >> /var/log/laundry-health.log 2>&1"
EXISTING_CRON="$(sudo crontab -l 2>/dev/null || true)"
if echo "$EXISTING_CRON" | grep -Fq "{INSTALL_DIR}/health-check.sh"; then
    echo "  Root cron entry already present."
else
    printf '%s\\n%s\\n' "$EXISTING_CRON" "$CRON_LINE" | sed '/^$/d' | sudo crontab -
    echo "  Installed root cron entry for health-check.sh."
fi

echo ""
echo "[3/6] Creating recordings and state directories..."
sudo mkdir -p /home/willipe/recordings
sudo chown willipe:willipe /home/willipe/recordings
sudo mkdir -p /var/lib/laundry-sensor
sudo mkdir -p /etc/laundry-sensor

echo ""
echo "[4/6] Ensuring TLS certificate..."
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
echo "[5/6] Installing systemd service..."
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

    # Pull history.log from remote before deploying (preserve observation log)
    print("[0/6] Pulling history.log from Pi (if it exists)...")
    local_history = REPO_DIR / "history.log"
    try:
        run(f"scp {PI}:{INSTALL_DIR}/history.log {local_history}")
        print("  Pulled history.log from remote.")
    except subprocess.CalledProcessError:
        print("  No history.log on remote yet, skipping pull.")

    # Write the remote setup script to a temp file so we can SCP it
    setup_script = Path(tempfile.mktemp(suffix=".sh"))
    setup_script.write_text(REMOTE_SETUP)

    try:
        # 1. SCP app files + setup script to the Pi
        print("[1/6] Copying files to Pi...")
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
