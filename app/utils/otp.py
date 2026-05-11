import random
import string
from datetime import datetime, timezone, timedelta

OTP_EXPIRE_MINUTES = 10
MAX_OTP_ATTEMPTS = 5


def generate_otp() -> str:
    """Generate a cryptographically random 6-digit OTP."""
    return ''.join(random.SystemRandom().choices(string.digits, k=6))


def otp_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)
