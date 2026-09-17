"""GitLab service operations for merge request metadata."""

from __future__ import annotations

import re
from typing import Any, TypedDict
from urllib.parse import unquote, urlsplit

import gitlab
from gitlab.exceptions import GitlabAuthenticationError as GitlabAuthError
from gitlab.exceptions import GitlabError

from config import Config


class GitLabServiceError(Exception):
    """Base error for GitLab service failures."""


class GitLabConfigurationError(GitLabServiceError):
    """Raised when required GitLab configuration is missing or invalid."""


class GitLabAuthenticationError(GitLabServiceError):
    """Raised when GitLab authentication fails."""


class InvalidMergeRequestURLError(GitLabServiceError):
    """Raised when a merge request URL cannot be parsed or is not trusted."""


class GitLabAPIError(GitLabServiceError):
    """Raised when GitLab returns an API error."""


class ChangedFile(TypedDict):
    """Normalized information about one changed merge request file."""

    file_path: str
    old_path: str | None
    is_new: bool
    is_deleted: bool
    is_renamed: bool
    diff: str


class MergeRequestChanges(TypedDict):
    """Normalized merge request changes and their combined diff text."""

    files: list[ChangedFile]
    combined_diff: str


class GitLabService:
    """Provide authenticated access to GitLab merge request metadata."""

    def __init__(self, config: type[Config] = Config) -> None:
        """Initialize a GitLab client from environment-backed configuration."""

        if not config.GITLAB_URL:
            raise GitLabConfigurationError("GITLAB_URL is not configured.")
        if not config.GITLAB_TOKEN:
            raise GitLabConfigurationError("GITLAB_TOKEN is not configured.")

        self._gitlab_url = config.GITLAB_URL.rstrip("/")
        self._client = gitlab.Gitlab(
            self._gitlab_url,
            private_token=config.GITLAB_TOKEN,
        )

    def validate_connection(self) -> bool:
        """Authenticate with GitLab and return whether the connection is valid."""

        try:
            self._client.auth()
        except GitlabAuthError as exc:
            raise GitLabAuthenticationError(
                "GitLab authentication failed."
            ) from exc
        except GitlabError as exc:
            raise GitLabAPIError(
                "GitLab connection validation failed."
            ) from exc

        return True

    def parse_merge_request_url(self, merge_request_url: str) -> tuple[str, int]:
        """Extract the project path and merge request IID from a GitLab URL."""

        configured = urlsplit(self._gitlab_url)
        parsed = urlsplit(merge_request_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InvalidMergeRequestURLError("The merge request URL is invalid.")

        try:
            host_matches = (
                parsed.hostname == configured.hostname
                and parsed.port == configured.port
            )
        except ValueError as exc:
            raise InvalidMergeRequestURLError(
                "The merge request URL is invalid."
            ) from exc

        if not host_matches:
            raise InvalidMergeRequestURLError(
                "The merge request URL does not belong to the configured GitLab instance."
            )

        base_path = configured.path.rstrip("/")
        if base_path and not (
            parsed.path == base_path or parsed.path.startswith(f"{base_path}/")
        ):
            raise InvalidMergeRequestURLError(
                "The merge request URL does not belong to the configured GitLab instance."
            )

        relative_path = parsed.path[len(base_path):] if base_path else parsed.path
        match = re.fullmatch(
            r"/(?P<project_path>.+)/-/merge_requests/(?P<iid>[1-9]\d*)/?",
            relative_path,
        )
        if not match:
            raise InvalidMergeRequestURLError(
                "The URL must identify a GitLab merge request."
            )

        project_path = unquote(match.group("project_path"))
        return project_path, int(match.group("iid"))

    def get_merge_request_metadata(self, merge_request_url: str) -> dict[str, Any]:
        """Retrieve basic metadata for the merge request identified by its URL."""

        project_path, merge_request_iid = self.parse_merge_request_url(
            merge_request_url
        )

        try:
            project = self._client.projects.get(project_path)
            merge_request = project.mergerequests.get(merge_request_iid)
        except GitlabError as exc:
            raise GitLabAPIError(
                "Unable to retrieve merge request metadata from GitLab."
            ) from exc

        author = getattr(merge_request, "author", None) or {}
        author_name = author.get("name") or author.get("username")

        return {
            "title": getattr(merge_request, "title", None),
            "description": getattr(merge_request, "description", None),
            "author": author_name,
            "source_branch": getattr(merge_request, "source_branch", None),
            "target_branch": getattr(merge_request, "target_branch", None),
            "state": getattr(merge_request, "state", None),
            "web_url": getattr(merge_request, "web_url", None),
            "created_at": getattr(merge_request, "created_at", None),
            "updated_at": getattr(merge_request, "updated_at", None),
        }

    def post_merge_request_comment(
        self,
        merge_request_url: str,
        review_content: str,
    ) -> dict[str, Any]:
        """Post review content as a note on the identified merge request."""

        project_path, merge_request_iid = self.parse_merge_request_url(
            merge_request_url
        )
        if not review_content.strip():
            raise ValueError("review_content is required.")

        try:
            project = self._client.projects.get(project_path)
            merge_request = project.mergerequests.get(merge_request_iid)
            note = merge_request.notes.create({"body": review_content})
        except GitlabError as exc:
            raise GitLabAPIError(
                "Unable to post the review to GitLab."
            ) from exc

        attributes = getattr(note, "attributes", {})
        return dict(attributes) if isinstance(attributes, dict) else {}

    def get_merge_request_changes(
        self, merge_request_url: str
    ) -> MergeRequestChanges:
        """Retrieve normalized changed files and combined diff text for an MR."""

        project_path, merge_request_iid = self.parse_merge_request_url(
            merge_request_url
        )

        try:
            project = self._client.projects.get(project_path)
            merge_request = project.mergerequests.get(merge_request_iid)
            changes_payload = merge_request.changes()
        except GitlabError as exc:
            raise GitLabAPIError(
                "Unable to retrieve merge request changes from GitLab."
            ) from exc

        if not isinstance(changes_payload, dict):
            raise GitLabAPIError("GitLab returned an invalid merge request changes response.")

        raw_changes = changes_payload.get("changes", [])
        if not isinstance(raw_changes, list):
            raise GitLabAPIError("GitLab returned an invalid merge request changes list.")

        changed_files: list[ChangedFile] = []
        for raw_change in raw_changes:
            if not isinstance(raw_change, dict):
                raise GitLabAPIError("GitLab returned an invalid changed file entry.")

            file_path = (
                raw_change.get("new_path")
                or raw_change.get("b_path")
                or raw_change.get("old_path")
                or raw_change.get("a_path")
            )
            if not isinstance(file_path, str):
                raise GitLabAPIError("GitLab returned a changed file without a path.")

            old_path = raw_change.get("old_path") or raw_change.get("a_path")
            diff = raw_change.get("diff") or ""
            if old_path is not None and not isinstance(old_path, str):
                old_path = str(old_path)
            if not isinstance(diff, str):
                diff = str(diff)

            changed_files.append(
                {
                    "file_path": file_path,
                    "old_path": old_path,
                    "is_new": bool(raw_change.get("new_file", False)),
                    "is_deleted": bool(raw_change.get("deleted_file", False)),
                    "is_renamed": bool(raw_change.get("renamed_file", False)),
                    "diff": diff,
                }
            )

        return {
            "files": changed_files,
            "combined_diff": "\n".join(file["diff"] for file in changed_files),
        }
