"""Unit tests for review rules loading and validation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.review_rules_service import (
    DEFAULT_REVIEW_RULES,
    RULE_CATEGORIES,
    SEVERITY_LEVELS,
    InvalidReviewRulesError,
    ReviewRulesService,
)
from app.claude_service import ClaudeService


VALID_RULES = """
general:
  tone: direct
  detail_level: detailed
  suggest_improvements: false
team:
  name: platform
  guidance:
    - Prefer small focused changes.
rules:
  code_quality:
    - id: quality-rule
      severity: low
      description: Keep code easy to maintain.
  security:
    - id: security-rule
      severity: critical
      description: Protect sensitive data.
  performance:
    - id: performance-rule
      severity: high
      description: Avoid unnecessary work.
  testing:
    - id: testing-rule
      severity: medium
      description: Cover important behavior.
"""


def test_valid_rules_are_loaded(tmp_path: Path) -> None:
    """A valid YAML document is returned as a validated rules structure."""

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(VALID_RULES, encoding="utf-8")

    rules = ReviewRulesService(rules_path).load()

    assert rules["general"]["tone"] == "direct"
    assert rules["team"]["name"] == "platform"
    assert rules["rules"]["security"][0]["severity"] == "critical"


def test_missing_rules_file_uses_default(tmp_path: Path) -> None:
    """A missing rules file falls back to the safe defaults."""

    rules = ReviewRulesService(tmp_path / "missing.yaml").load()

    assert rules == DEFAULT_REVIEW_RULES
    assert rules is not DEFAULT_REVIEW_RULES


def test_invalid_yaml_uses_default(tmp_path: Path) -> None:
    """Malformed YAML falls back without propagating a parser error."""

    rules_path = tmp_path / "invalid.yaml"
    rules_path.write_text("general: [", encoding="utf-8")

    assert ReviewRulesService(rules_path).load() == DEFAULT_REVIEW_RULES


def test_invalid_rule_structure_uses_default(tmp_path: Path) -> None:
    """A structurally invalid rules document falls back to defaults."""

    rules_path = tmp_path / "invalid-structure.yaml"
    rules_path.write_text(
        """
general:
  tone: professional
  detail_level: balanced
  suggest_improvements: true
team:
  name: default
  guidance: []
rules:
  code_quality:
    - id: missing-severity
      description: This rule is invalid.
""",
        encoding="utf-8",
    )

    assert ReviewRulesService(rules_path).load() == DEFAULT_REVIEW_RULES


def test_validate_reports_invalid_structure() -> None:
    """The explicit validator identifies an invalid top-level structure."""

    with pytest.raises(InvalidReviewRulesError):
        ReviewRulesService.validate({})


def test_all_severity_levels_are_supported() -> None:
    """The schema supports every configured severity level."""

    assert set(SEVERITY_LEVELS) == {"critical", "high", "medium", "low"}
    for severity in SEVERITY_LEVELS:
        document = {
            "general": {
                "tone": "professional",
                "detail_level": "balanced",
                "suggest_improvements": True,
            },
            "team": {"name": "default", "guidance": []},
            "rules": {
                category: [
                    {
                        "id": f"{category}-rule",
                        "severity": severity,
                        "description": "A valid rule.",
                    }
                ]
                for category in RULE_CATEGORIES
            },
        }
        ReviewRulesService.validate(document)


def test_all_rule_categories_are_supported() -> None:
    """The schema requires and supports each configured rule category."""

    rules = ReviewRulesService().load()

    assert set(rules["rules"]) == set(RULE_CATEGORIES)
    assert all(rules["rules"][category] for category in RULE_CATEGORIES)


def test_claude_service_can_load_default_rules() -> None:
    """ClaudeService exposes the validated bundled rules to the workflow."""

    service = ClaudeService(
        model="test-model",
        config=SimpleNamespace(ANTHROPIC_API_KEY="placeholder-api-key"),
        client=Mock(),
    )

    rules = service.load_review_rules()

    assert set(rules["rules"]) == set(RULE_CATEGORIES)
