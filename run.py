"""Application entry point."""

from app import create_app


app = create_app()


def main() -> None:
    """Start the Flask development server."""

    app.run(
        host=app.config["FLASK_HOST"],
        port=app.config["FLASK_PORT"],
        debug=app.config["FLASK_DEBUG"],
    )


if __name__ == "__main__":
    main()
