"""YouTube player-client override so Premium sessions are not limited to SABR-only 360p.

yt-dlp (2026.08) picks the ``web_creator`` / ``tv_downgraded`` / ``web`` clients for a Premium
cookie session. YouTube serves those clients SABR-only streams, every https format is skipped
for "missing a URL" and the info.json keeps just the progressive 360p format 18. ParseHub's
download stage replays that info.json with ``--load-info-json``, so its ``-S +codec:h264``
sort never sees a 1080p candidate. ``mweb`` still returns https/DASH formats up to 2160p and
needs no PO Token for Premium accounts, so the parse stage asks for it explicitly.

The parse stage also gets the download stage's ``-S`` sort: ``--dump-single-json`` reports the
top-level ``width``/``height`` of the format yt-dlp *would* select, and ParseHub turns those into
the ``VideoRef``/``VideoFile`` dimensions. Without the same sort the parse stage would describe
the 2160p AV1 pick while the download stage fetches 1080p h264.
"""

from __future__ import annotations

YOUTUBE_PLAYER_CLIENT = "mweb"
EXTRACTOR_ARGS = f"youtube:player_client={YOUTUBE_PLAYER_CLIENT}"
# Must stay equal to the ``-S`` value in ``YtbVideoParseResult.cli_args``; test_youtube_quality.py checks it.
FORMAT_SORT = "+codec:h264,filesize~500M"


def _with_player_client(args: list[str]) -> list[str]:
    for index in range(len(args) - 1):
        if args[index] != "--extractor-args" or not args[index + 1].startswith("youtube:"):
            continue
        if "player_client=" not in args[index + 1]:
            args[index + 1] = f"{args[index + 1]};player_client={YOUTUBE_PLAYER_CLIENT}"
        return args
    return [*args, "--extractor-args", EXTRACTOR_ARGS]


def extend_cli_args(args: list[str]) -> list[str]:
    """Return ``args`` with the YouTube player client and the download-stage format sort.

    An existing ``--extractor-args youtube:...`` value is merged rather than shadowed (yt-dlp keeps
    only the last value per extractor key), an explicit ``player_client`` wins over ours, and an
    explicit ``-S``/``--format-sort`` is left alone.
    """
    result = _with_player_client(list(args))
    if "-S" not in result and "--format-sort" not in result:
        result += ["-S", FORMAT_SORT]
    return result
