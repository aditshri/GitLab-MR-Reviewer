"""Unit tests for MarkdownRulesConverter."""

import yaml

from app.markdown_rules_converter import MarkdownRulesConverter
from app.review_rules_service import RULE_CATEGORIES, ReviewRulesService


def test_simple_markdown_rules_are_converted() -> None:
    """A simple Markdown rule becomes a structured rule."""

    result = MarkdownRulesConverter().convert("- Keep functions focused.")

    rule = result["rules"]["code_quality"][0]
    assert rule["description"] == "Keep functions focused."
    assert rule["severity"] == "medium"


def test_headings_select_rule_categories() -> None:
    """Supported headings route rules to their matching categories."""

    result = MarkdownRulesConverter().convert(
        "## Security Rules\n- [critical] Validate permissions.\n"
        "## Performance\n- Avoid repeated queries."
    )

    assert result["rules"]["security"][0]["severity"] == "critical"
    assert result["rules"]["performance"][0]["description"] == "Avoid repeated queries."


def test_bullet_point_rules_are_supported() -> None:
    """Dash, asterisk, and plus bullets are parsed as rules."""

    result = MarkdownRulesConverter().convert(
        "- Add tests.\n* Protect secrets.\n+ Document public APIs."
    )

    descriptions = [
        rule["description"] for rule in result["rules"]["code_quality"]
    ]
    assert descriptions == ["Add tests.", "Protect secrets.", "Document public APIs."]


def test_numbered_rules_are_supported() -> None:
    """Numbered Markdown rules are parsed without their list markers."""

    result = MarkdownRulesConverter().convert(
        "1. Use type annotations.\n2) Handle errors explicitly."
    )

    assert [rule["description"] for rule in result["rules"]["code_quality"]] == [
        "Use type annotations.",
        "Handle errors explicitly.",
    ]


def test_multiple_categories_are_preserved() -> None:
    """Rules under several headings remain separated by category."""

    result = MarkdownRulesConverter().convert(
        "# Testing\n- Cover edge cases.\n"
        "# Code Quality\n- Prefer readable names.\n"
        "# Security\n- Do not expose tokens."
    )

    assert len(result["rules"]["testing"]) == 1
    assert len(result["rules"]["code_quality"]) == 1
    assert len(result["rules"]["security"]) == 1


def test_empty_markdown_returns_valid_empty_rule_categories() -> None:
    """Empty Markdown is handled safely with a valid structure."""

    result = MarkdownRulesConverter().convert("  \n")

    assert all(result["rules"][category] == [] for category in RULE_CATEGORIES)
    ReviewRulesService.validate(result)


def test_unstructured_markdown_is_retained_gracefully() -> None:
    """Plain non-empty text is retained under the safe default category."""

    result = MarkdownRulesConverter().convert(
        "This content has no heading or list marker but is still meaningful."
    )

    assert result["rules"]["code_quality"][0]["description"] == (
        "This content has no heading or list marker but is still meaningful."
    )


def test_rule_text_and_severity_are_preserved() -> None:
    """Meaningful text remains intact while severity markers are normalized."""

    result = MarkdownRulesConverter().convert(
        "## Testing\n- [HIGH] Keep the exact acceptance criteria wording."
    )

    rule = result["rules"]["testing"][0]
    assert rule["severity"] == "high"
    assert rule["description"] == "Keep the exact acceptance criteria wording."


def test_markdown_can_be_serialized_to_yaml() -> None:
    """YAML serialization round-trips to the converted structure."""

    converter = MarkdownRulesConverter()
    expected = converter.convert("## Security\n- [critical] Validate input.")

    serialized = converter.to_yaml("## Security\n- [critical] Validate input.")

    assert yaml.safe_load(serialized) == expected


def test_converted_rules_are_compatible_with_review_rules_validator() -> None:
    """Converted output satisfies the existing review-rules schema."""

    result = MarkdownRulesConverter().convert(
        "## Testing\n- Add regression coverage.\n## Security\n- [low] Review permissions."
    )

    ReviewRulesService.validate(result)
    assert set(result["rules"]) == set(RULE_CATEGORIES)
