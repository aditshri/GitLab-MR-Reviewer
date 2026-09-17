"""Unit tests for review rules management."""

from pathlib import Path

import pytest
import yaml

from app.review_rules_service import DEFAULT_REVIEW_RULES
from app.rules_management_service import (
    InvalidRuleSetError,
    RuleSetNotFoundError,
    RulesManagementService,
    UnsafeRuleSetNameError,
)


VALID_YAML = yaml.safe_dump(DEFAULT_REVIEW_RULES, sort_keys=False)


def make_service(tmp_path: Path) -> RulesManagementService:
    """Create an isolated rules-management service."""

    default_path = tmp_path / "default.yaml"
    default_path.write_text(VALID_YAML, encoding="utf-8")
    return RulesManagementService(tmp_path / "rules", default_path)


def test_listing_rules_includes_default_and_custom_sets(tmp_path: Path) -> None:
    """Listing returns the bundled default and saved custom sets."""

    service = make_service(tmp_path)
    service.save_content("team.yaml", VALID_YAML)

    listed = service.list_rule_sets()

    assert {item["filename"] for item in listed} == {"default.yaml", "team.yaml"}
    assert next(item for item in listed if item["filename"] == "team.yaml")["is_current"]


def test_getting_current_rules_defaults_to_default_file(tmp_path: Path) -> None:
    """The default file is current before a custom set is selected."""

    service = make_service(tmp_path)

    assert service.get_current_rules() == DEFAULT_REVIEW_RULES


def test_reading_rule_content_returns_yaml(tmp_path: Path) -> None:
    """Raw rule content can be read by safe filename."""

    service = make_service(tmp_path)
    service.save_content("team.yaml", VALID_YAML)

    assert service.read_rule_content("team.yaml") == VALID_YAML


def test_saving_valid_rules_selects_custom_set(tmp_path: Path) -> None:
    """Valid YAML is persisted and selected as current."""

    service = make_service(tmp_path)
    info = service.save_content("team.yaml", VALID_YAML)

    assert info["filename"] == "team.yaml"
    assert service.download_current()[1] == "team.yaml"
    assert service.get_current_rules() == DEFAULT_REVIEW_RULES


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    """Malformed YAML cannot be saved."""

    with pytest.raises(InvalidRuleSetError):
        make_service(tmp_path).save_content("bad.yaml", "rules: [")


def test_invalid_rule_structure_is_rejected(tmp_path: Path) -> None:
    """YAML with an unsupported rules schema cannot be saved."""

    with pytest.raises(InvalidRuleSetError):
        make_service(tmp_path).save_content("bad.yaml", "rules: {}")


def test_yaml_upload_is_saved(tmp_path: Path) -> None:
    """YAML uploads use strict validation and custom storage."""

    service = make_service(tmp_path)

    info = service.upload_content("uploaded.yaml", VALID_YAML)

    assert info["filename"] == "uploaded.yaml"
    assert service.get_current_rules() == DEFAULT_REVIEW_RULES


def test_markdown_upload_is_converted_to_yaml(tmp_path: Path) -> None:
    """Markdown uploads become validated YAML custom rule sets."""

    service = make_service(tmp_path)

    info = service.upload_content(
        "uploaded.md",
        "## Security\n- [critical] Validate permissions.",
    )

    assert info["filename"] == "uploaded.yaml"
    rules = service.get_current_rules()
    assert rules["rules"]["security"][0]["description"] == "Validate permissions."


def test_reset_selects_default_without_deleting_custom_set(tmp_path: Path) -> None:
    """Reset returns selection to default and keeps custom files."""

    service = make_service(tmp_path)
    service.save_content("team.yaml", VALID_YAML)

    service.reset_to_default()

    assert service.download_current()[1] == "default.yaml"
    assert (tmp_path / "rules" / "team.yaml").exists()


def test_delete_custom_rules_removes_file(tmp_path: Path) -> None:
    """Custom rule sets can be deleted and default becomes current."""

    service = make_service(tmp_path)
    service.save_content("team.yaml", VALID_YAML)
    service.delete_custom_rules("team.yaml")

    assert not (tmp_path / "rules" / "team.yaml").exists()
    assert service.download_current()[1] == "default.yaml"


def test_download_and_markdown_export(tmp_path: Path) -> None:
    """Current rules are available as YAML and Markdown exports."""

    service = make_service(tmp_path)

    yaml_content, filename = service.download_current()
    markdown = service.export_current_markdown()

    assert filename == "default.yaml"
    assert yaml.safe_load(yaml_content) == DEFAULT_REVIEW_RULES
    assert "# Review Rules" in markdown
    assert "## Security" in markdown


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    """Traversal and nested filenames cannot escape the rules directory."""

    service = make_service(tmp_path)

    with pytest.raises(UnsafeRuleSetNameError):
        service.read_rule_content("../outside.yaml")
    with pytest.raises(UnsafeRuleSetNameError):
        service.save_content("nested/team.yaml", VALID_YAML)


def test_missing_rule_file_is_reported(tmp_path: Path) -> None:
    """Reading a missing custom set raises a safe not-found error."""

    with pytest.raises(RuleSetNotFoundError):
        make_service(tmp_path).read_rule_content("missing.yaml")
