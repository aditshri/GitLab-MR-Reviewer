"""Loading and validation for merge request review rules."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, TypedDict

import yaml


RULE_CATEGORIES = ("code_quality", "security", "performance", "testing")
SEVERITY_LEVELS = ("critical", "high", "medium", "low")


class ReviewRule(TypedDict):
    """A validated review rule."""

    id: str
    severity: str
    description: str


class ReviewRules(TypedDict):
    """Validated rules configuration consumed by the AI review service."""

    general: dict[str, Any]
    team: dict[str, Any]
    rules: dict[str, list[ReviewRule]]


class ReviewRulesError(Exception):
    """Base error for review rules failures."""


class InvalidReviewRulesError(ReviewRulesError):
    """Raised when a rules document does not match the supported schema."""


DEFAULT_REVIEW_RULES: ReviewRules = {
    "general": {
        "tone": "professional",
        "detail_level": "balanced",
        "suggest_improvements": True,
    },
    "team": {
        "name": "default",
        "guidance": [],
    },
    "rules": {
        "code_quality": [
            {
                "id": "clear-maintainable-code",
                "severity": "medium",
                "description": "Prefer clear, maintainable code with focused responsibilities.",
            }
        ],
        "security": [
            {
                "id": "protect-sensitive-data",
                "severity": "critical",
                "description": "Do not expose secrets, credentials, or sensitive data.",
            }
        ],
        "performance": [
            {
                "id": "avoid-unnecessary-work",
                "severity": "medium",
                "description": "Identify avoidable work that could affect runtime or resource usage.",
            }
        ],
        "testing": [
            {
                "id": "cover-behavior",
                "severity": "high",
                "description": "Check that important behavior and regression cases are tested.",
            }
        ],
    },
}


class ReviewRulesService:
    """Load, validate, and provide fallback review rules."""

    def __init__(self, rules_path: str | Path | None = None) -> None:
        """Set the rules file path, defaulting to the bundled rules file."""

        self._rules_path = Path(rules_path) if rules_path else Path(__file__).with_name(
            "review_rules.yaml"
        )

    def load(self) -> ReviewRules:
        """Load validated rules, falling back safely when loading fails."""

        try:
            document = yaml.safe_load(self._rules_path.read_text(encoding="utf-8"))
            self.validate(document)
        except (OSError, yaml.YAMLError, InvalidReviewRulesError):
            return deepcopy(DEFAULT_REVIEW_RULES)

        return document

    @staticmethod
    def validate(document: Any) -> None:
        """Validate a rules document against the supported schema."""

        if not isinstance(document, dict):
            raise InvalidReviewRulesError("Rules document must be a mapping.")

        for section in ("general", "team", "rules"):
            if not isinstance(document.get(section), dict):
                raise InvalidReviewRulesError(f"'{section}' must be a mapping.")

        general = document["general"]
        if not isinstance(general.get("tone"), str) or not general["tone"].strip():
            raise InvalidReviewRulesError("general.tone must be a non-empty string.")
        if not isinstance(general.get("detail_level"), str) or not general[
            "detail_level"
        ].strip():
            raise InvalidReviewRulesError(
                "general.detail_level must be a non-empty string."
            )
        if not isinstance(general.get("suggest_improvements"), bool):
            raise InvalidReviewRulesError(
                "general.suggest_improvements must be a boolean."
            )

        team = document["team"]
        if not isinstance(team.get("name"), str) or not team["name"].strip():
            raise InvalidReviewRulesError("team.name must be a non-empty string.")
        if not isinstance(team.get("guidance"), list) or not all(
            isinstance(item, str) for item in team["guidance"]
        ):
            raise InvalidReviewRulesError("team.guidance must be a list of strings.")

        rules = document["rules"]
        for category in RULE_CATEGORIES:
            category_rules = rules.get(category)
            if not isinstance(category_rules, list):
                raise InvalidReviewRulesError(
                    f"rules.{category} must be a list of rules."
                )
            for rule in category_rules:
                if not isinstance(rule, dict):
                    raise InvalidReviewRulesError("Each rule must be a mapping.")
                if not isinstance(rule.get("id"), str) or not rule["id"].strip():
                    raise InvalidReviewRulesError("Each rule requires a non-empty id.")
                if rule.get("severity") not in SEVERITY_LEVELS:
                    raise InvalidReviewRulesError(
                        f"Rule severity must be one of {SEVERITY_LEVELS}."
                    )
                if not isinstance(rule.get("description"), str) or not rule[
                    "description"
                ].strip():
                    raise InvalidReviewRulesError(
                        "Each rule requires a non-empty description."
                    )
