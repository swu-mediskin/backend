import asyncio
import logging
import operator
import os
from typing import Annotated, Optional, TypedDict

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from .info_answer import classify_intent, generate_info_answer
from .intake_graph import intake_graph
from .tools import search_info as run_search_info

NEW_SESSION_MESSAGE = "이 상담은 분석이 끝났어요. 다른 사진 분석은 새 상담에서 진행해 주세요."

# 개발 로그 (uvicorn 로그에 함께 출력: docker logs -f mediskin_backend)
logger = logging.getLogger("uvicorn.error")


class ChatState(TypedDict, total=False):
    image_id: Optional[int]
    image_path: Optional[str]
    image_sent: bool            # 이번 요청에 사진이 함께 왔는지
    age: Optional[int]
    gender: str
    messages: Annotated[list[dict], operator.add]
    intent: str                 # router가 고른 의도 (info / intake)
    extracted_data: Optional[dict]
    prediction: Optional[dict]
    tool_trace: list[dict]      # 이번 턴의 툴 호출·결과
    pending_calls: list[dict]   # info_answer가 요청한 툴 호출


# ======= 노드 ===================================
def user_input(state: ChatState) -> dict:
    messages = state.get("messages", [])
    value = interrupt({"reply": messages[-1]["text"] if messages else ""})

    update = {"messages": [{"role": "user", "text": value["text"]}], "image_sent": bool(value.get("image_id"))}
    
    if value.get("image_id") and not state.get("prediction"):
        update["image_id"] = value["image_id"]
        update["image_path"] = value["image_path"]
    return update


async def router(state: ChatState) -> dict:
    if state.get("prediction") and state.get("image_sent"):
        intent = "intake"
    else:
        intent = await classify_intent(state["messages"])
    logger.info("[chat] router → %s", intent)
    return {"intent": intent}


async def analyze(state: ChatState) -> dict:
    """문진 서브그래프 실행. 사용자와 여러 턴 주고받지만 바깥에서는 노드 하나"""
    if state.get("prediction"):
        return {"messages": [{"role": "model", "text": NEW_SESSION_MESSAGE}]}

    out = await intake_graph.ainvoke({
        "intake": [{"role": "user", "text": state["messages"][-1]["text"]}],
        "image_id": state.get("image_id"),
        "image_path": state.get("image_path"),
        "age": state.get("age"),
        "gender": state.get("gender"),
    })
    
    return {
        "messages": out["intake"][1:] + [{"role": "model", "text": out["reply"], "prediction": out["prediction"]}],
        "image_id": out["image_id"],
        "image_path": out["image_path"],
        "extracted_data": out["extracted_data"],
        "prediction": out["prediction"],
    }


async def info_answer(state: ChatState) -> dict:
    """정보 질문에 답변. 정보가 필요하면 search_info 툴 호출을 요청 (분석 결과와 무관)"""
    content, calls, text = await generate_info_answer(state["messages"], state.get("tool_trace", []))
    if calls:
        logger.info("[chat] info_answer → 툴 요청 %s", [f'{c["name"]}({c["args"]})' for c in calls])
        return {"tool_trace": state.get("tool_trace", []) + [content], "pending_calls": calls}
    logger.info("[chat] info_answer → 답변 완료")
    return {"messages": [{"role": "model", "text": text}], "tool_trace": [], "pending_calls": []}


def search_info(state: ChatState) -> dict:
    """info_answer가 요청한 search_info 호출을 실행하고 결과를 tool_trace에 추가"""
    parts = []
    for c in state["pending_calls"]:
        result = run_search_info(**c["args"])
        logger.info("[chat] search_info(%s) → found=%s", c["args"], result["found"])
        parts.append({"function_response": {"name": c["name"], "response": result}})
    return {"tool_trace": state["tool_trace"] + [{"role": "user", "parts": parts}], "pending_calls": []}


# ======= 라우팅 ===================================
def route_after_router(state: ChatState) -> str:
    return "info_answer" if state["intent"] == "info" else "analyze"


def route_after_info_answer(state: ChatState) -> str:
    return "search_info" if state.get("pending_calls") else "user_input"


def build_graph() -> StateGraph:
    g = StateGraph(ChatState)
    g.add_node("user_input", user_input)
    g.add_node("router", router)
    g.add_node("analyze", analyze)
    g.add_node("info_answer", info_answer)
    g.add_node("search_info", search_info)

    g.add_edge(START, "user_input")
    g.add_edge("user_input", "router")
    g.add_conditional_edges("router", route_after_router, ["info_answer", "analyze"])
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
