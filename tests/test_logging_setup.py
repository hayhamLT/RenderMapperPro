"""Tests for the stdlib logging setup."""
import logging

from core.logging_setup import APP_LOGGER, add_callback_handler, get_logger, setup_logging


def test_setup_logging_writes_banner_and_records(tmp_path):
    log = tmp_path / "app.log"
    setup_logging(log, version="9.9.9")
    get_logger("test").warning("hello-world")
    for h in logging.getLogger(APP_LOGGER).handlers:
        h.flush()
    text = log.read_text()
    assert "session start" in text
    assert "hello-world" in text
    assert "9.9.9" in text


def test_callback_handler_receives_records(tmp_path):
    setup_logging(tmp_path / "a.log")
    seen: list[tuple[str, str]] = []
    add_callback_handler(lambda lvl, msg: seen.append((lvl, msg)))
    get_logger().error("boom")
    assert any(lvl == "error" and "boom" in msg for lvl, msg in seen)
