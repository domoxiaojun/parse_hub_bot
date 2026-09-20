"""Fork caption rules: no body truncation, Rich markdown without ``<details>``, Telegraph client cleanup."""

from markdown import markdown
from parsehub.types import AnyParseResult, RichTextParseResult
from pyrogram import Client

from log import logger
from plugins.helpers import format_label, replace_url
from repo.settings import SettingsConfig
from utils.converter import clean_article_html
from utils.ph import Telegraph

logger = logger.bind(name="Captions")


def format_text(text: str) -> str:
    """保留完整内容并为长正文添加折叠块样式。"""
    text = text.strip()
    if len(text) > 500 or len(text.splitlines()) > 10:
        return f"<blockquote expandable>{text}</blockquote>"
    return text


def build_caption_by_str(
    title: str | None,
    content: str | None,
    raw_url: str,
    telegraph_url: str | None = None,
    *,
    hide_source: bool = False,
    custom_content: str = "",
    hide_title: bool = False,
    hide_desc: bool = False,
    rich: bool = False,
) -> str:
    """构建消息正文：标题 + 内容 + 来源链接"""
    title, content = title or "", content or ""
    if rich:
        parts = []
        if title and not hide_title:
            parts.append(f"**{title}**")
        if content and not hide_desc:
            parts.append(content)
        body = "\n\n".join(parts)
    elif telegraph_url:
        label = (title or content[:15]).replace("\n", " ") or "-"
        body = f"**[{label}]({telegraph_url})**"
    else:
        parts = []
        if not hide_title and title:
            parts.append(f"**{title}**")
        if not hide_desc and content:
            parts.append(content)
        body = format_text(("\n\n".join(parts)).strip())

    if custom_content:
        body += f"\n\n{custom_content}"

    if hide_source:
        return body
    return f"{body}\n\n{format_label(f"<a href='{raw_url}'>Source</a>")}"


def build_caption(
    parse_result: AnyParseResult,
    telegraph_url: str | None = None,
    *,
    custom_content: str = "",
    config: SettingsConfig,
    rich: bool = False,
) -> str:
    return build_caption_by_str(
        parse_result.title,
        replace_url(parse_result.platform, parse_result.markdown_content)
        if rich and isinstance(parse_result, RichTextParseResult)
        else parse_result.content,
        parse_result.raw_url,
        telegraph_url,
        hide_source=config.hide_source,
        custom_content=custom_content,
        hide_title=config.hide_title,
        hide_desc=config.hide_desc,
        rich=rich,
    )


async def _close_telegraph(telegraph: Telegraph) -> None:
    """Release the underlying HTTP client after page publication."""
    session = getattr(getattr(telegraph.telegraph, "_telegraph", None), "session", None)
    if session is not None:
        try:
            await session.aclose()
        except Exception as e:
            logger.debug(f"Telegraph 客户端关闭失败: {type(e).__name__}")


async def create_telegraph_page(html_content: str, cli: Client, parse_result: AnyParseResult) -> str:
    """创建 Telegraph 页面，返回页面 URL"""
    logger.debug(f"创建 Telegraph 页面: title={parse_result.title}")
    me = await cli.get_me()
    telegraph = Telegraph()
    try:
        page = await telegraph.create_page(
            parse_result.title or "-",
            html_content=html_content,
            author_name=f"{me.full_name} | @{me.username}",
            author_url=parse_result.raw_url,
        )
        logger.debug(f"Telegraph 页面已创建: {page.url}")
        return page.url
    finally:
        await _close_telegraph(telegraph)


async def create_richtext_telegraph(cli: Client, parse_result: RichTextParseResult) -> str:
    """将富文本解析结果转换为 Telegraph 页面，返回页面 URL"""
    logger.debug(f"富文本转 Telegraph: platform={parse_result.platform}, md_len={len(parse_result.markdown_content)}")
    md = replace_url(parse_result.platform, parse_result.markdown_content)
    html = clean_article_html(markdown(md))
    return await create_telegraph_page(html, cli, parse_result)
