from __future__ import annotations

from waitress import serve

from app import create_app


def main() -> None:
    application = create_app()
    serve(
        application,
        host=str(application.config["HOST"]),
        port=int(application.config["PORT"]),
    )


if __name__ == "__main__":
    main()
