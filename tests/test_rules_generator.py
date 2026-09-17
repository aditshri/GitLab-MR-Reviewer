"""Unit tests for RulesGenerator analysis."""

from app.rules_generator import RulesGenerator
from app.review_rules_service import RULE_CATEGORIES, ReviewRulesService


def test_empty_comment_input_returns_empty_analysis() -> None:
    """No comments produce an empty, well-shaped analysis."""

    result = RulesGenerator().analyze_project_comments([])

    assert result["scope"] == "project"
    assert result["comment_count"] == 0
    assert result["comments"] == []
    assert result["patterns"] == []


def test_single_review_comment_is_normalized_and_analyzed() -> None:
    """One structured comment is retained and categorized."""

    result = RulesGenerator().analyze_project_comments(
        [{"body": "Please add a test for this path.", "merge_request_id": "mr-1"}]
    )

    assert result["comment_count"] == 1
    assert result["comments"][0]["category"] == "testing"
    assert result["patterns"][0]["occurrences"] == 1


def test_comments_belonging_to_multiple_categories_are_classified() -> None:
    """Comments containing distinct feedback are assigned to their categories."""

    comments = [
        {"body": "Please add test coverage."},
        {"body": "Validate authorization and secrets."},
        {"body": "This query has a performance issue."},
        {"body": "Update the API documentation."},
    ]

    result = RulesGenerator().analyze_project_comments(comments)

    assert {comment["category"] for comment in result["comments"]} == {
        "testing",
        "security",
        "performance",
        "documentation",
    }


def test_repeated_feedback_is_aggregated() -> None:
    """Equivalent normalized comments become one recurring pattern."""

    result = RulesGenerator().analyze_project_comments(
        [
            {"body": "Please add test coverage.", "merge_request_id": "mr-1"},
            {"body": "  PLEASE   ADD TEST COVERAGE.  ", "merge_request_id": "mr-2"},
        ]
    )

    assert len(result["patterns"]) == 1
    assert result["patterns"][0]["occurrences"] == 2
    assert result["patterns"][0]["merge_request_ids"] == ["mr-1", "mr-2"]


def test_reviewer_level_analysis_filters_by_reviewer() -> None:
    """Reviewer analysis includes only comments from the selected reviewer."""

    result = RulesGenerator().analyze_reviewer_comments(
        [
            {"body": "Add a test.", "author": "reviewer-a"},
            {"body": "Handle this error.", "author": "reviewer-b"},
        ],
        reviewer="reviewer-a",
    )

    assert result["scope"] == "reviewer"
    assert result["subject"] == "reviewer-a"
    assert result["comment_count"] == 1
    assert result["comments"][0]["author"] == "reviewer-a"


def test_project_level_analysis_keeps_all_reviewers() -> None:
    """Project analysis aggregates comments across reviewers."""

    result = RulesGenerator().analyze_project_comments(
        [
            {"body": "Add a test.", "author": "reviewer-a"},
            {"body": "Handle this error.", "author": "reviewer-b"},
        ],
        project="project-a",
    )

    assert result["scope"] == "project"
    assert result["subject"] == "project-a"
    assert result["comment_count"] == 2


def test_unknown_feedback_is_retained_as_unclassified() -> None:
    """Feedback without a known keyword is not discarded."""

    result = RulesGenerator().analyze_project_comments(
        [{"body": "Consider this unusual domain-specific concern."}]
    )

    assert result["comments"][0]["category"] == "unclassified"
    assert result["patterns"][0]["category"] == "unclassified"


def test_comment_normalization_collapses_whitespace_and_case() -> None:
    """Normalized feedback is stable for aggregation."""

    result = RulesGenerator().analyze_project_comments(
        [{"body": "  Check   Error HANDLING  "}]
    )

    comment = result["comments"][0]
    assert comment["body"] == "Check   Error HANDLING"
    assert comment["normalized_body"] == "check error handling"
    assert comment["category"] == "error_handling"


def test_missing_and_empty_comment_bodies_are_ignored() -> None:
    """Comments without meaningful bodies do not enter analysis."""

    result = RulesGenerator().analyze_project_comments(
        [{"author": "reviewer"}, {"body": "   "}, {"body": None}]
    )

    assert result["comment_count"] == 0
    assert result["patterns"] == []


def test_mr_identifier_aliases_are_associated_with_patterns() -> None:
    """Supported MR identifier fields are retained in pattern history."""

    result = RulesGenerator().analyze_project_comments(
        [
            {"body": "Add test coverage.", "mr_id": 101},
            {"body": "Please add test coverage.", "merge_request_iid": "102"},
        ]
    )

    assert result["patterns"][0]["merge_request_ids"] == ["101", "102"]


def test_broader_feedback_categories_are_detected() -> None:
    """The generator recognizes the specification's extended categories."""

    comments = [
        {"body": "Simplify this complexity."},
        {"body": "Avoid duplicate logic."},
        {"body": "Use type annotations."},
        {"body": "Improve logging here."},
        {"body": "Use the database transaction correctly."},
        {"body": "The API endpoint response is inconsistent."},
        {"body": "Rename this identifier."},
        {"body": "Follow this best practice."},
        {"body": "Fix the formatting style."},
    ]

    result = RulesGenerator().analyze_project_comments(comments)

    assert {comment["category"] for comment in result["comments"]} >= {
        "complexity",
        "duplication",
        "type_safety",
        "logging",
        "database",
        "api_design",
        "naming",
        "best_practices",
        "style",
    }


def test_similar_feedback_is_grouped_and_representatives_are_retained() -> None:
    """Politeness and punctuation differences do not split recurring feedback."""

    result = RulesGenerator().analyze_project_comments(
        [
            {"body": "Please add tests!"},
            {"body": "Add testing."},
            {"body": "Can you add tests?"},
        ]
    )

    pattern = result["patterns"][0]
    assert pattern["occurrences"] == 3
    assert len(pattern["representative_comments"]) == 3


def test_patterns_have_deterministic_ordering() -> None:
    """Equal-count patterns are sorted consistently by category and feedback."""

    comments = [{"body": "Use type annotations."}, {"body": "Add test coverage."}]

    first = RulesGenerator().analyze_project_comments(comments)["patterns"]
    second = RulesGenerator().analyze_project_comments(reversed(comments))["patterns"]

    assert first == second


def test_reviewer_rule_generation_is_schema_compatible() -> None:
    """Reviewer analysis can produce validated reusable rules."""

    rules = RulesGenerator().generate_reviewer_rules(
        [{"body": "Add test coverage.", "author": "reviewer-a"}],
        reviewer="reviewer-a",
    )

    ReviewRulesService.validate(rules)
    assert rules["rules"]["testing"]


def test_project_rules_are_consolidated_into_existing_categories() -> None:
    """Project patterns are consolidated into the four YAML rule categories."""

    rules = RulesGenerator().generate_project_rules(
        [
            {"body": "Protect secrets."},
            {"body": "Improve performance."},
            {"body": "Add tests."},
            {"body": "Refactor this code."},
        ],
        project="project-a",
    )

    ReviewRulesService.validate(rules)
    assert set(rules["rules"]) == set(RULE_CATEGORIES)
    assert rules["rules"]["security"]
    assert rules["rules"]["performance"]
    assert rules["rules"]["testing"]
    assert rules["rules"]["code_quality"]


def test_malformed_comment_entries_are_ignored_safely() -> None:
    """Non-mapping historical entries do not break project analysis."""

    result = RulesGenerator().analyze_project_comments(
        [None, "not a comment", 42, {"body": "Add tests."}]
    )

    assert result["comment_count"] == 1
