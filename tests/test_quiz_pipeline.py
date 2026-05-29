# -*- coding: utf-8 -*-
"""
Quiz Pipeline Integration Tests
================================
Run:
    cd backend
    python -m pytest tests/test_quiz_pipeline.py -v -s
"""
import sys
import types
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId
from datetime import datetime, timezone

# ── Stub missing local-only packages before any app import ───────────────────
# agora_token_builder uses sub-module imports: from agora_token_builder.RtcTokenBuilder import ...
_agora_pkg = types.ModuleType("agora_token_builder")
_agora_rtc = types.ModuleType("agora_token_builder.RtcTokenBuilder")
_agora_rtc.Role_Publisher = 1
_agora_rtc.RtcTokenBuilder = MagicMock()
_agora_pkg.RtcTokenBuilder = _agora_rtc
sys.modules["agora_token_builder"] = _agora_pkg
sys.modules["agora_token_builder.RtcTokenBuilder"] = _agora_rtc

# ─────────────────────────────────────────────────────────────────────────────
# Print helpers (ASCII-only — Windows cp1252 safe)
# ─────────────────────────────────────────────────────────────────────────────
DIV = "-" * 60

def header(title: str):
    print(f"\n{DIV}\n  {title}\n{DIV}")

def ok(msg: str):
    print(f"  [PASS] {msg}")

def info(msg: str):
    print(f"  [INFO] {msg}")

def warn(msg: str):
    print(f"  [WARN] {msg}")

# ─────────────────────────────────────────────────────────────────────────────
# Sample Hindi transcripts
# ─────────────────────────────────────────────────────────────────────────────
TRANSCRIPT_PHYSICS = """
Newton ke teen niyam bahut important hain. Pehla niyam: inertia ka niyam —
koi vastu apni avastha nahi badlegi jab tak baahri bal na lage.
Doosra niyam: F = ma, yaani Force barabar mass guna acceleration.
Agar ek 5 kg ki vastu par 10 Newton ka bal lage toh acceleration 2 m/s^2 hoga.
Teesra niyam: har kriya ki samaan aur viprit pratikriya hoti hai.
"""

