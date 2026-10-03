"""Run logging plus an isolated handler for each sequential company."""
import logging
from pathlib import Path
from .privacy import safe_text

class PrivateFormatter(logging.Formatter):
    def format(self, record):
        return safe_text(super().format(record))

def setup_logging(path: Path, verbose: bool = False):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = PrivateFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in (logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

def company_handler(folder: Path) -> logging.Handler:
    handler = logging.FileHandler(folder / "logs" / "collection.log", encoding="utf-8")
    handler.setFormatter(PrivateFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler
