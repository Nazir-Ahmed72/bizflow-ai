from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent.llm import OrderLLM
from app.agent.service import OrderAgent, postgres_checkpointer
from app.api.finance import router as finance_router
from app.api.staff import router as staff_router
from app.tools import ConversationBusy


class ChatRequest(BaseModel):
    phone: str = Field(min_length=5, max_length=40, examples=["+92 300 1234567"])
    message: str = Field(min_length=1, max_length=2000, examples=["Send 10 cartons of Dalda oil"])


class ChatResponse(BaseModel):
    thread_id: str
    messages: list[str]
    waiting_for: Optional[Literal["product_choice", "stock_choice", "confirmation"]]
    status: Optional[str]
    order_number: Optional[str]
    invoice_number: Optional[str]


class ChatMessage(BaseModel):
    id: int
    direction: Literal["in", "out"]
    text: str


class ConversationResponse(BaseModel):
    thread_id: str
    waiting_for: Optional[str]
    messages: list[ChatMessage]


def create_app(llm: Optional[OrderLLM] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if llm is None:
            from app.agent.llm import GroqLLM
            model = GroqLLM()
        else:
            model = llm
        with postgres_checkpointer() as saver:
            app.state.agent = OrderAgent(model, saver)
            yield

    app = FastAPI(title="BizFlow AI", version="0.1.0", lifespan=lifespan,
                  description="Order agent for a Karachi FMCG distributor.")

    app.include_router(staff_router)
    app.include_router(finance_router)

    def get_agent(request: Request) -> OrderAgent:
        return request.app.state.agent

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, agent: OrderAgent = Depends(get_agent)) -> ChatResponse:
        try:
            reply = agent.handle_message(body.phone, body.message)
        except ConversationBusy:
            raise HTTPException(status_code=409, detail="Another message from this number is still being processed.")
        return ChatResponse(thread_id=reply.thread_id, messages=reply.messages, waiting_for=reply.waiting_for,
                            status=reply.status, order_number=reply.order_number,
                            invoice_number=reply.invoice_number)

    @app.get("/conversations/{phone}", response_model=ConversationResponse)
    def conversation(phone: str, limit: int = 50, agent: OrderAgent = Depends(get_agent)) -> ConversationResponse:
        return ConversationResponse(**agent.conversation(phone, limit=min(max(limit, 1), 200)))

    return app


app = create_app()
