from __future__ import annotations

from tubetape.ui import Reporter


class TestReporterNonTty:
    def test_status_plain_line(self, capsys):
        reporter = Reporter(tty=False)
        reporter.status("hello")
        assert capsys.readouterr().out == "hello\n"

    def test_progress(self, capsys):
        reporter = Reporter(tty=False)
        reporter.progress(2, 4, "upload")
        assert "upload 2/4 (50%)" in capsys.readouterr().out

    def test_quota(self, capsys):
        reporter = Reporter(tty=False)
        reporter.quota(1600, 10000)
        assert "quota: 1600/10000" in capsys.readouterr().out

    def test_task_null_context(self, capsys):
        reporter = Reporter(tty=False)
        with reporter.task("scanning"):
            pass
        assert "task: scanning" in capsys.readouterr().out
