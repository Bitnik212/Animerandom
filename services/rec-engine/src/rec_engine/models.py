"""Response schemas. IDs, scores and reason codes only; never display text."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

ReasonCode = Literal["similar_to", "liked_by_similar_users", "popular_in_genre", "popular"]


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ReasonCode
    anime_id: int | None = None  # similar_to
    genre: str | None = None  # popular_in_genre

    @model_serializer(mode="wrap")
    def _drop_unused(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Only the field that belongs to the code is sent."""
        data: dict[str, Any] = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class RecItem(BaseModel):
    anime_id: int
    score: float
    reason: Reason


class ModelInfo(BaseModel):
    als: str | None
    embedding: str


class RecResponse(BaseModel):
    items: list[RecItem]
    model: ModelInfo


class SimilarItem(BaseModel):
    anime_id: int
    score: float


class SimilarResponse(BaseModel):
    items: list[SimilarItem]


class Health(BaseModel):
    status: Literal["ok"]
    als_model: str | None
    embedding_model: str
