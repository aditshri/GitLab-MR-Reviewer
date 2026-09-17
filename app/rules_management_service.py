"""Management operations for default and custom review rule sets."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from app.markdown_rules_converter import MarkdownRulesConverter
from app.review_rules_service import (
    DEFAULT_REVIEW_RULES,
    RULE_CATEGORIES,
    InvalidReviewRulesError,
    ReviewRules,
    ReviewRulesService,
)
from config import Config


class RulesManagementError(Exception):
    """Base error for rules-management failures."""


class RuleSetNotFoundError(RulesManagementError):
    """Raised when a requested rule set does not exist."""


class InvalidRuleSetError(RulesManagementError):
    """Raised when rules content is malformed or unsupported."""


class UnsafeRuleSetNameError(RulesManagementError):
    """Raised when a supplied filename could escape the rules directory."""


class RulesManagementService:
    """List, read, validate, save, and export review rule sets."""

    DEFAULT_FILENAME = "default.yaml"
    CURRENT_MARKER = ".current"
    _ALLOWED_SUFFIXES = {".yaml", ".yml"}

    def __init__(
        self,
        rules_directory: str | Path | None = None,
        default_rules_path: str | Path | None = None,
    ) -> None:
        """Configure custom rules storage and the bundled default rules path."""

        project_root = Path(__file__).resolve().parent.parent
        configured_directory = rules_directory or "rules"
        directory = Path(configured_directory)
        self._rules_directory = (
            directory if directory.is_absolute() else project_root / directory
        ).resolve()
        self._default_rules_path = Path(default_rules_path or Path(__file__).with_name("review_rules.yaml")).resolve()

    def list_rule_sets(self) -> list[dict[str, Any]]:
        """Return the bundled default and available custom YAML rule sets."""

        current = self._current_filename()
        rule_sets = [
            {
                "filename": self.DEFAULT_FILENAME,
                "is_default": True,
                "is_current": current == self.DEFAULT_FILENAME,
            }
        ]
        if self._rules_directory.exists():
            for path in sorted(self._rules_directory.iterdir()):
                if path.is_file() and path.suffix.lower() in self._ALLOWED_SUFFIXES:
                    rule_sets.append(
                        {
                            "filename": path.name,
                            "is_default": False,
                            "is_current": current == path.name,
                        }
                    )
        return rule_sets

    def get_current_rules(self) -> ReviewRules:
        """Return the currently selected validated rule set."""

        filename = self._current_filename()
        return self._read_rules(self._path_for_filename(filename))

    def read_rule_content(self, filename: str) -> str:
        """Return raw YAML content for the default or a custom rule set."""

        path = self._path_for_filename(filename)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuleSetNotFoundError("Rule set was not found.") from exc
        except OSError as exc:
            raise RulesManagementError("Unable to read the rule set.") from exc

    def save_rules(self, filename: str, rules: ReviewRules) -> dict[str, Any]:
        """Validate and save a structured custom YAML rule set."""

        path = self._custom_path(filename)
        self._validate(rules)
        self._write_rules(path, rules)
        self._set_current(path.name)
        return self._rule_set_info(path.name)

    def save_content(self, filename: str, content: str) -> dict[str, Any]:
        """Validate and save YAML content as a custom rule set."""

        path = self._custom_path(filename)
        rules = self._parse_yaml(content)
        self._write_rules(path, rules)
        self._set_current(path.name)
        return self._rule_set_info(path.name)

    def upload_content(self, filename: str, content: str) -> dict[str, Any]:
        """Convert Markdown uploads or validate YAML uploads before saving."""

        supplied_path = self._validate_filename(filename)
        if supplied_path.suffix.lower() in {".md", ".markdown"}:
            target_name = f"{supplied_path.stem}.yaml"
            rules = MarkdownRulesConverter().convert(content)
            return self.save_rules(target_name, rules)
        return self.save_content(supplied_path.name, content)

    def reset_to_default(self) -> dict[str, Any]:
        """Select the bundled default rule set without deleting custom sets."""

        self._read_rules(self._default_rules_path)
        self._set_current(self.DEFAULT_FILENAME)
        return self._rule_set_info(self.DEFAULT_FILENAME)

    def delete_custom_rules(self, filename: str) -> None:
        """Delete a custom rule set and select default if it was current."""

        path = self._custom_path(filename)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise RuleSetNotFoundError("Rule set was not found.") from exc
        except OSError as exc:
            raise RulesManagementError("Unable to delete the rule set.") from exc
        if self._current_filename() == path.name:
            self._set_current(self.DEFAULT_FILENAME)

    def download_current(self) -> tuple[str, str]:
        """Return current YAML content and a safe download filename."""

        filename = self._current_filename()
        return self.read_rule_content(filename), filename

    def export_current_markdown(self) -> str:
        """Export the current structured rules as readable Markdown."""

        rules = self.get_current_rules()
        sections = [
            "# Review Rules",
            "",
            f"Tone: {rules['general']['tone']}",
            f"Detail level: {rules['general']['detail_level']}",
            f"Suggest improvements: {str(rules['general']['suggest_improvements']).lower()}",
            "",
        ]
        for category in RULE_CATEGORIES:
            sections.extend([f"## {category.replace('_', ' ').title()}", ""])
            for rule in rules["rules"][category]:
                sections.append(f"- [{rule['severity']}] {rule['description']}")
            sections.append("")
        return "\n".join(sections).rstrip() + "\n"

    def _path_for_filename(self, filename: str) -> Path:
        """Resolve the default or safe custom filename."""

        if filename in {"default", self.DEFAULT_FILENAME}:
            return self._default_rules_path
        return self._custom_path(filename)

    def _custom_path(self, filename: str) -> Path:
        """Resolve a required safe custom YAML filename."""

        path = self._validate_filename(filename)
        if path.name in {"default", self.DEFAULT_FILENAME}:
            raise InvalidRuleSetError("The default rule set cannot be modified.")
        if path.suffix.lower() not in self._ALLOWED_SUFFIXES:
            raise InvalidRuleSetError("Rule set filename must use YAML format.")
        return self._rules_directory / path.name

    @staticmethod
    def _validate_filename(filename: str) -> Path:
        """Reject absolute, nested, traversal, and unsupported filename values."""

        if not isinstance(filename, str) or not filename.strip():
            raise UnsafeRuleSetNameError("A rule set filename is required.")
        candidate = Path(filename)
        if (
            candidate.is_absolute()
            or candidate.name != filename
            or ".." in candidate.parts
            or "/" in filename
            or "\\" in filename
        ):
            raise UnsafeRuleSetNameError("Invalid rule set filename.")
        return candidate

    def _read_rules(self, path: Path) -> ReviewRules:
        """Read and strictly validate one YAML rule set."""

        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuleSetNotFoundError("Rule set was not found.") from exc
        except (OSError, yaml.YAMLError) as exc:
            raise InvalidRuleSetError("Rule set content is invalid.") from exc
        try:
            self._validate(document)
        except InvalidReviewRulesError as exc:
            raise InvalidRuleSetError("Rule set structure is invalid.") from exc
        return document

    @staticmethod
    def _validate(rules: Any) -> None:
        """Apply the existing strict review-rules validator."""

        try:
            ReviewRulesService.validate(rules)
        except InvalidReviewRulesError:
            raise

    def _parse_yaml(self, content: str) -> ReviewRules:
        """Parse and strictly validate YAML content."""

        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise InvalidRuleSetError("Rule set content is invalid YAML.") from exc
        try:
            self._validate(document)
        except InvalidReviewRulesError as exc:
            raise InvalidRuleSetError("Rule set structure is invalid.") from exc
        return document

    def _write_rules(self, path: Path, rules: ReviewRules) -> None:
        """Write validated rules atomically within the custom directory."""

        self._rules_directory.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".tmp")
        temporary_path.write_text(
            yaml.safe_dump(rules, sort_keys=False),
            encoding="utf-8",
        )
        temporary_path.replace(path)

    def _current_filename(self) -> str:
        """Read the selected filename, falling back to the default."""

        marker = self._rules_directory / self.CURRENT_MARKER
        try:
            selected = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return self.DEFAULT_FILENAME
        if selected == self.DEFAULT_FILENAME:
            return selected
        try:
            self._custom_path(selected)
        except RulesManagementError:
            return self.DEFAULT_FILENAME
        if not (self._rules_directory / selected).is_file():
            return self.DEFAULT_FILENAME
        return selected

    def _set_current(self, filename: str) -> None:
        """Persist the selected rule-set filename in the configured directory."""

        self._rules_directory.mkdir(parents=True, exist_ok=True)
        (self._rules_directory / self.CURRENT_MARKER).write_text(
            filename,
            encoding="utf-8",
        )

    def _rule_set_info(self, filename: str) -> dict[str, Any]:
        """Build public rule-set metadata without filesystem details."""

        return {
            "filename": filename,
            "is_default": filename == self.DEFAULT_FILENAME,
            "is_current": self._current_filename() == filename,
        }
