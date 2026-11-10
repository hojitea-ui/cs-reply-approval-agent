"""멈춤 기준별 개입률 / 놓침 / 헛멈춤 비교표. 실행: python evaluate.py  (결과는 eval_result.md 로도 저장)"""
from agent import ACTIVE, CRITERIA, classify_rules, gate_reasons, retrieve_policy
from samples import SAMPLES


def analyze(text):
    cat, conf = classify_rules(text)       # 평가는 재현 가능하도록 규칙 기반 판정만 사용
    return conf, retrieve_policy(text, cat)


CONFIGS = [
    ("기준 없음 (전부 자동)", ()),
    ("전부 멈춤", "ALL"),
    ("확신도 < 0.7 만", ("low_confidence",)),
    ("규정 인용 없음 만", ("no_citation",)),
    ("위험 표현 만", ("risk_words",)),
    ("확신도 + 규정 인용 없음", ("low_confidence", "no_citation")),
    ("세 기준 모두 (채택)", ACTIVE),
]


def run(active):
    stopped = missed = false_stop = 0
    miss_ids, fs_ids = [], []
    for tid, text, needs in SAMPLES:
        conf, policy = analyze(text)
        stop = True if active == "ALL" else bool(gate_reasons(text, conf, policy, active))
        stopped += stop
        if needs and not stop:
            missed += 1; miss_ids.append(tid)
        if stop and not needs:
            false_stop += 1; fs_ids.append(tid)
    return stopped, missed, false_stop, miss_ids, fs_ids


def main():
    n = len(SAMPLES)
    need = sum(1 for _, _, x in SAMPLES if x)
    lines = [f"전체 {n}건 (사람이 봐야 하는 건 {need}건, 자동 처리 가능 {n - need}건)\n",
             "| 기준 | 사람에게 온 건 | 개입률 | 놓침 | 헛멈춤 | 놓친 건 | 헛멈춘 건 |",
             "|---|---|---|---|---|---|---|"]
    for name, active in CONFIGS:
        stopped, missed, fs, mids, fids = run(active)
        lines.append(f"| {name} | {stopped} | {stopped / n:.0%} | {missed} | {fs} | "
                     f"{', '.join(mids) or '-'} | {', '.join(fids) or '-'} |")
    out = "\n".join(lines)
    print(out)
    open("eval_result.md", "w", encoding="utf-8").write(out + "\n")


if __name__ == "__main__":
    main()
