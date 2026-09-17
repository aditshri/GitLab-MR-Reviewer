"""Convert Markdown review rules into the validated YAML-compatible structure."""

from __future__ import annotations

import re
from copy import deepcopy
import yaml

from app.review_rules_service import (
    DEFAULT_REVIEW_RULES,
    RULE_CATEGORIES,
    ReviewRules,
    ReviewRulesService,
)


DEFAULT_RULES_DOCUMENT = {
    "general": {
        "team_name": "Default",
        "enabled_categories": [
            "code_quality",
            "security",
            "performance",
            "testing",
        ],
    },
    "security": [],
    "testing": [],
    "output": {
        "include_positive_feedback": True,
        "severity_levels": ["critical", "high", "medium", "low"],
    },
    "ai_behavior": {
        "tone": "constructive",
        "detail_level": "comprehensive",
        "suggest_improvements": True,
    },
}


def convert_markdown_to_yaml(markdown_text: str) -> dict:
    """Convert category headings and bullet rules into the current rule schema."""

    document = deepcopy(DEFAULT_RULES_DOCUMENT)
    if not isinstance(markdown_text, str) or not markdown_text.strip():
        return document

    categories: dict[str, list[str]] = {}
    current_category: str | None = None
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue

        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if heading:
            current_category = _normalize_category_name(heading.group(1))
            categories.setdefault(current_category, [])
            continue

        if current_category is None:
            continue
        rule = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(.+)$", line)
        if rule:
            categories[current_category].append(rule.group(1).strip())

    document["general"]["enabled_categories"] = list(categories)
    document.update(categories)
    return document


def _normalize_category_name(value: str) -> str:
    """Convert a Markdown heading into a stable YAML category key."""

    normalized = re.sub(r"\s+rules?$", "", value.strip().lower())
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return normalized or "uncategorized"


_CATEGORY_ALIASES = {
    "code quality": "code_quality",
    "code_quality": "code_quality",
    "security": "security",
    "performance": "performance",
    "testing": "testing",
    "tests": "testing",
}

_SEVERITY_PATTERN = re.compile(
    r"^(?:\[(?P<bracketed>critical|high|medium|low)\]|"
    r"\((?P<parenthesized>critical|high|medium|low)\)|"
    r"(?P<prefix>critical|high|medium|low)\s*[:\-])\s*",
    re.IGNORECASE,
)


class MarkdownRulesConverter:
    """Parse Markdown rule content into validated review rules."""

    def convert(self, markdown: str) -> ReviewRules:
        """Convert Markdown headings and rule lists into review rules."""

        rules = deepcopy(DEFAULT_REVIEW_RULES)
        rules["rules"] = {category: [] for category in RULE_CATEGORIES}
        if not markdown.strip():
            return rules

        current_category = "code_quality"
        counters = {category: 0 for category in RULE_CATEGORIES}

        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("```"):
                continue

            heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
            if heading:
                current_category = self._category_for_heading(heading.group(1))
                continue

            rule_text = self._rule_text(line)
            if not rule_text:
                continue

            severity, description = self._extract_severity(rule_text)
            counters[current_category] += 1
            rules["rules"][current_category].append(
                {
                    "id": self._rule_id(current_category, counters[current_category], description),
                    "severity": severity,
                    "description": description,
                }
            )

        ReviewRulesService.validate(rules)
        return rules

    def to_yaml(self, markdown: str) -> str:
        """Convert Markdown rules and serialize the result as YAML."""

        return yaml.safe_dump(self.convert(markdown), sort_keys=False)

    @staticmethod
    def _category_for_heading(heading: str) -> str:
        """Map a Markdown heading to a supported rule category."""

        normalized = re.sub(r"\s+rules?$", "", heading.strip().lower())
        normalized = re.sub(r"[^a-z0-9_ ]+", "", normalized).strip()
        return _CATEGORY_ALIASES.get(normalized, "code_quality")

    @staticmethod
    def _rule_text(line: str) -> str:
        """Remove common Markdown list markers while preserving rule text."""

        if line.startswith(("- ", "* ", "+ ")):
            return line[2:].strip()
        return re.sub(r"^\d+[.)]\s+", "", line).strip()

    @staticmethod
    def _extract_severity(text: str) -> tuple[str, str]:
        """Extract an optional severity marker, defaulting to medium."""

        match = _SEVERITY_PATTERN.match(text)
        if not match:
            return "medium", text
        severity = next(value for value in match.groups() if value is not None).lower()
        return severity, text[match.end() :].strip()

    @staticmethod
    def _rule_id(category: str, number: int, description: str) -> str:
        """Create a stable readable rule ID from category and position."""

        slug = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")
        return f"{category}-{number}-{slug[:40]}"
