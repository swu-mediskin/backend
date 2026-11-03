from fastapi import HTTPException, status
from google import genai
from google.genai import types
from app.schemas import ExtractedMedicalData
from app.schemas import DiagnosisRequest, ChatResponse

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
    1. reply에는 환자에게 보여줄 자연스러운 한국어 답변을 써. 한 번에 1~2개 정도만 질문해서 환자가 부담 없이 답하게 해.
    2. 아직 모르는 정보가 있으면 is_complete는 false, extracted_data는 null로 두고 이어서 질문해.
    3. 필요한 정보가 충분히 모였거나, 환자가 더 이상 답하기 어렵다고 하거나 그만하고 싶어하면
       is_complete를 true로 하고 extracted_data를 채워. 이때 reply에는 정리한 내용을 짧게 안내해.
    4. 확진이나 처방은 하지 마. 증상이 심각해 보이면 병원 방문을 권유해.

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