"""Unit tests for GitLab merge request URL parsing."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gitlab.exceptions import GitlabError

from app.gitlab_service import (
    GitLabAPIError,
    GitLabService,
    InvalidMergeRequestURLError,
)


@pytest.fixture
def gitlab_service() -> GitLabService:
    """Create a service with non-sensitive placeholder configuration."""

    config = SimpleNamespace(
        GITLAB_URL="https://gitlab.example.invalid",
        GITLAB_TOKEN="placeholder-token",
    )
    return GitLabService(config)


def test_valid_merge_request_url_returns_project_and_iid(
    gitlab_service: GitLabService,
) -> None:
    """A standard MR URL returns its project path and numeric IID."""

    result = gitlab_service.parse_merge_request_url(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert result == ("team/project", 17)


def test_nested_project_groups_are_parsed(
    gitlab_service: GitLabService,
) -> None:
    """Nested GitLab groups remain part of the project path."""

    result = gitlab_service.parse_merge_request_url(
        "https://gitlab.example.invalid/team/platform/service/-/merge_requests/23"
    )

    assert result == ("team/platform/service", 23)


def test_trailing_slash_is_accepted(
    gitlab_service: GitLabService,
) -> None:
    """An MR URL ending with a slash parses like the same URL without it."""

    result = gitlab_service.parse_merge_request_url(
        "https://gitlab.example.invalid/team/project/-/merge_requests/31/"
    )

    assert result == ("team/project", 31)


def test_different_gitlab_host_is_rejected(
    gitlab_service: GitLabService,
) -> None:
    """URLs from a host other than the configured GitLab instance are rejected."""

    with pytest.raises(InvalidMergeRequestURLError):
        gitlab_service.parse_merge_request_url(
            "https://other-gitlab.example.invalid/team/project/-/merge_requests/17"
        )


def test_url_without_merge_request_segment_is_rejected(
    gitlab_service: GitLabService,
) -> None:
    """A URL without the GitLab merge request path segment is rejected."""

    with pytest.raises(InvalidMergeRequestURLError):
        gitlab_service.parse_merge_request_url(
            "https://gitlab.example.invalid/team/project/merge_requests/17"
        )


def test_non_numeric_merge_request_iid_is_rejected(
    gitlab_service: GitLabService,
) -> None:
    """A merge request IID must be a positive integer."""

    with pytest.raises(InvalidMergeRequestURLError):
        gitlab_service.parse_merge_request_url(
            "https://gitlab.example.invalid/team/project/-/merge_requests/not-a-number"
        )


def test_invalid_url_is_rejected(
    gitlab_service: GitLabService,
) -> None:
    """Malformed values without a valid HTTP URL are rejected."""

    with pytest.raises(InvalidMergeRequestURLError):
        gitlab_service.parse_merge_request_url("not-a-url")


def _change(**overrides: object) -> dict[str, object]:
    """Build a minimal GitLab changed-file response entry."""

    change = {
        "old_path": "src/example.py",
        "new_path": "src/example.py",
        "new_file": False,
        "deleted_file": False,
        "renamed_file": False,
        "diff": "@@ -1 +1 @@\n-old\n+new",
    }
    change.update(overrides)
    return change


def _mock_changes_response(
    gitlab_service: GitLabService,
    changes: list[dict[str, object]],
) -> tuple[Mock, Mock, Mock]:
    """Attach mocked project, merge request, and changes response objects."""

    client = Mock()
    project = Mock()
    merge_request = Mock()
    merge_request.changes.return_value = {"changes": changes}
    project.mergerequests.get.return_value = merge_request
    client.projects.get.return_value = project
    gitlab_service._client = client
    return client, project, merge_request


def test_modified_file_is_returned_correctly(
    gitlab_service: GitLabService,
) -> None:
    """A normal modified file preserves its path and diff."""

    change = _change(diff="@@ -1 +1 @@\n-old line\n+new line")
    _mock_changes_response(gitlab_service, [change])

    result = gitlab_service.get_merge_request_changes(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert result["files"] == [
        {
            "file_path": "src/example.py",
            "old_path": "src/example.py",
            "is_new": False,
            "is_deleted": False,
            "is_renamed": False,
            "diff": "@@ -1 +1 @@\n-old line\n+new line",
        }
    ]


def test_new_file_is_identified_correctly(
    gitlab_service: GitLabService,
) -> None:
    """A GitLab new-file flag is mapped to is_new."""

    _mock_changes_response(
        gitlab_service,
        [_change(new_path="src/new.py", new_file=True, diff="+++ src/new.py")],
    )

    result = gitlab_service.get_merge_request_changes(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert result["files"][0]["file_path"] == "src/new.py"
    assert result["files"][0]["is_new"] is True


def test_deleted_file_is_identified_correctly(
    gitlab_service: GitLabService,
) -> None:
    """A GitLab deleted-file flag is mapped to is_deleted."""

    _mock_changes_response(
        gitlab_service,
        [_change(deleted_file=True, diff="--- src/example.py")],
    )

    result = gitlab_service.get_merge_request_changes(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert result["files"][0]["is_deleted"] is True


def test_renamed_file_is_identified_correctly(
    gitlab_service: GitLabService,
) -> None:
    """A renamed file preserves its old path and rename flag."""

    _mock_changes_response(
        gitlab_service,
        [
            _change(
                old_path="src/old.py",
                new_path="src/new.py",
                renamed_file=True,
                diff="rename from src/old.py\nrename to src/new.py",
            )
        ],
    )

    result = gitlab_service.get_merge_request_changes(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert result["files"][0]["file_path"] == "src/new.py"
    assert result["files"][0]["old_path"] == "src/old.py"
    assert result["files"][0]["is_renamed"] is True


def test_multiple_changed_files_are_returned(
    gitlab_service: GitLabService,
) -> None:
    """All entries in GitLab's changes list are returned."""

    _mock_changes_response(
        gitlab_service,
        [
            _change(new_path="src/one.py"),
            _change(new_path="src/two.py"),
        ],
    )

    result = gitlab_service.get_merge_request_changes(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert [file["file_path"] for file in result["files"]] == [
        "src/one.py",
        "src/two.py",
    ]


def test_combined_diff_contains_all_file_diffs(
    gitlab_service: GitLabService,
) -> None:
    """The combined diff includes each changed file's diff text."""

    _mock_changes_response(
        gitlab_service,
        [
            _change(diff="diff for one"),
            _change(new_path="src/two.py", diff="diff for two"),
        ],
    )

    result = gitlab_service.get_merge_request_changes(
        "https://gitlab.example.invalid/team/project/-/merge_requests/17"
    )

    assert result["combined_diff"] == "diff for one\ndiff for two"


def test_gitlab_api_error_is_converted_to_service_error(
    gitlab_service: GitLabService,
) -> None:
    """A GitLab client error becomes the service's GitLabAPIError."""

    client = Mock()
    client.projects.get.side_effect = GitlabError("request failed")
    gitlab_service._client = client

    with pytest.raises(GitLabAPIError):
        gitlab_service.get_merge_request_changes(
            "https://gitlab.example.invalid/team/project/-/merge_requests/17"
        )
