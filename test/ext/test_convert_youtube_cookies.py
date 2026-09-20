from __future__ import annotations

import json

import pytest

from scripts.convert_youtube_cookies import convert_netscape, serialize_cookies, write_private


def test_convert_netscape_filters_domains_and_keeps_httponly() -> None:
    source = """# Netscape HTTP Cookie File
.google.com\tTRUE\t/\tTRUE\t0\tignored\tsecret
.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tvalue
#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\taccount
www.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tnew-value
"""

    cookies = convert_netscape(source)

    assert cookies == {"PREF": "new-value", "SID": "account"}
    assert json.loads(serialize_cookies(cookies, "json")) == cookies
    assert serialize_cookies(cookies, "header") == "PREF=new-value; SID=account\n"


def test_convert_netscape_rejects_malformed_rows() -> None:
    with pytest.raises(ValueError, match="第 2 行"):
        convert_netscape("# Netscape HTTP Cookie File\nnot-a-cookie-row\n")


def test_write_private_sets_mode_600(tmp_path) -> None:
    output = tmp_path / "youtube-cookie.json"

    write_private(output, '{"PREF":"value"}\n')

    assert output.read_text(encoding="utf-8") == '{"PREF":"value"}\n'
    assert output.stat().st_mode & 0o777 == 0o600
