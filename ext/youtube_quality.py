"""Keep the mweb workaround and use the same 1080p policy for parsing and downloading."""

from __future__ import annotations

YOUTUBE_PLAYER_CLIENT = "mweb"
EXTRACTOR_ARGS = f"youtube:player_client={YOUTUBE_PLAYER_CLIENT}"
# Prefer the best <=1080p; if none exists, yt-dlp chooses the smallest higher rendition.
# Resolution wins over codec and file size; within a resolution prefer fps, H.264, then bitrate.
FORMAT_SORT = "res:1080,fps,+codec:h264,br"


def _with_player_client(args: list[str]) -> list[str]:
    for index in range(len(args) - 1):
        if args[index] != "--extractor-args" or not args[index + 1].startswith("youtube:"):
            continue
        if "player_client=" not in args[index + 1]:
            args[index + 1] = f"{args[index + 1]};player_client={YOUTUBE_PLAYER_CLIENT}"
        return args
    return [*args, "--extractor-args", EXTRACTOR_ARGS]


def download_cli_args(args: list[str]) -> list[str]:
    """Replace upstream's codec/500MB preference with the user-selected resolution policy."""
    result: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-S", "--format-sort"}:
            index += 2
            continue
        if not (
            arg.startswith("--format-sort=")
            or arg in {"--format-sort-force", "--no-format-sort-force"}
        ):
            result.append(arg)
        index += 1
    return [*result, "-S", FORMAT_SORT, "--format-sort-force"]


def extend_cli_args(args: list[str]) -> list[str]:
    """Apply the player client at parse time and the shared format policy at both stages."""
    return download_cli_args(_with_player_client(list(args)))
