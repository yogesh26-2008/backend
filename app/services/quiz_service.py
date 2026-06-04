import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import settings
from app.services.notification_service import send_quiz_ready_push, is_fcm_ready
from app.utils.background import fire_and_forget

logger = logging.getLogger(__name__)

TIMEOUT = settings.quiz_generation_timeout_ms / 1000  # convert ms → seconds
DIFFICULTY_POINTS = {"saral": 1, "samanya": 2, "kathin": 3}

# ─────────────────────────────────────────────────────────────────────────────
# STT — Speech to Text
# ─────────────────────────────────────────────────────────────────────────────

_GROQ_MAX_BYTES = 24 * 1024 * 1024  # 24 MB — Groq Whisper hard limit is 25 MB


async def _download_video(video_url: str) -> bytes:
    """Download video bytes directly — no ffmpeg needed."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(video_url)
        resp.raise_for_status()
        return resp.content


async def _stt_groq_whisper(video_bytes: bytes, api_key: str, filename: str = "video.mp4") -> str:
    """Send video/audio bytes directly to Groq Whisper (supports mp4, webm, mov, mp3…)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
    mime = {
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
    }.get(ext, "video/mp4")

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, video_bytes, mime)},
            data={"model": "whisper-large-v3", "language": "hi"},
        )
        resp.raise_for_status()
        return resp.json().get("text") or ""


async def _stt_sarvam(video_bytes: bytes, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": api_key},
            files={"file": ("audio.mp3", video_bytes, "audio/mpeg")},
            data={"language_code": "hi-IN", "model": "saarika:v1"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("transcript") or data.get("text") or ""


async def get_transcript(db: AsyncIOMotorDatabase, video_id: str, video_url: str, topic: str = "general") -> str:
    # 1. Cache check
    cached = await db.transcript_cache.find_one({"video_id": video_id})
    if cached:
        logger.info(f"[STT] Cache hit: {video_id}")
        return cached["transcript"]

    transcript = ""

    # 2. Try downloading video and sending to STT providers
    if video_url:
        logger.info(f"[STT] Downloading video: {video_id}")
        try:
            video_bytes = await _download_video(video_url)
            if len(video_bytes) > _GROQ_MAX_BYTES:
                logger.warning(f"[STT] Video too large ({len(video_bytes)//1024//1024}MB), using topic fallback")
                video_bytes = b""
        except Exception as e:
            logger.error(f"[STT] Download failed: {e}")
            video_bytes = b""

        if video_bytes:
            # Detect filename from URL for correct MIME type
            url_path = video_url.split("?")[0].rstrip("/")
            filename = url_path.rsplit("/", 1)[-1] or "video.mp4"
            if "." not in filename:
                filename = "video.mp4"

            providers = [
                ("groq_whisper", lambda: _stt_groq_whisper(video_bytes, settings.groq_api_key, filename), settings.groq_api_key),
                ("sarvam", lambda: _stt_sarvam(video_bytes, settings.sarvam_api_key), settings.sarvam_api_key),
            ]
            for name, fn, key in providers:
                if not key or key == "your_sarvam_key_here":
                    continue
                try:
                    transcript = await fn()
                    if transcript.strip():
                        logger.info(f"[STT] Success via {name}: {video_id}")
                        break
                except Exception as e:
                    logger.error(f"[STT] {name} failed: {e}")

    # 3. Fallback: use topic as minimal transcript so MCQ can still be generated
    if not transcript:
        transcript = f"Yeh video {topic} topic ke baare mein hai. Is topic ke important concepts, definitions aur key points cover kiye gaye hain."
        logger.warning(f"[STT] Using topic fallback for {video_id}: topic={topic}")

    await db.transcript_cache.insert_one({
        "video_id": video_id,
        "transcript": transcript,
        "language": "hi",
        "topic_tags": [topic],
        "created_at": datetime.now(timezone.utc),
    })
    return transcript


# ─────────────────────────────────────────────────────────────────────────────
# MCQ Generation
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are an expert Indian education content creator. "
    "Generate MCQs for competitive exam students (JEE/NEET/UPSC/CUET). "
    "Always respond in valid JSON only. No markdown, no explanation outside JSON."
)


def _build_user_prompt(pattern: str, transcripts: List[str]) -> str:
    combined = "\n\n".join(
        f"--- Video {i+1} Transcript ---\n{t}" for i, t in enumerate(transcripts)
    )
    return f"""Niche diye gaye video transcripts ke content se exactly 5 MCQ banao. Pattern: {pattern}

Pattern A: 2 saral + 2 samanya + 1 kathin
Pattern B: 1 saral + 2 samanya + 2 kathin

Difficulty definitions:
- saral: Class 10 level, direct recall
- samanya: Class 11-12 application level
- kathin: JEE/NEET preliminary reasoning level

Rules:
- 3 questions: directly from transcript content
- 2 questions: related concept, one step deeper, SAME SUBJECT only
- No fabricated facts
- Hindi mein questions likhna preferred
- Technical terms English mein rakh sakte ho
- Subject boundary kabhi mat todna

VIDEO TRANSCRIPTS:
{combined}

Return ONLY this JSON structure, nothing else:
{{
  "questions": [
    {{
      "question_text": "string",
      "options": ["string", "string", "string", "string"],
      "correct_answer_index": 0,
      "explanation": "string",
      "difficulty": "saral"
    }}
  ]
}}"""


def _parse_questions(raw: str) -> list:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    parsed = json.loads(text)
    questions = parsed.get("questions", [])
    if len(questions) != 5:
        raise ValueError(f"Expected 5 questions, got {len(questions)}")
    for q in questions:
        if len(q.get("options", [])) != 4:
            raise ValueError("Each question needs exactly 4 options")
        if q.get("difficulty") not in ("saral", "samanya", "kathin"):
            raise ValueError(f"Invalid difficulty: {q.get('difficulty')}")
    return questions


async def _call_mcq_provider(url: str, api_key: str, model: str, messages: list) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 2000},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def generate_mcqs(pattern: str, transcripts: List[str]) -> tuple:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(pattern, transcripts)},
    ]
    providers = [
        ("sarvam_chat", "https://api.sarvam.ai/v1/chat/completions", settings.sarvam_api_key, "sarvam-m"),
        ("groq", "https://api.groq.com/openai/v1/chat/completions", settings.groq_api_key, "llama-3.3-70b-versatile"),
        ("cerebras", "https://api.cerebras.ai/v1/chat/completions", settings.cerebras_api_key, "llama-3.3-70b"),
        ("sambanova", "https://api.sambanova.ai/v1/chat/completions", settings.sambanova_api_key, "Meta-Llama-3.3-70B-Instruct"),
    ]
    for name, url, key, model in providers:
        if not key or key == "your_sarvam_key_here":
            continue
        try:
            logger.info(f"[MCQ] Trying {name}")
            raw = await _call_mcq_provider(url, key, model, messages)
            questions = _parse_questions(raw)
            logger.info(f"[MCQ] Success via {name}")
            return questions, name
        except Exception as e:
            logger.error(f"[MCQ] {name} failed: {e}")
    raise RuntimeError("All MCQ providers failed")


