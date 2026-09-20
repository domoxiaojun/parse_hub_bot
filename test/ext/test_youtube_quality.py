"""YouTube parse-stage player client: the hook must yield arguments yt-dlp itself accepts."""

from types import SimpleNamespace

import yt_dlp
from parsehub.parsers.parser.youtube import YtbVideoParseResult
from parsehub.types import VideoRef

from ext.youtube_quality import EXTRACTOR_ARGS, FORMAT_SORT, YOUTUBE_PLAYER_CLIENT, extend_cli_args

PARSEHUB_PARSE_ARGS = [
    "--quiet",
    "--no-progress",
    "--no-playlist",
    "--dump-single-json",
    "--no-download",
    "--no-warnings",
]
SORT_ARGS = ["-S", FORMAT_SORT]


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
    assert extend_cli_args(other) == [*other, "--extractor-args", EXTRACTOR_ARGS]


def test_extend_does_not_mutate_input() -> None:
    args = list(PARSEHUB_PARSE_ARGS)
    extend_cli_args(args)
    assert args == PARSEHUB_PARSE_ARGS


def test_parse_stage_sort_matches_parsehub_download_stage() -> None:
    download_args = YtbVideoParseResult(dl=SimpleNamespace(), video=VideoRef(url="https://youtu.be/x")).cli_args
    assert download_args[download_args.index("-S") + 1] == FORMAT_SORT


def test_yt_dlp_parses_extended_args() -> None:
    _, _, _, ydl_opts = yt_dlp.parse_options(extend_cli_args(PARSEHUB_PARSE_ARGS))
    assert ydl_opts["extractor_args"] == {"youtube": {"player_client": ["mweb"]}}
    assert ydl_opts["format_sort"] == ["+codec:h264", "filesize~500M"]
    _, _, _, merged = yt_dlp.parse_options(extend_cli_args(["--extractor-args", "youtube:lang=zh-Hans"]))
    assert merged["extractor_args"] == {"youtube": {"lang": ["zh-Hans"], "player_client": ["mweb"]}}
