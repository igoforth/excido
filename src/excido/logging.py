from contextlib import contextmanager
from logging import DEBUG, INFO, Formatter, Handler, basicConfig, getLogger

basicConfig(level=INFO)
logger = getLogger(__name__)


class LogCaptureHandler(Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.captured_records = []

    def emit(self, record):
        self.captured_records.append(self.format(record))

    def release_logs(self):
        return self.captured_records

    def clear_logs(self):
        self.captured_records.clear()


@contextmanager
def log_capture_context(logger, level=DEBUG):
    capture_handler = LogCaptureHandler()
    capture_handler.setLevel(level)
    formatter = Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    capture_handler.setFormatter(formatter)

    original_level = logger.level
    original_propagate = logger.propagate  # Save the original propagate value

    logger.setLevel(level)
    logger.addHandler(capture_handler)
    logger.propagate = False  # Prevent log messages from propagating to parent loggers

    try:
        yield capture_handler
    finally:
        logger.setLevel(original_level)
        logger.removeHandler(capture_handler)
        logger.propagate = original_propagate  # Restore the original propagate value
