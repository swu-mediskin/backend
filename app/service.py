import asyncio
import os
import requests
from fastapi import HTTPException, status
from google import genai
from google.genai import types
from app.schemas import ExtractedMedicalData
from app.schemas import DiagnosisRequest, ChatResponse, PredictionResult

# AI 레포(swu-mediskin/ai) 예측 서버 주소
AI_SERVER_URL = os.getenv("AI_SERVER_URL", "http://3.37.47.74:8080/predict")

# AI 서버 class_names 순서와 동일해야 함 (['NEV', 'BCC', 'SCC', 'MEL'])
AI_CLASS_NAMES = ["NEV", "BCC", "SCC", "MEL"]
AI_CLASS_NAMES_KO = {
    "NEV": "모반(점)",
    "BCC": "기저세포암",
    "SCC": "편평세포암",
    "MEL": "흑색종",
}

client = genai.Client()

# 🔥 무료 티어 429(호출 한도 초과)를 피하기 위한 모델 후보군 (앞에서부터 순서대로 시도)
CANDIDATE_MODELS = [
    'gemini-3.5-flash',
    'gemini-3.1-flash-lite'
]


async def _generate_with_fallback(contents, system_instruction: str, response_schema):
    """모델 리스트를 순회하면서 성공할 때까지 찔러보기 (Fallback 로직)"""
    last_error = None

    for model_name in CANDIDATE_MODELS:
        try:
            print(f"🚀 [{model_name}] 모델로 연결 시도 중...")

            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0.1,
                ),
            )

            print(f"✅ 성공! 최종 선택된 모델: {model_name}")
            return response_schema.model_validate_json(response.text)

        except Exception as e:
            last_error = e
            print(f"⚠️ [{model_name}] 실패 ({e}) -> 다음 모델로 넘어갑니다.")
            continue # 실패하면 다음 모델 이름으로 다시 시도

    # 모든 모델이 실패했을 때만 최종 에러 반환
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"사용 가능한 모든 모델 호출 실패. 마지막 에러: {str(last_error)}"
    )

async def extract_and_translate_via_gemini(korean_text: str) -> ExtractedMedicalData:
    """한 문장짜리 증상 설명 → 메타데이터 + 영문 임상 문장 (단발성)"""
    system_instruction = """
    너는 피부과 및 의료 AI 백엔드 시스템의 고도화된 데이터 전처리 전문가야.
    사용자가 입력한 한국어 증상 설명 문장을 분석하여 지정된 JSON 스키마에 맞게 메타데이터를 추출하고,
    의료 비전-언어 모델(BioMedCLIP) 입력에 가장 최적화된 '영문 임상 문장(clinical_prompt)'을 생성해야 해.

    [지침 사항]
    1. 한국어 오타나 구어체를 분석해 boolean 값을 정확히 정규화하세요.
    2. clinical_prompt를 작성할 때, 사용자가 말하지 않은 정보(null)나 증상이 없다고 한 정보(false)는 문장에 절대 포함하지 마세요.
    3. 적절한 의학 영단어로 번역하여 자연스럽게 1줄로 조립하세요.
    """

    return await _generate_with_fallback(
        contents=f"환자 입력 문장: {korean_text}",
        system_instruction=system_instruction,
        response_schema=ExtractedMedicalData,
    )


