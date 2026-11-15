"""
정보 검색 툴: 모델이 질문에 해당하는 질환(클래스)을 고르면 app/knowledge/{코드}.md 문서 전체를 반환
"""
from pathlib import Path

from google.genai import types

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

# 문서 파일명(코드) → 질환명
DISEASES = {
    "NEV": "모반(점), Nevus",
    "BCC": "기저세포암, Basal Cell Carcinoma",
    "SCC": "편평상피세포암, Squamous Cell Carcinoma",
    "MEL": "흑색종, Melanoma",
}

SEARCH_INFO_DECLARATION = types.FunctionDeclaration(
    name="search_info",
    description=(
        "피부질환 참고 문서를 가져온다. 질문이 어떤 질환에 관한 것인지 골라 해당 질환 코드를 넘긴다. "
        "질환별 코드: " + ", ".join(f"{code}={name}" for code, name in DISEASES.items())
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "disease": types.Schema(
                type=types.Type.STRING,
                enum=list(DISEASES),
                description="질문이 해당하는 질환 코드",
            )
        },
        required=["disease"],
    ),
)


def search_info(disease: str) -> dict:
    path = KNOWLEDGE_DIR / f"{disease}.md"
    if not path.exists():
        return {"found": False, "note": "등록된 참고 자료가 없습니다."}
    return {"found": True, "documents": [{"code": disease, "content": path.read_text(encoding="utf-8")}]}
