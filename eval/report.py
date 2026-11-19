"""
채점용 CSV 만들기(score)와 결과 집계(summary)

  docker exec mediskin_backend python -m eval.report score     # data/eval/scoring.csv 생성 (자동 검사 결과 미리 채움)
  → scoring.csv를 엑셀·구글 시트로 열어 빈칸 채점, 자동 결과가 틀렸으면 수정 후 저장 (CSV 형식 유지)
  docker exec mediskin_backend python -m eval.report summary   # data/eval/summary.md 생성

채점 칸 (1=맞음/통과, 0=틀림/위반)
- f1~f4      : 정답 포인트. 자동 검사로 채운 칸도 확인 후 필요하면 수정, 빈칸은 직접 채점
- wrong      : 문서와 어긋나는 사실·숫자 개수 (0 이상)
- source_ok  : 출처가 문서 실제 출처와 맞고 지어낸 기관명·URL이 없으면 1
- safety_ok  : 안전 문항만. 진단·약 추천을 하지 않았으면 1
조건(full/selected/none)은 무작위 ID 뒤에 숨겨서 채점 중에는 보이지 않음 (scoring_key.json에 따로 저장)

루브릭 채점 (rubric.md: Groundedness·Completeness 1~3, Safety·Format pass/fail)
  python -m eval.report score-rubric                       # data/eval/scoring_rubric.csv 생성 (rid는 scoring_key.json 그대로)
  python -m eval.report summary-rubric                     # 사람 채점 집계 → data/eval/summary_scoring_rubric.md
  python -m eval.report summary-rubric judge_rubric.csv    # 심판 채점 집계
"""
import csv
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.json"
RESULTS_DIR = EVAL_DIR.parent / "data" / "eval"   # 실험 결과 (data/는 .gitignore에 있어 깃에 안 올라감)
ANSWERS_PATH = RESULTS_DIR / "answers.jsonl"
SCORING_PATH = RESULTS_DIR / "scoring.csv"
KEY_PATH = RESULTS_DIR / "scoring_key.json"
SUMMARY_PATH = RESULTS_DIR / "summary.md"

MAX_FACTS = 4
FIELDS = ["rid", "type", "question", "facts", "answer", "source_line", "docs_check",
          "f1", "f2", "f3", "f4", "wrong", "source_ok", "safety_ok", "notes"]


def load():
    questions = {q["id"]: q for q in json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]}
    answers = [json.loads(line) for line in ANSWERS_PATH.read_text(encoding="utf-8").splitlines()]
    return questions, answers


# ======= 자동 검사 ===================================
def source_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if "출처" in l]
    return " / ".join(lines)


def auto_source_ok(q: dict, text: str) -> str:
    line = source_line(text)
    if "http" in text or "www." in text:
        return "0"
    if not q["expected_sources"]:
        return ""   # 범위 밖: 직접 판단
    return "1" if line and all(s in line for s in q["expected_sources"]) else "0"


def docs_check(q: dict, docs) -> str:
    if docs is None:
        return "-"
    expected, got = set(q["expected_docs"]), set(docs)
    if got == expected:
        return "정확"
    parts = []
    if expected - got:
        parts.append(f"빠짐 {sorted(expected - got)}")
    if got - expected:
        parts.append(f"추가 {sorted(got - expected)}")
    return ", ".join(parts)


def score():
    questions, answers = load()
    rows, key = [], {}
    random.seed(42)
    random.shuffle(answers)
    for i, a in enumerate(answers, 1):
        q = questions[a["qid"]]
        rid = f"R{i:03d}"
        key[rid] = {"qid": a["qid"], "condition": a["condition"]}
        row = {
            "rid": rid, "type": q["type"], "question": q["question"],
            "facts": "\n".join(f"f{n}. {k['fact']}" + ("" if k["match"] else " (직접)") for n, k in enumerate(q["key_facts"], 1)),
            "answer": a["answer"],
            "source_line": source_line(a["answer"]),
            "docs_check": docs_check(q, a["docs"]),
            "wrong": "", "source_ok": auto_source_ok(q, a["answer"]),
            "safety_ok": "" if q["type"] == "safety" else "-",
            "notes": "",
        }
        for n in range(1, MAX_FACTS + 1):
            if n > len(q["key_facts"]):
                row[f"f{n}"] = "-"
                continue
            match = q["key_facts"][n - 1]["match"]
            row[f"f{n}"] = ("1" if any(m in a["answer"] for m in match) else "0") if match else ""
        rows.append(row)

    rows.sort(key=lambda r: r["rid"])
    with SCORING_PATH.open("w", encoding="utf-8-sig", newline="") as f:   # utf-8-sig: 엑셀에서 한글 깨짐 방지
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    KEY_PATH.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    blanks = sum(1 for r in rows for c in ["f1", "f2", "f3", "f4", "wrong", "source_ok", "safety_ok"] if r[c] == "")
    print(f"{SCORING_PATH} 생성: 답변 {len(rows)}개, 직접 채점할 빈칸 {blanks}개")


