import asyncio
import operator
import os
from typing import Annotated, Optional, TypedDict

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from app.schemas import ChatMessage, DiagnosisRequest
from app.service import chat_via_gemini, format_prediction_message, predict_via_ai_server

from .info_answer import generate_info_answer
from .tools import search_info as run_search_info

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
    tool_trace: list[dict]      # 이번 턴의 툴 호출·결과
    pending_calls: list[dict]   # info_answer가 요청한 툴 호출


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


async def info_answer(state: ChatState) -> dict:
    """정보 질문에 답변. 정보가 필요하면 search_info 툴 호출을 요청 (분석 결과와 무관)"""
    content, calls, text = await generate_info_answer(state["messages"], state.get("tool_trace", []))
    if calls:
        return {"tool_trace": state.get("tool_trace", []) + [content], "pending_calls": calls}
    return {"messages": [{"role": "model", "text": text}], "tool_trace": [], "pending_calls": []}


def search_info(state: ChatState) -> dict:
    """info_answer가 요청한 search_info 호출을 실행하고 결과를 tool_trace에 추가"""
    parts = [
        {"function_response": {"name": c["name"], "response": run_search_info(**c["args"])}}
        for c in state["pending_calls"]
    ]
    return {"tool_trace": state["tool_trace"] + [{"role": "user", "parts": parts}], "pending_calls": []}


# ======= 라우팅 ===================================
def route_after_input(state: ChatState) -> str:
    # 분석 전에는 문진(analyze), 분석 후에는 정보 답변(info_answer)
    return "info_answer" if state.get("phase") == RESULT else "analyze"


def route_after_info_answer(state: ChatState) -> str:
    return "search_info" if state.get("pending_calls") else "user_input"


def build_graph() -> StateGraph:
    g = StateGraph(ChatState)
    g.add_node("user_input", user_input)
    g.add_node("analyze", analyze)
    g.add_node("info_answer", info_answer)
    g.add_node("search_info", search_info)

    g.add_edge(START, "user_input")
    g.add_conditional_edges("user_input", route_after_input, ["analyze", "info_answer"])
    g.add_edge("analyze", "user_input")
    g.add_conditional_edges("info_answer", route_after_info_answer, ["search_info", "user_input"])
    g.add_edge("search_info", "info_answer")
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
