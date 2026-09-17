"""Flask routes that orchestrate the review services."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Flask, Response, current_app, jsonify, redirect, render_template, request, session

from app.audit_service import AuditService
from app.ai_service import AIReviewService, AIServiceError
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
from app.rules_generator import RulesGenerator
from config import Config

SUPPORTED_REVIEW_TYPES = {"Quick", "Comprehensive", "Security", "Performance"}


def create_review_blueprint(
    gitlab_service: GitLabService | None = None,
    claude_service: ClaudeService | None = None,
    audit_service: AuditService | None = None,
    rules_service: RulesManagementService | None = None,
    gitlab: GitLabService | None = None,
    ai: AIReviewService | None = None,
    audit: AuditService | None = None,
    rules: RulesManagementService | None = None,
    rules_generator: RulesGenerator | None = None,
) -> Blueprint:
    """Create the review blueprint with optional service dependencies."""

    blueprint = Blueprint("review", __name__)

    configured_gitlab = gitlab or gitlab_service
    configured_ai = ai
    configured_audit = audit or audit_service
    configured_rules = rules or rules_service
    configured_generator = rules_generator

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
        valid_review_type = (
            isinstance(review_type, str)
            and (
                review_type in SUPPORTED_REVIEW_TYPES
                or (configured_ai is not None and review_type.lower() in {"quick", "comprehensive", "security", "performance"})
            )
        )
        if not valid_review_type:
            return jsonify({"error": "review_type is invalid."}), 400

        rules = payload.get("review_rules")
        requirements = payload.get("requirements", [])
        if rules is not None and not isinstance(rules, (dict, list, str)):
            return jsonify({"error": "review_rules must be a string, list, or object."}), 400
        if not isinstance(requirements, (list, str)):
            return jsonify({"error": "requirements must be a string or list."}), 400

        if configured_ai is not None:
            if configured_gitlab is None:
                return jsonify({"error": "GitLab service is unavailable."}), 503
            try:
                project_path, mr_iid = configured_gitlab.parse_mr_url(mr_url)
                metadata = configured_gitlab.get_mr_details(project_path, mr_iid)
                diff = configured_gitlab.get_mr_diff(project_path, mr_iid)
                rules_document = configured_ai.load_rules(
                    current_app.config.get("REVIEW_RULES_FILE", "review_rules.yaml")
                )
                generated = configured_ai.generate_review(
                    metadata, diff, rules_document, review_type.lower(), requirements
                )
                review_id = configured_audit.log_review(
                    mr_url, str(metadata.get("title") or ""), str(metadata.get("author") or ""),
                    review_type, {**generated, "diff": diff},
                ) if configured_audit else None
                session["last_review"] = {"mr_url": mr_url, "project_path": project_path, "mr_iid": mr_iid, "review": generated}
                return jsonify({**generated, "review_id": review_id, "mr": metadata})
            except (AIServiceError, GitLabAPIError, GitLabConfigurationError, GitLabAuthenticationError) as exc:
                return jsonify({"error": str(exc)}), 502
            except InvalidMergeRequestURLError as exc:
                return jsonify({"error": str(exc)}), 400

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

        if configured_ai is not None:
            last_review = session.get("last_review", {})
            mr_url = payload.get("mr_url") or last_review.get("mr_url")
            review = payload.get("review") or last_review.get("review")
            if not isinstance(mr_url, str) or not isinstance(review, dict):
                return jsonify({"error": "No last review is available to post."}), 400
            if configured_gitlab is None:
                return jsonify({"error": "GitLab service is unavailable."}), 503
            try:
                project_path, mr_iid = configured_gitlab.parse_mr_url(mr_url)
                body = configured_ai.format_for_gitlab(review)
                result = configured_gitlab.post_comment(project_path, mr_iid, body)
                return jsonify({"status": "posted", "mr_url": mr_url, "note": result})
            except (GitLabAPIError, GitLabAuthenticationError, GitLabConfigurationError) as exc:
                return jsonify({"error": str(exc)}), 502

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
    @blueprint.get("/api/rules/list")
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
    @blueprint.get("/api/rules/export-markdown")
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
            filename = request.args.get("filename", filename)
            service = configured_rules or RulesManagementService()
            content = service.read_rule_content(filename)
            return jsonify({"filename": filename, "content": content})
        except RuleSetNotFoundError:
            return jsonify({"error": "Rule set was not found."}), 404
        except (InvalidRuleSetError, UnsafeRuleSetNameError):
            return jsonify({"error": "Invalid rule set filename or content."}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to read rule set."}), 500

    @blueprint.get("/api/rules/content")
    def read_current_rules_content() -> tuple[Any, int] | Any:
        """Return the selected rule set's raw YAML content."""

        service = configured_rules or RulesManagementService()
        filename = request.args.get("filename", "default.yaml")
        try:
            return jsonify({"filename": filename, "content": service.read_rule_content(filename)})
        except RuleSetNotFoundError:
            return jsonify({"error": "Rule set was not found."}), 404
        except RulesManagementError:
            return jsonify({"error": "Unable to read rule set."}), 500

    @blueprint.post("/api/rules")
    @blueprint.post("/api/rules/save")
    def save_rules() -> tuple[Any, int] | Any:
        """Save validated YAML content or a structured rule set."""

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        filename = payload.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            return jsonify({"error": "filename is required."}), 400
        service = configured_rules or RulesManagementService()
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
            info = (configured_rules or RulesManagementService()).upload_content(filename, content)
            return jsonify(info)
        except (InvalidRuleSetError, UnsafeRuleSetNameError) as exc:
            return jsonify({"error": str(exc)}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to upload rule set."}), 500

    @blueprint.post("/api/rules/reset")
    def reset_rules() -> tuple[Any, int] | Any:
        """Select the bundled default rule set."""

        try:
            return jsonify((configured_rules or RulesManagementService()).reset_to_default())
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
            filename = request.args.get("filename", filename)
            (configured_rules or RulesManagementService()).delete_custom_rules(filename)
            return jsonify({"status": "deleted", "filename": filename})
        except RuleSetNotFoundError:
            return jsonify({"error": "Rule set was not found."}), 404
        except UnsafeRuleSetNameError as exc:
            return jsonify({"error": str(exc)}), 400
        except RulesManagementError:
            return jsonify({"error": "Unable to delete rule set."}), 500

    @blueprint.delete("/api/rules/delete")
    @blueprint.post("/api/rules/delete")
    def delete_rules_by_query() -> tuple[Any, int] | Any:
        """Delete a custom rule set named by the query string."""

        payload = request.get_json(silent=True) or {}
        filename = payload.get("filename") or request.args.get("filename")
        if not filename:
            return jsonify({"error": "filename is required."}), 400
        return delete_rules(filename)

    @blueprint.post("/api/rules/generate-from-user")
    def generate_user_rules() -> tuple[Any, int] | Any:
        payload = request.get_json(silent=True) or {}
        if configured_generator is None:
            return jsonify({"error": "Rules generator is unavailable."}), 503
        project_path = payload.get("project_path")
        username = payload.get("username")
        if not isinstance(project_path, str) or not isinstance(username, str):
            return jsonify({"error": "project_path and username are required."}), 400
        try:
            return jsonify(configured_generator.generate_from_user_history(project_path, username, payload.get("limit", 50)))
        except (ValueError, GitLabAPIError, GitLabAuthenticationError) as exc:
            return jsonify({"error": str(exc)}), 502

    @blueprint.post("/api/rules/generate-from-project")
    def generate_project_rules() -> tuple[Any, int] | Any:
        payload = request.get_json(silent=True) or {}
        if configured_generator is None or not isinstance(payload.get("project_path"), str):
            return jsonify({"error": "project_path is required."}), 400
        try:
            return jsonify(configured_generator.generate_from_project_history(payload["project_path"], payload.get("limit", 100)))
        except (ValueError, GitLabAPIError, GitLabAuthenticationError) as exc:
            return jsonify({"error": str(exc)}), 502

    @blueprint.post("/api/rules/generate-from-project-consolidated")
    def generate_consolidated_rules() -> tuple[Any, int] | Any:
        payload = request.get_json(silent=True) or {}
        if configured_generator is None or not isinstance(payload.get("project_path"), str):
            return jsonify({"error": "project_path is required."}), 400
        try:
            return jsonify(configured_generator.generate_consolidated_rules(payload["project_path"]))
        except (ValueError, GitLabAPIError, GitLabAuthenticationError) as exc:
            return jsonify({"error": str(exc)}), 502

    @blueprint.get("/api/audit/trail/<path:mr_url>")
    def audit_trail(mr_url: str) -> Any:
        return jsonify(configured_audit.get_trail(mr_url) if configured_audit else [])

    @blueprint.get("/api/audit/review/<review_id>")
    def audit_review(review_id: str) -> tuple[Any, int] | Any:
        review = configured_audit.get_review(review_id) if configured_audit else None
        return (jsonify(review), 200) if review else (jsonify({"error": "Review not found."}), 404)

    @blueprint.get("/api/audit/recent")
    def audit_recent() -> Any:
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(configured_audit.get_recent(limit) if configured_audit else [])

    @blueprint.get("/api/audit/summary/<path:mr_url>")
    def audit_summary(mr_url: str) -> Any:
        return jsonify(configured_audit.get_summary(mr_url) if configured_audit else {})

    @blueprint.get("/api/audit/export/<path:mr_url>")
    def audit_export(mr_url: str) -> Any:
        export_format = request.args.get("format", "json")
        try:
            return jsonify({"path": configured_audit.export(mr_url, export_format)}) if configured_audit else jsonify({"error": "Audit service unavailable."}), 503
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.get("/")
    def index() -> str:
        return render_template("index.html")

    @blueprint.get("/about")
    def about() -> str:
        return render_template("about.html")

    @blueprint.get("/api/health")
    def health() -> Any:
        gitlab_connected = False
        if configured_gitlab is not None:
            try:
                gitlab_connected = configured_gitlab.validate_connection()
            except Exception:
                gitlab_connected = False
        return jsonify({
            "status": "ok",
            "gitlab_connected": gitlab_connected,
            "ai_configured": configured_ai is not None,
        })

    return blueprint


def register_routes(
    app: Flask,
    gitlab_service: GitLabService | None = None,
    claude_service: ClaudeService | None = None,
    audit_service: AuditService | None = None,
    rules_service: RulesManagementService | None = None,
    gitlab: GitLabService | None = None,
    ai: AIReviewService | None = None,
    audit: AuditService | None = None,
    rules: RulesManagementService | None = None,
    rules_generator: RulesGenerator | None = None,
) -> None:
    """Register the review blueprint on a Flask application."""

    app.register_blueprint(
        create_review_blueprint(
            gitlab_service=gitlab_service,
            claude_service=claude_service,
            audit_service=audit_service,
            rules_service=rules_service,
            gitlab=gitlab,
            ai=ai,
            audit=audit,
            rules=rules,
            rules_generator=rules_generator,
        )
    )


def _create_claude_service() -> ClaudeService:
    """Create ClaudeService from app configuration without inventing a model."""

    model = current_app.config.get("CLAUDE_MODEL", "")
    return ClaudeService(model=model, config=Config)
