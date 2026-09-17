"""OpenAI-compatible AI review service for GitHub Models."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from openai import APIConnectionError, APIError, AuthenticationError, OpenAI, RateLimitError


logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """Raised when the AI review service cannot complete an operation."""


DEFAULT_RULES: dict[str, Any] = {
    "enabled_categories": ["code_quality", "security", "performance", "testing"],
    "rules": {
        "code_quality": ["Check correctness, readability, and maintainability."],
        "security": ["Check authentication, authorization, secrets, and input validation."],
        "performance": ["Check unnecessary work, inefficient algorithms, and resource usage."],
        "testing": ["Check coverage of changed behavior and regression cases."],
    },
}

_REVIEW_GUIDANCE = {
    "quick": "Prioritize only the highest-confidence, highest-impact findings and keep the review concise.",
    "comprehensive": "Review broadly and deeply for correctness, maintainability, testing, security, and operational risks.",
    "security": "Focus deeply on vulnerabilities, authorization, authentication, secrets, unsafe input handling, and data exposure.",
    "performance": "Focus deeply on inefficient algorithms, unnecessary work, resource usage, latency, and scalability.",
}

_REVIEW_TYPES = frozenset(_REVIEW_GUIDANCE)


class AIReviewService:
    """Generate structured code reviews through the GitHub Models API."""

    def __init__(self, config: Any) -> None:
        """Create an OpenAI client configured for GitHub Models."""

        token = getattr(config, "GITHUB_MODELS_TOKEN", None)
        if not token:
            raise AIServiceError("GITHUB_MODELS_TOKEN is missing.")

        self.model = getattr(config, "GITHUB_MODELS_MODEL", None)
        endpoint = getattr(config, "GITHUB_MODELS_ENDPOINT", None)
        if not self.model:
            raise AIServiceError("GITHUB_MODELS_MODEL is missing.")
        if not endpoint:
            raise AIServiceError("GITHUB_MODELS_ENDPOINT is missing.")

        try:
            self.client = OpenAI(api_key=token, base_url=endpoint)
        except Exception as exc:
            raise AIServiceError("Unable to initialize the GitHub Models client.") from exc

    @staticmethod
    def load_rules(rules_file_path: str | Path) -> dict[str, Any]:
        """Load YAML review rules, falling back to defaults on any file error."""

        try:
            rules = yaml.safe_load(Path(rules_file_path).read_text(encoding="utf-8"))
            if not isinstance(rules, dict):
                raise ValueError("rules document must be a mapping")
            return rules
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Unable to load review rules from %s: %s", rules_file_path, exc)
            return dict(DEFAULT_RULES)

    def build_review_prompt(
        self,
        mr_details: Mapping[str, Any],
        diff: str,
        rules: Mapping[str, Any],
        review_type: str,
        requirements: str | None = None,
    ) -> str:
        """Build the system instructions and user review context as one prompt."""

        review_type = review_type.lower()
        if review_type not in _REVIEW_TYPES:
            allowed = ", ".join(sorted(_REVIEW_TYPES))
            raise AIServiceError(f"Unsupported review_type '{review_type}'. Use: {allowed}.")

        requirements_section = requirements.strip() if isinstance(requirements, str) else ""
        return "\n".join(
            [
                "SYSTEM INSTRUCTIONS",
                "You are an expert code reviewer. Return only valid JSON with this shape:",
                '{"summary": "", "issues": [{"severity": "", "category": "", "file": "", "line": "", "description": "", "suggestion": ""}], "positives": [""]}',
                "Use evidence from the diff. Do not invent files or line numbers.",
                f"Review focus ({review_type}): {_REVIEW_GUIDANCE[review_type]}",
                "",
                "USER REVIEW CONTEXT",
                "Merge request details:",
                self._format_mapping(mr_details),
                "",
                "Enabled review rules:",
                self._format_rules(rules),
                "",
                "Acceptance criteria and requirements:",
                requirements_section or "No additional requirements were provided.",
                "",
                "Code diff:",
                diff,
            ]
        )

    def generate_review(
        self,
        mr_details: Mapping[str, Any],
        diff: str,
        rules: Mapping[str, Any],
        review_type: str,
        requirements: str | None = None,
    ) -> dict[str, Any]:
        """Generate and defensively parse a structured review response."""

        prompt = self.build_review_prompt(
            mr_details=mr_details,
            diff=diff,
            rules=rules,
            review_type=review_type,
            requirements=requirements,
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Return a valid JSON code review matching the requested schema.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
        except RateLimitError as exc:
            raise AIServiceError("GitHub Models rate limit exceeded.") from exc
        except AuthenticationError as exc:
            raise AIServiceError("GitHub Models authentication failed. Check GITHUB_MODELS_TOKEN.") from exc
        except APIConnectionError as exc:
            raise AIServiceError("Unable to connect to the GitHub Models endpoint.") from exc
        except APIError as exc:
            raise AIServiceError("GitHub Models API request failed.") from exc
        except Exception as exc:
            raise AIServiceError("GitHub Models request failed unexpectedly.") from exc

        raw_text = self._response_text(response)
        try:
            parsed = json.loads(self._strip_json_fence(raw_text))
        except (json.JSONDecodeError, TypeError):
            return {
                "summary": "",
                "issues": [],
                "positives": [],
                "raw_text": raw_text,
            }
        return self._normalize_review(parsed, raw_text)

    @staticmethod
    def format_for_gitlab(review_dict: Mapping[str, Any]) -> str:
        """Render a structured review as GitLab-flavored Markdown."""

        sections = ["## AI Code Review", "", "### Summary", str(review_dict.get("summary") or "").strip()]
        issues = review_dict.get("issues") or []
        sections.extend(["", "### Issues"])
        if isinstance(issues, list) and issues:
            for issue in issues:
                if not isinstance(issue, Mapping):
                    continue
                severity = str(issue.get("severity") or "Medium").title()
                category = str(issue.get("category") or "General")
                location = str(issue.get("file") or "unknown file")
                line = str(issue.get("line") or "")
                if line:
                    location += f":{line}"
                sections.append(f"- [{severity}] **{category}** ({location}): {issue.get('description', '')}")
                suggestion = str(issue.get("suggestion") or "").strip()
                if suggestion:
                    sections.append(f"  Suggestion: {suggestion}")
        else:
            sections.append("- No issues identified.")

        sections.extend(["", "### Positives"])
        positives = review_dict.get("positives") or []
        if isinstance(positives, list) and positives:
            sections.extend(f"- {positive}" for positive in positives)
        else:
            sections.append("- No positives provided.")
        return "\n".join(sections).strip() + "\n"

    @staticmethod
    def _format_mapping(values: Mapping[str, Any]) -> str:
        return "\n".join(f"- {key}: {value}" for key, value in values.items())

    @staticmethod
    def _format_rules(rules: Mapping[str, Any]) -> str:
        rule_groups = rules.get("rules", rules)
        enabled = rules.get("enabled_categories")
        if isinstance(enabled, list):
            enabled_categories = [str(category) for category in enabled]
        elif isinstance(rule_groups, Mapping):
            enabled_categories = [str(category) for category, values in rule_groups.items() if values]
        else:
            return "- No enabled rules provided."

        lines: list[str] = []
        for category in enabled_categories:
            values = rule_groups.get(category, []) if isinstance(rule_groups, Mapping) else []
            lines.append(f"### {category}")
            if isinstance(values, list):
                lines.extend(f"- {value}" for value in values)
            elif values:
                lines.append(f"- {values}")
        return "\n".join(lines) or "- No enabled rules provided."

    @staticmethod
    def _response_text(response: Any) -> str:
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError, TypeError):
            return ""
        if isinstance(content, str):
            return content
        return str(content or "")

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.IGNORECASE | re.DOTALL)
        return match.group(1) if match else text

    @staticmethod
    def _normalize_review(value: Any, raw_text: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"summary": "", "issues": [], "positives": [], "raw_text": raw_text}

        issues = value.get("issues")
        normalized_issues = []
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, Mapping):
                    normalized_issues.append(
                        {
                            "severity": str(issue.get("severity") or "Medium"),
                            "category": str(issue.get("category") or "General"),
                            "file": str(issue.get("file") or ""),
                            "line": str(issue.get("line") or ""),
                            "description": str(issue.get("description") or ""),
                            "suggestion": str(issue.get("suggestion") or ""),
                        }
                    )
        positives = value.get("positives")
        normalized_positives = [str(item) for item in positives] if isinstance(positives, list) else []
        return {
            "summary": str(value.get("summary") or ""),
            "issues": normalized_issues,
            "positives": normalized_positives,
            "raw_text": raw_text,
        }
