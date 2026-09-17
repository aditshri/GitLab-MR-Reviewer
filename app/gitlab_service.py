"""GitLab service operations for merge request metadata."""

from __future__ import annotations

import re
import logging
from typing import Any, TypedDict
from urllib.parse import unquote, urlsplit

import gitlab
from gitlab.exceptions import GitlabAuthenticationError as GitlabAuthError
from gitlab.exceptions import GitlabError

from config import Config


logger = logging.getLogger(__name__)


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
        """Initialize a GitLab client without blocking application startup."""

        self._gitlab_url = (getattr(config, "GITLAB_URL", "") or "").rstrip("/")
        self._gitlab_token = getattr(config, "GITLAB_TOKEN", None)
        self._client: gitlab.Gitlab | None = None
        self._initialization_error: Exception | None = None

        try:
            self._client = gitlab.Gitlab(
                self._gitlab_url,
                private_token=self._gitlab_token,
            )
        except Exception as exc:  # python-gitlab can raise several init errors.
            self._initialization_error = exc
            logger.exception("Unable to initialize the GitLab client.")

    def validate_connection(self) -> bool:
        """Authenticate with GitLab and return whether the connection is valid."""

        client = self._require_client()
        try:
            client.auth()
        except GitlabAuthError as exc:
            raise GitLabAuthenticationError(
                "GitLab authentication failed."
            ) from exc
        except GitlabError as exc:
            raise GitLabAPIError(
                "GitLab connection validation failed."
            ) from exc

        return True

    def parse_mr_url(self, mr_url: str) -> tuple[str, int]:
        """Extract a project path and merge request IID from a GitLab URL."""

        return self.parse_merge_request_url(mr_url)

    def parse_merge_request_url(self, merge_request_url: str) -> tuple[str, int]:
        """Extract the project path and merge request IID from a GitLab URL."""

        if not self._gitlab_url:
            raise GitLabConfigurationError("GITLAB_URL is not configured.")

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

    def get_mr_details(self, project_path: str, mr_iid: int) -> dict[str, Any]:
        """Retrieve the requested merge request metadata."""

        merge_request = self._get_merge_request(project_path, mr_iid)
        author = getattr(merge_request, "author", None) or {}
        if isinstance(author, dict):
            author = author.get("name") or author.get("username")

        return {
            "title": getattr(merge_request, "title", None),
            "description": getattr(merge_request, "description", None),
            "author": author,
            "source_branch": getattr(merge_request, "source_branch", None),
            "target_branch": getattr(merge_request, "target_branch", None),
            "web_url": getattr(merge_request, "web_url", None),
        }

    def get_merge_request_metadata(self, merge_request_url: str) -> dict[str, Any]:
        """Retrieve basic metadata for the merge request identified by its URL."""

        project_path, merge_request_iid = self.parse_mr_url(merge_request_url)
        return self.get_mr_details(project_path, merge_request_iid)

    def get_mr_diff(self, project_path: str, mr_iid: int) -> str:
        """Return all changed-file diffs as one LLM-ready string."""

        merge_request = self._get_merge_request(project_path, mr_iid)
        try:
            changes_payload = merge_request.changes()
        except GitlabError as exc:
            raise self._api_error(exc, "merge request changes") from exc

        if not isinstance(changes_payload, dict):
            raise GitLabAPIError("GitLab returned an invalid merge request changes response.")
        changes = changes_payload.get("changes", [])
        if not isinstance(changes, list):
            raise GitLabAPIError("GitLab returned an invalid merge request changes list.")

        diffs = [
            str(change.get("diff", ""))
            for change in changes
            if isinstance(change, dict)
        ]
        return "\n".join(diffs)

    def post_merge_request_comment(
        self,
        merge_request_url: str,
        review_content: str,
    ) -> dict[str, Any]:
        """Post review content as a note on the identified merge request."""

        project_path, merge_request_iid = self.parse_mr_url(merge_request_url)
        return self.post_comment(project_path, merge_request_iid, review_content)

    def post_comment(
        self,
        project_path: str,
        mr_iid: int,
        comment_body: str,
    ) -> dict[str, Any]:
        """Post a note to a merge request and return GitLab's note data."""

        if not comment_body.strip():
            raise ValueError("comment_body is required.")

        merge_request = self._get_merge_request(project_path, mr_iid)
        try:
            note = merge_request.notes.create({"body": comment_body})
        except GitlabError as exc:
            raise self._api_error(exc, "merge request comment") from exc

        attributes = getattr(note, "attributes", {})
        return dict(attributes) if isinstance(attributes, dict) else {}

    def get_review_comments(
        self,
        project_path: str,
        username: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return merge-request note bodies from a project, optionally by reviewer."""

        if limit < 0:
            raise ValueError("limit must not be negative.")
        project = self._get_project(project_path)
        try:
            merge_requests = project.mergerequests.list(state="all", all=True)
        except GitlabError as exc:
            raise self._api_error(exc, f"review history for project '{project_path}'") from exc

        comments: list[dict[str, Any]] = []
        for merge_request in merge_requests:
            merge_request_iid = getattr(merge_request, "iid", None)
            try:
                full_merge_request = project.mergerequests.get(merge_request_iid)
                notes = full_merge_request.notes.list(all=True)
            except GitlabError as exc:
                raise self._api_error(
                    exc,
                    f"review history for merge request !{merge_request_iid}",
                ) from exc

            for note in notes:
                attributes = getattr(note, "attributes", {})
                if not isinstance(attributes, dict):
                    attributes = {}
                author = attributes.get("author") or getattr(note, "author", {}) or {}
                reviewer = (
                    author.get("username") or author.get("name")
                    if isinstance(author, dict)
                    else str(author)
                )
                if username and reviewer != username:
                    continue
                body = attributes.get("body") or getattr(note, "body", "")
                if not isinstance(body, str) or not body.strip():
                    continue
                comments.append(
                    {
                        "body": body.strip(),
                        "author": reviewer,
                        "merge_request_id": str(merge_request_iid),
                        "project": project_path,
                    }
                )
                if len(comments) >= limit:
                    return comments
        return comments

    def get_merge_request_changes(
        self, merge_request_url: str
    ) -> MergeRequestChanges:
        """Retrieve normalized changed files and combined diff text for an MR."""

        project_path, merge_request_iid = self.parse_mr_url(merge_request_url)
        merge_request = self._get_merge_request(project_path, merge_request_iid)

        try:
            changes_payload = merge_request.changes()
        except GitlabError as exc:
            raise self._api_error(exc, "merge request changes") from exc

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

    def _require_client(self) -> gitlab.Gitlab:
        """Return the client or raise a clear configuration/authentication error."""

        if not self._gitlab_token:
            raise GitLabAuthenticationError(
                "GitLab authentication failed: GITLAB_TOKEN is missing."
            )
        if self._client is None:
            raise GitLabServiceError(
                "GitLab client initialization failed. Check GITLAB_URL."
            ) from self._initialization_error
        return self._client

    def _get_project(self, project_path: str) -> Any:
        """Retrieve a project while translating GitLab errors into service errors."""

        client = self._require_client()
        try:
            return client.projects.get(project_path)
        except GitlabError as exc:
            raise self._api_error(exc, f"GitLab project '{project_path}'") from exc

    def _get_merge_request(self, project_path: str, mr_iid: int) -> Any:
        """Retrieve an MR after resolving its project."""

        project = self._get_project(project_path)
        try:
            return project.mergerequests.get(mr_iid)
        except GitlabError as exc:
            raise self._api_error(
                exc,
                f"GitLab merge request !{mr_iid} in project '{project_path}'",
            ) from exc

    @staticmethod
    def _api_error(exc: GitlabError, resource: str) -> GitLabServiceError:
        """Map common python-gitlab failures to human-readable service errors."""

        response_code = getattr(exc, "response_code", None)
        if isinstance(exc, GitlabAuthError) or response_code in {401, 403}:
            return GitLabAuthenticationError(
                "GitLab authentication failed. Check GITLAB_TOKEN."
            )
        if response_code == 404:
            if resource.startswith("GitLab project"):
                return GitLabAPIError(f"GitLab project not found: {resource}.")
            if resource.startswith("GitLab merge request"):
                return GitLabAPIError(f"GitLab merge request not found: {resource}.")
            return GitLabAPIError(f"GitLab resource not found: {resource}.")
        return GitLabAPIError(f"GitLab request failed while accessing {resource}.")