# ─────────────────────────────────────────────────────────────────────────────
# Quiz Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _pick_videos(quiz_pool: list, count: int = 5) -> list:
    topic_count: dict = {}
    for v in quiz_pool:
        t = v.get("topic", "general")
        topic_count[t] = topic_count.get(t, 0) + 1
    dominant = max(topic_count, key=lambda k: topic_count[k]) if topic_count else None
    prioritized = (
        [v for v in quiz_pool if v.get("topic") == dominant] +
        [v for v in quiz_pool if v.get("topic") != dominant]
        if dominant else quiz_pool
    )
    return [v["video_id"] for v in prioritized[:count]]


async def _find_cached_quiz(db: AsyncIOMotorDatabase, video_ids: list) -> Optional[dict]:
    sorted_ids = sorted(video_ids)
    return await db.quizzes.find_one({
        "source_video_ids": {"$all": sorted_ids, "$size": len(sorted_ids)},
        "status": "ready",
    })


async def _run_generation(db: AsyncIOMotorDatabase, quiz_id: str, user_id: str, quiz_pool: list, pattern: str):
    try:
        video_ids = _pick_videos(quiz_pool)
        pool_map = {v["video_id"]: v for v in quiz_pool}

        cached = await _find_cached_quiz(db, video_ids)
        if cached:
            logger.info(f"[Quiz] Reusing cached quiz for {video_ids}")
            await db.quizzes.update_one(
                {"quiz_id": quiz_id},
                {"$set": {
                    "questions": cached["questions"],
                    "status": "ready",
                    "ai_provider_used": "cache",
                    "source_video_ids": video_ids,
                }},
            )
        else:
            transcripts = []
            for vid in video_ids:
                entry = pool_map.get(vid, {})
                video_url = entry.get("video_url", "")
                topic = entry.get("topic", "general")
                t = await get_transcript(db, vid, video_url, topic)
                transcripts.append(t)

            questions, provider = await generate_mcqs(pattern, transcripts)

            await db.quizzes.update_one(
                {"quiz_id": quiz_id},
                {"$set": {
                    "questions": questions,
                    "status": "ready",
                    "ai_provider_used": provider,
                    "source_video_ids": video_ids,
                }},
            )

        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"pending_quiz_id": quiz_id, "last_quiz_pattern": pattern}},
        )
        logger.info(f"[Quiz] Quiz {quiz_id} ready for user {user_id}")

        # ── Send FCM data push so Flutter can navigate without polling ──────
        if is_fcm_ready():
            try:
                user_doc = await db.users.find_one(
                    {"_id": ObjectId(user_id)},
                    {"fcm_token": 1},
                )
                fcm_token = user_doc.get("fcm_token") if user_doc else None
                if fcm_token:
                    # FCM push: fire-and-forget, NO retry — duplicates unacceptable
                    fire_and_forget(
                        send_quiz_ready_push(
                            fcm_token=fcm_token,
                            quiz_id=quiz_id,
                            user_id=user_id,
                        )
                    )
                    logger.info(f"[Quiz] FCM quiz_ready scheduled for {user_id[:8]}")
            except Exception as fcm_err:
                logger.warning(f"[Quiz] FCM push failed (non-fatal): {fcm_err}")

    except Exception as e:
        logger.error(f"[Quiz] Generation failed for {quiz_id}: {e}")
        await db.quizzes.update_one({"quiz_id": quiz_id}, {"$set": {"status": "failed"}})