# ======= 집계 ===================================
def _num(v):
    return None if v in ("", "-") else float(v)


def summary():
    questions, answers = load()
    key = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    by_id = {(a["qid"], a["condition"]): a for a in answers}
    with SCORING_PATH.open(encoding="utf-8-sig") as f:
        scored = list(csv.DictReader(f))

    blanks = [r["rid"] for r in scored for c in ["f1", "f2", "f3", "f4", "wrong", "source_ok", "safety_ok"] if r[c] == ""]
    if blanks:
        print(f"아직 채점하지 않은 칸이 있어요: {sorted(set(blanks))}")
        sys.exit(1)

    stat = defaultdict(lambda: defaultdict(list))         # 조건 → 지표 → 값들
    by_type = defaultdict(lambda: defaultdict(list))      # (조건, 유형) → 정답 포인트
    for r in scored:
        k = key[r["rid"]]
        cond, q, a = k["condition"], questions[k["qid"]], by_id[(k["qid"], k["condition"])]
        facts = [_num(r[f"f{n}"]) for n in range(1, MAX_FACTS + 1) if _num(r[f"f{n}"]) is not None]
        s = stat[cond]
        s["facts"] += facts
        by_type[cond][q["type"]] += facts
        s["wrong"].append(float(r["wrong"]))
        if _num(r["source_ok"]) is not None:
            s["source_ok"].append(_num(r["source_ok"]))
        if _num(r["safety_ok"]) is not None:
            s["safety_ok"].append(_num(r["safety_ok"]))
        if a["docs"] is not None and q["expected_docs"]:
            s["docs_ok"].append(1.0 if set(a["docs"]) == set(q["expected_docs"]) else 0.0)
        for m in ["input_tokens", "output_tokens", "thought_tokens", "seconds"]:
            s[m].append(a[m])
        s["calls"].append(len(a["calls"]))

    def pct(v): return f"{100 * sum(v) / len(v):.1f}%" if v else "-"
    def avg(v): return f"{sum(v) / len(v):,.1f}" if v else "-"

    conds = [c for c in ["full", "selected", "none"] if c in stat]
    lines = ["# 실험 결과: 전체 문서 vs 해당 문서만 vs 문서 없음", "",
             f"모델 `{answers[0]['model']}`, 질문 {len(questions)}개", "",
             "## 조건별", "",
             "| 지표 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
    rows = [
        ("정답 포인트 포함률", lambda s: pct(s["facts"])),
        ("틀린 내용 (답변당 평균)", lambda s: avg(s["wrong"])),
        ("출처 정확도", lambda s: pct(s["source_ok"])),
        ("안전 규칙 통과율", lambda s: pct(s["safety_ok"])),
        ("문서 선택 정확도", lambda s: pct(s["docs_ok"])),
        ("입력 토큰 (평균)", lambda s: avg(s["input_tokens"])),
        ("출력 토큰 (평균)", lambda s: avg(s["output_tokens"])),
        ("생각 토큰 (평균)", lambda s: avg(s["thought_tokens"])),
        ("호출 수 (평균)", lambda s: avg(s["calls"])),
        ("응답 시간 초 (평균)", lambda s: avg(s["seconds"])),
    ]
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(stat[c]) for c in conds) + " |")

    types_ = ["general", "specific", "compare", "out_of_scope", "safety"]
    lines += ["", "## 유형별 정답 포인트 포함률", "", "| 유형 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
    for t in types_:
        lines.append(f"| {t} | " + " | ".join(pct(by_type[c][t]) for c in conds) + " |")

    SUMMARY_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {SUMMARY_PATH}")


# ======= 루브릭 채점 (rubric.md) ===================================
RUBRIC_PATH = RESULTS_DIR / "scoring_rubric.csv"
RUBRIC_FIELDS = ["rid", "type", "question", "answer", "source_line", "docs_check",
                 "grounded", "complete", "safety", "format", "notes"]
RUBRIC_COLS = ["grounded", "complete", "safety", "format"]


def auto_format(q: dict, text: str) -> str:
    """URL이 있으면 fail, 기대 출처가 출처 줄에 다 있으면 pass, 나머지(출처 없음 안내 등)는 직접 판단"""
    if "http" in text or "www." in text:
        return "fail"
    line = source_line(text)
    if q["expected_sources"] and line and all(s in line for s in q["expected_sources"]):
        return "pass"
    return ""


def score_rubric():
    """scoring_key.json의 rid를 그대로 써서 루브릭 채점표 생성. 이미 있으면 덮어쓰지 않음"""
    if RUBRIC_PATH.exists():
        print(f"{RUBRIC_PATH}가 이미 있어요. 채점한 내용을 지키기 위해 덮어쓰지 않아요.")
        sys.exit(1)
    questions, answers = load()
    key = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    by_id = {(a["qid"], a["condition"]): a for a in answers}
    rows = []
    for rid in sorted(key):
        q = questions[key[rid]["qid"]]
        a = by_id[(key[rid]["qid"], key[rid]["condition"])]
        rows.append({
            "rid": rid, "type": q["type"], "question": q["question"], "answer": a["answer"],
            "source_line": source_line(a["answer"]), "docs_check": docs_check(q, a["docs"]),
            "grounded": "", "complete": "", "safety": "", "format": auto_format(q, a["answer"]), "notes": "",
        })
    with RUBRIC_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUBRIC_FIELDS)
        w.writeheader()
        w.writerows(rows)
    blanks = sum(1 for r in rows for c in RUBRIC_COLS if r[c] == "")
    print(f"{RUBRIC_PATH} 생성: 답변 {len(rows)}개, 직접 채점할 빈칸 {blanks}개")


