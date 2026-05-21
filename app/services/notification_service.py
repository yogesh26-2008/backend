"""
notification_service.py
─────────────────────────────────────────────────────────────────────────────
Centralised FCM push notification dispatcher.

Supported notification types
  • welcome  — sent on login / signup
  • follow   — sent when someone follows the user
  • message  — sent when a chat message arrives for an offline recipient

All sends are fire-and-forget background tasks with exponential retry.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging
from bson import ObjectId

_initialized = False
_pending_tasks: set[asyncio.Task] = set()

_MAX_RETRIES   = 3
_RETRY_BACKOFF = [0.5, 1, 2]  # 3 delays for 3 retry attempts


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


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def schedule_welcome_notification(fcm_token: Optional[str], name: str, is_signup: bool):
    """
    Enqueue a welcome push to be sent ~2 s after login / signup.
    Flutter's local notification shows first; this FCM push is the
    backup for users who background the app immediately.
    The Flutter onMessage listener suppresses type='welcome' to avoid duplicates.
    """
    if not _initialized:
        print("[FCM] ⚠️ Not initialized — skipping welcome")
        return
    if not fcm_token:
        print("[FCM] ⚠️ No FCM token — skipping welcome")
        return
    task = asyncio.create_task(_send_welcome_with_delay(fcm_token, name, is_signup))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def schedule_follow_notification(
    fcm_token: Optional[str],
    follower_username: str,
    follower_name: str,
    follower_id: str = "",
    recipient_id: str = "",
    db=None,
):
    """
    Fire-and-forget follow notification.
    Called from follow endpoint — does NOT block the API response.

    Pre-generates a MongoDB ObjectId so both the DB record and the FCM push
    carry the same `id`. This lets Flutter deduplicate when a user is online
    and receives BOTH a WebSocket notification AND an FCM foreground push.
    """
    # Pre-generate notification ID — shared between DB insert and FCM payload
    notif_id = str(ObjectId())

    # Always persist — even if FCM is not initialised
    if db and recipient_id:
        task_db = asyncio.create_task(
            _store_follow_notification(
                db, notif_id, recipient_id, follower_id, follower_username, follower_name
            )
        )
        _pending_tasks.add(task_db)
        task_db.add_done_callback(_pending_tasks.discard)

    if not _initialized or not fcm_token:
        return

    task = asyncio.create_task(
        _send_follow_notification(fcm_token, follower_username, follower_name, notif_id)
    )
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _store_follow_notification(
    db,
    notif_id: str,
    recipient_id: str,
    from_user_id: str,
    from_username: str,
    from_name: str,
):
    """Persist a follow notification document in MongoDB and deliver via WebSocket."""
    from datetime import datetime, timezone
    try:
        await db.notifications.insert_one({
            "_id": ObjectId(notif_id),
            "recipient_id": recipient_id,
            "type": "follow",
            "from_user_id": from_user_id,
            "from_username": from_username,
            "from_name": from_name,
            "text": "started following you",
            "read": False,
            "created_at": datetime.now(timezone.utc),
        })
        print(f"[NOTIF] ✅ stored follow notification {from_username}→{recipient_id} id={notif_id}")

        # Real-time WebSocket delivery if the user is currently online/connected
        try:
            from app.services.chat_service import manager
            if recipient_id in manager.active_connections:
                payload = json.dumps({
                    "type": "notification",
                    "notification": {
                        "id": notif_id,
                        "recipient_id": recipient_id,
                        "type": "follow",
                        "from_user_id": from_user_id,
                        "from_username": from_username,
                        "from_name": from_name,
                        "text": "started following you",
                        "read": False,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                })
                await manager.send_personal_message(payload, recipient_id)
                print(f"[NOTIF] ✅ sent real-time WS notification to user {recipient_id}")
        except Exception as ws_err:
            print(f"[NOTIF] ⚠️ WebSocket delivery failed: {ws_err}")
    except Exception as e:
        print(f"[NOTIF] ❌ store failed: {e}")


async def _send_follow_notification(
    fcm_token: str,
    follower_username: str,
    follower_name: str,
    notif_id: str = "",
):
    """Send an FCM push for a new follower."""
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
                tag=f"follow_{follower_username}",  # collapse rapid follows
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
            "id":       notif_id,       # ← shared with DB record for Flutter dedup
            "username": follower_username,
            "title":    title,
            "body":     body,
        },
        token=fcm_token,
    )
    await _dispatch(msg, label=f"follow→{follower_username}")


def schedule_message_notification(
    fcm_token: Optional[str],
    sender_username: str,
    conversation_id: str,
):
    """
    Enqueue a 'new message' push for a recipient who is currently offline
    (i.e. has no active WebSocket connection).

    Since messages are E2E-encrypted the backend cannot read the content —
    so the notification body uses a generic template identical to WhatsApp /
    Instagram ("sent you a message").

    The notification carries a data payload so the Flutter app can route
    directly to the correct conversation when tapped.
    """
    if not _initialized:
        return
    if not fcm_token:
        return
    task = asyncio.create_task(
        _send_message_notification(fcm_token, sender_username, conversation_id)
    )
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers — welcome
# ─────────────────────────────────────────────────────────────────────────────

async def _send_welcome_with_delay(fcm_token: str, name: str, is_signup: bool):
    await asyncio.sleep(2)
    await _send_welcome(fcm_token, name, is_signup)


async def _send_welcome(fcm_token: str, name: str, is_signup: bool):
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
                channel_id="trandia_v4",
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

    await _dispatch(msg, label=f"welcome→{first_name}")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers — message notification
# ─────────────────────────────────────────────────────────────────────────────

async def _send_message_notification(
    fcm_token: str,
    sender_username: str,
    conversation_id: str,
):
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
                "apns-priority":  "10",
                "apns-push-type": "alert",
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
