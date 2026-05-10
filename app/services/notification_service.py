import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging

_initialized = False
_pending_tasks: set[asyncio.Task] = set()

_MAX_RETRIES   = 3
_RETRY_BACKOFF = [1, 2]


def init_firebase(cred_path: str):
    global _initialized
    if _initialized:
        return
    json_str = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if json_str:
        try:
            cred = credentials.Certificate(json.loads(json_str))
            firebase_admin.initialize_app(cred)
            _initialized = True
            print("[FCM] ✅ Firebase initialized (env var)")
            return
        except Exception as e:
            print(f"[FCM] ❌ init failed: {e}")
            return
    if cred_path and Path(cred_path).exists():
        try:
            firebase_admin.initialize_app(credentials.Certificate(cred_path))
            _initialized = True
            print("[FCM] ✅ Firebase initialized (file)")
        except Exception as e:
            print(f"[FCM] ❌ init failed: {e}")
    else:
        print("[FCM] ⚠️ No credentials — notifications disabled")


def schedule_welcome_notification(fcm_token: Optional[str], name: str, is_signup: bool):
    if not _initialized:
        print("[FCM] ⚠️ Not initialized — skipping")
        return
    if not fcm_token:
        print("[FCM] ⚠️ No FCM token — skipping")
        return
    task = asyncio.create_task(_send_with_delay(fcm_token, name, is_signup))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _send_with_delay(fcm_token: str, name: str, is_signup: bool):
    # ── Delay reduced from 6s → 2s ────────────────────────────────────────
    # Flutter now shows the local notification IMMEDIATELY (zero delay) from
    # HomeScreen as soon as permission is confirmed. The backend FCM push is
    # a BACKUP for background delivery only. 2s is enough for HomeScreen to
    # load and mark the local notification as shown. The foreground listener
    # ignores 'welcome' type pushes so no duplicate appears.
    await asyncio.sleep(2)
    await _send(fcm_token, name, is_signup)


async def _send(fcm_token: str, name: str, is_signup: bool):
    first_name = name.split()[0] if name else "there"

    if is_signup:
        title    = "Welcome to Trandia ✦"
        subtitle = "Your account is ready"
        body     = (
            f"Hi {first_name}, you're all set. "
            "Explore conversations and connect with people around you."
        )
    else:
        title    = f"Welcome back, {first_name} ✦"
        subtitle = "Great to have you back"
        body     = "Your feed and conversations are right where you left them."

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        android=messaging.AndroidConfig(
            priority="high",
            ttl=3600,
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                # Updated to match new channel ID in fcm_service.dart
                channel_id="trandia_ch3",
                color="#00C853",
                tag="trandia_welcome",
            ),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10", "apns-push-type": "alert"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title, subtitle=subtitle, body=body),
                    badge=1,
                    sound="default",
                )
            ),
        ),
        data={
            "type": "welcome",
            "event": "signup" if is_signup else "login",
            "title": title,
            "body": body,
        },
        token=fcm_token,
    )

    last_error: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = await asyncio.to_thread(messaging.send, msg)
            print(f"[FCM] ✅ Sent to {first_name} — {response}")
            return
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF[attempt]
                print(f"[FCM] ⚠️ Attempt {attempt + 1} failed: {e} — retry in {wait}s")
                await asyncio.sleep(wait)

    print(f"[FCM] ❌ Failed after {_MAX_RETRIES} attempts: {last_error}")
