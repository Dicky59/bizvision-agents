"""Wiring: the pieces `bv chat`, the evals and (M5) the API share."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant.graph import Assistant, AssistantDeps, Retriever
from agents.assistant.service import ChatService
from core.embeddings.client import EmbeddingClient
from core.llm.client import LLMClient
from core.rag.retrieve import RetrievedChunk, retrieve
from core.tenants.config import Lang

SessionMaker = async_sessionmaker[AsyncSession]


def make_retriever(sessionmaker: SessionMaker, embedder: EmbeddingClient) -> Retriever:
    async def retriever(
        tenant_id: uuid.UUID,
        query: str,
        language: Lang,
        top_k: int,
        conversation_id: uuid.UUID | None,
    ) -> list[RetrievedChunk]:
        async with sessionmaker() as session:
            return await retrieve(
                session,
                embedder,
                tenant_id,
                query,
                language,
                top_k,
                conversation_id=conversation_id,
            )

    return retriever


def make_chat_service(
    llm: LLMClient, embedder: EmbeddingClient, sessionmaker: SessionMaker
) -> ChatService:
    deps = AssistantDeps(llm=llm, retriever=make_retriever(sessionmaker, embedder))
    return ChatService(Assistant(deps), sessionmaker)
