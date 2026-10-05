from __future__ import annotations

import os
import sys
from zoneinfo import ZoneInfo

import pytest

from tubetape.cli import build_parser, parse_args


def _parse(*argv):
    return parse_args(list(argv))


class TestDefaults:
    def test_defaults(self):
        args = _parse()
        assert args.image_duration == 3.0
        assert args.input == os.path.abspath(".")
        assert args.db == os.path.join(os.path.abspath("."), "tubetape.json")
        assert args.segment_duration == 3600.0
        assert args.crf == 16
        assert args.max_resolution == (7680, 4320)
        assert args.canvas_mode == "max"
        assert args.fps == 60
        assert args.x264_preset == "slow"
        assert args.ken_burns is False
        assert args.privacy == "private"
        assert args.flush is False
        assert args.watch is True
        assert args.dry_run is False
        assert args.mtime_interval == 3600.0
        assert args.quota_backoff == 3600.0
        assert args.timezone is not None
        assert args.web_port == 8080

    def test_db_default_joins_input(self, tmp_path):
        args = _parse("--input", str(tmp_path))
        assert args.db == os.path.join(str(tmp_path), "tubetape.json")


class TestExplicitValues:
    def test_all_explicit(self, tmp_path):
        args = _parse(
            "--image-duration", "5",
            "--input", str(tmp_path),
            "--db", str(tmp_path / "custom.json"),
            "--segment-duration", "30s",
            "--timezone", "UTC",
            "--crf", "20",
            "--max-resolution", "1920x1080",
            "--ken-burns",
            "--privacy", "unlisted",
            "--flush",
            "--no-watch",
            "--dry-run",
        )
        assert args.image_duration == 5.0
        assert args.input == str(tmp_path)
        assert args.db == str(tmp_path / "custom.json")
        assert args.segment_duration == 30.0
        assert str(args.timezone) == "UTC"
        assert args.crf == 20
        assert args.max_resolution == (1920, 1080)
        assert args.ken_burns is True
        assert args.privacy == "unlisted"
        assert args.flush is True
        assert args.watch is False
        assert args.dry_run is True

    def test_segment_duration_clock_notation(self):
        args = _parse("--segment-duration", "0:10:00")
        assert args.segment_duration == 600.0

    def test_timezone_iana(self):
        args = _parse("--timezone", "Asia/Shanghai")
        assert isinstance(args.timezone, ZoneInfo)
        assert args.timezone.key == "Asia/Shanghai"


class TestInvalidValues:
    @pytest.mark.parametrize(
        "argv",
        [
            ("--privacy", "public"),
            ("--crf", "abc"),
            ("--segment-duration", "nonsense"),
            ("--image-duration", "0"),
            ("--segment-duration", "0"),
            ("--crf", "-1"),
            ("--timezone", "Not/AZone"),
            ("--max-resolution", "1920"),
        ],
    )
    def test_argparse_error(self, argv):
        with pytest.raises(SystemExit):
            _parse(*argv)


def test_parser_prog():
    assert build_parser().prog == "tubetape"


