"""Crash capture / next-launch reporting (core.crash): the pure pieces — report
files, fault-dump sweeping, acknowledgement, summaries, and the prefilled
GitHub-issue URL. The UI dialog on top is exercised by the smoke layer."""
from __future__ import annotations

import os
import urllib.parse

from core import crash


def test_write_report_has_header_and_body(tmp_path):
    p = crash.write_crash_report(tmp_path, "Traceback ...\nValueError: boom",
                                 version="1.2.3")
    assert p is not None and p.name.startswith("crash-")
    text = p.read_text()
    assert "version: 1.2.3" in text
    assert "kind: exception" in text
    assert "ValueError: boom" in text


def test_pending_newest_first_and_acknowledge(tmp_path):
    a = crash.write_crash_report(tmp_path, "AError: one", version="1")
    b = crash.write_crash_report(tmp_path, "BError: two", version="1")
    assert a is not None and b is not None
    pending = crash.pending_reports(tmp_path)
    assert pending == [b, a]                       # newest first
    crash.acknowledge(b)
    assert crash.pending_reports(tmp_path) == [a]  # seen ones drop out
    assert b.with_name(b.name + ".seen").exists()  # but stay on disk


def test_pending_on_missing_dir_is_empty(tmp_path):
    assert crash.pending_reports(tmp_path / "nope") == []


def test_fault_capture_clean_exit_leaves_nothing(tmp_path):
    crash.enable_fault_capture(tmp_path)
    try:
        assert list(tmp_path.glob("fault-*.dump")), "dump file not armed"
    finally:
        crash.disable_fault_capture()
    assert not list(tmp_path.glob("fault-*.dump")), "clean exit must remove dump"


def test_collect_faults_converts_dead_pid_dump(tmp_path):
    # A non-empty dump from a pid that no longer exists = a native crash.
    (tmp_path / "fault-999999999.dump").write_text(
        "Fatal Python error: Segmentation fault\n\nThread 0x01 (most recent call first):")
    reports = crash.collect_faults(tmp_path, version="9.9.9")
    assert len(reports) == 1
    assert "kind: native-fault" in reports[0].read_text()
    assert not list(tmp_path.glob("fault-*.dump"))          # dump consumed


def test_collect_faults_removes_empty_leftovers_silently(tmp_path):
    (tmp_path / "fault-999999998.dump").write_text("")
    assert crash.collect_faults(tmp_path, version="1") == []
    assert not list(tmp_path.glob("fault-*.dump"))


def test_collect_faults_leaves_live_pid_alone(tmp_path):
    mine = tmp_path / f"fault-{os.getpid()}.dump"
    mine.write_text("not a crash, I'm still running")
    assert crash.collect_faults(tmp_path, version="1") == []
    assert mine.exists()


def test_summarize_finds_exception_line(tmp_path):
    p = crash.write_crash_report(
        tmp_path, "Traceback (most recent call last):\n  ...\nTypeError: bad thing",
        version="1")
    assert p is not None
    assert crash.summarize(p) == "TypeError: bad thing"


def test_summarize_finds_fatal_error_line(tmp_path):
    p = crash.write_crash_report(
        tmp_path, "Fatal Python error: Segmentation fault\n\nThread 0x01:",
        version="1", kind="native-fault")
    assert p is not None
    assert crash.summarize(p).startswith("Fatal Python error")


def test_issue_url_prefills_and_truncates(tmp_path):
    p = crash.write_crash_report(tmp_path, "x" * 20000 + "\nOSError: disk", version="2.0")
    assert p is not None
    url = crash.github_issue_url("owner/repo", p, version="2.0", body_limit=500)
    assert url.startswith("https://github.com/owner/repo/issues/new?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "OSError: disk" in q["title"][0] and "2.0" in q["title"][0]
    assert "truncated" in q["body"][0]
    assert len(q["body"][0]) < 2000                # clipped, not the full 20k
