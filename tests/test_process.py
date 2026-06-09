import subprocess
import sys

from core.utils import iter_process_output


def _run(code):
    return subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)


def test_iter_process_output_collects_all_lines():
    p = _run("import sys\n[print('line%d' % i) or sys.stdout.flush() for i in range(3)]")
    lines = list(iter_process_output(p))
    p.wait()
    assert lines == ["line0", "line1", "line2"]


def test_iter_process_output_hard_timeout_terminates():
    p = _run("import time\nprint('start', flush=True)\ntime.sleep(30)\nprint('end')")
    fired = []
    lines = list(iter_process_output(p, hard_timeout=1, on_timeout=lambda k, s: fired.append(k)))
    p.wait()
    assert "start" in lines and "end" not in lines
    assert fired == ["hard"]