async def trigger_quiz_generation(db: AsyncIOMotorDatabase, user_id: str, quiz_pool: list, last_pattern: str) -> str:
    pattern = "B" if last_pattern == "A" else "A"
    quiz_id = str(uuid.uuid4())

    await db.quizzes.insert_one({
        "quiz_id": quiz_id,
        "user_id": user_id,
        "source_video_ids": [],
        "questions": [],
        "status": "generating",
        "pattern": pattern,
        "ai_provider_used": None,
        "created_at": datetime.now(timezone.utc),
        "attempted": False,
        "attempt_result": None,
    })

    from app.task_queue import task_queue
    await task_queue.enqueue(_run_generation, db, quiz_id, user_id, quiz_pool, pattern)
    return quiz_id


# ─────────────────────────────────────────────────────────────────────────────
# Watch Event Handler
# ─────────────────────────────────────────────────────────────────────────────

QUIZ_TRIGGER_COUNT  = 5    # quiz triggers after 5 unique learn videos
POOL_THRESHOLD_PCT  = 65   # 65%+ watch adds video to quiz pool
COUNT_THRESHOLD_PCT = 35   # 35%+ watch increments learn_view_count
MIN_WATCH_SECONDS   = 2    # 2s floor (bot prevention only — real guard is % threshold)
POOL_MIN_SIZE       = 5    # need at least 5 videos in pool to generate quiz


async def handle_watch_event(
    db: AsyncIOMotorDatabase,
    user_id: str,
    video_id: str,
    watch_percentage: float,
    watch_duration_seconds: float,
    video_topic: str,
    video_url: str,
) -> dict:
    if watch_duration_seconds < MIN_WATCH_SECONDS:
        user = await db.users.find_one({"_id": ObjectId(user_id)}, {"learn_view_count": 1})
        return {"count": user.get("learn_view_count", 0) if user else 0, "quiz_triggered": False}

    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise ValueError("User not found")

    viewed_ids = user.get("viewed_video_ids", [])
    quiz_pool = user.get("quiz_pool", [])
    learn_count = user.get("learn_view_count", 0)
    updates: dict = {}

    if watch_percentage >= COUNT_THRESHOLD_PCT and video_id not in viewed_ids:
        updates["$inc"] = {"learn_view_count": 1}
        updates["$push"] = {"viewed_video_ids": video_id}
        learn_count += 1

    if watch_percentage >= POOL_THRESHOLD_PCT:
        already_in_pool = any(v["video_id"] == video_id for v in quiz_pool)
        if not already_in_pool:
            entry = {
                "video_id": video_id,
                "topic": video_topic or "general",
                "video_url": video_url or "",
                "added_at": datetime.now(timezone.utc),
            }
            if "$push" in updates:
                updates["$push"]["quiz_pool"] = entry
            else:
                updates["$push"] = {"quiz_pool": entry}
            quiz_pool.append(entry)

    if updates:
        await db.users.update_one({"_id": ObjectId(user_id)}, updates)

    if learn_count >= QUIZ_TRIGGER_COUNT and len(quiz_pool) >= POOL_MIN_SIZE:
        last_pattern = user.get("last_quiz_pattern", "B")
        quiz_id = await trigger_quiz_generation(db, user_id, quiz_pool, last_pattern)
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"learn_view_count": 0, "quiz_pool": []}},
        )
        return {"count": 0, "quiz_triggered": True, "quiz_id": quiz_id}

    return {"count": learn_count, "quiz_triggered": False}
