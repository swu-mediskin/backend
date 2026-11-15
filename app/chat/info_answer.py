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


async def generate_info_answer(messages: list[dict], tool_trace: list[dict]):
    """
    반환: (model 응답 Content dict, 툴 호출 목록, 최종 답변 텍스트)
    """
    contents = [types.Content(role=m["role"], parts=[types.Part(text=m["text"])]) for m in messages]
    contents += [types.Content.model_validate_json(json.dumps(c)) for c in tool_trace]

    config = types.GenerateContentConfig(
        system_instruction=INFO_ANSWER_PROMPT,
        tools=[types.Tool(function_declarations=[SEARCH_INFO_DECLARATION])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.3,
    )

    last_error = None
    for model_name in CANDIDATE_MODELS:
        try:
            response = await client.aio.models.generate_content(model=model_name, contents=contents, config=config)
            break
        except Exception as e:
            last_error = e
    else:
        raise last_error

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
