"""
실험: 전체 문서(full) vs 해당 문서만(selected, 현재 방식) vs 문서 없음(none)
- 질문 × 조건마다 답변을 만들어 data/eval/answers.jsonl에 한 줄씩 저장 (이미 있는 항목은 건너뜀 → 중단 후 다시 실행하면 이어서 진행)
- .env의 GEMINI_API_KEY5~13을 돌려 쓰며, 429(한도 초과)면 다음 키로 넘어감. 키 값은 출력·저장하지 않음

실행 (컨테이너 안):
  docker exec mediskin_backend python -m eval.run_eval                 # 전체
  docker exec mediskin_backend python -m eval.run_eval --only G1       # 일부 질문만
"""
import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from google import genai
from google.genai import errors, types

from app.chat.info_answer import INFO_ANSWER_PROMPT
from app.chat.tools import DISEASES, KNOWLEDGE_DIR, SEARCH_INFO_DECLARATION, search_info

MODEL = "gemini-3.5-flash"
TEMPERATURE = 0.3
CONDITIONS = ["full", "selected", "none"]

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.json"
RESULTS_PATH = EVAL_DIR.parent / "data" / "eval" / "answers.jsonl"   # data/는 깃에 안 올라감
ENV_PATH = EVAL_DIR.parent / ".env"


# ======= 조건별 시스템 프롬프트 ===================================
# 현재 프롬프트에서 규칙 1(툴 사용 규칙)만 조건에 맞게 바꿈. 나머지 규칙은 동일
RULE1 = re.compile(r"1\. .*?(?=\n2\. )", re.S)
assert RULE1.search(INFO_ANSWER_PROMPT), "INFO_ANSWER_PROMPT의 규칙 1을 찾지 못함"

FULL_RULE1 = (
    "1. 아래 [참고 자료]를 근거로 답하고 끝에 해당 자료의 출처를 붙입니다. "
    "참고 자료에 없는 질환이면 일반적인 수준에서만 조심스럽게 설명하고 출처가 없다고 밝힙니다."
)
NONE_RULE1 = "1. 참고 자료 없이 일반적인 수준에서 조심스럽게 설명하고, 참고한 출처가 없다고 밝힙니다."


def _all_docs() -> str:
    return "\n\n".join(
        f"===== {code}.md =====\n{(KNOWLEDGE_DIR / f'{code}.md').read_text(encoding='utf-8')}" for code in DISEASES
    )


SYSTEM_PROMPTS = {
    "full": RULE1.sub(FULL_RULE1, INFO_ANSWER_PROMPT) + "\n[참고 자료]\n" + _all_docs(),
    "selected": INFO_ANSWER_PROMPT,
    "none": RULE1.sub(NONE_RULE1, INFO_ANSWER_PROMPT),
}


# ======= 키 순환 ===================================
class AllKeysExhausted(Exception):
    pass


env = dotenv_values(ENV_PATH)
KEYS = [(n, env[f"GEMINI_API_KEY{n}"]) for n in range(5, 14) if env.get(f"GEMINI_API_KEY{n}")]
CLIENTS = {n: genai.Client(api_key=k) for n, k in KEYS}
key_order = [n for n, _ in KEYS]   # 앞에 있는 키부터 사용, 한도가 찬 키는 뒤로 빠짐
exhausted = set()


def call_gemini(contents, config) -> tuple:
    """(response, 기록) 반환. 모든 키가 분당 한도에 걸렸으면 1분 기다렸다가 한 번 더 돌림"""
    for round_ in range(2):
        try:
            return _call_once(contents, config)
        except AllKeysExhausted:
            if round_ == 0 and len(exhausted) < len(key_order):
                print("    · 사용 가능한 키가 모두 분당 한도 → 60초 대기", flush=True)
                time.sleep(60)
                continue
            raise


