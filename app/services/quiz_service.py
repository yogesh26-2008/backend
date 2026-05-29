import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import settings

logger = logging.getLogger(__name__)

TIMEOUT = settings.quiz_generation_timeout_ms / 1000  # convert ms → seconds
DIFFICULTY_POINTS = {"saral": 1, "samanya": 2, "kathin": 3}

# ─────────────────────────────────────────────────────────────────────────────
# STT — Speech to Text
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_audio(video_url: str, tmp_path: str) -> None:
    """Use ffmpeg subprocess to extract mono 16kHz mp3 from video URL."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_url,
        "-vn",
        "-ar", "16000",
        "-ac", "1",
        "-ab", "64k",
        "-f", "mp3",
        tmp_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.wait(), timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed to extract audio")


async def _stt_sarvam(audio_bytes: bytes, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": api_key},
            files={"file": ("audio.mp3", audio_bytes, "audio/mpeg")},
            data={"language_code": "hi-IN", "model": "saarika:v1"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("transcript") or data.get("text") or ""


async def _stt_groq_whisper(audio_bytes: bytes, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("audio.mp3", audio_bytes, "audio/mpeg")},
            data={"model": "whisper-large-v3", "language": "hi"},
        )
        resp.raise_for_status()
        return resp.json().get("text") or ""


async def get_transcript(db: AsyncIOMotorDatabase, video_id: str, video_url: str) -> str:
    cached = await db.transcript_cache.find_one({"video_id": video_id})
    if cached:
        logger.info(f"[STT] Cache hit: {video_id}")
        return cached["transcript"]

    logger.info(f"[STT] Extracting audio: {video_id}")
    tmp_path = os.path.join(tempfile.gettempdir(), f"trandia_{uuid.uuid4().hex}.mp3")
    try:
        await _extract_audio(video_url, tmp_path)
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    transcript = ""
    providers = [
        ("sarvam", lambda: _stt_sarvam(audio_bytes, settings.sarvam_api_key), settings.sarvam_api_key),
        ("groq_whisper", lambda: _stt_groq_whisper(audio_bytes, settings.groq_api_key), settings.groq_api_key),
    ]
    for name, fn, key in providers:
        if not key or key == "your_sarvam_key_here":
            continue
        try:
            transcript = await fn()
            if transcript.strip():
                logger.info(f"[STT] Success via {name}")
                break
        except Exception as e:
            logger.error(f"[STT] {name} failed: {e}")

    if not transcript:
        raise RuntimeError("All STT providers failed")

    await db.transcript_cache.insert_one({
        "video_id": video_id,
        "transcript": transcript,
        "language": "hi",
        "topic_tags": [],
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
                video_url = pool_map.get(vid, {}).get("video_url", "")
                t = await get_transcript(db, vid, video_url)
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

    asyncio.create_task(_run_generation(db, quiz_id, user_id, quiz_pool, pattern))
    return quiz_id


# ─────────────────────────────────────────────────────────────────────────────
# Watch Event Handler
# ─────────────────────────────────────────────────────────────────────────────

QUIZ_TRIGGER_COUNT = 5       # TEST: 5 reels → trigger (production: 50)
POOL_THRESHOLD_PCT  = 5       # TEST: 5% watch → pool entry (production: 65)
COUNT_THRESHOLD_PCT = 5       # TEST: 5% watch → count++  (production: 35)
MIN_WATCH_SECONDS   = 1       # TEST: 1s minimum           (production: 15)
POOL_MIN_SIZE       = 5       # how many videos needed in pool before quiz


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
