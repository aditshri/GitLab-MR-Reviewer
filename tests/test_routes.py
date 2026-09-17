"""Unit tests for the core review route."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

from app.audit_service import AuditService
from app.claude_service import ClaudeAPIError
from app.gitlab_service import GitLabAPIError
from app.routes import register_routes
from app.rules_management_service import (
    InvalidRuleSetError,
    RuleSetNotFoundError,
    UnsafeRuleSetNameError,
)


MR_URL = "https://gitlab.example.invalid/team/project/-/merge_requests/7"


@pytest.fixture
def services() -> tuple[Mock, Mock, Mock]:
    """Create mocked GitLab, Claude, and audit services."""

    gitlab = Mock()
    gitlab.get_merge_request_metadata.return_value = {
        "title": "Improve parser",
        "author": "author",
        "web_url": MR_URL,
    }
    gitlab.get_merge_request_changes.return_value = {
        "files": [],
        "combined_diff": "diff content",
    }

    claude = Mock()
    claude.load_review_rules.return_value = {"rules": {}}
    claude.review_merge_request.return_value = "Review result"

    audit = Mock()
    audit.log_review.return_value = {
        "review_id": "review-1",
        "review_type": "Quick",
        "timestamp": "2026-09-16T12:00:00+00:00",
        "diff_hash": "hash-1",
    }
    return gitlab, claude, audit


@pytest.fixture
def client(services: tuple[Mock, Mock, Mock]) -> tuple[Flask, tuple[Mock, Mock, Mock]]:
    """Create a Flask test client with injected service mocks."""

    gitlab, claude, audit = services
    app = Flask(__name__)
    app.testing = True
    register_routes(app, gitlab, claude, audit)
    return app, services


def test_successful_review_orchestrates_services_and_returns_json(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """A valid request retrieves, reviews, audits, and returns MR data."""

    app, (gitlab, claude, audit) = client
    response = app.test_client().post(
        "/api/review",
        json={
            "mr_url": MR_URL,
            "review_type": "Quick",
            "review_rules": ["Check tests"],
            "requirements": ["Keep behavior unchanged"],
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "review": "Review result",
        "mr": {"url": MR_URL, "title": "Improve parser", "author": "author", "web_url": MR_URL},
        "review_metadata": {
            "review_id": "review-1",
            "review_type": "Quick",
            "timestamp": "2026-09-16T12:00:00+00:00",
            "diff_hash": "hash-1",
        },
    }
    gitlab.parse_merge_request_url.assert_called_once_with(MR_URL)
    claude.review_merge_request.assert_called_once_with(
        metadata=gitlab.get_merge_request_metadata.return_value,
        combined_diff="diff content",
        review_type="Quick",
        review_rules=["Check tests"],
        requirements=["Keep behavior unchanged"],
    )
    audit.log_review.assert_called_once()


def test_missing_mr_url_returns_bad_request(client: tuple[Flask, tuple[Mock, Mock, Mock]]) -> None:
    """A request without mr_url is rejected."""

    app, (gitlab, _, _) = client
    response = app.test_client().post("/api/review", json={"review_type": "Quick"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "mr_url is required."
    gitlab.parse_merge_request_url.assert_not_called()


def test_health_endpoint_returns_expected_keys() -> None:
    """The health endpoint reports stable system status fields."""

    app = Flask(__name__)
    app.testing = True
    gitlab = Mock()
    gitlab.validate_connection.return_value = True
    register_routes(app, gitlab=gitlab, ai=None)

    response = app.test_client().get("/api/health")

    assert response.status_code == 200
    assert set(response.get_json()) == {
        "status",
        "gitlab_connected",
        "ai_configured",
    }
    assert response.get_json()["status"] == "ok"


def test_invalid_review_type_returns_bad_request(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """An unsupported review type is rejected before service calls."""

    app, (gitlab, _, _) = client
    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "Unknown"},
    )

    assert response.status_code == 400
    gitlab.parse_merge_request_url.assert_not_called()


def test_gitlab_failure_returns_bad_gateway(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """GitLab service failures are returned without internal details."""

    app, (gitlab, _, _) = client
    gitlab.get_merge_request_metadata.side_effect = GitLabAPIError("GitLab unavailable")

    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "Quick"},
    )

    assert response.status_code == 502
    assert response.get_json() == {"error": "GitLab unavailable"}


def test_claude_failure_returns_bad_gateway(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """Claude service failures are returned as gateway errors."""

    app, (_, claude, _) = client
    claude.review_merge_request.side_effect = ClaudeAPIError("Review unavailable")

    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "Quick"},
    )

    assert response.status_code == 502
    assert response.get_json() == {"error": "Review unavailable"}


def test_audit_failure_returns_internal_server_error(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """Audit persistence failures do not report a false successful review."""

    app, (_, _, audit) = client
    audit.log_review.side_effect = OSError("Audit storage unavailable")

    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "Quick"},
    )

    assert response.status_code == 500
    assert response.get_json() == {"error": "Audit storage unavailable"}


def test_optional_requirements_default_to_empty_list(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """Omitting requirements passes an empty list to Claude."""

    app, (_, claude, _) = client
    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "Quick"},
    )

    assert response.status_code == 200
    assert claude.review_merge_request.call_args.kwargs["requirements"] == []


def test_optional_review_rules_load_from_claude_service(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """Omitting review rules loads the existing default rules service path."""

    app, (_, claude, _) = client
    loaded_rules = {"rules": {"security": []}}
    claude.load_review_rules.return_value = loaded_rules

    response = app.test_client().post(
        "/api/review",
        json={"mr_url": MR_URL, "review_type": "Security"},
    )

    assert response.status_code == 200
    claude.load_review_rules.assert_called_once_with()
    assert claude.review_merge_request.call_args.kwargs["review_rules"] == loaded_rules


def test_successful_review_posting_returns_posted_status(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """A valid generated review is posted through GitLabService."""

    app, (gitlab, _, _) = client
    response = app.test_client().post(
        "/api/post-review",
        json={"mr_url": MR_URL, "review_content": "Generated review"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "posted", "mr_url": MR_URL}
    gitlab.post_merge_request_comment.assert_called_once_with(
        MR_URL,
        "Generated review",
    )


@pytest.mark.parametrize(
    "payload",
    [{"review_content": "Generated review"}, {"mr_url": MR_URL}, {"mr_url": MR_URL, "review_content": ""}],
)
def test_review_posting_rejects_missing_or_invalid_data(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
    payload: dict[str, str],
) -> None:
    """Missing or empty post-review fields return HTTP 400."""

    app, (gitlab, _, _) = client
    response = app.test_client().post("/api/post-review", json=payload)

    assert response.status_code == 400
    gitlab.post_merge_request_comment.assert_not_called()


def test_gitlab_failure_during_review_posting_returns_bad_gateway(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """GitLab posting failures return HTTP 502."""

    app, (gitlab, _, _) = client
    gitlab.post_merge_request_comment.side_effect = GitLabAPIError("GitLab unavailable")

    response = app.test_client().post(
        "/api/post-review",
        json={"mr_url": MR_URL, "review_content": "Generated review"},
    )

    assert response.status_code == 502
    assert response.get_json() == {"error": "GitLab unavailable"}


def test_generated_review_content_is_passed_to_gitlab(
    client: tuple[Flask, tuple[Mock, Mock, Mock]],
) -> None:
    """The endpoint forwards the exact generated review content."""

    app, (gitlab, _, _) = client
    generated_review = "## Findings\n- Handle this error path."

    response = app.test_client().post(
        "/api/post-review",
        json={"mr_url": MR_URL, "review_content": generated_review},
    )

    assert response.status_code == 200
    assert gitlab.post_merge_request_comment.call_args.args == (
        MR_URL,
        generated_review,
    )


@pytest.fixture
def rules_client() -> tuple[Flask, Mock]:
    """Create a Flask client with a mocked rules-management service."""

    rules_service = Mock()
    rules_service.list_rule_sets.return_value = [
        {"filename": "default.yaml", "is_default": True, "is_current": True}
    ]
    rules_service.get_current_rules.return_value = {"rules": {}}
    rules_service.read_rule_content.return_value = "rules: {}\n"
    rules_service.download_current.return_value = ("rules: {}\n", "default.yaml")
    rules_service.export_current_markdown.return_value = "# Review Rules\n"
    rules_service.save_content.return_value = {"filename": "custom.yaml"}
    rules_service.save_rules.return_value = {"filename": "custom.yaml"}
    rules_service.upload_content.return_value = {"filename": "custom.yaml"}
    rules_service.reset_to_default.return_value = {"filename": "default.yaml"}

    app = Flask(__name__)
    app.testing = True
    register_routes(app, rules_service=rules_service)
    return app, rules_service


def test_rules_management_endpoints_list_current_and_read(
    rules_client: tuple[Flask, Mock],
) -> None:
    """List, current, and read endpoints delegate to the rules service."""

    app, service = rules_client
    test_client = app.test_client()

    assert test_client.get("/api/rules").status_code == 200
    assert test_client.get("/api/rules/current").status_code == 200
    assert test_client.get("/api/rules/custom.yaml").status_code == 200
    service.list_rule_sets.assert_called_once_with()
    service.get_current_rules.assert_called_once_with()
    service.read_rule_content.assert_called_once_with("custom.yaml")


def test_rules_management_save_and_upload_endpoints(
    rules_client: tuple[Flask, Mock],
) -> None:
    """Save and upload endpoints forward structured and text content."""

    app, service = rules_client
    test_client = app.test_client()

    assert test_client.post(
        "/api/rules", json={"filename": "custom.yaml", "content": "yaml"}
    ).status_code == 200
    assert test_client.post(
        "/api/rules/upload", json={"filename": "custom.md", "content": "markdown"}
    ).status_code == 200
    service.save_content.assert_called_once_with("custom.yaml", "yaml")
    service.upload_content.assert_called_once_with("custom.md", "markdown")


def test_rules_management_reset_delete_and_exports(
    rules_client: tuple[Flask, Mock],
) -> None:
    """Reset, delete, YAML download, and Markdown export are exposed."""

    app, service = rules_client
    test_client = app.test_client()

    assert test_client.post("/api/rules/reset").status_code == 200
    assert test_client.delete("/api/rules/custom.yaml").status_code == 200
    assert test_client.get("/api/rules/download").status_code == 200
    assert test_client.get("/api/rules/markdown").status_code == 200
    service.reset_to_default.assert_called_once_with()
    service.delete_custom_rules.assert_called_once_with("custom.yaml")
    service.download_current.assert_called()
    service.export_current_markdown.assert_called_once_with()


@pytest.mark.parametrize(
    "method,path,payload,exception",
    [
        ("get", "/api/rules/missing.yaml", None, RuleSetNotFoundError("missing")),
        ("post", "/api/rules", {"filename": "bad.yaml", "content": "bad"}, InvalidRuleSetError("bad")),
        ("get", "/api/rules/../outside.yaml", None, UnsafeRuleSetNameError("unsafe")),
    ],
)
def test_rules_management_errors_return_safe_status_codes(
    rules_client: tuple[Flask, Mock],
    method: str,
    path: str,
    payload: dict[str, str] | None,
    exception: Exception,
) -> None:
    """Missing and invalid rules requests return appropriate client errors."""

    app, service = rules_client
    if method == "get" and "missing" in path:
        service.read_rule_content.side_effect = exception
    elif method == "get":
        service.read_rule_content.side_effect = exception
    else:
        service.save_content.side_effect = exception

    response = getattr(app.test_client(), method)(path, json=payload)

    assert response.status_code in {400, 404}
