from contextlib import redirect_stdout
from io import StringIO

from demo_cli import main


def test_summary_command_prints_queue_summary() -> None:
    buffer = StringIO()
    with redirect_stdout(buffer):
        assert main(["summary"]) == 0
    output = buffer.getvalue()
    assert "tasks queued" in output
    assert "Fix the queue estimate bug" in output
