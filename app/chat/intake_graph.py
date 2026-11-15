"""
문진 서브그래프: 사진 확인 → 문진 LLM과 여러 턴 주고받기 → AI 서버 예측
- 바깥 그래프의 analyze 노드 안에서 실행되며, 바깥에서는 노드 하나로 보임
- 사용자에게 보낼 말은 interrupt({"reply": ...})로 내보냄
"""
import operator
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.schemas import ChatMessage, ChatRequest, ExtractedMedicalData
from app.service import chat_via_gemini, format_prediction_message, predict_via_ai_server

ASK_IMAGE_MESSAGE = "상담을 위해 먼저 피부 사진을 올려 주세요."


class IntakeState(TypedDict, total=False):
    intake: Annotated[list[dict], operator.add]   # 문진 대화만
    image_id: Optional[int]
    image_path: Optional[str]
    age: Optional[int]
    gender: str
    extracted_data: Optional[dict]
    prediction: Optional[dict]
    reply: str                                     


# ======= 노드 ===================================
def ask_image(state: IntakeState) -> dict:
    value = interrupt({"reply": ASK_IMAGE_MESSAGE})
    update = {"intake": [{"role": "model", "text": ASK_IMAGE_MESSAGE}, {"role": "user", "text": value["text"]}]}
    if value.get("image_id"):
        update["image_id"] = value["image_id"]
        update["image_path"] = value["image_path"]
    return update


async def intake_llm(state: IntakeState) -> dict:
    request = ChatRequest(
        messages=[ChatMessage(role=m["role"], text=m["text"]) for m in state["intake"]],
        age=state.get("age"),
        gender=state.get("gender") or "미상",
        image_id=state.get("image_id"),
    )
    chat = await chat_via_gemini(request)
    if chat.is_complete and chat.extracted_data is not None:
        return {"extracted_data": chat.extracted_data.model_dump(), "reply": chat.reply}
    return {"intake": [{"role": "model", "text": chat.reply}]}


def wait_answer(state: IntakeState) -> dict:
    value = interrupt({"reply": state["intake"][-1]["text"]})
    return {"intake": [{"role": "user", "text": value["text"]}]}


async def predict(state: IntakeState) -> dict:
    prediction = await predict_via_ai_server(state["image_path"], ExtractedMedicalData(**state["extracted_data"]))
    return {
        "prediction": prediction.model_dump(),
        "reply": f"{state['reply']}\n\n{format_prediction_message(prediction)}",
    }


# ======= 라우팅 ===================================
def route_image(state: IntakeState) -> str:
    return "intake_llm" if state.get("image_path") else "ask_image"


def route_after_intake_llm(state: IntakeState) -> str:
    return "predict" if state.get("extracted_data") else "wait_answer"


def build_intake_graph() -> StateGraph:
    g = StateGraph(IntakeState)
    g.add_node("ask_image", ask_image)
    g.add_node("intake_llm", intake_llm)
    g.add_node("wait_answer", wait_answer)
    g.add_node("predict", predict)

    g.add_conditional_edges(START, route_image, ["intake_llm", "ask_image"])
    g.add_conditional_edges("ask_image", route_image, ["intake_llm", "ask_image"])
    g.add_conditional_edges("intake_llm", route_after_intake_llm, ["predict", "wait_answer"])
    g.add_edge("wait_answer", "intake_llm")
    g.add_edge("predict", END)
    return g


# 체크포인터는 바깥 그래프의 것을 그대로 사용
intake_graph = build_intake_graph().compile()
