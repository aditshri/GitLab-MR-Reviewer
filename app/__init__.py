"""Flask application package."""

from flask import Flask

from config import Config
from app.routes import register_routes


def create_app() -> Flask:
	"""Create and configure the Flask application."""

	app = Flask(__name__)
	app.config.from_object(Config)
	register_routes(app)

	@app.get("/")
	def health_check() -> dict[str, str]:
		return {"status": "ok"}

	return app
