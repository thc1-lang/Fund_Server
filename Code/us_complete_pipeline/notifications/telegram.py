"""Telegram alerts using environment credentials, with no extra dependencies."""

import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def credential(name):
    """Read process settings, then current Windows user's persistent settings."""
    value = os.getenv(name, "").strip()
    if not value and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0]).strip()
        except OSError:
            pass
    return value


def send_telegram(message, title="US pipeline", *, secrets=()):
    """Return delivery success; missing credentials/network errors never stop a run."""
    try:
        token = credential("TELEGRAM_BOT_TOKEN")
        chat_id = credential("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print(
                "NOTIFY | Skipped: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
                file=sys.stderr,
            )
            return False

        def clean(value):
            value = str(value)
            for secret in (token, chat_id, credential("FRED_API_KEY"), *secrets):
                if secret:
                    value = value.replace(str(secret), "[redacted]")
            return value

        text = clean(f"{title}\n{message}")
        # Preserve both the context and the final error when an alert is too long.
        if len(text) > 4096:
            marker = "\n[... shortened; see full local log ...]\n"
            text = text[:2000] + marker + text[-(4096 - 2000 - len(marker)) :]
        payload = urlencode(
            {
                "chat_id": chat_id,
                "text": text,
            }
        ).encode()
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=10) as response:
            if json.load(response).get("ok") is not True:
                raise ValueError("Telegram rejected the notification")
        return True
    except Exception:
        # Do not print transport errors: they can contain request credentials.
        print(
            "NOTIFY | Delivery failed; pipeline behavior is unchanged", file=sys.stderr
        )
        return False


if __name__ == "__main__":
    raise SystemExit(
        0
        if send_telegram(
            "Telegram test from the US pipeline.", "Pipeline notification test"
        )
        else 1
    )
