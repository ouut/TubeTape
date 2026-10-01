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
        assert args.segment_duration == 1200.0
        assert args.crf == 18
        assert args.max_resolution == (3840, 2160)
        assert args.ken_burns is False
        assert args.privacy == "private"
        assert args.no_rebuild is False
        assert args.rebuild_cooldown == 86400.0
        assert args.flush is False
        assert args.watch is True
        assert args.dry_run is False
        assert args.timezone is not None

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
            "--no-rebuild",
            "--rebuild-cooldown", "1h",
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
        assert args.no_rebuild is True
        assert args.rebuild_cooldown == 3600.0
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
