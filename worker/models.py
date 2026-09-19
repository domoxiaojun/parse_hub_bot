"""Validated, versioned HTTP inputs; credentials are never returned by the API."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageDelivery(InputModel):
    surface: Literal["message"]
    chatId: str = Field(pattern=r"^-?[1-9][0-9]{0,19}$")
    replyToMessageId: int | None = Field(default=None, gt=0, le=2147483647)
    messageThreadId: int | None = Field(default=None, gt=0, le=2147483647)
    silent: bool = False
    protect: bool = False


class InlineDelivery(InputModel):
    surface: Literal["inline", "guest"]
    inlineMessageId: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_=-]+$")


DeliveryTarget = Annotated[MessageDelivery | InlineDelivery, Field(discriminator="surface")]


class JobInput(InputModel):
    text: str = Field(min_length=1, max_length=32768)
    accountId: str = Field(min_length=1, max_length=32)
    mode: Literal["auto", "read_only"] = "auto"
    outputMode: Literal["preview", "raw", "zip"] = "preview"
    refresh: bool = False
    requestId: str = Field(min_length=1, max_length=128)
    idempotencyKey: str = Field(min_length=1, max_length=128)
    delivery: DeliveryTarget | None = None


class ProxyDefaults(InputModel):
    parser_proxies: list[str] = Field(default_factory=list, max_length=256)
    downloader_proxies: list[str] = Field(default_factory=list, max_length=256)


class PlatformConfig(ProxyDefaults):
    cookies: list[str] = Field(default_factory=list, max_length=256)


class ConfigInput(InputModel):
    version: str = Field(min_length=1, max_length=128)
    defaults: ProxyDefaults = Field(default_factory=ProxyDefaults)
    platforms: dict[str, PlatformConfig] = Field(default_factory=dict, max_length=256)
