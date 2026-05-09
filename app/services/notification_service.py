import asyncio
import json
import os
from pathlib import Path
import firebase_admin
from firebase_admin import credentials, messaging

_initialized = False


def init_firebase(cred_path: str):
    global _initialized
    if _initialized:
        return

    # Priority 1: FIREBASE_CREDENTIALS_JSON env var (Railway / cloud deploy)
    json_str = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if json_str:
        try:
            cred_dict = json.loads(json_str)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            _initialized = True
            print("[FCM] Firebase Admin SDK initialized (from env var)")
            return
        except Exception as e:
            print(f"[FCM] WARNING: init from env var failed — {e}")
            return

    # Priority 2: File path (local development)
    if not cred_path or not cred_path.strip():
        print("[FCM] WARNING: No Firebase credentials — push notifications disabled")
        return

    path = Path(cred_path)
    if not path.exists():
        print(f"[FCM] WARNING: credentials file not found at {path.resolve()}")
        return

    try:
        cred = credentials.Certificate(str(path))
        firebase_admin.initialize_app(cred)
        _initialized = True
        print("[FCM] Firebase Admin SDK initialized (from file)")
    except Exception as e:
        print(f"[FCM] WARNING: init failed — {e}")


async def send_welcome_notification(fcm_token: str, name: str, is_signup: bool):
    """Send a premium welcome notification after signup or login.

    Runs the synchronous Firebase send() in a thread pool so the FastAPI
    event loop is never blocked during the FCM HTTP round-trip.
    Notification failure is always non-fatal — auth must still succeed.
    """
    if not _initialized or not fcm_token:
        return

    first_name = name.split()[0] if name else "there"

    if is_signup:
        title    = "Welcome to Trandia ✦"
        subtitle = "Your account is ready"          # shown on iOS between title & body
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
            # ── Notification payload (shown in system tray) ──────────────────
            notification=messaging.Notification(
                title=title,
                body=body,
            ),

            # ── Android config ───────────────────────────────────────────────
            android=messaging.AndroidConfig(
                priority="high",
                ttl=3600,                            # discard if undelivered after 1 h
                notification=messaging.AndroidNotification(
                    title=title,
                    body=body,
                    channel_id="trandia_welcome",
                    color="#00C853",                 # Trandia green accent
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                    tag="trandia_welcome",           # replaces previous welcome notif
                    # Notification style: inbox / bigText auto-selected by OS
                    notification_priority=messaging.NotificationPriority.HIGH,
                    visibility=messaging.Visibility.PUBLIC,
                ),
            ),

            # ── APNs (iOS) config ────────────────────────────────────────────
            apns=messaging.APNSConfig(
                headers={
                    "apns-priority": "10",           # immediate delivery
                    "apns-push-type": "alert",
                },
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(
                            title=title,
                            subtitle=subtitle,       # iOS shows 3-line hierarchy
                            body=body,
                        ),
                        badge=1,
                        sound="default",
                        content_available=True,
                    )
                ),
            ),

            # ── Data payload (Flutter can read these in background handler) ──
            data={
                "type": "welcome",
                "screen": "home",
                "event": "signup" if is_signup else "login",
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },

            token=fcm_token,
        )

        # messaging.send() is synchronous — run in thread pool
        response = await asyncio.to_thread(messaging.send, msg)
        print(f"[FCM] ✅ Welcome notification sent → {first_name}: {response}")

    except Exception as e:
        # Notification failure must NEVER crash the auth flow
        print(f"[FCM] Send failed (non-fatal): {e}")