async def chat_via_gemini(request_data: DiagnosisRequest) -> ChatResponse:
    """
    AI 채팅창처럼 여러 턴에 걸쳐 문진하는 함수.
    서버는 대화를 저장하지 않으므로, 프론트가 매 요청마다 지금까지의 전체 대화(messages)를 보내야 함.
    """
    # 1. 메타데이터 변수화 (값이 없으면 '알 수 없음'으로 처리)
    current_age = request_data.age if request_data.age else "알 수 없음"
    current_gender = request_data.gender

    system_instruction = f"""
    너는 피부과 문진을 담당하는 친절한 AI 상담원이자, 의료 AI 백엔드의 데이터 전처리 전문가야.
    환자와 여러 번 대화를 주고받으며 피부 증상 정보를 수집하고,
    충분한 정보가 모이면 의료 비전-언어 모델(BioMedCLIP) 입력용 데이터를 만들어야 해.

    [기본 메타데이터]
    - 환자 나이: {current_age}
    - 환자 성별: {current_gender}
    (대화 중에 나이나 성별을 알 수 있는 정보가 나오면 그것을 우선 반영하고, 없다면 위 기본값을 사용해.)

    [수집할 정보]
    병변 부위(region), 가려움(itch), 통증(pain), 병변이 커졌는지(growth), 흡연(smoke), 음주(drink), 나이(age), 성별(gender)

    [대화 진행 방식]
    매 답변(reply)은 반드시 아래 2단계 구조로 작성해.
      (1) 공감·요약: 환자가 방금 한 말을 자연스럽게 되짚어 줘. 예) "팔에 일주일 전부터 붉은 게 올라왔군요."
      (2) 연결 질문: 방금 말한 내용과 관련 있는 미수집 항목으로 자연스럽게 이어서 물어봐.
          예) "혹시 그 부위가 가렵거나 따갑지는 않으세요?"

    질문 원칙:
    1. 매 턴마다 지금까지의 전체 대화를 보고 [수집할 정보] 중 아직 확인되지 않은 항목을 스스로 점검해.
    2. 한 번에 1~2개만 물어보고, 방금 대화 맥락과 가까운 항목부터 물어봐.
       - 증상 관련 항목(부위 → 가려움/통증 → 크기 변화)을 먼저, 생활습관(흡연/음주)은 나중에 물어봐.
       - 생활습관은 "피부 상태에 영향을 줄 수 있어서 여쭤보는데요," 처럼 이유를 덧붙여 부담을 줄여.
    3. 환자가 한 번에 여러 정보를 말하면 모두 반영하고, 이미 답한 항목은 다시 묻지 마.
    4. 답이 애매하면("좀 그런 것 같아요", "가끔요") 한 번만 가볍게 구체화해서 확인해.
       예) "가끔 가려우시군요. 긁고 싶을 정도로 가려운 편인가요?"
    5. 환자가 질문과 관계없는 말을 하거나 걱정을 표현하면 먼저 짧게 공감한 뒤 질문으로 돌아와.
    6. 기본 메타데이터로 나이·성별이 이미 주어졌다면 다시 묻지 마.
    7. 말투는 부드러운 존댓말("~군요", "~셨네요", "~은 어떠세요?")을 쓰고, 딱딱한 설문지처럼 나열하지 마.

    종료 조건:
    - 아직 모르는 정보가 있으면 is_complete는 false, extracted_data는 null로 두고 이어서 질문해.
    - 필요한 정보가 모두 모였거나, 환자가 더 이상 답하기 어렵다고 하거나 그만하고 싶어하면
      is_complete를 true로 하고 extracted_data를 채워. 이때 reply에는 수집한 내용을 짧게 요약해 안내해.
      AI 이미지 분석 결과는 시스템이 reply 뒤에 덧붙이니, 결과를 추측하거나 언급하지 말고 요약 뒤 "이제 사진과 함께 분석해 볼게요." 정도로만 마무리해.
    - 확진이나 처방은 하지 마. 증상이 심각해 보이면(급격한 크기 증가, 출혈, 심한 통증 등) 병원 방문을 권유해.

    [extracted_data 작성 지침]
    1. 단편적인 문장이 아닌 전체 대화 흐름을 종합해서 하나의 결과를 도출해.
    2. 한국어 오타나 구어체를 분석해 boolean 값을 정확히 정규화해.
    3. clinical_prompt를 작성할 때, 환자가 언급하지 않은 정보나 '없다'고 한 증상은 절대 문장에 포함하지 마.
    """

    # 2. 대화 기록을 Gemini의 멀티턴 형식(user / model 역할)으로 변환
    contents = [
        types.Content(role=msg.role, parts=[types.Part(text=msg.text)])
        for msg in request_data.messages
    ]

    return await _generate_with_fallback(
        contents=contents,
        system_instruction=system_instruction,
        response_schema=ChatResponse,
    )


def _to_ai_bool(value) -> str:
    """AI 서버 형식: 'true' / 'false' / 'unk'(모름)"""
    if value is None:
        return "unk"
    return "true" if value else "false"


def _call_ai_server(file_path: str, data: dict) -> dict:
    with open(file_path, "rb") as f:
        response = requests.post(
            AI_SERVER_URL,
            data=data,
            files={"file": (os.path.basename(file_path), f)},
            timeout=60,
        )
    response.raise_for_status()
    return response.json()


async def predict_via_ai_server(file_path: str, extracted: ExtractedMedicalData) -> PredictionResult:
    """문진 종료 시 이미지 + 추출된 메타데이터로 AI 서버(/predict)를 호출해 클래스와 확률을 받아옴"""
    data = {
        "age": str(extracted.age or 0),
        "gender": (extracted.gender or "unk").upper(),
        "region": (extracted.region or "unk").upper(),
        "itch": _to_ai_bool(extracted.itch),
        "hurt": _to_ai_bool(extracted.pain),
        "grew": _to_ai_bool(extracted.growth),
        "smoke": _to_ai_bool(extracted.smoke),
        "drink": _to_ai_bool(extracted.drink),
    }

    try:
        result = await asyncio.to_thread(_call_ai_server, file_path, data)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI 서버 예측 호출에 실패했습니다: {e}",
        )

    code = result["prediction"]
    return PredictionResult(
        prediction=code,
        prediction_name=AI_CLASS_NAMES_KO.get(code, code),
        confidence=result["confidence"],
        all_probabilities=dict(zip(AI_CLASS_NAMES, result.get("all_probabilities", []))),
    )


def format_prediction_message(prediction: PredictionResult) -> str:
    return (
        f"사진과 말씀해 주신 내용을 함께 분석한 결과, "
        f"가장 가능성이 높은 유형은 {prediction.prediction_name}({prediction.prediction})이며 "
        f"확률은 {prediction.confidence * 100:.1f}%입니다. "
        f"이 결과는 참고용이며 정확한 진단은 피부과 전문의에게 받아보시길 권해 드려요. 문진을 마칠게요."
    )
