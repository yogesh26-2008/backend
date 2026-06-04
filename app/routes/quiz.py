from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request
import logging

from app.database import get_db
from app.limiter import limiter
from app.utils.jwt_handler import get_current_user_id
from app.models.quiz import (
    WatchEventRequest, WatchEventResponse,
    QuizStatusResponse, QuizResponse,
    SubmitQuizRequest, SubmitQuizResponse,
    AnswerRevealRequest, AnswerRevealResponse,
)
from app.services.quiz_service import handle_watch_event

router = APIRouter()
logger = logging.getLogger(__name__)

DIFFICULTY_POINTS = {"saral": 1, "samanya": 2, "kathin": 3}


def _quiz_doc_to_response(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    return doc


@router.post("/video-watch-event", response_model=WatchEventResponse)
@limiter.limit("60/minute")
async def video_watch_event(
    request: Request,
    body: WatchEventRequest,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    # body.user_id is intentionally ignored — authenticated user_id is used
    try:
        result = await handle_watch_event(
            db=db,
            user_id=current_user_id,
            video_id=body.video_id,
            watch_percentage=body.watch_percentage,
            watch_duration_seconds=body.watch_duration_seconds,
            video_topic=body.video_topic or "general",
            video_url=body.video_url or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"[Quiz] watch-event error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")

    if result.get("quiz_triggered"):
        return WatchEventResponse(
            count=result["count"],
            quiz_triggered=True,
            quiz_id=result["quiz_id"],
            status="quiz_generating",
            eta=30,
        )
    return WatchEventResponse(count=result["count"], quiz_triggered=False)


@router.get("/status/{user_id}", response_model=QuizStatusResponse)
async def quiz_status(
    user_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    # Reject requests where path user_id != authenticated user (IDOR prevention)
    if user_id != current_user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        oid = ObjectId(current_user_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    user = await db.users.find_one({"_id": oid}, {"pending_quiz_id": 1})
    if not user or not user.get("pending_quiz_id"):
        return QuizStatusResponse(has_pending_quiz=False)

    quiz = await db.quizzes.find_one({"quiz_id": user["pending_quiz_id"]})
    if not quiz:
        return QuizStatusResponse(has_pending_quiz=False)

    return QuizStatusResponse(
        has_pending_quiz=True,
        quiz_id=quiz["quiz_id"],
        status=quiz["status"],
    )


@router.get("/{quiz_id}", response_model=QuizResponse)
async def get_quiz(
    quiz_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    quiz = await db.quizzes.find_one({"quiz_id": quiz_id})
    # Return 404 for missing OR unauthorized — don't reveal existence to non-owner
    if not quiz or quiz["user_id"] != current_user_id:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz["status"] != "ready":
        raise HTTPException(status_code=202, detail=f"Quiz status: {quiz['status']}")

    # Anti-cheat: never send correct answers / explanations up-front. They are
    # revealed one question at a time via POST /quiz/{id}/answer, only after the
    # user commits an answer.
    safe_questions = [
        {
            "question_text": q.get("question_text", ""),
            "options": q.get("options", []),
            "difficulty": q.get("difficulty", "saral"),
            "correct_answer_index": None,
            "explanation": None,
        }
        for q in quiz["questions"]
    ]

    return QuizResponse(
        quiz_id=quiz["quiz_id"],
        user_id=quiz["user_id"],
        source_video_ids=quiz["source_video_ids"],
        questions=safe_questions,
        status=quiz["status"],
        pattern=quiz["pattern"],
        ai_provider_used=quiz.get("ai_provider_used"),
        created_at=quiz["created_at"],
        attempted=quiz.get("attempted", False),
    )


@router.post("/{quiz_id}/answer", response_model=AnswerRevealResponse)
@limiter.limit("60/minute")
async def reveal_answer(
    request: Request,
    quiz_id: str,
    body: AnswerRevealRequest,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """
    Reveal the correct answer for ONE question — only after the user commits.
    Records the user's answer first-write-wins so the reveal can't be used to
    brute-force the quiz; submit scores from these recorded answers.
    """
    if not (0 <= body.question_index <= 4):
        raise HTTPException(status_code=400, detail="Invalid question_index")

    quiz = await db.quizzes.find_one({"quiz_id": quiz_id})
    if not quiz or quiz["user_id"] != current_user_id:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.get("status") != "ready":
        raise HTTPException(status_code=400, detail="Quiz not ready")
    if quiz.get("attempted"):
        raise HTTPException(status_code=400, detail="Quiz already submitted")

    questions = quiz.get("questions", [])
    if body.question_index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question_index")

    q = questions[body.question_index]
    correct_index = q.get("correct_answer_index", 0)
    explanation = q.get("explanation", "")

    # Record the answer first-write-wins (only sets it if not already recorded).
    field = f"recorded_answers.{body.question_index}"
    await db.quizzes.update_one(
        {"quiz_id": quiz_id, field: {"$exists": False}},
        {"$set": {field: body.selected}},
    )

    return AnswerRevealResponse(
        correct_index=correct_index,
        explanation=explanation,
        is_correct=(body.selected == correct_index),
    )


@router.post("/{quiz_id}/submit", response_model=SubmitQuizResponse)
async def submit_quiz(
    quiz_id: str,
    body: SubmitQuizRequest,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    if len(body.answers) != 5:
        raise HTTPException(status_code=400, detail="answers must have 5 values")
    if len(body.time_per_question) != 5:
        raise HTTPException(status_code=400, detail="time_per_question must have 5 values")
    if any(t < 8 for t in body.time_per_question):
        raise HTTPException(status_code=400, detail="Minimum 8 seconds required per question")

    # Atomically claim the attempt — prevents a double-submit race from scoring
    # twice. find_one_and_update returns the PRE-update doc (or None if no match).
    quiz = await db.quizzes.find_one_and_update(
        {"quiz_id": quiz_id, "user_id": current_user_id,
         "status": "ready", "attempted": {"$ne": True}},
        {"$set": {"attempted": True}},
    )
    if quiz is None:
        # No atomic match — determine why, preserving the original error codes.
        existing = await db.quizzes.find_one(
            {"quiz_id": quiz_id}, {"user_id": 1, "status": 1, "attempted": 1}
        )
        if not existing or existing.get("user_id") != current_user_id:
            raise HTTPException(status_code=404, detail="Quiz not found")
        if existing.get("status") != "ready":
            raise HTTPException(status_code=400, detail="Quiz not ready")
        raise HTTPException(status_code=400, detail="Quiz already attempted")

    # Score from server-recorded answers (set via /answer) so a client that saw
    # the reveal cannot change its committed answer. For any question that was
    # not recorded (network hiccup, or an older app version), fall back to the
    # submitted answer — safe, because a missing record means the client never
    # saw that answer either.
    recorded = quiz.get("recorded_answers") or {}

    score = 0
    skill_delta = 0
    correct_answers = []
    explanations = []

    for i, q in enumerate(quiz["questions"]):
        ci = q["correct_answer_index"]
        correct_answers.append(ci)
        explanations.append(q.get("explanation", ""))
        if str(i) in recorded:
            user_ans = recorded[str(i)]
        elif i < len(body.answers):
            user_ans = body.answers[i]
        else:
            user_ans = None
        if user_ans == ci:
            score += 1
            skill_delta += DIFFICULTY_POINTS.get(q.get("difficulty", "saral"), 1)

    await db.quizzes.update_one(
        {"quiz_id": quiz_id},
        {"$set": {
            "attempted": True,
            "attempt_result": {
                "score": score,
                "time_per_question": body.time_per_question,
                "completed_at": datetime.now(timezone.utc),
            },
        }},
    )

    try:
        await db.users.update_one(
            {"_id": ObjectId(quiz["user_id"])},
            {"$inc": {"skill_score": skill_delta}, "$set": {"pending_quiz_id": None}},
        )
    except Exception as e:
        logger.error(f"[Quiz] skill_score update failed: {e}")

    return SubmitQuizResponse(
        score=score,
        total=5,
        correct_answers=correct_answers,
        explanations=explanations,
        skill_score_delta=skill_delta,
    )
