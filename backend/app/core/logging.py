import logging

from app.core.config import settings


def _resolve_log_level(level_name: str) -> int:
    level = getattr(logging, (level_name or "").upper(), None)
    return level if isinstance(level, int) else logging.INFO


def configure_logging() -> None:
    level = _resolve_log_level(settings.log_level)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )

    root_logger.setLevel(level)
    logging.getLogger("app").setLevel(level)
