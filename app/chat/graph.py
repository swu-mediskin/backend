import asyncio
import operator
import os
from typing import Annotated, Optional, TypedDict

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.schemas import ChatMessage, DiagnosisRequest
from app.service import chat_via_gemini, format_prediction_message, predict_via_ai_server

INTAKE = "INTAKE"   # 문진 중
RESULT = "RESULT"   # 분석 완료


class ChatState(TypedDict, total=False):
    image_id: int   # image_id = 대화 구분값
    image_path: str
    age: Optional[int]
    gender: str
    messages: Annotated[list[dict], operator.add]
    phase: str
    extracted_data: Optional[dict]
    prediction: Optional[dict]


# ======= 노드 ===================================
def user_input(state: ChatState) -> dict:
    text = interrupt({"phase": state.get("phase", INTAKE)})
    return {"messages": [{"role": "user", "text": text}]}


async def analyze(state: ChatState) -> dict:
    request = DiagnosisRequest(
        messages=[ChatMessage(role=m["role"], text=m["text"]) for m in state["messages"]],
        age=state.get("age"),
        gender=state.get("gender") or "미상",
        image_id=state["image_id"],
    )

    chat = await chat_via_gemini(request)
    if not chat.is_complete or chat.extracted_data is None:
        return {"messages": [{"role": "model", "text": chat.reply}]}

    prediction = await predict_via_ai_server(state["image_path"], chat.extracted_data)
    return {
        "messages": [{"role": "model", "text": f"{chat.reply}\n\n{format_prediction_message(prediction)}"}],
        "phase": RESULT,
        "extracted_data": chat.extracted_data.model_dump(),
        "prediction": prediction.model_dump(),
    }


# ======= 라우팅 ===================================
def route_after_analyze(state: ChatState) -> str:
    return END if state.get("phase") == RESULT else "user_input"


def build_graph() -> StateGraph:
    g = StateGraph(ChatState)
    g.add_node("user_input", user_input)
    g.add_node("analyze", analyze)

    g.add_edge(START, "user_input")
    g.add_edge("user_input", "analyze")
    g.add_conditional_edges("analyze", route_after_analyze, ["user_input", END])
    return g


# 체크포인터
CHECKPOINT_PATH = os.getenv("CHAT_CHECKPOINT_PATH", "data/chat_checkpoints.sqlite")
_graph = None
_lock = asyncio.Lock()


async def get_graph():
    global _graph
    async with _lock:
        if _graph is None:
            os.makedirs(os.path.dirname(CHECKPOINT_PATH) or ".", exist_ok=True)
            conn = await aiosqlite.connect(CHECKPOINT_PATH)
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            _graph = build_graph().compile(checkpointer=saver)
    return _graph