TRANSCRIPT_CHEMISTRY = """
Periodic table mein elements atomic number ke hisaab se arrange hote hain.
Alkali metals group 1 mein hote hain — sodium, potassium etc.
Valence electrons ki wajah se chemical bonding hoti hai.
Ionic bond mein electrons transfer hote hain, covalent mein share hote hain.
pH scale 0 se 14 tak hoti hai — 7 neutral, 7 se kam acidic, 7 se zyada basic.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Minimal in-memory MongoDB fake
# ─────────────────────────────────────────────────────────────────────────────
class FakeCollection:
    def __init__(self):
        self._docs = []

    async def find_one(self, query, *args, **kwargs):
        for doc in self._docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                return doc
        return None

    async def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self._docs.append(doc)
        m = MagicMock()
        m.inserted_id = doc["_id"]
        return m

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self._docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                if "$set" in update:
                    doc.update(update["$set"])
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        doc[k] = doc.get(k, 0) + v
                if "$push" in update:
                    for k, v in update["$push"].items():
                        if k not in doc:
                            doc[k] = []
                        doc[k].append(v)
                break
        return MagicMock()

class FakeDB:
    def __init__(self):
        self.transcript_cache = FakeCollection()
        self.quizzes          = FakeCollection()
        self.users            = FakeCollection()

# ─────────────────────────────────────────────────────────────────────────────
# TEST 1 — MCQ generation via live API
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mcq_generation_with_hindi_transcript():
    header("TEST 1 -- MCQ Generation (live AI, Hindi transcript)")
    from app.services.quiz_service import generate_mcqs
    from app.config import settings

    has_key = any([settings.groq_api_key, settings.cerebras_api_key, settings.sambanova_api_key])
    if not has_key:
        warn("No AI keys set -- skipping")
        pytest.skip("No AI API keys configured")

    info("Sending 2 Hindi transcripts (physics + chemistry) ...")
    t0 = time.time()
    questions, provider = await generate_mcqs("A", [TRANSCRIPT_PHYSICS, TRANSCRIPT_CHEMISTRY])
    elapsed = time.time() - t0

    ok(f"Provider used   : {provider}")
    ok(f"Time taken      : {elapsed:.1f}s")
    ok(f"Questions count : {len(questions)}")

    diff_count = {}
    for i, q in enumerate(questions):
        d = q.get("difficulty", "?")
        diff_count[d] = diff_count.get(d, 0) + 1
        print(f"\n  Q{i+1} [{d.upper()}]")
        print(f"    {q['question_text'][:85]}...")
        print(f"    Correct: Option {q['correct_answer_index']+1} -- {q['options'][q['correct_answer_index']][:45]}")
        print(f"    Explanation: {q.get('explanation','')[:60]}...")

    info(f"Difficulty breakdown: {diff_count}")

    assert len(questions) == 5
    assert diff_count.get("saral", 0)   == 2, f"Pattern A needs 2 saral, got {diff_count}"
    assert diff_count.get("samanya", 0) == 2, f"Pattern A needs 2 samanya, got {diff_count}"
    assert diff_count.get("kathin", 0)  == 1, f"Pattern A needs 1 kathin, got {diff_count}"
    for q in questions:
        assert len(q["options"]) == 4
        assert 0 <= q["correct_answer_index"] <= 3
        assert q["difficulty"] in ("saral", "samanya", "kathin")

    ok("Pattern A distribution CORRECT (2 saral + 2 samanya + 1 kathin)")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2 — STT with a real public Hindi audio clip
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stt_with_hindi_audio():
    header("TEST 2 -- STT (Groq Whisper, synthesized Hindi speech)")
    from app.services.quiz_service import _stt_groq_whisper
    from app.config import settings

    if not settings.groq_api_key:
        warn("GROQ_API_KEY not set -- skipping STT test")
        pytest.skip("GROQ_API_KEY not configured")

    # Generate a minimal valid WAV file with silence (8kHz mono 1s).
    # Groq Whisper accepts it and returns an empty/minimal transcript —
    # enough to prove the HTTP call reaches the provider and returns.
    import struct, io
    sample_rate = 8000
    num_samples = sample_rate  # 1 second of silence
    wav_buf = io.BytesIO()
    # RIFF header
    data_size = num_samples * 2
    wav_buf.write(b"RIFF")
    wav_buf.write(struct.pack("<I", 36 + data_size))
    wav_buf.write(b"WAVE")
    # fmt chunk
    wav_buf.write(b"fmt ")
    wav_buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
    # data chunk
    wav_buf.write(b"data")
    wav_buf.write(struct.pack("<I", data_size))
    wav_buf.write(b"\x00" * data_size)
    audio_bytes = wav_buf.getvalue()

    info(f"Sending {len(audio_bytes)} bytes of synthesized WAV to Groq Whisper ...")
    t0 = time.time()
    transcript = await _stt_groq_whisper(audio_bytes, settings.groq_api_key)
    elapsed = time.time() - t0

    ok(f"Provider used  : groq_whisper")
    ok(f"Time taken     : {elapsed:.1f}s")
    ok(f"Transcript     : {transcript!r}  (silence -> empty string is valid)")

    assert isinstance(transcript, str)
    ok("STT pipeline reached Groq Whisper and returned a response")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3 — Transcript cache: second call must NOT hit STT API
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_transcript_cache_hit():
    header("TEST 3 -- Transcript cache hit (no duplicate STT calls)")
    from app.services.quiz_service import get_transcript

    db = FakeDB()
    VID = "cache_test_vid_001"
    CACHED = "Newton ka pehla niyam inertia ka niyam hai."

    await db.transcript_cache.insert_one({
        "video_id": VID, "transcript": CACHED,
        "language": "hi", "created_at": datetime.now(timezone.utc),
    })
    info(f"Pre-seeded cache for video_id={VID!r}")

    with patch("app.services.quiz_service._extract_audio", new_callable=AsyncMock) as mock_ffmpeg:
        result = await get_transcript(db, VID, "https://fake.com/video.mp4")

    ok(f"Returned       : {result!r}")
    ok(f"ffmpeg calls   : {mock_ffmpeg.call_count}  (expected 0)")

    assert result == CACHED
    mock_ffmpeg.assert_not_called()
    ok("Cache hit confirmed -- STT API never called")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4 — Watch event counter: increment, dedup, quiz_pool
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_watch_event_counter():
    header("TEST 4 -- Watch event counter logic")
    from app.services.quiz_service import handle_watch_event

    UID = str(ObjectId())
    db = FakeDB()
    await db.users.insert_one({
        "_id": ObjectId(UID),
        "learn_view_count": 0,
        "viewed_video_ids": [],
        "quiz_pool": [],
        "last_quiz_pattern": "B",
    })

    # --- sub-case a: too short ---
    info("Case A: 10s watch (<15s) -- must be ignored")
    r = await handle_watch_event(db, UID, "v001", 80.0, 10.0, "physics", "http://x/v1.mp4")
    assert r["count"] == 0 and not r["quiz_triggered"]
    ok(f"  count={r['count']}  triggered={r['quiz_triggered']}  -- IGNORED")

    # --- sub-case b: 35%+ watch, new video ---
    info("Case B: 40% watch, 20s -- must increment")
    r = await handle_watch_event(db, UID, "v001", 40.0, 20.0, "physics", "http://x/v1.mp4")
    assert r["count"] == 1
    ok(f"  count={r['count']}  -- INCREMENTED")

    # --- sub-case c: same video again ---
    info("Case C: same video v001 again -- abuse prevention, must NOT increment")
    r = await handle_watch_event(db, UID, "v001", 40.0, 20.0, "physics", "http://x/v1.mp4")
    assert r["count"] == 1
    ok(f"  count={r['count']}  -- DUPLICATE BLOCKED")

    # --- sub-case d: 65%+ watch, new video -> added to quiz_pool ---
    info("Case D: 70% watch on v002 -- must increment + add to quiz_pool")
    r = await handle_watch_event(db, UID, "v002", 70.0, 40.0, "chemistry", "http://x/v2.mp4")
    assert r["count"] == 2
    user = await db.users.find_one({"_id": ObjectId(UID)})
    pool_ids = [v["video_id"] for v in user.get("quiz_pool", [])]
    assert "v002" in pool_ids
    ok(f"  count={r['count']}  quiz_pool={pool_ids}  -- ADDED TO POOL")

    ok("All watch-event rules validated")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5 — Full trigger flow: 50 views -> quiz generation fires
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_quiz_trigger_at_50_views():
    header("TEST 5 -- Full trigger flow (50th watch event -> quiz)")
    from app.services.quiz_service import handle_watch_event

    UID = str(ObjectId())
    db = FakeDB()
    pool = [
        {"video_id": f"pool_{i}", "topic": "physics",
         "video_url": f"http://x/p{i}.mp4", "added_at": datetime.now(timezone.utc)}
        for i in range(5)
    ]
    await db.users.insert_one({
        "_id": ObjectId(UID),
        "learn_view_count": 49,
        "viewed_video_ids": [f"prev_{i}" for i in range(49)],
        "quiz_pool": pool,
        "last_quiz_pattern": "A",
    })
    info("User at 49 views, pool=5 videos, pattern=A -- sending 50th event ...")

    tasks_launched = []
    def mock_create_task(coro, **kw):
        coro.close()
        tasks_launched.append("generation_task")
        return MagicMock()

    with patch("app.services.quiz_service.asyncio.create_task", side_effect=mock_create_task):
        r = await handle_watch_event(db, UID, "trigger_50", 70.0, 30.0, "physics", "http://x/t.mp4")

    ok(f"quiz_triggered  : {r['quiz_triggered']}")
    ok(f"quiz_id         : {r.get('quiz_id')}")
    ok(f"count after     : {r['count']}  (should be 0 -- reset)")
    ok(f"tasks launched  : {len(tasks_launched)}  (should be 1)")

    assert r["quiz_triggered"] is True
    assert r["quiz_id"] is not None
    assert r["count"] == 0
    assert len(tasks_launched) == 1

    quiz = await db.quizzes.find_one({"quiz_id": r["quiz_id"]})
    assert quiz["status"] == "generating"
    assert quiz["pattern"] == "B"          # was A -> alternates to B
    ok(f"Quiz in DB      : status={quiz['status']}  pattern={quiz['pattern']}")

    user = await db.users.find_one({"_id": ObjectId(UID)})
    assert user["learn_view_count"] == 0
    assert user["quiz_pool"] == []
    ok("Counter reset=0, pool cleared, pattern A->B")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6 — HTTP endpoints (FastAPI TestClient, no live DB)
# ─────────────────────────────────────────────────────────────────────────────
class TestQuizHTTPEndpoints:

    @pytest.fixture(autouse=True)
    def setup_client(self):
        from app.main import app
        from app.database import get_db
        from fastapi.testclient import TestClient

        self.db = FakeDB()

        # FastAPI dependency override — the correct way to inject a fake DB
        async def override_get_db():
            return self.db

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app, raise_server_exceptions=False)
        yield
        app.dependency_overrides.clear()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    # --- 6a: watch event, short duration ---
    def test_6a_watch_event_too_short(self):
        header("TEST 6a -- POST /quiz/video-watch-event (short, ignored)")
        with patch("app.routes.quiz.handle_watch_event", new_callable=AsyncMock) as m:
            m.return_value = {"count": 0, "quiz_triggered": False}
            r = self.client.post("/quiz/video-watch-event", json={
                "user_id": str(ObjectId()),
                "video_id": "v_short",
                "watch_percentage": 80.0,
                "watch_duration_seconds": 10.0,
            })
        info(f"Status={r.status_code}  Body={r.json()}")
        assert r.status_code == 200
        assert r.json()["quiz_triggered"] is False
        ok("Short watch event -> quiz_triggered=false")

    # --- 6b: quiz status, no pending ---
    def test_6b_quiz_status_no_pending(self):
        header("TEST 6b -- GET /quiz/status/:userId (no pending quiz)")
        UID = str(ObjectId())
        self._run(self.db.users.insert_one({"_id": ObjectId(UID), "pending_quiz_id": None}))
        r = self.client.get(f"/quiz/status/{UID}")
        info(f"Status={r.status_code}  Body={r.json()}")
        assert r.status_code == 200
        assert r.json()["has_pending_quiz"] is False
        ok("No pending quiz -> has_pending_quiz=false")

    # --- 6c: quiz status, generating ---
    def test_6c_quiz_status_generating(self):
        header("TEST 6c -- GET /quiz/status/:userId (quiz generating)")
        UID = str(ObjectId())
        QID = "gen-quiz-xyz"
        self._run(self.db.users.insert_one({"_id": ObjectId(UID), "pending_quiz_id": QID}))
        self._run(self.db.quizzes.insert_one({"quiz_id": QID, "user_id": UID, "status": "generating"}))
        r = self.client.get(f"/quiz/status/{UID}")
        info(f"Status={r.status_code}  Body={r.json()}")
        assert r.status_code == 200
        assert r.json()["has_pending_quiz"] is True
        assert r.json()["status"] == "generating"
        ok("Generating quiz status correctly returned")

    # --- 6d: submit, time < 8s rejected ---
    def test_6d_submit_too_fast(self):
        header("TEST 6d -- POST /quiz/:id/submit (< 8s per question, rejected)")
        r = self.client.post("/quiz/any-id/submit", json={
            "answers": [0, 1, 2, 0, 1],
            "time_per_question": [5.0, 12.0, 8.0, 10.0, 9.0],  # 5s < 8s
        })
        info(f"Status={r.status_code}  Body={r.json()}")
        assert r.status_code == 400
        detail = r.json().get("detail", "").lower()
        assert "8" in detail or "minimum" in detail or "seconds" in detail
        ok("Too-fast submission rejected with 400")

    # --- 6e: correct scoring ---
    def test_6e_submit_scoring(self):
        header("TEST 6e -- POST /quiz/:id/submit (scoring: saral=1, samanya=2, kathin=3)")
        UID = str(ObjectId())
        QID = "score-test-001"
        questions = [
            {"question_text": "Q1", "options": ["A","B","C","D"], "correct_answer_index": 0,
             "explanation": "E1", "difficulty": "saral"},     # user: 0 CORRECT  +1pt
            {"question_text": "Q2", "options": ["A","B","C","D"], "correct_answer_index": 2,
             "explanation": "E2", "difficulty": "samanya"},   # user: 2 CORRECT  +2pt
            {"question_text": "Q3", "options": ["A","B","C","D"], "correct_answer_index": 1,
             "explanation": "E3", "difficulty": "kathin"},    # user: 1 CORRECT  +3pt
            {"question_text": "Q4", "options": ["A","B","C","D"], "correct_answer_index": 3,
             "explanation": "E4", "difficulty": "saral"},     # user: 0 WRONG
            {"question_text": "Q5", "options": ["A","B","C","D"], "correct_answer_index": 0,
             "explanation": "E5", "difficulty": "samanya"},   # user: 1 WRONG
        ]
        self._run(self.db.quizzes.insert_one({
            "quiz_id": QID, "user_id": UID,
            "questions": questions, "status": "ready", "attempted": False,
        }))
        self._run(self.db.users.insert_one({"_id": ObjectId(UID), "skill_score": 0}))

        r = self.client.post(f"/quiz/{QID}/submit", json={
            "answers": [0, 2, 1, 0, 1],
            "time_per_question": [12.0, 15.0, 10.0, 9.0, 11.0],
        })

        info(f"Status={r.status_code}  Body={r.json()}")
        assert r.status_code == 200
        body = r.json()
        # 3 correct: Q1(saral+1) + Q2(samanya+2) + Q3(kathin+3) = 6 pts
        assert body["score"] == 3,              f"Expected score=3, got {body['score']}"
        assert body["total"] == 5
        assert body["skill_score_delta"] == 6,  f"Expected delta=6, got {body['skill_score_delta']}"
        ok(f"score={body['score']}/5  skill_delta=+{body['skill_score_delta']}")
        ok("Scoring: saral=1pt, samanya=2pt, kathin=3pt -> CORRECT")

    # --- 6f: double attempt blocked ---
    def test_6f_double_attempt_blocked(self):
        header("TEST 6f -- POST /quiz/:id/submit (double attempt blocked)")
        UID = str(ObjectId())
        QID = "double-attempt-001"
        self._run(self.db.quizzes.insert_one({
            "quiz_id": QID, "user_id": UID,
            "questions": [], "status": "ready", "attempted": True,
        }))
        r = self.client.post(f"/quiz/{QID}/submit", json={
            "answers": [0]*5, "time_per_question": [10.0]*5,
        })
        info(f"Status={r.status_code}  Body={r.json()}")
        assert r.status_code == 400
        ok("Double attempt blocked with 400")


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7 — Pattern alternation A->B->A->B
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_quiz_pattern_alternation():
    header("TEST 7 -- Quiz pattern alternation (A->B->A->B)")
    from app.services.quiz_service import trigger_quiz_generation

    UID = str(ObjectId())
    db = FakeDB()
    pool = [
        {"video_id": f"v{i}", "topic": "physics",
         "video_url": f"http://x/v{i}.mp4", "added_at": datetime.now(timezone.utc)}
        for i in range(5)
    ]

    patterns = []
    for last in ["B", "A", "B", "A"]:
        def _task(coro, **kw):
            coro.close()
            return MagicMock()
        with patch("app.services.quiz_service.asyncio.create_task", side_effect=_task):
            qid = await trigger_quiz_generation(db, UID, pool, last)
        quiz = await db.quizzes.find_one({"quiz_id": qid})
        patterns.append(quiz["pattern"])
        info(f"  last={last!r}  ->  new={quiz['pattern']!r}")

    assert patterns == ["A", "B", "A", "B"], f"Wrong sequence: {patterns}"
    ok(f"Sequence: {' -> '.join(patterns)}  -- CORRECT")
