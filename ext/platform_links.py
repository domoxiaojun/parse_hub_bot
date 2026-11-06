"""Structured host checks and explicit legacy entry points for registered providers."""

from urllib.parse import urlsplit, urlunsplit

DOMAINS = {
    "weixin": ("mp.weixin.qq.com",), "twitter": ("x.com", "twitter.com", "fixupx.com", "t.co"),
    "youtube": ("youtube.com", "youtu.be"), "bilibili": ("bilibili.com", "b23.tv", "bili2233.cn"),
    "douyin": ("douyin.com",), "tiktok": ("tiktok.com",), "instagram": ("instagram.com",),
    "threads": ("threads.com", "threads.net"), "weibo": ("weibo.com", "weibo.cn"),
    "zhihu": ("zhihu.com",), "xhs": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
    "kuaishou": ("kuaishou.com", "chenzhongtech.com"), "facebook": ("facebook.com",),
    "snapchat": ("snapchat.com",), "coolapk": ("coolapk.com",), "tieba": ("tieba.baidu.com",),
    "douban": ("douban.com", "douc.cc"), "xiaoheihe": ("xiaoheihe.cn",),
    "pipix": ("pipix.com",), "zuiyou": ("xiaochuankeji.cn", "izuiyou.com"),
    "bangumi": ("bgm.tv", "bangumi.tv", "chii.in"), "medium": ("medium.com",),
    "toutiao": ("toutiao.com",), "weixin_channels": ("weixin.qq.com",),
}


def valid_host(url: str, platform: str) -> bool:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        return (p.scheme in {"http", "https"} and not p.username and not p.password and p.port in {None, 80, 443}
                and any(host == d or host.endswith("." + d) for d in DOMAINS.get(platform, ())))
    except ValueError:
        return False


def normalize(url: str) -> str:
    p = urlsplit(url)
    if p.hostname in {"threads.net", "www.threads.net"}:
        return urlunsplit(("https", "www.threads.com", p.path, p.query, p.fragment))
    return url


def extra_match(url: str, platform: str) -> bool:
    p = urlsplit(url)
    if platform == "weixin":
        return p.path == "/s" and bool(p.query)
    if platform == "twitter":
        return p.hostname == "t.co" and bool(p.path.strip("/"))
    if platform == "youtube":
        return p.path.startswith("/live/") and bool(p.path.removeprefix("/live/"))
    return False


def advertised_patterns(platform: str, original: str) -> list[str]:
    patterns = [original]
    if platform == "weixin":
        patterns.append(r"https?://mp\.weixin\.qq\.com/s\?.+")
    elif platform == "twitter":
        patterns.append(r"https?://t\.co/[^/?#]+(?:[?#].*)?")
    elif platform == "youtube":
        patterns.append(r"https?://(?:www\.|m\.)?youtube\.com/live/[^/?#]+(?:[?#].*)?")
    elif platform == "threads":
        patterns.append(r"https?://(?:www\.)?threads\.net/@[^/]+/post/.+")
    return patterns
