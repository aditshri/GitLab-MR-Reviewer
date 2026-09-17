"""Tests for the GitHub Models-backed Flask routes."""

from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask

from app.routes import register_routes


MR_URL = "https://gitlab.example.invalid/team/project/-/merge_requests/7"


def _client() -> tuple[Flask, Mock, Mock, Mock, Mock]:
    gitlab = Mock()
    gitlab.parse_mr_url.return_value = ("team/project", 7)
    gitlab.get_mr_details.return_value = {
        "title": "Improve parser",
        "author": "author",
        "web_url": MR_URL,
    }
    gitlab.get_mr_diff.return_value = "diff content"
    gitlab.post_comment.return_value = {"id": 1}

    ai = Mock()
    ai.load_rules.return_value = {"general": {}, "team": {}, "rules": {}}
    ai.generate_review.return_value = {
        "summary": "Review result",
        "issues": [],
        "positives": ["Clear change"],
        "raw_text": "{}",
    }
    ai.format_for_gitlab.return_value = "## AI Code Review"

    audit = Mock()
    audit.log_review.return_value = "review-1"
    rules = Mock()
    rules.get_current_rules.return_value = {"general": {}, "team": {}, "rules": {}}

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.testing = True
    register_routes(app, gitlab=gitlab, ai=ai, audit=audit, rules=rules)
    return app, gitlab, ai, audit, rules


def test_review_uses_current_rules_service_and_ai_flow() -> None:
    app, gitlab, ai, audit, rules = _client()

    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "quick", "requirements": "Keep behavior stable"},
    )

    assert response.status_code == 200
    assert response.get_json()["review_id"] == "review-1"
    rules.get_current_rules.assert_called_once_with()
    ai.load_rules.assert_not_called()
    ai.generate_review.assert_called_once()
    audit.log_review.assert_called_once()
    gitlab.get_mr_details.assert_called_once_with("team/project", 7)
    gitlab.get_mr_diff.assert_called_once_with("team/project", 7)


def test_post_review_uses_session_last_review() -> None:
    app, gitlab, ai, _, _ = _client()
    client = app.test_client()

    review_response = client.post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "quick"},
    )
    assert review_response.status_code == 200

    response = client.post("/api/post-review", json={})

    assert response.status_code == 200
    gitlab.post_comment.assert_called_once_with("team/project", 7, "## AI Code Review")
    ai.format_for_gitlab.assert_called_once()


def test_health_endpoint_returns_expected_keys() -> None:
    app, gitlab, _, _, _ = _client()
    gitlab.validate_connection.return_value = True

    response = app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "gitlab_connected": True,
        "ai_configured": True,
    }


def test_health_reports_unconfigured_ai_without_crashing() -> None:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.testing = True
    gitlab = Mock()
    gitlab.validate_connection.return_value = False
    register_routes(app, gitlab=gitlab, ai=None)

    response = app.test_client().get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "gitlab_connected": False,
        "ai_configured": False,
    }


def test_rules_current_endpoint_uses_injected_rules_service() -> None:
    app, _, _, _, rules = _client()
    rules.download_current.return_value = ("rules: {}\n", "default.yaml")

    response = app.test_client().get("/api/rules/current")

    assert response.status_code == 200
    assert response.get_json()["filename"] == "default.yaml"
    rules.get_current_rules.assert_called_once_with()