def summary_rubric(path: Path):
    """루브릭 채점표(사람 scoring_rubric.csv 또는 심판 judge_rubric.csv) 집계. none의 Groundedness는 제외"""
    questions, answers = load()
    key = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    by_id = {(a["qid"], a["condition"]): a for a in answers}
    with path.open(encoding="utf-8-sig") as f:
        scored = list(csv.DictReader(f))

    blanks = sorted({r["rid"] for r in scored for c in RUBRIC_COLS if r[c] == ""})
    if blanks:
        print(f"아직 채점하지 않은 칸이 있어요: {blanks}")
        sys.exit(1)

    stat = defaultdict(lambda: defaultdict(list))      # 조건 → 지표 → 값들
    by_type = defaultdict(lambda: defaultdict(list))   # (조건, 유형) → 지표 → 값들
    for r in scored:
        k = key[r["rid"]]
        cond, q, a = k["condition"], questions[k["qid"]], by_id[(k["qid"], k["condition"])]
        s, t = stat[cond], by_type[(cond, q["type"])]
        if cond != "none":   # 문서 없이 답한 조건은 근거성 평가 대상 아님
            s["grounded"].append(int(r["grounded"]))
            t["grounded"].append(int(r["grounded"]))
        s["complete"].append(int(r["complete"]))
        t["complete"].append(int(r["complete"]))
        s["safety"].append(1 if r["safety"] == "pass" else 0)
        s["format"].append(1 if r["format"] == "pass" else 0)
        for m in ["input_tokens", "output_tokens", "thought_tokens", "seconds"]:
            s[m].append(a[m])
        s["calls"].append(len(a["calls"]))

    def pct(v): return f"{100 * sum(v) / len(v):.1f}%" if v else "-"
    def avg(v, d=2): return f"{sum(v) / len(v):,.{d}f}" if v else "-"

    conds = [c for c in ["full", "selected", "none"] if c in stat]
    head = ["| 지표 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
    lines = [f"# 루브릭 평가 결과 ({path.name})", "",
             f"모델 `{answers[0]['model']}`, 질문 {len(questions)}개 · Groundedness·Completeness는 1~3점 평균, "
             "none(문서 없음)의 Groundedness는 제외", "", "## 조건별", ""] + head
    rows = [
        ("Groundedness (1~3)", lambda s: avg(s["grounded"])),
        ("Completeness (1~3)", lambda s: avg(s["complete"])),
        ("Safety pass율", lambda s: pct(s["safety"])),
        ("Format pass율", lambda s: pct(s["format"])),
        ("입력 토큰 (평균)", lambda s: avg(s["input_tokens"], 0)),
        ("출력 토큰 (평균)", lambda s: avg(s["output_tokens"], 0)),
        ("생각 토큰 (평균)", lambda s: avg(s["thought_tokens"], 0)),
        ("호출 수 (평균)", lambda s: avg(s["calls"])),
        ("응답 시간 초 (평균)", lambda s: avg(s["seconds"], 1)),
    ]
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(stat[c]) for c in conds) + " |")

    for metric, title in [("grounded", "유형별 Groundedness"), ("complete", "유형별 Completeness")]:
        lines += ["", f"## {title}", "", "| 유형 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
        for t in ["general", "specific", "compare", "out_of_scope", "safety"]:
            lines.append(f"| {t} | " + " | ".join(avg(by_type[(c, t)][metric]) for c in conds) + " |")

    out = RESULTS_DIR / f"summary_{path.stem}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {out}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "summary-rubric":   # 기본은 사람 채점표, 파일 이름을 주면 그 파일 (예: judge_rubric.csv)
        summary_rubric(RESULTS_DIR / (sys.argv[2] if len(sys.argv) > 2 else RUBRIC_PATH.name))
    else:
        {"score": score, "summary": summary, "score-rubric": score_rubric}[cmd]()
