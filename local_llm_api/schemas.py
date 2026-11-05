from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class StreamOptions(StrictModel):
    include_usage: bool = False


class ChatRequest(StrictModel):
    model: str | None = Field(default=None, min_length=1, max_length=256)
    messages: list[Message] = Field(min_length=1, max_length=128)
    max_tokens: int = Field(default=512, ge=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=1.0, gt=0, le=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    stop: str | list[str] | None = None
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def check_options(self):
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        stops = [self.stop] if isinstance(self.stop, str) else self.stop
        if stops is not None and (not 1 <= len(stops) <= 4 or any(not s for s in stops)):
            raise ValueError("stop must contain between one and four nonempty strings")
        return self
