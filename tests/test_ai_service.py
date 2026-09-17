"""Unit tests for the GitHub Models AI review service."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ai_service import AIReviewService


@pytest.fixture
def service() -> AIReviewService:
    """Create an AI service with a mocked OpenAI client."""

    config = SimpleNamespace(
        GITHUB_MODELS_TOKEN="test-token",
        GITHUB_MODELS_ENDPOINT="https://models.example.test/inference",
        GITHUB_MODELS_MODEL="test-model",
    )
    review_service = AIReviewService(config)
    review_service.client = Mock()
    return review_service


def _prompt_kwargs(review_type: str) -> dict[str, object]:
    """Build representative prompt input."""

    return {
        "mr_details": {"title": "Improve parser", "author": "alice"},
        "diff": "diff --git a/parser.py b/parser.py\n+return value",
        "rules": {
            "rules": {
                "code_quality": [],
                "security": [
                    {
                        "id": "validate-input",
                        "severity": "high",
                        "description": "Validate untrusted input.",
                    }
                ],
                "performance": [],
                "testing": [
                    {
                        "id": "cover-behavior",
                        "severity": "medium",
                        "description": "Cover changed behavior.",
                    }
                ],
            },
            "general": {
                "tone": "constructive",
                "detail_level": "comprehensive",
                "suggest_improvements": True,
            },
            "team": {"name": "default", "guidance": []},
        },
        "review_type": review_type,
        "requirements": "Keep the parser backward compatible.",
    }


@pytest.mark.parametrize("review_type", ["quick", "comprehensive", "security", "performance"])
def test_build_review_prompt_contains_context_for_each_review_type(
    service: AIReviewService,
    review_type: str,
) -> None:
    """Each review mode prompt contains its focus and all key review sections."""

    prompt = service.build_review_prompt(**_prompt_kwargs(review_type))

    assert f"Review focus ({review_type})" in prompt
    assert "SYSTEM INSTRUCTIONS" in prompt
    assert "USER REVIEW CONTEXT" in prompt
    assert "Merge request details:" in prompt
    assert "Enabled review rules:" in prompt
    assert "Acceptance criteria and requirements:" in prompt
    assert "Code diff:" in prompt
    assert "Validate untrusted input." in prompt
    assert "Keep the parser backward compatible." in prompt


def test_generate_review_parses_mocked_json_response(
    service: AIReviewService,
) -> None:
    """A valid mocked OpenAI response becomes the structured review shape."""

    service.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"summary":"Looks good","issues":[],"positives":["Clear change"]}'
                )
            )
        ]
    )

    result = service.generate_review(**_prompt_kwargs("quick"))

    assert result["summary"] == "Looks good"
    assert result["issues"] == []
    assert result["positives"] == ["Clear change"]
    assert result["raw_text"]
    call = service.client.chat.completions.create.call_args
    assert call.kwargs["model"] == "test-model"
    assert call.kwargs["messages"][0]["role"] == "system"
    assert call.kwargs["messages"][1]["role"] == "user"
    assert "Improve parser" in call.kwargs["messages"][1]["content"]


def test_generate_review_falls_back_when_response_is_not_json(
    service: AIReviewService,
) -> None:
    """Malformed model output is retained as raw text without raising."""

    service.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not valid JSON"))]
    )

    result = service.generate_review(**_prompt_kwargs("quick"))

    assert result == {
        "summary": "",
        "issues": [],
        "positives": [],
        "raw_text": "not valid JSON",
    }
