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
        assert args.mtime_interval == 600.0
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

    def test_pipeline_ensures_uploader_before_scan(self, tmp_path, monkeypatch):
        from tubetape import cli

        args = self._args(tmp_path)
        events = []

        def fake_build_uploader(a, d):
            events.append("build_uploader")
            return None  # Simulates failed / missing credentials

        def fake_scan(*a, **k):
            events.append("scan")
            raise AssertionError("scan() must not be called when credentials are missing or waiting!")

        monkeypatch.setattr(cli, "_build_uploader", fake_build_uploader)
        monkeypatch.setattr(cli, "scan", fake_scan)

        rc = cli.run_pipeline(args)
        assert rc == cli._EXIT_ERROR
        assert events == ["build_uploader"]



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

class TestKeepSegments:
    def test_keep_segments_arg_parse(self, tmp_path):
        from tubetape.cli import parse_args

        args = parse_args(["--input", str(tmp_path), "--keep-segments", "5"])
        assert args.keep_segments == 5

        # Default is 0
        args_default = parse_args(["--input", str(tmp_path)])
        assert args_default.keep_segments == 0

        # Negative value errors
        with pytest.raises(SystemExit):
            parse_args(["--input", str(tmp_path), "--keep-segments", "-1"])

    def test_rotate_uploaded_segments_zero(self, tmp_path):
        from tubetape.cli import rotate_uploaded_segments

        seg_dir = tmp_path / "uploaded_segments"
        seg_dir.mkdir()
        (seg_dir / "seg1.mp4").write_text("dummy")
        (seg_dir / "seg2.mp4").write_text("dummy")
        (seg_dir / ".hidden.mp4").write_text("dummy")
        (seg_dir / "other.txt").write_text("dummy")

        deleted = rotate_uploaded_segments(str(seg_dir), 0)
        assert len(deleted) == 2
        assert not (seg_dir / "seg1.mp4").exists()
        assert not (seg_dir / "seg2.mp4").exists()
        # Hidden files and non-mp4 files should not be deleted
        assert (seg_dir / ".hidden.mp4").exists()
        assert (seg_dir / "other.txt").exists()

    def test_rotate_uploaded_segments_keep_n(self, tmp_path):
        import time
        from tubetape.cli import rotate_uploaded_segments

        seg_dir = tmp_path / "uploaded_segments"
        seg_dir.mkdir()

        # Create files with staggered mtime
        f1 = seg_dir / "oldest.mp4"
        f1.write_text("1")
        os.utime(f1, (time.time() - 300, time.time() - 300))

        f2 = seg_dir / "middle.mp4"
        f2.write_text("2")
        os.utime(f2, (time.time() - 200, time.time() - 200))

        f3 = seg_dir / "newest.mp4"
        f3.write_text("3")
        os.utime(f3, (time.time() - 100, time.time() - 100))

        # Keep 2 -> oldest should be deleted, middle and newest kept
        deleted = rotate_uploaded_segments(str(seg_dir), 2)
        assert deleted == [str(f1)]
        assert not f1.exists()
        assert f2.exists()
        assert f3.exists()

    def test_cli_flags_force_scan_and_no_upload(self):
        args = _parse()
        assert args.force_scan is False
        assert args.no_upload is False

        args2 = _parse("--force-scan", "--no-upload")
        assert args2.force_scan is True
        assert args2.no_upload is True

    def test_run_pipeline_no_upload_mode(self, tmp_path, monkeypatch):
        import json
        from conftest import make_png
        from tubetape import cli
        from tubetape.db import Database

        make_png(tmp_path / "img1.png", size=(10, 10))
        db_path = tmp_path / "db.json"

        # Mock transcoder to return a dummy file without needing ffmpeg
        def fake_transcode(files, out_path, config, progress=None):
            with open(out_path, "wb") as f:
                f.write(b"dummy-mp4-content")
            return out_path, [["0:00", "Start"]]

        monkeypatch.setattr(cli, "transcode_segment", fake_transcode)

        rc = cli.main(
            [
                "--no-upload",
                "--no-watch",
                "--flush",
                "--keep-segments", "1",
                "--input", str(tmp_path),
                "--db", str(db_path),
                "--segment-duration", "20s",
            ]
        )
        assert rc == 0
        db = Database.load(str(db_path))
        assert len(db.segments) == 1
        seg = list(db.segments.values())[0]
        assert seg["youtube_video_id"] is None
        assert seg["status"] == "sealed"
        assert seg["output_path"] is not None
        assert os.path.isfile(seg["output_path"])

    def test_run_pipeline_auto_skip_scan_when_paths_match(self, tmp_path, monkeypatch):
        from tubetape import cli
        from tubetape.db import Database
        from tubetape import scanner

        # Create the file on disk so disk_paths == {"img1.png"}
        (tmp_path / "img1.png").write_bytes(b"dummy")

        db_path = tmp_path / "db.json"
        db = Database(path=str(db_path))
        db.upsert_file("f1", {
            "path": "img1.png",
            "name": "img1.png",
            "type": "image",
            "captured_at_utc": "2024-01-01T00:00:00Z",
            "duration_seconds": 3.0,
            "size_bytes": 100,
        })
        db.save()

        def fake_scan_files(*args, **kwargs):
            raise AssertionError("scan_files should not be called when paths match")

        monkeypatch.setattr(scanner, "scan_files", fake_scan_files)

        rc = cli.main(
            [
                "--dry-run",
                "--no-watch",
                "--flush",
                "--input", str(tmp_path),
                "--db", str(db_path),
            ]
        )
        assert rc == 0

    def test_run_pipeline_force_scan_forces_scan_files(self, tmp_path, monkeypatch):
        from tubetape import cli
        from tubetape.db import Database
        from tubetape import scanner

        (tmp_path / "img1.png").write_bytes(b"dummy")

        db_path = tmp_path / "db.json"
        db = Database(path=str(db_path))
        db.upsert_file("f1", {
            "path": "img1.png",
            "name": "img1.png",
            "type": "image",
            "captured_at_utc": "2024-01-01T00:00:00Z",
            "duration_seconds": 3.0,
            "size_bytes": 100,
        })
        db.save()

        scanned_called = []
        orig_scan_files = scanner.scan_files

        def fake_scan_files(*args, **kwargs):
            scanned_called.append(1)
            return orig_scan_files(*args, **kwargs)

        monkeypatch.setattr(scanner, "scan_files", fake_scan_files)

        args = cli.parse_args([
            "--force-scan",
            "--dry-run",
            "--no-watch",
            "--input", str(tmp_path),
            "--db", str(db_path),
        ])
        rc = cli.run_pipeline(args)
        assert rc == 0
        assert len(scanned_called) == 1

    def test_run_pipeline_incremental_scan_on_new_file(self, tmp_path, monkeypatch):
        from tubetape import cli
        from tubetape.db import Database
        from tubetape import scanner
        from conftest import make_png

        make_png(tmp_path / "img1.png", size=(10, 10))
        make_png(tmp_path / "new_img.png", size=(10, 10))

        db_path = tmp_path / "db.json"
        db = Database(path=str(db_path))
        db.upsert_file("f1", {
            "path": "img1.png",
            "name": "img1.png",
            "type": "image",
            "captured_at_utc": "2024-01-01T00:00:00Z",
            "duration_seconds": 3.0,
            "size_bytes": 100,
        })
        db.save()

        scanned_paths = []
        orig_scan_files = scanner.scan_files

        def fake_scan_files(*args, **kwargs):
            candidates = kwargs.get("candidates") or []
            for c in candidates:
                scanned_paths.append(c[1])
            return orig_scan_files(*args, **kwargs)

        monkeypatch.setattr(scanner, "scan_files", fake_scan_files)

        rc = cli.main([
            "--dry-run",
            "--no-watch",
            "--flush",
            "--input", str(tmp_path),
            "--db", str(db_path),
        ])
        assert rc == 0
        # Only new_img.png should be scanned, img1.png is retained from DB
        assert scanned_paths == ["new_img.png"]

    def test_cli_and_db_config_precedence(self, tmp_path):
        from tubetape.coordinator import AppCoordinator
        from tubetape.db import Database

        db_path = tmp_path / "tubetape.json"
        db = Database(
            path=str(db_path),
            config={
                "crf": 22,
                "image_duration": 5.0,
                "segment_duration": 1200.0,
            },
        )
        db.save()

        # Explicitly pass --crf 28 on CLI, but omit image-duration and segment-duration
        args = parse_args([
            "--input", str(tmp_path),
            "--db", str(db_path),
            "--crf", "28",
        ])
        coord = AppCoordinator(args, db=db)
        coord.load_saved_config()

        # 1. CLI explicit flag wins over DB config
        assert coord.args.crf == 28
        # 2. DB config overrides built-in code default
        assert coord.args.image_duration == 5.0
        assert coord.args.segment_duration == 1200.0
        # 3. Non-configured parameters retain built-in code default
        assert coord.args.fps == 60

    def test_run_pipeline_saves_db_immediately_after_scan(self, tmp_path, monkeypatch):
        from conftest import make_png
        from tubetape import cli
        from tubetape.db import Database

        make_png(tmp_path / "img1.png", size=(10, 10))
        db_path = tmp_path / "tubetape.json"

        # Intercept transcode_segment to abort pipeline after scan
        def fail_transcode(*args, **kwargs):
            raise RuntimeError("Stop after scan")

        monkeypatch.setattr(cli, "transcode_segment", fail_transcode)

        args = cli.parse_args([
            "--no-upload",
            "--no-watch",
            "--flush",
            "--input", str(tmp_path),
            "--db", str(db_path),
            "--segment-duration", "20s",
        ])

        with pytest.raises(RuntimeError, match="Stop after scan"):
            cli.run_pipeline(args)

        # Database must have been saved to disk immediately after scan
        assert db_path.exists()
        loaded_db = Database.load(str(db_path))
        assert len(loaded_db.files) == 1
        assert "img1.png" in [rec["path"] for rec in loaded_db.files.values()]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))

