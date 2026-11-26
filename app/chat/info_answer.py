"""
정보 답변(info_answer): 질문을 보고 필요하면 search_info 툴 호출을 요청하고, 검색 결과를 근거로 답변을 작성
- 분석 결과와 무관하게 동작 (분석 전에도 사용 가능)
"""
import json

from google.genai import types

from app.service import CANDIDATE_MODELS, client

from .tools import SEARCH_INFO_DECLARATION, search_info

INFO_ANSWER_PROMPT = """
당신은 피부암·피부질환 정보를 알려 주는 친절한 안내원입니다. 의사가 아닙니다.

규칙:
1. 질환의 정의·증상·위험 요인·치료 등 의학 정보를 설명할 때는 반드시 search_info 툴로 해당 질환의 참고 자료를 먼저 가져와야 합니다.
   여러 질환을 비교하는 질문이면 질환마다 한 번씩 가져옵니다. 등록된 질환(모반, 기저세포암, 편평상피세포암, 흑색종)이 아니면 툴을 쓰지 않습니다.
   자료가 있으면 그 내용을 근거로 답하고 끝에 출처를 붙입니다. 자료가 없으면 일반적인 수준에서만 조심스럽게 설명하고 출처가 없다고 밝힙니다.
2. 사용자 본인의 병변이 어떤 질환인지 판단하지 않습니다. 정확한 진단은 피부과 전문의에게 받아야 한다고 안내합니다.
3. 약이나 연고를 추천하거나 처방하지 않습니다.
4. 쉬운 한국어 존댓말로 3~6문장 정도로 답합니다.
"""


CLASSIFY_PROMPT = """
피부 상담 챗봇에 들어온 사용자의 마지막 메시지가 어떤 의도인지 하나만 고릅니다.
- info: 피부질환(모반, 피부암 등)에 대한 일반적인 궁금증 (예: "흑색종이 뭐예요?", "기저세포암은 전이돼요?")
- intake: 자기 피부 상태를 설명하거나 상담·분석을 원함 (예: "발바닥 점이 커졌어요", "제 사진 봐주세요")
인사처럼 애매한 메시지는 info로 고릅니다.
"""


def _format_prediction(prediction: dict) -> str:
    probs = sorted(prediction["all_probabilities"].items(), key=lambda kv: kv[1], reverse=True)
    return (
        f"[AI 분석 결과] 1순위: {prediction['prediction_name']}({prediction['prediction']}) / 전체 확률: "
        + ", ".join(f"{code} {p * 100:.1f}%" for code, p in probs)
    )


def _to_contents(messages: list[dict]) -> list[types.Content]:
    contents = []
    for m in messages:
        text = m["text"]
        # 분석 결과 메시지에는 확률 전체를 덧붙여 모델에 전달 (사용자에게 보이는 문장은 그대로)
        if m.get("prediction"):
            text += "\n\n" + _format_prediction(m["prediction"])
        contents.append(types.Content(role=m["role"], parts=[types.Part(text=text)]))
    return contents


async def _generate(contents, config):
    last_error = None
    for model_name in CANDIDATE_MODELS:
        try:
            return await client.aio.models.generate_content(model=model_name, contents=contents, config=config)
        except Exception as e:
            last_error = e
    raise last_error


async def classify_intent(messages: list[dict]) -> str:
    """마지막 메시지가 정보 질문(info)인지 상담·분석 요청(intake)인지 분류"""
    config = types.GenerateContentConfig(
        system_instruction=CLASSIFY_PROMPT,
        response_mime_type="text/x.enum",
        response_schema={"type": "STRING", "enum": ["info", "intake"]},
        temperature=0,
    )
    response = await _generate(_to_contents(messages), config)
    return response.text.strip()


async def generate_info_answer(messages: list[dict], tool_trace: list[dict]):
    """
    반환: (model 응답 Content dict, 툴 호출 목록, 최종 답변 텍스트)
    """
    contents = _to_contents(messages)
    contents += [types.Content.model_validate_json(json.dumps(c)) for c in tool_trace]

    config = types.GenerateContentConfig(
        system_instruction=INFO_ANSWER_PROMPT,
        tools=[types.Tool(function_declarations=[SEARCH_INFO_DECLARATION])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.3,
    )
    response = await _generate(contents, config)

    content = response.candidates[0].content.model_dump(mode="json", exclude_none=True)
    calls = [{"name": fc.name, "args": dict(fc.args or {})} for fc in (response.function_calls or [])]
    text = None if calls else response.text
    return content, calls, text


# 테스트용: 그래프 없이 info_answer → search_info → info_answer … 반복을 직접 실행
async def answer_question(question: str) -> dict:
    messages = [{"role": "user", "text": question}]
    tool_trace, searches = [], []
    while True:
        content, calls, text = await generate_info_answer(messages, tool_trace)
        if not calls:
            return {"answer": text, "searches": searches}
        tool_trace.append(content)
        parts = []
        for c in calls:
            result = search_info(**c["args"])
            searches.append({
                "disease": c["args"].get("disease"),
                "found": result["found"],
                "codes": [d["code"] for d in result.get("documents", [])],
            })
            parts.append({"function_response": {"name": c["name"], "response": result}})
        tool_trace.append({"role": "user", "parts": parts})
