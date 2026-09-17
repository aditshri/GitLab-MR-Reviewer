"""Application entry point."""

from app import create_app


app = create_app()


def main() -> None:
    """Start the Flask development server."""

    app.run(
        host="127.0.0.1",
        port=app.config["PORT"],
        debug=app.config["DEBUG"],
    )


if __name__ == "__main__":
    main()
