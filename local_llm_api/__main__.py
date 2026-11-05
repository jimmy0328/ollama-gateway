import uvicorn

from .app import create_app
from .config import Settings


def main():
    settings = Settings()
    # One gateway worker keeps the admission limit shared by every request.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1,
                proxy_headers=False, timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
