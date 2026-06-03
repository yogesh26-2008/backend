from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class WatchEventRequest(BaseModel):
    user_id: Optional[str] = None   # ignored server-side; authenticated user_id is used
    video_id: str
    watch_percentage: float
    watch_duration_seconds: float
    video_topic: Optional[str] = "general"
    video_url: Optional[str] = ""


class WatchEventResponse(BaseModel):
    count: int
    quiz_triggered: bool
    quiz_id: Optional[str] = None
    status: Optional[str] = None
    eta: Optional[int] = None


class QuizQuestion(BaseModel):
    question_text: str
    options: List[str]
    correct_answer_index: int
    explanation: str
    difficulty: str  # saral / samanya / kathin


class QuizResponse(BaseModel):
    quiz_id: str
    user_id: str
    source_video_ids: List[str]
    questions: List[QuizQuestion]
    status: str
    pattern: str
    ai_provider_used: Optional[str] = None
    created_at: datetime
    attempted: bool


class QuizStatusResponse(BaseModel):
    has_pending_quiz: bool
    quiz_id: Optional[str] = None
    status: Optional[str] = None


class SubmitQuizRequest(BaseModel):
    answers: List[int]
    time_per_question: List[float]


class SubmitQuizResponse(BaseModel):
    score: int
    total: int
    correct_answers: List[int]
    explanations: List[str]
    skill_score_delta: int
