from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.types import Command
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.schemas import DiagnosisRequest, DiagnosisResponse

from .graph import INTAKE, RESULT, get_graph

router = APIRouter(prefix="/chat", tags=["Chat"])


def _config(image_id: int) -> dict:
    return {"configurable": {"thread_id": f"image-{image_id}"}}


@router.post("", response_model=DiagnosisResponse)
async def chat(request: DiagnosisRequest, db: Session = Depends(get_db)):
    image = db.get(models.SkinImage, request.image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="해당 이미지를 찾을 수 없습니다. 이미지를 먼저 업로드해 주세요.")

    graph = await get_graph()
    config = _config(request.image_id)
    snapshot = await graph.aget_state(config)

    if not snapshot.values:
        await graph.ainvoke(
            {
                "image_id": image.id,
                "image_path": image.file_path,
                "age": request.age,
                "gender": request.gender,
                "phase": INTAKE,
            },
            config,
        )
        snapshot = await graph.aget_state(config)

    if snapshot.values.get("phase") == RESULT:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="분석이 완료된 상담입니다.")

    # 전체 대화 중 마지막 user 메시지만 그래프에 전달 (프론트와의 용이한 연결을 위함)
    before = len(snapshot.values.get("messages", []))
    values = await graph.ainvoke(Command(resume=request.messages[-1].text), config)

    new_replies = [m["text"] for m in values["messages"][before + 1:] if m["role"] == "model"]
    return DiagnosisResponse(
        reply="\n\n".join(new_replies),
        is_complete=values.get("phase") == RESULT,
        extracted_data=values.get("extracted_data"),
        prediction=values.get("prediction"),
    )
