from importlib.util import find_spec
from socket import gethostname

from app.core.config import settings
from app.core.exceptions import MediaError
from app.services.tts import check_model
from app.workers.celery_app import celery


def main():
    config = settings()
    if config.tts_enabled:
        try:
            check_model(config.tts_model_dir)
        except MediaError as exc:
            raise SystemExit(exc.code) from None
        if find_spec("vieneu") is None:
            raise SystemExit("TTS_SDK_UNAVAILABLE")
    if not celery.control.ping(destination=[f"celery@{gethostname()}"], timeout=3):
        raise SystemExit("CELERY_UNAVAILABLE")


if __name__ == "__main__":
    main()