def _call_once(contents, config) -> tuple:
    """429면 다음 키, 503이면 잠시 후 같은 키로 재시도"""
    for key_no in key_order:
        if key_no in exhausted:
            continue
        for attempt in range(3):
            start = time.time()
            try:
                response = CLIENTS[key_no].models.generate_content(model=MODEL, contents=contents, config=config)
            except errors.ClientError as e:
                if e.code != 429:
                    raise
                if "PerDay" in str(e.details):   # 하루 한도 → 이 키는 오늘 더 못 씀
                    print(f"    · 키 {key_no} 하루 한도 초과 → 다음 키", flush=True)
                    exhausted.add(key_no)
                    break
                print(f"    · 키 {key_no} 분당 한도 초과 → 다음 키", flush=True)   # 분당 한도 → 다른 키로
                break
            except errors.ServerError as e:
                print(f"    · 키 {key_no} 서버 오류 {e.code} → {5 * (attempt + 1)}초 뒤 재시도", flush=True)
                time.sleep(5 * (attempt + 1))
                continue
            u = response.usage_metadata
            record = {
                "key_no": key_no,
                "seconds": round(time.time() - start, 2),
                "input_tokens": u.prompt_token_count or 0,
                "output_tokens": u.candidates_token_count or 0,
                "thought_tokens": u.thoughts_token_count or 0,
                "total_tokens": u.total_token_count or 0,
            }
            return response, record
    raise AllKeysExhausted()


# ======= 조건별 답변 생성 ===================================
def answer(question: str, condition: str) -> dict:
    contents = [types.Content(role="user", parts=[types.Part(text=question)])]
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPTS[condition], temperature=TEMPERATURE)
    if condition == "selected":
        config.tools = [types.Tool(function_declarations=[SEARCH_INFO_DECLARATION])]
        config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)

    calls, docs = [], []
    for _ in range(5):   # selected: 툴 호출 → 결과 전달 → 답변 (최대 5번 왕복)
        response, record = call_gemini(contents, config)
        calls.append(record)
        if not response.function_calls:
            return {"answer": response.text, "docs": docs if condition == "selected" else None, "calls": calls}

        contents.append(response.candidates[0].content)
        parts = []
        for fc in response.function_calls:
            result = search_info(**dict(fc.args or {}))
            docs.append(dict(fc.args or {}).get("disease"))
            parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=parts))
    raise RuntimeError("툴 호출이 5번을 넘음")


# ======= 실행 ===================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="실행할 질문 ID (예: G1 S3)")
    parser.add_argument("--conditions", nargs="*", default=CONDITIONS, choices=CONDITIONS)
    args = parser.parse_args()

    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if RESULTS_PATH.exists():
        for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["qid"], r["condition"]))

    print(f"모델 {MODEL} / 키 {len(KEYS)}개 / 질문 {len(questions)}개 × 조건 {args.conditions} / 이미 완료 {len(done)}개", flush=True)
    for q in questions:
        for cond in args.conditions:
            if (q["id"], cond) in done:
                continue
            print(f"[{q['id']:>3} {cond:>8}] {q['question']}", flush=True)
            try:
                out = answer(q["question"], cond)
            except AllKeysExhausted:
                print("모든 키의 한도가 찼어요. 저장된 결과는 그대로 두고 멈춥니다. 나중에 다시 실행하면 이어서 진행해요.")
                return
            row = {
                "qid": q["id"], "condition": cond, "model": MODEL,
                "answer": out["answer"], "docs": out["docs"], "calls": out["calls"],
                "input_tokens": sum(c["input_tokens"] for c in out["calls"]),
                "output_tokens": sum(c["output_tokens"] for c in out["calls"]),
                "thought_tokens": sum(c["thought_tokens"] for c in out["calls"]),
                "seconds": round(sum(c["seconds"] for c in out["calls"]), 2),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            with RESULTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            docs = f" docs={row['docs']}" if cond == "selected" else ""
            print(f"    → 호출 {len(out['calls'])}번, 입력 {row['input_tokens']} / 출력 {row['output_tokens']} / 생각 {row['thought_tokens']} 토큰, {row['seconds']}초{docs}", flush=True)
    print("완료")


if __name__ == "__main__":
    main()
