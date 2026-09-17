"""Claude service for generating merge request reviews."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Mapping

from anthropic import APIError, Anthropic

from config import Config
from app.review_rules_service import ReviewRules, ReviewRulesService

ReviewType = Literal["Quick", "Comprehensive", "Security", "Performance"]


class ClaudeServiceError(Exception):
    """Base error for Claude service failures."""


class ClaudeConfigurationError(ClaudeServiceError):
    """Raised when required Claude configuration is missing."""


class ClaudeAPIError(ClaudeServiceError):
    """Raised when the Claude API request fails."""


class UnsupportedReviewTypeError(ClaudeServiceError):
    """Raised when an unsupported review type is requested."""


class ClaudeService:
    """Prepare merge request review prompts and send them to Claude."""

    _REVIEW_TYPE_GUIDANCE: dict[ReviewType, str] = {
        "Quick": "Focus on the most important correctness and maintainability issues.",
        "Comprehensive": "Provide a broad review covering correctness, maintainability, testing, and risks.",
        "Security": "Focus on vulnerabilities, unsafe handling, authorization, secrets, and input validation.",
        "Performance": "Focus on inefficient algorithms, unnecessary work, resource usage, and scalability risks.",
    }

    def __init__(
        self,
        model: str,
        config: type[Config] = Config,
        client: Anthropic | None = None,
    ) -> None:
        """Initialize the Anthropic client with the configured API key and model."""

        if not config.ANTHROPIC_API_KEY:
            raise ClaudeConfigurationError("ANTHROPIC_API_KEY is not configured.")
        if not model.strip():
            raise ClaudeConfigurationError("A Claude model must be provided.")

        self._model = model
        self._client = client or Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def review_merge_request(
        self,
        metadata: dict[str, Any],
        combined_diff: str,
        review_type: ReviewType,
        review_rules: ReviewRules | Sequence[str] | str,
        requirements: Sequence[str] | str,
    ) -> str:
        """Generate a Claude review for the supplied merge request context."""

        prompt = self._build_review_prompt(
            metadata=metadata,
            combined_diff=combined_diff,
            review_type=review_type,
            review_rules=review_rules,
            requirements=requirements,
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
        except APIError as exc:
            raise ClaudeAPIError("Claude review request failed.") from exc

        return self._extract_text(response)

    def load_review_rules(self, rules_path: str | Path | None = None) -> ReviewRules:
        """Load validated review rules for use by the review workflow."""

        return ReviewRulesService(rules_path).load()

    def _build_review_prompt(
        self,
        metadata: dict[str, Any],
        combined_diff: str,
        review_type: ReviewType,
        review_rules: ReviewRules | Sequence[str] | str,
        requirements: Sequence[str] | str,
    ) -> str:
        """Build the review prompt from MR context and review instructions."""

        if review_type not in self._REVIEW_TYPE_GUIDANCE:
            raise UnsupportedReviewTypeError(
                f"Unsupported review type: {review_type}."
            )

        return "\n".join(
            [
                "Review the following GitLab merge request.",
                "",
                "## Merge Request Information",
                self._format_metadata(metadata),
                "",
                f"## Review Type: {review_type}",
                self._REVIEW_TYPE_GUIDANCE[review_type],
                "",
                "## Review Rules",
                self._format_instructions(review_rules),
                "",
                "## Requirements and Acceptance Criteria",
                self._format_instructions(requirements),
                "",
                "## Code Changes",
                combined_diff,
                "",
                "Return a clear review focused on actionable findings and evidence from the changes.",
            ]
        )

    @staticmethod
    def _format_metadata(metadata: dict[str, Any]) -> str:
        """Format merge request metadata without adding credentials."""

        return "\n".join(
            f"- {key}: {value}" for key, value in metadata.items()
        )

    @staticmethod
    def _format_instructions(
        values: ReviewRules | Sequence[str] | str,
    ) -> str:
        """Format either text or a sequence of instruction lines."""

        if isinstance(values, str):
            return values
        if isinstance(values, Mapping) and "rules" in values:
            lines = []
            for category, rules in values["rules"].items():
                lines.append(f"### {category}")
                lines.extend(
                    f"- [{rule['severity']}] {rule['description']}"
                    for rule in rules
                )
            return "\n".join(lines)
        return "\n".join(f"- {value}" for value in values)

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Extract text blocks from an Anthropic messages response."""

        text_blocks = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(text_blocks)
