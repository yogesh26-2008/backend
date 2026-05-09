import asyncio
import json
import os
from pathlib import Path
import firebase_admin
from firebase_admin import credentials, messaging

_initialized = False
_pending_tasks: set[asyncio.Task] = set()


def init_firebase(cred_path: str):
    global _initialized
    if _initialized:
        return

    json_str = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if json_str:
        try:
            cred_dict = json.loads(json_str)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            _initialized = True
            print("[FCM] ✅ Firebase Admin SDK initialized (from env var)")
            return
        except Exception as e:
            print(f"[FCM] ❌ init from env var failed — {e}")
            return

    if not cred_path or not cred_path.strip():
        print("[FCM] ⚠️  No credentials — push notifications disabled")
        return

    path = Path(cred_path)
    if not path.exists():
        print(f"[FCM] ⚠️  Credentials file not found: {path.resolve()}")
        return

    try:
        cred = credentials.Certificate(str(path))
        firebase_admin.initialize_app(cred)
        _initialized = True
        print("[FCM] ✅ Firebase Admin SDK initialized (from file)")
    except Exception as e:
        print(f"[FCM] ❌ init from file failed — {e}")


def schedule_welcome_notification(fcm_token: str | None, name: str, is_signup: bool):
    """Fire-and-forget — schedules notification as background task."""
    if not _initialized:
        print("[FCM] ⚠️  Not initialized — set FIREBASE_CREDENTIALS_JSON on Railway")
        return
    if not fcm_token:
        print("[FCM] ⚠️  fcm_token is None — Flutter did not send a token")
        return

    task = asyncio.create_task(_send(fcm_token, name, is_signup))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


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

    try:
        msg = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            android=messaging.AndroidConfig(
                priority="high",
                ttl=3600,
                notification=messaging.AndroidNotification(
                    title=title,
                    body=body,
                    channel_id="trandia_welcome",
                    color="#00C853",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                    tag="trandia_welcome",
                    # FIX: Removed NotificationPriority and Visibility —
                    # these attributes don't exist in firebase-admin==6.6.0
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10", "apns-push-type": "alert"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(
                            title=title,
                            subtitle=subtitle,
                            body=body,
                        ),
                        badge=1,
                        sound="default",
                        content_available=True,
                    )
                ),
            ),
            data={
                "type": "welcome",
                "screen": "home",
                "event": "signup" if is_signup else "login",
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },
            token=fcm_token,
        )

        response = await asyncio.to_thread(messaging.send, msg)
        print(f"[FCM] ✅ Notification sent to {first_name} — {response}")

    except Exception as e:
        print(f"[FCM] ❌ Send failed — {e}")
        print(f"[FCM]    token prefix: {fcm_token[:30]}...")
