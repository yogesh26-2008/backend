from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_get_users_me_unauthorized():
    response = client.get("/users/me")
    # Expected to be unauthorized since we haven't provided a token
    assert response.status_code == 403


def test_get_posts_pagination_unauthorized():
    response = client.get("/posts/?skip=10&limit=5")
    # Expected to be unauthorized
    assert response.status_code == 403

