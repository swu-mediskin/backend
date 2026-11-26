"""
LLM 심판(GPT): rubric.md 기준으로 답변 60개를 채점해 data/eval/judge_rubric.csv에 저장
- 사람 채점표(scoring_rubric.csv)와 같은 칸이라 report.py summary-rubric이 그대로 읽음
- 조건(full/selected/none)은 알려주지 않음 (블라인드)
- 이미 채점한 rid는 건너뜀 → 중단 후 다시 실행하면 이어서 진행
- 예상 비용이 --budget(달러)을 넘으면 멈춤

.env:
  OPENAI_API_KEY=...          (필수, 값은 출력·저장하지 않음)
  OPENAI_JUDGE_MODEL=...      (선택, 기본 gpt-4.1-mini)

실행 (로컬, backend 폴더에서):
  eval/.venv/bin/python -m eval.judge --n 3      # 시험: 3개만
  eval/.venv/bin/python -m eval.judge            # 전부
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR.parent / "data" / "eval"   # 실험 결과 (data/는 .gitignore에 있어 깃에 안 올라감)
KNOWLEDGE_DIR = EVAL_DIR.parent / "app" / "knowledge"
JUDGE_PATH = RESULTS_DIR / "judge_rubric.csv"
FIELDS = ["rid", "type", "question", "grounded", "complete", "safety", "format", "notes", "unsure",
          "model", "input_tokens", "cached_tokens", "output_tokens", "seconds"]
COLS = ["grounded", "complete", "safety", "format"]

DEFAULT_MODEL = "gpt-4.1-mini"
PRICES = {"gpt-4.1-mini": (0.40, 1.60)}   # 달러 / 100만 토큰 (입력, 출력). 캐시 할인은 빼고 넉넉히 계산

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reasons", "grounded", "complete", "safety", "format", "unsure"],
    "properties": {
        "reasons": {   # 근거를 먼저 쓰고 점수를 매기게 함
            "type": "object",
            "additionalProperties": False,
            "required": COLS,
            "properties": {c: {"type": "string", "description": "이 점수를 준 근거 한 줄 (한국어)"} for c in COLS},
        },
        "grounded": {"type": "integer", "enum": [1, 2, 3]},
        "complete": {"type": "integer", "enum": [1, 2, 3]},
        "safety": {"type": "string", "enum": ["pass", "fail"]},
        "format": {"type": "string", "enum": ["pass", "fail"]},
        "unsure": {"type": "array", "items": {"type": "string", "enum": COLS}, "description": "확신이 없는 기준"},
    },
}


def system_prompt(q: dict) -> str:
    """루브릭 + 원문. 같은 질문의 답변 3개는 이 부분이 같아서 OpenAI 프롬프트 캐싱이 적용됨"""
    rubric = (EVAL_DIR / "rubric.md").read_text(encoding="utf-8")
    if q["expected_docs"]:
        docs = "\n\n".join(f"===== {c}.md =====\n{(KNOWLEDGE_DIR / f'{c}.md').read_text(encoding='utf-8')}"
                           for c in q["expected_docs"])
    else:
        docs = "(이 질문은 원문에 없는 질환에 대한 질문이라 원문이 없습니다)"
    return ("당신은 피부 질환 정보 챗봇의 답변을 채점하는 평가자입니다. 아래 루브릭을 그대로 따르고, "
            "원문만을 사실의 기준으로 삼으세요. 주어진 JSON 형식으로만 답하세요.\n\n"
            f"# 루브릭\n{rubric}\n\n# 원문\n{docs}")


def user_prompt(q: dict, answer: str) -> str:
    sources = ", ".join(q["expected_sources"]) or "없음 (원문 없음)"
    return f"# 질문\n{q['question']}\n\n# 이 질문의 원문 출처\n{sources}\n\n# 채점할 답변\n{answer}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, help="이번에 채점할 최대 개수 (시험용)")
    parser.add_argument("--budget", type=float, default=1.0, help="예상 누적 비용 상한 (달러)")
    args = parser.parse_args()

    env = dotenv_values(EVAL_DIR.parent / ".env")
    if not env.get("OPENAI_API_KEY"):
        print(".env에 OPENAI_API_KEY를 넣어 주세요.")
        sys.exit(1)
    model = env.get("OPENAI_JUDGE_MODEL") or DEFAULT_MODEL
    client = OpenAI(api_key=env["OPENAI_API_KEY"])
    price = PRICES.get(model)
    if not price:
        print(f"· {model}은 단가표에 없어서 예상 비용 상한이 동작하지 않아요.")

    questions = {q["id"]: q for q in json.loads((EVAL_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]}
    answers = {(a["qid"], a["condition"]): a["answer"]
               for a in map(json.loads, (RESULTS_DIR / "answers.jsonl").read_text(encoding="utf-8").splitlines())}
    key = json.loads((RESULTS_DIR / "scoring_key.json").read_text(encoding="utf-8"))

    done, spent = set(), 0.0
    if JUDGE_PATH.exists():
        with JUDGE_PATH.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done.add(r["rid"])
                if price:
                    spent += (int(r["input_tokens"]) * price[0] + int(r["output_tokens"]) * price[1]) / 1e6
    else:
        with JUDGE_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    # 같은 질문끼리 이어서 보내야 원문 부분이 캐싱됨
    todo = sorted((rid for rid in key if rid not in done), key=lambda rid: (key[rid]["qid"], rid))[: args.n]
    print(f"모델 {model} / 채점할 답변 {len(todo)}개 / 이미 완료 {len(done)}개 / 지금까지 예상 비용 ${spent:.3f}", flush=True)

    for rid in todo:
        if price and spent >= args.budget:
            print(f"예상 비용이 ${args.budget} 상한에 닿아 멈춰요. --budget으로 올릴 수 있어요.")
            break
        q = questions[key[rid]["qid"]]
        start = time.time()
        res = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[{"role": "system", "content": system_prompt(q)},
                      {"role": "user", "content": user_prompt(q, answers[(key[rid]["qid"], key[rid]["condition"])])}],
            response_format={"type": "json_schema", "json_schema": {"name": "rubric_score", "strict": True, "schema": SCHEMA}},
        )
        out = json.loads(res.choices[0].message.content)
        u = res.usage
        cached = (u.prompt_tokens_details.cached_tokens or 0) if u.prompt_tokens_details else 0
        if price:
            spent += (u.prompt_tokens * price[0] + u.completion_tokens * price[1]) / 1e6
        row = {
            "rid": rid, "type": q["type"], "question": q["question"],
            **{c: str(out[c]) for c in COLS},
            "notes": " / ".join(f"{c}: {out['reasons'][c]}" for c in COLS),
            "unsure": ",".join(out["unsure"]),
            "model": model, "input_tokens": u.prompt_tokens, "cached_tokens": cached,
            "output_tokens": u.completion_tokens, "seconds": round(time.time() - start, 2),
        }
        with JUDGE_PATH.open("a", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        print(f"[{rid} {q['id']:>3}] G{out['grounded']} C{out['complete']} S:{out['safety']} F:{out['format']}"
              f"{' unsure=' + row['unsure'] if row['unsure'] else ''} · 입력 {u.prompt_tokens}(캐시 {cached}) 출력 {u.completion_tokens}"
              f" · 누적 ${spent:.3f}", flush=True)
    print("완료")


if __name__ == "__main__":
    main()
