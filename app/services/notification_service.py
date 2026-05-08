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

    # Priority 1: FIREBASE_CREDENTIALS_JSON env var (for Railway / cloud deploy)
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

    # Priority 2: File path (for local development)
    if not cred_path or not cred_path.strip():
        print("[FCM] WARNING: No Firebase credentials provided — notifications disabled")
        return

    path = Path(cred_path)
    if not path.exists():
        print(f"[FCM] WARNING: credentials file not found at {path.resolve()} — notifications disabled")
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
    if is_signup:
        title = "Welcome to Trandia!"
        body = f"Hello {name}, your account is ready. Start exploring Trandia today."
    else:
        title = "Welcome back to Trandia!"
        body = f"Good to see you again, {name}. Pick up right where you left off."

    try:
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=fcm_token,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="trandia_auth",
                    color="#00C853",
                ),
            ),
        )
        response = messaging.send(msg)
        print(f"[FCM] Notification sent: {response}")
    except Exception as e:
        print(f"[FCM] Send failed: {e}")
