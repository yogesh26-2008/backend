"""
notification_service.py
─────────────────────────────────────────────────────────────────────────────
FCM push dispatcher.
DB insert + WS delivery is handled in users.py (directly awaited).
This module only sends FCM pushes.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging

_initialized = False

_MAX_RETRIES   = 3
_RETRY_BACKOFF = [0.5, 1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────

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


def is_fcm_ready() -> bool:
    return _initialized


# ─────────────────────────────────────────────────────────────────────────────
# Async send functions — call these directly with asyncio.create_task()
# from within async route handlers. Never wrap in sync functions.
# ─────────────────────────────────────────────────────────────────────────────

async def send_follow_push(
    fcm_token: str,
    follower_username: str,
    follower_name: str,
    notif_id: str = "",
):
    """
    Send FCM push for a new follower.
    Call as: asyncio.create_task(send_follow_push(...))
    from within an async route handler.
    """
    title = follower_name or follower_username
    body  = "started following you"

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        android=messaging.AndroidConfig(
            priority="high",
            ttl=3600,
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                channel_id="trandia_v4",
                color="#00C853",
                tag=f"follow_{follower_username}",
            ),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10", "apns-push-type": "alert"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title,
                        subtitle="New follower",
                        body=body,
                    ),
                    badge=1,
                    sound="default",
                )
            ),
        ),
        data={
            "type":     "follow",
            "id":       notif_id,
            "username": follower_username,
            "title":    title,
            "body":     body,
        },
        token=fcm_token,
    )
    await _dispatch(msg, label=f"follow→{follower_username}")


async def send_message_push(
    fcm_token: str,
    sender_username: str,
    conversation_id: str,
):
    """Send FCM push for a new message."""
    title = sender_username
    body  = "sent you a message 💬"

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        android=messaging.AndroidConfig(
            priority="high",
            ttl=3600,
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                channel_id="trandia_v4",
                icon="@mipmap/launcher_icon",
                color="#FFFFFF",
                tag=f"msg_{conversation_id}",
                notification_count=1,
            ),
        ),
        apns=messaging.APNSConfig(
            headers={
                "apns-priority":    "10",
                "apns-push-type":   "alert",
                "apns-collapse-id": conversation_id,
            },
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title,
                        subtitle="Trandia",
                        body=body,
                    ),
                    badge=1,
                    sound="default",
                    content_available=True,
                )
            ),
        ),
        data={
            "type":            "message",
            "conversation_id": conversation_id,
            "sender":          sender_username,
            "title":           title,
            "body":            body,
        },
        token=fcm_token,
    )
    await _dispatch(msg, label=f"msg→{sender_username}")


async def send_like_push(
    fcm_token: str,
    liker_name: str,
    liker_username: str,
    post_id: str,
    notif_id: str = "",
):
    """Send FCM push when someone likes a post."""
    title = liker_name or liker_username
    body  = "liked your post ❤️"

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        android=messaging.AndroidConfig(
            priority="high",
            ttl=3600,
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                channel_id="trandia_v4",
                color="#FF4444",
                tag=f"like_{post_id}",
            ),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10", "apns-push-type": "alert"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title,
                        subtitle="New like",
                        body=body,
                    ),
                    badge=1,
                    sound="default",
                )
            ),
        ),
        data={
            "type":     "like",
            "id":       notif_id,
            "post_id":  post_id,
            "username": liker_username,
            "title":    title,
            "body":     body,
        },
        token=fcm_token,
    )
    await _dispatch(msg, label=f"like→{liker_username}")


async def send_welcome_push(fcm_token: str, name: str, is_signup: bool):
    """Send welcome push ~2s after login/signup."""
    await asyncio.sleep(2)
    first_name = name.split()[0] if name else "there"

    if is_signup:
        title = "Welcome to Trandia ✦"
        body  = f"Hi {first_name}, you're all set."
    else:
        title = f"Welcome back, {first_name} ✦"
        body  = "Your feed and conversations are right where you left them."

    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        android=messaging.AndroidConfig(
            priority="high", ttl=3600,
            notification=messaging.AndroidNotification(
                title=title, body=body,
                channel_id="trandia_v4", color="#00C853", tag="trandia_welcome",
            ),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10", "apns-push-type": "alert"},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(title=title, body=body),
                    badge=1, sound="default",
                )
            ),
        ),
        data={"type": "welcome", "event": "signup" if is_signup else "login",
              "title": title, "body": body},
        token=fcm_token,
    )
    await _dispatch(msg, label=f"welcome→{first_name}")


# ─────────────────────────────────────────────────────────────────────────────
# Legacy sync wrappers — kept so auth.py / chat.py don't break
# These schedule tasks from within sync context; only use from async callers.
# ─────────────────────────────────────────────────────────────────────────────

def schedule_welcome_notification(
    fcm_token: Optional[str],
    name: str,
    is_signup: bool,
    master_enabled: bool = True,
):
    if not _initialized or not fcm_token or not master_enabled:
        return
    asyncio.create_task(send_welcome_push(fcm_token, name, is_signup))


def schedule_message_notification(
    fcm_token: Optional[str],
    sender_username: str,
    conversation_id: str,
):
    if not _initialized or not fcm_token:
        return
    asyncio.create_task(send_message_push(fcm_token, sender_username, conversation_id))


# Backward-compat stub (DB insert removed — handled in users.py)
def schedule_follow_notification(
    fcm_token: Optional[str],
    follower_username: str = "",
    follower_name: str = "",
    follower_id: str = "",
    recipient_id: str = "",
    db=None,
):
    if _initialized and fcm_token:
        asyncio.create_task(
            send_follow_push(fcm_token, follower_username, follower_name)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Shared dispatcher with retry
# ─────────────────────────────────────────────────────────────────────────────

async def _dispatch(msg: messaging.Message, label: str):
    last_error: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = await asyncio.to_thread(messaging.send, msg)
            print(f"[FCM] ✅ {label}: {response}")
            return
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF[attempt]
                print(f"[FCM] ⚠️ {label} attempt {attempt + 1} failed: {e} — retry in {wait}s")
                await asyncio.sleep(wait)
    print(f"[FCM] ❌ {label} failed after {_MAX_RETRIES} attempts: {last_error}")
