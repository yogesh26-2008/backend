"""
Deterministic unit tests for input-validation and URL-transform helpers.
No DB, no network.
"""
from app.routes.users import _sanitize_username, _USERNAME_RE
from app.utils.cloudinary_transform import optimize_image, optimize_video
from app.routes.agora import _extract_participants


# ── Username sanitisation ─────────────────────────────────────────────────────

def test_username_sanitize_basic():
    assert _sanitize_username("  Hello World ") == "hello_world"
    assert _sanitize_username("@Yogesh.K") == "yogesh.k"


def test_username_sanitize_collapses_and_trims():
    assert _sanitize_username("a__b..c") == "a_b_c"     # collapse repeats
    assert _sanitize_username("___leading") == "leading"  # trim edges


def test_username_sanitize_max_length():
    assert len(_sanitize_username("x" * 50)) <= 20


def test_username_regex_accepts_valid():
    assert _USERNAME_RE.match("yogesh_k")
    assert _USERNAME_RE.match("user.name")
    assert _USERNAME_RE.match("abc")


def test_username_regex_rejects_invalid():
    assert not _USERNAME_RE.match("ab")            # too short
    assert not _USERNAME_RE.match("has space")
    assert not _USERNAME_RE.match("UPPER")         # lowercase only
    assert not _USERNAME_RE.match("emoji😀user")


# ── Cloudinary URL transforms ─────────────────────────────────────────────────

def test_optimize_image_injects_once():
    url = "https://res.cloudinary.com/demo/image/upload/v1/trandia/posts/abc.jpg"
    out = optimize_image(url, width=540)
    assert "f_auto" in out and "q_auto:eco" in out and "w_540" in out
    assert optimize_image(out) == out              # idempotent (no double inject)


def test_optimize_image_ignores_non_cloudinary():
    other = "https://example.com/pic.jpg"
    assert optimize_image(other) == other


def test_optimize_video_caps_quality_and_size():
    url = "https://res.cloudinary.com/demo/video/upload/v1/trandia/posts/v.mp4"
    out = optimize_video(url)
    assert "vc_auto" in out and "w_720" in out and "br_1200k" in out


# ── Agora channel parsing ─────────────────────────────────────────────────────

def test_agora_channel_valid():
    a, b = "a" * 24, "b" * 24
    assert _extract_participants(f"trandia_{a}_{b}") == (a, b)


def test_agora_channel_invalid():
    a, b = "a" * 24, "b" * 24
    assert _extract_participants("bad") is None
    assert _extract_participants(f"trandia_{a}") is None         # only one id
    assert _extract_participants(f"nope_{a}_{b}") is None        # wrong prefix
    assert _extract_participants(f"trandia_short_{b}") is None   # wrong id length
