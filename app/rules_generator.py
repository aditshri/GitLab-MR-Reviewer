"""Analysis foundation for generating review rules from review comments."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, Literal, TypedDict

from app.review_rules_service import (
    DEFAULT_REVIEW_RULES,
    ReviewRules,
    ReviewRulesService,
)

AnalysisScope = Literal["reviewer", "project"]

CATEGORIES = (
    "testing",
    "security",
    "performance",
    "documentation",
    "complexity",
    "code_quality",
    "error_handling",
    "naming",
    "best_practices",
    "style",
    "duplication",
    "database",
    "api_design",
    "logging",
    "resource_management",
    "type_safety",
    "unclassified",
)

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "testing": ("test", "tests", "testing", "coverage", "assert"),
    "security": (
        "security",
        "secret",
        "secrets",
        "token",
        "credential",
        "permission",
        "auth",
        "authorization",
    ),
    "performance": ("performance", "slow", "latency", "efficient", "cache"),
    "documentation": ("document", "documentation", "docs", "readme", "comment"),
    "complexity": ("complexity", "complex", "cyclomatic", "simplify"),
    "code_quality": ("refactor", "maintainable", "readable", "clean code"),
    "error_handling": ("error", "exception", "failure", "handle", "raise"),
    "naming": ("naming", "name", "identifier"),
    "best_practices": ("best practice", "idiomatic", "standard practice", "convention"),
    "style": ("style", "format", "lint", "whitespace"),
    "duplication": ("duplicate", "duplication", "repeated", " DRY "),
    "database": ("database", "sql", "query", "transaction", "migration", "index"),
    "api_design": ("api", "endpoint", "request", "response", "rest", "http"),
    "logging": ("log", "logging", "logger", "trace", "observability"),
    "resource_management": ("resource", "close", "cleanup", "connection", "leak", "file handle"),
    "type_safety": ("type", "typing", "annotation", "typed", "mypy", "null"),
}


class ReviewComment(TypedDict, total=False):
    """Structured historical review comment input."""

    body: str
    author: str
    reviewer: str
    merge_request_id: str
    mr_id: str
    merge_request_iid: str
    project: str


class NormalizedComment(TypedDict):
    """Normalized comment retained for analysis and later rule generation."""

    body: str
    normalized_body: str
    author: str | None
    merge_request_id: str | None
    project: str | None
    category: str


class ReviewPattern(TypedDict):
    """An aggregated recurring feedback pattern."""

    category: str
    normalized_feedback: str
    occurrences: int
    examples: list[str]
    merge_request_ids: list[str]
    representative_comments: list[str]


class RulesAnalysis(TypedDict):
    """Structured analysis that can later be converted into YAML rules."""

    scope: AnalysisScope
    subject: str | None
    comment_count: int
    comments: list[NormalizedComment]
    patterns: list[ReviewPattern]


class RulesGenerator:
    """Normalize review comments and extract recurring feedback patterns."""

    def analyze_reviewer_comments(
        self,
        comments: Iterable[Mapping[str, Any]],
        reviewer: str | None = None,
    ) -> RulesAnalysis:
        """Analyze comments from one reviewer, optionally filtering by identity."""

        selected = list(comments)
        if reviewer is not None:
            selected = [
                comment
                for comment in selected
                if self._comment_reviewer(comment) == reviewer
            ]
        return self._analyze(selected, scope="reviewer", subject=reviewer)

    def analyze_project_comments(
        self,
        comments: Iterable[Mapping[str, Any]],
        project: str | None = None,
    ) -> RulesAnalysis:
        """Analyze all supplied project comments as a separate aggregation scope."""

        return self._analyze(list(comments), scope="project", subject=project)

    def generate_reviewer_rules(
        self,
        comments: Iterable[Mapping[str, Any]],
        reviewer: str,
    ) -> ReviewRules:
        """Generate validated YAML-compatible rules from one reviewer's comments."""

        return self.consolidate_project_rules(
            self.analyze_reviewer_comments(comments, reviewer)
        )

    def generate_project_rules(
        self,
        comments: Iterable[Mapping[str, Any]],
        project: str | None = None,
    ) -> ReviewRules:
        """Generate validated consolidated rules from project comment history."""

        return self.consolidate_project_rules(
            self.analyze_project_comments(comments, project)
        )

    def consolidate_project_rules(self, analysis: RulesAnalysis) -> ReviewRules:
        """Convert analyzed patterns into the existing four-category rule schema."""

        rules = deepcopy(DEFAULT_REVIEW_RULES)
        rules["rules"] = {
            "code_quality": [],
            "security": [],
            "performance": [],
            "testing": [],
        }
        counters: dict[str, int] = defaultdict(int)
        for pattern in analysis["patterns"]:
            target_category = self._rule_category(pattern["category"])
            counters[target_category] += 1
            description = f"Review for recurring feedback: {pattern['normalized_feedback']}."
            rules["rules"][target_category].append(
                {
                    "id": self._rule_id(target_category, counters[target_category], pattern["normalized_feedback"]),
                    "severity": "medium",
                    "description": description,
                }
            )
        ReviewRulesService.validate(rules)
        return rules

    def _analyze(
        self,
        comments: Iterable[Mapping[str, Any]],
        scope: AnalysisScope,
        subject: str | None,
    ) -> RulesAnalysis:
        normalized_comments = [
            normalized
            for comment in comments
            if isinstance(comment, Mapping)
            and (normalized := self._normalize_comment(comment)) is not None
        ]
        grouped: dict[tuple[str, str], list[NormalizedComment]] = defaultdict(list)
        for comment in normalized_comments:
            grouped[
                (comment["category"], self._pattern_key(comment["normalized_body"]))
            ].append(comment)

        patterns = [
            {
                "category": category,
                "normalized_feedback": normalized_feedback,
                "occurrences": len(grouped_comments),
                "examples": list(dict.fromkeys(comment["body"] for comment in grouped_comments)),
                "representative_comments": list(
                    dict.fromkeys(comment["body"] for comment in grouped_comments)
                )[:3],
                "merge_request_ids": list(
                    dict.fromkeys(
                        comment["merge_request_id"]
                        for comment in grouped_comments
                        if comment["merge_request_id"] is not None
                    )
                ),
            }
            for (category, normalized_feedback), grouped_comments in grouped.items()
        ]
        patterns.sort(
            key=lambda pattern: (
                -pattern["occurrences"],
                pattern["category"],
                pattern["normalized_feedback"],
            )
        )

        return {
            "scope": scope,
            "subject": subject,
            "comment_count": len(normalized_comments),
            "comments": normalized_comments,
            "patterns": patterns,
        }

    @staticmethod
    def _normalize_comment(
        comment: Mapping[str, Any],
    ) -> NormalizedComment | None:
        """Normalize one structured comment, ignoring missing or empty bodies."""

        body = comment.get("body")
        if not isinstance(body, str):
            return None
        normalized_body = re.sub(r"\s+", " ", body).strip()
        if not normalized_body:
            return None

        author = RulesGenerator._comment_reviewer(comment)
        return {
            "body": body.strip(),
            "normalized_body": normalized_body.lower(),
            "author": author,
            "merge_request_id": RulesGenerator._optional_identifier(
                comment.get("merge_request_id")
                or comment.get("mr_id")
                or comment.get("merge_request_iid")
            ),
            "project": RulesGenerator._optional_string(comment.get("project")),
            "category": RulesGenerator._categorize(normalized_body),
        }

    @staticmethod
    def _comment_reviewer(comment: Mapping[str, Any]) -> str | None:
        """Read the reviewer identity from supported structured input fields."""

        value = comment.get("reviewer") or comment.get("author")
        if isinstance(value, Mapping):
            value = value.get("username") or value.get("name")
        return RulesGenerator._optional_string(value)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        """Return a trimmed string value or None."""

        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        """Return a normalized string identifier from common scalar values."""

        if isinstance(value, (str, int)):
            return str(value).strip() or None
        return None

    @staticmethod
    def _pattern_key(normalized_body: str) -> str:
        """Normalize equivalent feedback for recurring-pattern aggregation."""

        pattern = re.sub(r"[^a-z0-9\s]", " ", normalized_body.lower())
        pattern = re.sub(r"\s+", " ", pattern).strip()
        pattern = re.sub(r"^(please|could you|can you|consider)\s+", "", pattern)
        return re.sub(r"\b(tests|testing)\b", "test", pattern)

    @staticmethod
    def _categorize(body: str) -> str:
        """Assign the first matching category, retaining unknown feedback."""

        searchable = body.lower()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(
                re.search(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", searchable)
                for keyword in keywords
            ):
                return category
        return "unclassified"

    @staticmethod
    def _rule_category(category: str) -> str:
        """Map analysis categories into the existing review-rules schema."""

        if category == "security":
            return "security"
        if category == "performance" or category == "resource_management":
            return "performance"
        if category == "testing":
            return "testing"
        return "code_quality"

    @staticmethod
    def _rule_id(category: str, number: int, feedback: str) -> str:
        """Create a deterministic rule ID from category and feedback."""

        slug = re.sub(r"[^a-z0-9]+", "-", feedback.lower()).strip("-")[:40]
        return f"generated-{category}-{number}-{slug}"
