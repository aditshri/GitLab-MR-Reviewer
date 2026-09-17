"""Flask application factory and service container."""

from flask import Flask

from app.ai_service import AIReviewService, AIServiceError
from app.audit_service import AuditService
from app.gitlab_service import GitLabService
from app.rules_generator import RulesGenerator
from app.rules_management_service import RulesManagementService
from config import Config


def create_app(config_class: type[Config] = Config) -> Flask:
	"""Create a configured Flask app with shared service instances."""

	app = Flask(__name__, template_folder="../templates")
	app.config.from_object(config_class)
	app.secret_key = app.config.get("FLASK_SECRET_KEY") or "development-only-secret"

	gitlab_service = GitLabService(config_class)
	audit_service = AuditService()
	try:
		ai_service = AIReviewService(config_class)
	except AIServiceError:
		ai_service = None

	app.extensions["services"] = {
		"gitlab": gitlab_service,
		"ai": ai_service,
		"audit": audit_service,
		"rules": RulesManagementService(),
		"rules_generator": RulesGenerator(gitlab_service),
	}

	from app.routes import register_routes

	register_routes(app, **app.extensions["services"])
	return app
