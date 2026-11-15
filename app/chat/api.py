from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from langgraph.types import Command
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.schemas import ChatRequest, ChatSessionResponse

from .graph import get_graph
from .info_answer import answer_question

router = APIRouter(prefix="/chat", tags=["Chat"])


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


@router.post("", response_model=ChatSessionResponse)
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    image = None
    if request.image_id is not None:
        image = db.get(models.SkinImage, request.image_id)
        if image is None:
            raise HTTPException(status_code=404, detail="해당 이미지를 찾을 수 없습니다. 이미지를 먼저 업로드해 주세요.")

    graph = await get_graph()
    analyzed_before = False   # 이번 요청 전에 이미 분석이 끝났는지

    if request.session_id:
        session_id = request.session_id
        config = _config(session_id)
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
        analyzed_before = snapshot.values.get("prediction") is not None
    else:
        # 첫 요청: 새 세션을 만들고 사용자 입력 대기 상태까지 실행
        session_id = uuid4().hex
        config = _config(session_id)
        await graph.ainvoke({"age": request.age, "gender": request.gender}, config)

    # 전체 대화 중 마지막 user 메시지만 그래프에 전달 (프론트와의 용이한 연결을 위함)
    values = await graph.ainvoke(
        Command(resume={
            "text": request.messages[-1].text,
            "image_id": image.id if image else None,
            "image_path": image.file_path if image else None,
        }),
        config,
    )

    just_completed = not analyzed_before and values.get("prediction") is not None

    return ChatSessionResponse(
        session_id=session_id,
        reply=values["__interrupt__"][0].value["reply"],
        is_complete=just_completed,
        extracted_data=values.get("extracted_data") if just_completed else None,
        prediction=values.get("prediction") if just_completed else None,
    )


# chat 세션에 저장된 대화와 분석 결과 조회
@router.get("/{session_id}")
async def get_session(session_id: str):
    graph = await get_graph()
    snapshot = await graph.aget_state(_config(session_id), subgraphs=True)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    intake_in_progress = []
    for task in snapshot.tasks:
        if task.name == "analyze" and hasattr(task.state, "values"):
            intake_in_progress = task.state.values.get("intake", [])[1:]  # [0]은 messages에 이미 있음

    values = snapshot.values
    return {
        "session_id": session_id,
        "messages": values.get("messages", []),
        "intake_in_progress": intake_in_progress,
        "image_id": values.get("image_id"),
        "extracted_data": values.get("extracted_data"),
        "prediction": values.get("prediction"),
    }


class InfoTestRequest(BaseModel):
    question: str


# 테스트용: 정보 답변 로직(info_answer ⇄ search_info)만 실행. 분석·대화 저장 없음
@router.post("/info-test")
async def info_test(req: InfoTestRequest):
    return await answer_question(req.question)
