import asyncio
import json
import os
from pathlib import Path
import firebase_admin
from firebase_admin import credentials, messaging

_initialized = False
_pending_tasks: set[asyncio.Task] = set()

# BUG FIX #4 — FCM send retry constants.
# A transient network error, Firebase 500, or quota blip caused the single
# send() attempt to fail and the notification to be permanently lost.
# Three attempts with exponential back-off (1 s, 2 s) covers the vast majority
# of transient failures without delaying the happy-path more than one second.
_MAX_RETRIES   = 3
_RETRY_BACKOFF = [1, 2]  # seconds before attempt 2 and 3


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


def schedule_welcome_notification(fcm_token: str | None, name: str, is_signup: bool):
    if not _initialized:
        print("[FCM] ⚠️ Not initialized — skipping notification")
        return
    if not fcm_token:
        print("[FCM] ⚠️ fcm_token is None — no device to send to")
        return
    task = asyncio.create_task(_send_with_delay(fcm_token, name, is_signup))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _send_with_delay(fcm_token: str, name: str, is_signup: bool):
    # 3 s delay so the notification arrives AFTER the app has navigated to
    # HomeScreen and the onMessage listener is active.
    await asyncio.sleep(3)
    await _send(fcm_token, name, is_signup)


async def _send(fcm_token: str, name: str, is_signup: bool):
    first_name = name.split()[0] if name else "there"

    if is_signup:
        title    = "Welcome to Trandia ✦"
        subtitle = "Your account is ready"
        body     = (
            f"Hi {first_name}, you're all set. "
            "Explore conversations, connect with people around you, "
            "and make your voice heard."
        )
    else:
        title    = f"Welcome back, {first_name} ✦"
        subtitle = "Great to have you back"
        body     = (
            "Your feed, connections, and conversations "
            "are right where you left them."
        )

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        android=messaging.AndroidConfig(
            priority="high",
            ttl=3600,
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                # Must match _kChannelId in fcm_service.dart AND
                # default_notification_channel_id in AndroidManifest.xml
                channel_id="trandia_ch2",
                color="#00C853",
                tag="trandia_welcome",
            ),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10", "apns-push-type": "alert"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title, subtitle=subtitle, body=body,
                    ),
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

    # BUG FIX #4 — Retry with exponential back-off.
    # Previously a single send() failure (network blip, Firebase 503, etc.)
    # silently dropped the notification with no retry.  Now we attempt up to
    # _MAX_RETRIES times before giving up, logging each failure.
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = await asyncio.to_thread(messaging.send, msg)
            print(f"[FCM] ✅ Sent to {first_name} — {response}")
            return  # success — exit immediately
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF[attempt]
                print(
                    f"[FCM] ⚠️ Send attempt {attempt + 1} failed: {e} "
                    f"— retrying in {wait}s"
                )
                await asyncio.sleep(wait)

    print(f"[FCM] ❌ Send failed after {_MAX_RETRIES} attempts: {last_error}")
