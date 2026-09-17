"""Flask routes that orchestrate the review services."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Flask, Response, current_app, jsonify, request

from app.audit_service import AuditService
from app.claude_service import (
    ClaudeAPIError,
    ClaudeConfigurationError,
    ClaudeService,
    UnsupportedReviewTypeError,
)
from app.gitlab_service import (
    GitLabAuthenticationError,
    GitLabAPIError,
    GitLabConfigurationError,
    GitLabService,
    InvalidMergeRequestURLError,
)
from app.rules_management_service import (
    InvalidRuleSetError,
    RuleSetNotFoundError,
    RulesManagementError,
    RulesManagementService,
    UnsafeRuleSetNameError,
)
from config import Config

SUPPORTED_REVIEW_TYPES = {"Quick", "Comprehensive", "Security", "Performance"}


def create_review_blueprint(
    gitlab_service: GitLabService | None = None,
    claude_service: ClaudeService | None = None,
    audit_service: AuditService | None = None,
    rules_service: RulesManagementService | None = None,
) -> Blueprint:
    """Create the review blueprint with optional service dependencies."""

    blueprint = Blueprint("review", __name__)

    @blueprint.post("/api/review")
    def review() -> tuple[Any, int] | Any:
        """Run a merge request review and persist its audit record."""

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400

        mr_url = payload.get("mr_url")
        review_type = payload.get("review_type")
        if not isinstance(mr_url, str) or not mr_url.strip():
            return jsonify({"error": "mr_url is required."}), 400
        if not isinstance(review_type, str) or review_type not in SUPPORTED_REVIEW_TYPES:
            return jsonify({"error": "review_type is invalid."}), 400

        rules = payload.get("review_rules")
        requirements = payload.get("requirements", [])
        if rules is not None and not isinstance(rules, (dict, list, str)):
            return jsonify({"error": "review_rules must be a string, list, or object."}), 400
        if not isinstance(requirements, (list, str)):
            return jsonify({"error": "requirements must be a string or list."}), 400

        gitlab = gitlab_service or GitLabService()
        claude = claude_service or _create_claude_service()
        audit = audit_service or AuditService()

        try:
            gitlab.parse_merge_request_url(mr_url)
            metadata = gitlab.get_merge_request_metadata(mr_url)
            changes = gitlab.get_merge_request_changes(mr_url)
            if rules is None:
                rules = claude.load_review_rules()
            review_content = claude.review_merge_request(
                metadata=metadata,
                combined_diff=changes["combined_diff"],
                review_type=review_type,
                review_rules=rules,
                requirements=requirements,
            )
            record = audit.log_review(
                mr_url=mr_url,
                mr_title=str(metadata.get("title") or ""),
                mr_author=str(metadata.get("author") or ""),
                review_type=review_type,
                review_content=review_content,
                diff=changes["combined_diff"],
            )
        except InvalidMergeRequestURLError as exc:
            return jsonify({"error": str(exc)}), 400
        except UnsupportedReviewTypeError as exc:
            return jsonify({"error": str(exc)}), 400
        except (
            GitLabAuthenticationError,
            GitLabConfigurationError,
            GitLabAPIError,
        ) as exc:
            return jsonify({"error": str(exc)}), 502
        except (ClaudeAPIError, ClaudeConfigurationError) as exc:
            return jsonify({"error": str(exc)}), 502
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 500

        return jsonify(
            {
                "review": review_content,
                "mr": {
                    "url": mr_url,
                    "title": metadata.get("title"),
                    "author": metadata.get("author"),
                    "web_url": metadata.get("web_url"),
                },
                "review_metadata": {
                    "review_id": record["review_id"],
                    "review_type": record["review_type"],
                    "timestamp": record["timestamp"],
                    "diff_hash": record["diff_hash"],
                },
            }
        )

    @blueprint.post("/api/post-review")
    def post_review() -> tuple[Any, int] | Any:
        """Post generated review content to a merge request."""

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400

        mr_url = payload.get("mr_url")
        review_content = payload.get("review_content")
        if not isinstance(mr_url, str) or not mr_url.strip():
            return jsonify({"error": "mr_url is required."}), 400
        if not isinstance(review_content, str) or not review_content.strip():
            return jsonify({"error": "review_content is required."}), 400

        gitlab = gitlab_service or GitLabService()
        try:
            gitlab.parse_merge_request_url(mr_url)
            gitlab.post_merge_request_comment(mr_url, review_content)
        except InvalidMergeRequestURLError as exc:
            return jsonify({"error": str(exc)}), 400
        except (
            GitLabAuthenticationError,
            GitLabConfigurationError,
            GitLabAPIError,
        ) as exc:
            return jsonify({"error": str(exc)}), 502
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"status": "posted", "mr_url": mr_url})

    @blueprint.get("/api/rules")
    def list_rules() -> Any:
        """List the default and custom rule sets."""

        try:
            service = rules_service or RulesManagementService()
            return jsonify({"rule_sets": service.list_rule_sets()})
        except RulesManagementError:
            return jsonify({"error": "Unable to list rule sets."}), 500

    @blueprint.get("/api/rules/current")
    def current_rules() -> tuple[Any, int] | Any:
        """Return the currently selected validated rule set."""

        service = rules_service or RulesManagementService()
        try:
            rules = service.get_current_rules()
            filename = service.download_current()[1]
            return jsonify({"filename": filename, "rules": rules})
        except RuleSetNotFoundError:
            return jsonify({"error": "Current rule set was not found."}), 404
        except (InvalidRuleSetError, UnsafeRuleSetNameError):
            return jsonify({"error": "Current rule set is invalid."}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to read current rules."}), 500

    @blueprint.get("/api/rules/download")
    def download_rules() -> Response | tuple[Any, int]:
        """Download the currently selected rules as YAML."""

        try:
            content, filename = (rules_service or RulesManagementService()).download_current()
        except RuleSetNotFoundError:
            return jsonify({"error": "Current rule set was not found."}), 404
        except RulesManagementError:
            return jsonify({"error": "Unable to download current rules."}), 500
        return Response(
            content,
            mimetype="application/x-yaml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @blueprint.get("/api/rules/markdown")
    def export_rules_markdown() -> Response | tuple[Any, int]:
        """Download the currently selected rules as Markdown."""

        try:
            content = (rules_service or RulesManagementService()).export_current_markdown()
        except RuleSetNotFoundError:
            return jsonify({"error": "Current rule set was not found."}), 404
        except RulesManagementError:
            return jsonify({"error": "Unable to export current rules."}), 500
        return Response(
            content,
            mimetype="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="review_rules.md"'},
        )

    @blueprint.get("/api/rules/<path:filename>")
    def read_rules(filename: str) -> tuple[Any, int] | Any:
        """Read raw YAML content for one rule set."""

        try:
            service = rules_service or RulesManagementService()
            content = service.read_rule_content(filename)
            return jsonify({"filename": filename, "content": content})
        except RuleSetNotFoundError:
            return jsonify({"error": "Rule set was not found."}), 404
        except (InvalidRuleSetError, UnsafeRuleSetNameError):
            return jsonify({"error": "Invalid rule set filename or content."}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to read rule set."}), 500

    @blueprint.post("/api/rules")
    def save_rules() -> tuple[Any, int] | Any:
        """Save validated YAML content or a structured rule set."""

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        filename = payload.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            return jsonify({"error": "filename is required."}), 400
        service = rules_service or RulesManagementService()
        try:
            if isinstance(payload.get("content"), str):
                info = service.save_content(filename, payload["content"])
            elif isinstance(payload.get("rules"), dict):
                info = service.save_rules(filename, payload["rules"])
            else:
                return jsonify({"error": "content or rules is required."}), 400
            return jsonify(info)
        except (InvalidRuleSetError, UnsafeRuleSetNameError) as exc:
            return jsonify({"error": str(exc)}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to save rule set."}), 500

    @blueprint.post("/api/rules/upload")
    def upload_rules() -> tuple[Any, int] | Any:
        """Upload YAML or Markdown rule content and select it."""

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        filename = payload.get("filename")
        content = payload.get("content")
        if not isinstance(filename, str) or not filename.strip():
            return jsonify({"error": "filename is required."}), 400
        if not isinstance(content, str):
            return jsonify({"error": "content is required."}), 400
        try:
            info = (rules_service or RulesManagementService()).upload_content(filename, content)
            return jsonify(info)
        except (InvalidRuleSetError, UnsafeRuleSetNameError) as exc:
            return jsonify({"error": str(exc)}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to upload rule set."}), 500

    @blueprint.post("/api/rules/reset")
    def reset_rules() -> tuple[Any, int] | Any:
        """Select the bundled default rule set."""

        try:
            return jsonify((rules_service or RulesManagementService()).reset_to_default())
        except RuleSetNotFoundError:
            return jsonify({"error": "Default rule set was not found."}), 404
        except (InvalidRuleSetError, UnsafeRuleSetNameError):
            return jsonify({"error": "Default rule set is invalid."}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to reset rules."}), 500

    @blueprint.delete("/api/rules/<path:filename>")
    def delete_rules(filename: str) -> tuple[Any, int] | Any:
        """Delete one custom rule set."""

        try:
            (rules_service or RulesManagementService()).delete_custom_rules(filename)
            return jsonify({"status": "deleted", "filename": filename})
        except RuleSetNotFoundError:
            return jsonify({"error": "Rule set was not found."}), 404
        except UnsafeRuleSetNameError as exc:
            return jsonify({"error": str(exc)}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to delete rule set."}), 500

    return blueprint


def register_routes(
    app: Flask,
    gitlab_service: GitLabService | None = None,
    claude_service: ClaudeService | None = None,
    audit_service: AuditService | None = None,
    rules_service: RulesManagementService | None = None,
) -> None:
    """Register the review blueprint on a Flask application."""

    app.register_blueprint(
        create_review_blueprint(
            gitlab_service=gitlab_service,
            claude_service=claude_service,
            audit_service=audit_service,
            rules_service=rules_service,
        )
    )


def _create_claude_service() -> ClaudeService:
    """Create ClaudeService from app configuration without inventing a model."""

    model = current_app.config.get("CLAUDE_MODEL", "")
    return ClaudeService(model=model, config=Config)
