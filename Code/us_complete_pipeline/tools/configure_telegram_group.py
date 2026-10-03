"""Discover and activate the existing bot's shared Telegram control group."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notifications.control import WORK, api, save_json
from notifications.telegram import credential
from run_lock import acquire_lock

OFFSET_PATH = WORK / "telegram-offset.json"


def checkpoint(offset):
    previous = -1
    if OFFSET_PATH.exists():
        import json

        previous = int(json.loads(OFFSET_PATH.read_text(encoding="utf-8"))["offset"])
    offset = max(previous, offset)
    save_json(OFFSET_PATH, {"offset": offset})
    return offset


def poll(offset, timeout):
    updates = api("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
    newest = max((int(item["update_id"]) for item in updates), default=offset - 1)
    return updates, max(offset, newest + 1)


def discover(timeout_seconds=600):
    if not credential("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("Existing TELEGRAM_BOT_TOKEN is missing")
    if api("getWebhookInfo").get("url"):
        raise RuntimeError("A webhook is active for this bot; stop it before polling")
    # Drop everything queued before this setup. It must never become a command.
    latest = api("getUpdates", offset=-1, timeout=0, allowed_updates=["message"])
    offset = int(latest[-1]["update_id"]) + 1 if latest else 0
    offset = checkpoint(offset)
    print("Send one normal message in the new Telegram group from each person who should be authorised.", flush=True)
    groups = {}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        updates, offset = poll(offset, min(15, max(0, int(deadline - time.monotonic()))))
        offset = checkpoint(offset)
        for item in updates:
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            if chat.get("type") not in ("group", "supergroup") or sender.get("is_bot"):
                continue
            if not isinstance(chat.get("id"), int) or chat["id"] >= 0:
                continue
            if not isinstance(sender.get("id"), int) or sender["id"] <= 0:
                continue
            group = groups.setdefault(chat["id"], {"title": chat.get("title") or "(untitled group)", "type": chat["type"], "users": {}})
            group["users"][sender["id"]] = {"first_name": sender.get("first_name") or "(unnamed)", "username": sender.get("username") or ""}
        if any(len(group["users"]) >= 2 for group in groups.values()):
            return groups, offset
    raise TimeoutError("Did not receive messages from two human users in one group before timeout")


def choose(groups):
    candidates = [(chat_id, info) for chat_id, info in groups.items() if len(info["users"]) >= 2]
    print("\nDiscovered groups and human users:", flush=True)
    for number, (chat_id, info) in enumerate(candidates, 1):
        print(f"\n{number}. GROUP: {info['title']} ({info['type']})\n   Chat ID: {chat_id}", flush=True)
        for user_id, user in info["users"].items():
            suffix = f" @{user['username']}" if user["username"] else ""
            print(f"   USER: {user['first_name']}{suffix} — {user_id}", flush=True)
    if len(candidates) == 1:
        return candidates[0]
    selected = input("Select the group number: ").strip()
    if not selected.isdecimal() or not (1 <= int(selected) <= len(candidates)):
        raise RuntimeError("No valid group was selected")
    return candidates[int(selected) - 1]


def persist(chat_id, users):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "TELEGRAM_CHAT_ID", 0, winreg.REG_SZ, str(chat_id))
        winreg.SetValueEx(key, "TELEGRAM_ALLOWED_USER_IDS", 0, winreg.REG_SZ, ",".join(str(user) for user in users))
        try:
            winreg.DeleteValue(key, "TELEGRAM_ALLOWED_USER_ID")
        except FileNotFoundError:
            pass


def main():
    if sys.platform != "win32":
        raise RuntimeError("This setup requires Windows")
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "telegram-listener.lock").open("a+") as lock:
        acquire_lock(lock)  # Fails if another getUpdates listener still owns this bot.
        groups, offset = discover()
        chat_id, info = choose(groups)
        users = list(info["users"])
        confirmation = input("Configure this group and these users? Type YES to confirm: ").strip()
        if confirmation != "YES":
            raise RuntimeError("Configuration was not confirmed")
        # Consume setup updates before making the group active.
        while True:
            updates, offset = poll(offset, 0)
            offset = checkpoint(offset)
            if not updates:
                break
        persist(chat_id, users)
        print("Group configuration saved; old single-user setting removed. Bot token unchanged.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Telegram HTTP errors can contain the secret token in their URL.
        print("Telegram group setup stopped; check listener state, bot connection and setup inputs.", file=sys.stderr)
        raise SystemExit(1)
