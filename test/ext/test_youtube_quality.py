"""YouTube parse-stage player client: the hook must yield arguments yt-dlp itself accepts."""

from types import SimpleNamespace
from typing import Any

import pytest
import yt_dlp
from parsehub.parsers.parser.youtube import YtbParse, YtbVideoParseResult
from parsehub.types import VideoRef

from ext.hooks import install_youtube_client
from ext.youtube_quality import EXTRACTOR_ARGS, FORMAT_SORT, YOUTUBE_PLAYER_CLIENT, download_cli_args, extend_cli_args

PARSEHUB_PARSE_ARGS = [
    "--quiet",
    "--no-progress",
    "--no-playlist",
    "--dump-single-json",
    "--no-download",
    "--no-warnings",
]
SORT_ARGS = ["-S", FORMAT_SORT, "--format-sort-force"]


def test_extend_appends_mweb_player_client_and_sort() -> None:
    assert YOUTUBE_PLAYER_CLIENT == "mweb"
    assert extend_cli_args([]) == ["--extractor-args", EXTRACTOR_ARGS, *SORT_ARGS]
    assert extend_cli_args(PARSEHUB_PARSE_ARGS) == [
        *PARSEHUB_PARSE_ARGS,
        "--extractor-args",
        EXTRACTOR_ARGS,
        *SORT_ARGS,
    ]


def test_extend_merges_into_existing_youtube_extractor_args() -> None:
    merged = extend_cli_args(["--extractor-args", "youtube:lang=zh-Hans", "--no-warnings"])
    assert merged == ["--extractor-args", "youtube:lang=zh-Hans;player_client=mweb", "--no-warnings", *SORT_ARGS]
    explicit = ["--extractor-args", "youtube:player_client=web_safari", *SORT_ARGS]
    assert extend_cli_args(explicit) == explicit
    other = ["--extractor-args", "bilibili:prefer_multi_flv", "--format-sort", "res"]
    assert extend_cli_args(other) == [
        "--extractor-args", "bilibili:prefer_multi_flv", "--extractor-args", EXTRACTOR_ARGS, *SORT_ARGS,
    ]


def test_extend_does_not_mutate_input() -> None:
    args = list(PARSEHUB_PARSE_ARGS)
    extend_cli_args(args)
    assert args == PARSEHUB_PARSE_ARGS


def test_parse_stage_sort_matches_parsehub_download_stage() -> None:
    install_youtube_client()
    download_args = YtbVideoParseResult(dl=SimpleNamespace(), video=VideoRef(url="https://youtu.be/x")).cli_args
    assert download_args[download_args.index("-S") + 1] == FORMAT_SORT
    assert download_cli_args(download_args) == download_args
    assert download_cli_args(["--quiet", "--format-sort=res", "--no-format-sort-force"]) == ["--quiet", *SORT_ARGS]


def test_yt_dlp_parses_extended_args() -> None:
    _, _, _, ydl_opts = yt_dlp.parse_options(extend_cli_args(PARSEHUB_PARSE_ARGS))
    assert ydl_opts["extractor_args"] == {"youtube": {"player_client": ["mweb"]}}
    assert ydl_opts["format_sort"] == ["res:1080", "fps", "+codec:h264", "br"]
    assert ydl_opts["format_sort_force"]
    _, _, _, merged = yt_dlp.parse_options(extend_cli_args(["--extractor-args", "youtube:lang=zh-Hans"]))
    assert merged["extractor_args"] == {"youtube": {"lang": ["zh-Hans"], "player_client": ["mweb"]}}


def format_info(name: str, height: int, *, codec: str = "avc1.640028", size: int = 500) -> dict[str, Any]:
    return {
        "format_id": name, "url": f"https://example.com/{name}.mp4", "ext": "mp4", "protocol": "https",
        "vcodec": codec, "acodec": "mp4a.40.2", "width": height * 16 // 9, "height": height,
        "fps": 30, "filesize": size * 1024**2, "tbr": 1000,
    }


@pytest.mark.parametrize("formats,expected", [
    ([format_info("720", 720), format_info("1080", 1080, size=1200), format_info("4k", 2160)], "1080"),
    ([format_info("720", 720), format_info("1080-av1", 1080, codec="av01.0.12M.08")], "1080-av1"),
    ([format_info("1080-h264", 1080), format_info("1080-av1", 1080, codec="av01.0.12M.08")], "1080-h264"),
    ([format_info("720", 720), format_info("4k", 2160)], "720"),
    ([format_info("1440", 1440), format_info("4k", 2160)], "1440"),
    ([{**format_info("portrait-1080", 1080), "width": 1080, "height": 1920},
      {**format_info("portrait-4k", 2160), "width": 2160, "height": 3840}], "portrait-1080"),
])
def test_real_yt_dlp_selects_same_1080p_policy_at_both_stages(formats: list[dict[str, Any]], expected: str) -> None:
    install_youtube_client()
    download = YtbVideoParseResult(dl=SimpleNamespace(), video=VideoRef(url="https://youtu.be/fixture"))
    for args in (YtbParse().cli_args, download.cli_args):
        _, _, _, options = yt_dlp.parse_options(args)
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True,
            "format_sort": options["format_sort"], "format_sort_force": options["format_sort_force"],
        }) as ydl:
            info = {"formats": [dict(f) for f in formats], "_format_sort_fields": ["vcodec", "res"]}
            ydl.sort_formats(info)
            assert info["formats"][-1]["format_id"] == expected
