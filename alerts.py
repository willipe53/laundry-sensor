"""Pluggable alert dispatch. Selected via ALERT_BACKEND env var.

Supported backends: aws_sns, pushover, ntfy, twilio, webhook.
All use stdlib urllib except aws_sns which lazy-imports boto3.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from base64 import b64encode

logger = logging.getLogger(__name__)

_last_result: dict = {}


def get_last_result() -> dict:
    """Last alert delivery result for status display."""
    return _last_result


def get_backend_name() -> str:
    return os.environ.get("ALERT_BACKEND", "none")


def send(title: str, message: str) -> dict:
    """Dispatch an alert through the configured backend.

    Returns {"ok": bool, "backend": str, "detail": str, "time": float}.
    Never raises — failures are logged and returned.
    """
    global _last_result
    backend = os.environ.get("ALERT_BACKEND", "").strip().lower()
    result = {"ok": False, "backend": backend, "detail": "", "time": time.time()}

    try:
        if backend == "aws_sns":
            result = _send_aws_sns(title, message, result)
        elif backend == "pushover":
            result = _send_pushover(title, message, result)
        elif backend == "ntfy":
            result = _send_ntfy(title, message, result)
        elif backend == "twilio":
            result = _send_twilio(title, message, result)
        elif backend == "webhook":
            result = _send_webhook(title, message, result)
        elif backend in ("", "none"):
            result["detail"] = "No alert backend configured"
            logger.info("Alert not sent (no backend): %s: %s", title, message)
        else:
            result["detail"] = f"Unknown backend: {backend}"
            logger.error("Unknown ALERT_BACKEND=%r", backend)
    except Exception as e:
        result["detail"] = str(e)
        logger.exception("Alert dispatch failed for backend=%s", backend)

    _last_result = result
    return result


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def _send_aws_sns(title: str, message: str, result: dict) -> dict:
    import boto3  # lazy import
    client = boto3.client("sns",
                          region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    phone = os.environ["ALERT_SMS_TO"]
    resp = client.publish(PhoneNumber=phone, Message=f"{title}: {message}")
    result["ok"] = True
    result["detail"] = f"MessageId={resp.get('MessageId', '?')}"
    logger.info("AWS SNS sent to %s: %s", phone, result["detail"])
    return result


def _send_pushover(title: str, message: str, result: dict) -> dict:
    data = urllib.parse.urlencode({
        "token": os.environ["PUSHOVER_TOKEN"],
        "user": os.environ["PUSHOVER_USER"],
        "title": title,
        "message": message,
    }).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                data=data, method="POST")
    resp = urllib.request.urlopen(req, timeout=15)
    result["ok"] = resp.status == 200
    result["detail"] = f"HTTP {resp.status}"
    logger.info("Pushover: %s", result["detail"])
    return result


def _send_ntfy(title: str, message: str, result: dict) -> dict:
    topic = os.environ["NTFY_TOPIC"]
    url = os.environ.get("NTFY_URL", "https://ntfy.sh") + "/" + topic
    req = urllib.request.Request(url, data=message.encode(), method="POST",
                                headers={"Title": title})
    resp = urllib.request.urlopen(req, timeout=15)
    result["ok"] = resp.status == 200
    result["detail"] = f"HTTP {resp.status}"
    logger.info("ntfy (%s): %s", topic, result["detail"])
    return result


def _send_twilio(title: str, message: str, result: dict) -> dict:
    import urllib.parse
    sid = os.environ["TWILIO_SID"]
    token = os.environ["TWILIO_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": os.environ["TWILIO_FROM"],
        "To": os.environ["TWILIO_TO"],
        "Body": f"{title}: {message}",
    }).encode()
    cred = b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST",
                                headers={"Authorization": f"Basic {cred}"})
    resp = urllib.request.urlopen(req, timeout=15)
    result["ok"] = resp.status in (200, 201)
    result["detail"] = f"HTTP {resp.status}"
    logger.info("Twilio: %s", result["detail"])
    return result


def _send_webhook(title: str, message: str, result: dict) -> dict:
    url = os.environ["WEBHOOK_URL"]
    payload = json.dumps({"title": title, "message": message}).encode()
    req = urllib.request.Request(url, data=payload, method="POST",
                                headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=15)
    result["ok"] = resp.status in range(200, 300)
    result["detail"] = f"HTTP {resp.status}"
    logger.info("Webhook (%s): %s", url, result["detail"])
    return result
