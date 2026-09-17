"""Unit tests for ClaudeService."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from anthropic import APIError

from app.claude_service import (
    ClaudeAPIError,
    ClaudeService,
    UnsupportedReviewTypeError,
)


@pytest.fixture
def config() -> SimpleNamespace:
    """Provide non-sensitive test configuration."""

    return SimpleNamespace(ANTHROPIC_API_KEY="placeholder-api-key")


@pytest.fixture
def client() -> Mock:
    """Provide a mocked Anthropic client."""

    mocked_client = Mock()
    mocked_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Review text")]
    )
    return mocked_client


@pytest.fixture
def service(config: SimpleNamespace, client: Mock) -> ClaudeService:
    """Create ClaudeService with an injected mocked client."""

    return ClaudeService(
        model="test-model",
        config=config,
        client=client,
    )


def _review_kwargs(review_type: str = "Quick") -> dict[str, object]:
    """Build representative merge request review input."""

    return {
        "metadata": {"title": "Improve parser", "state": "opened"},
        "combined_diff": "diff --git a/parser.py b/parser.py",
        "review_type": review_type,
        "review_rules": ["Flag correctness issues"],
        "requirements": ["Keep the parser backward compatible"],
    }


def test_service_accepts_configured_api_key_and_model(
    config: SimpleNamespace,
    client: Mock,
) -> None:
    """A configured API key and explicit model initialize the service."""

    service = ClaudeService(model="test-model", config=config, client=client)

    assert service._model == "test-model"
    assert service._client is client


@pytest.mark.parametrize(
    "review_type",
    ["Quick", "Comprehensive", "Security", "Performance"],
)
def test_all_supported_review_types_are_accepted(
    service: ClaudeService,
    review_type: str,
) -> None:
    """Each supported review type produces a Claude request."""

    result = service.review_merge_request(**_review_kwargs(review_type))

    assert result == "Review text"


def test_unsupported_review_type_raises_service_exception(
    service: ClaudeService,
) -> None:
    """Unsupported review types are rejected before calling Claude."""

    with pytest.raises(UnsupportedReviewTypeError):
        service.review_merge_request(**_review_kwargs("Incorrect"))


def test_prompt_contains_metadata_type_rules_requirements_and_diff(
    service: ClaudeService,
    client: Mock,
) -> None:
    """The generated prompt contains every major review context section."""

    service.review_merge_request(
        metadata={"title": "Improve parser", "state": "opened"},
        combined_diff="diff --git a/parser.py b/parser.py",
        review_type="Security",
        review_rules=["Flag correctness issues"],
        requirements=["Keep the parser backward compatible"],
    )

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Improve parser" in prompt
    assert "Security" in prompt
    assert "Flag correctness issues" in prompt
    assert "Keep the parser backward compatible" in prompt
    assert "diff --git a/parser.py b/parser.py" in prompt


def test_string_review_rules_are_included(
    service: ClaudeService,
    client: Mock,
) -> None:
    """String review rules are included as supplied."""

    review_kwargs = _review_kwargs()
    review_kwargs["review_rules"] = "Report only high-confidence issues."
    service.review_merge_request(**review_kwargs)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Report only high-confidence issues." in prompt


def test_sequence_review_rules_are_included(
    service: ClaudeService,
    client: Mock,
) -> None:
    """Each sequence review rule is included as a prompt item."""

    review_kwargs = _review_kwargs()
    review_kwargs["review_rules"] = ["Check validation", "Check error handling"]
    service.review_merge_request(**review_kwargs)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "- Check validation" in prompt
    assert "- Check error handling" in prompt


def test_string_requirements_are_included(
    service: ClaudeService,
    client: Mock,
) -> None:
    """String requirements are included as supplied."""

    review_kwargs = _review_kwargs()
    review_kwargs["requirements"] = "The response must remain backward compatible."
    service.review_merge_request(**review_kwargs)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "The response must remain backward compatible." in prompt


def test_sequence_requirements_are_included(
    service: ClaudeService,
    client: Mock,
) -> None:
    """Each sequence requirement is included as a prompt item."""

    review_kwargs = _review_kwargs()
    review_kwargs["requirements"] = [
        "Support Python 3.11",
        "Keep output deterministic",
    ]
    service.review_merge_request(**review_kwargs)

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "- Support Python 3.11" in prompt
    assert "- Keep output deterministic" in prompt


def test_successful_response_is_returned_as_review_text(
    service: ClaudeService,
    client: Mock,
) -> None:
    """Text from a successful Claude response is returned to the caller."""

    result = service.review_merge_request(**_review_kwargs())

    assert result == "Review text"
    client.messages.create.assert_called_once()
    assert client.messages.create.call_args.kwargs["model"] == "test-model"


def test_anthropic_api_error_becomes_claude_api_error(
    service: ClaudeService,
    client: Mock,
) -> None:
    """Anthropic API errors are converted to ClaudeAPIError."""

    client.messages.create.side_effect = APIError(
        "request failed",
        request=Mock(),
        body={},
    )

    with pytest.raises(ClaudeAPIError):
        service.review_merge_request(**_review_kwargs())


def test_empty_and_non_text_blocks_are_ignored(
    service: ClaudeService,
    client: Mock,
) -> None:
    """Empty text and non-text response blocks are handled without failure."""

    client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text=""),
            SimpleNamespace(type="tool_use", text="ignored"),
            SimpleNamespace(type="text", text="Final finding"),
        ]
    )

    result = service.review_merge_request(**_review_kwargs())

    assert result == "\nFinal finding"
