from fastapi import HTTPException, status
from google import genai
from google.genai import types
from app.schemas import ExtractedMedicalData

client = genai.Client()

def extract_and_translate_via_gemini(korean_text: str) -> ExtractedMedicalData:
    system_instruction = """
    너는 피부과 및 의료 AI 백엔드 시스템의 고도화된 데이터 전처리 전문가야.
    사용자가 입력한 한국어 증상 설명 문장을 분석하여 지정된 JSON 스키마에 맞게 메타데이터를 추출하고, 
    의료 비전-언어 모델(BioMedCLIP) 입력에 가장 최적화된 '영문 임상 문장(clinical_prompt)'을 생성해야 해.

    [지침 사항]
    1. 한국어 오타나 구어체를 분석해 boolean 값을 정확히 정규화하세요.
    2. clinical_prompt를 작성할 때, 사용자가 말하지 않은 정보(null)나 증상이 없다고 한 정보(false)는 문장에 절대 포함하지 마세요.
    3. 적절한 의학 영단어로 번역하여 자연스럽게 1줄로 조립하세요.
    """

    # 🔥 무료 티어에서 확실하게 작동하는 최신 1.5 모델 후보군
    # (gemini-1.5-flash-8b는 무료 티어 제한이 가장 널널해서 429 에러를 피하기 제일 좋음)
    candidate_models = [
        'gemini-3.5-flash',
        'gemini-3.1-flash-lite'
    ]

    last_error = None

    # 모델 리스트를 순회하면서 성공할 때까지 찔러보기 (Fallback 로직)
    for model_name in candidate_models:
        try:
            print(f"🚀 [{model_name}] 모델로 연결 시도 중...")
            
            response = client.models.generate_content(
                model=model_name,
                contents=f"환자 입력 문장: {korean_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=ExtractedMedicalData,
                    temperature=0.1,
                ),
            )
            
            print(f"✅ 성공! 최종 선택된 모델: {model_name}")
            return ExtractedMedicalData.model_validate_json(response.text)

        except Exception as e:
            last_error = e
            print(f"⚠️ [{model_name}] 실패 ({e}) -> 다음 모델로 넘어갑니다.")
            continue # 실패하면 다음 모델 이름으로 다시 시도

    # 3개 다 실패했을 때만 최종 에러 반환
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
        detail=f"사용 가능한 모든 모델 호출 실패. 마지막 에러: {str(last_error)}"
    )