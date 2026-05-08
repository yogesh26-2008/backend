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
    if not _initialized or not fcm_token:
        return

    first_name = name.split()[0] if name else "there"

    if is_signup:
        title = "Welcome to Trandia! 🎉"
        body = (
            f"Hey {first_name}, your account is live. "
            "Start exploring conversations, connect with people, and make your voice heard."
        )
        image = None
    else:
        title = f"Welcome back, {first_name}! 👋"
        body = (
            "Great to see you again. "
            "Your conversations are waiting — jump right back in."
        )
        image = None

    try:
        msg = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
                image=image,
            ),
            token=fcm_token,
            android=messaging.AndroidConfig(
                priority="high",
                ttl=3600,               # notification expires in 1 hour
                notification=messaging.AndroidNotification(
                    channel_id="trandia_auth",
                    color="#00C853",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                    tag="trandia_welcome",
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        badge=1,
                        sound="default",
                        content_available=True,
                    )
                ),
            ),
            data={
                "type": "welcome",
                "screen": "home",
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },
        )
        response = messaging.send(msg)
        print(f"[FCM] ✅ Notification sent to {first_name}: {response}")
    except Exception as e:
        print(f"[FCM] Send failed: {e}")