class TestRunPipeline:
    def test_dry_run_end_to_end(self, tmp_path, capsys):
        from tubetape.cli import main
        from conftest import make_png

        make_png(tmp_path / "a.png", size=(10, 10))
        make_png(tmp_path / "b.png", size=(20, 20))

        rc = main(
            [
                "--dry-run",
                "--no-watch",
                "--input", str(tmp_path),
                "--db", str(tmp_path / "db.json"),
                "--segment-duration", "20s",
                "--flush",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "dry-run" in out
        assert "segment" in out
        assert "1 segment(s)" in out
        # Dry-run is read-only: no database file is written.
        assert not (tmp_path / "db.json").exists()

    def test_dry_run_without_flush_holds_partial(self, tmp_path, capsys):
        from tubetape.cli import main
        from conftest import make_png

        make_png(tmp_path / "a.png", size=(10, 10))
        rc = main(
            [
                "--dry-run",
                "--no-watch",
                "--input", str(tmp_path),
                "--db", str(tmp_path / "db.json"),
                "--segment-duration", "20s",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "0 segment(s)" in out
        assert "1 pending" in out


class TestRunPipelineReconcile:
    def _args(self, tmp_path):
        from tubetape.cli import parse_args

        return parse_args(
            [
                "--input", str(tmp_path),
                "--db", str(tmp_path / "db.json"),
                "--segment-duration", "20s",
                "--flush",
                "--no-watch",
            ]
        )

    def test_skips_segment_already_on_youtube(self, tmp_path, monkeypatch, capsys):
        import json

        from conftest import make_png
        from tubetape import cli, planner, scanner

        make_png(tmp_path / "a.png", size=(10, 10))
        args = self._args(tmp_path)
        file_id = scanner.file_sha256(str(tmp_path / "a.png"))
        sid = planner.segment_id([file_id], cli._segment_params(args))

        class _FakeUploader:
            service = object()

            def __init__(self):
                self.uploaded = []

            def upload(self, *a, **k):
                self.uploaded.append(1)
                return "should-not-be-called"

            def verify(self, *a, **k):
                pass

            def delete_video(self, *a, **k):
                pass

        fake = _FakeUploader()
        monkeypatch.setattr(cli, "_build_uploader", lambda a, d: fake)
        monkeypatch.setattr(cli, "fetch_remote_index", lambda service: {sid: "vid-x"})

        rc = cli.run_pipeline(args)
        assert rc == cli._EXIT_OK
        assert fake.uploaded == []  # no upload, no transcode
        db = json.loads((tmp_path / "db.json").read_text())
        assert db["segments"][sid]["youtube_video_id"] == "vid-x"

    def test_skips_segment_already_on_youtube_by_short_id(self, tmp_path, monkeypatch):
        import json

        from conftest import make_png
        from tubetape import cli, planner, scanner

        make_png(tmp_path / "a.png", size=(10, 10))
        args = self._args(tmp_path)
        file_id = scanner.file_sha256(str(tmp_path / "a.png"))
        sid = planner.segment_id([file_id], cli._segment_params(args))
        short_id = sid[:16]

        class _FakeUploader:
            service = object()

            def __init__(self):
                self.uploaded = []

            def upload(self, *a, **k):
                self.uploaded.append(1)
                return "should-not-be-called"

            def verify(self, *a, **k):
                pass

            def delete_video(self, *a, **k):
                pass

        fake = _FakeUploader()
        monkeypatch.setattr(cli, "_build_uploader", lambda a, d: fake)
        # Remote index only has the 16-character prefix
        monkeypatch.setattr(cli, "fetch_remote_index", lambda service: {short_id: "vid-short"})

        rc = cli.run_pipeline(args)
        assert rc == cli._EXIT_OK
        assert fake.uploaded == []
        db = json.loads((tmp_path / "db.json").read_text())
        assert db["segments"][sid]["youtube_video_id"] == "vid-short"

    def test_quota_error_returns_quota_code(self, tmp_path, monkeypatch, capsys):
        from conftest import make_png
        from tubetape import cli
        from tubetape.uploader import QuotaExceededError

        make_png(tmp_path / "a.png", size=(10, 10))
        args = self._args(tmp_path)

        class _FakeUploader:
            service = object()

            def upload(self, *a, **k):
                raise QuotaExceededError("quota")

            def verify(self, *a, **k):
                pass

            def delete_video(self, *a, **k):
                pass

        monkeypatch.setattr(cli, "_build_uploader", lambda a, d: _FakeUploader())
        monkeypatch.setattr(cli, "fetch_remote_index", lambda service: {})

        assert cli.run_pipeline(args) == cli._EXIT_QUOTA


class TestLogin:
    def test_login_flag_parses(self, tmp_path):
        from tubetape.cli import parse_args

        args = parse_args(["--login", "--db", str(tmp_path / "db.json")])
        assert args.login is True

    def test_login_missing_client_secret(self, tmp_path):
        from tubetape import cli

        args = cli.parse_args(["--login", "--db", str(tmp_path / "db.json")])
        assert cli._run_login(args) == cli._EXIT_ERROR

    def test_login_saves_token(self, tmp_path, monkeypatch):
        from tubetape import auth, cli

        (tmp_path / "client_secret.json").write_text("{}")
        args = cli.parse_args(["--login", "--db", str(tmp_path / "db.json")])
        called = {}

        def fake_flow(client_secret_path, token_path=None, **kwargs):
            called["client"] = client_secret_path
            called["token"] = token_path
            with open(token_path, "w") as handle:
                handle.write("{}")
            return object()

        monkeypatch.setattr(auth, "headless_oauth_flow", fake_flow)
        rc = cli._run_login(args)
        assert rc == cli._EXIT_OK
        assert called["client"] == str(tmp_path / "client_secret.json")
        assert called["token"] == str(tmp_path / "token.json")
        assert (tmp_path / "token.json").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
