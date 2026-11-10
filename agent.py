"""고객 문의 자동 답변 에이전트 (Human-in-the-Loop).

흐름:  draft(판정+초안) -> check(멈춤 기준 검사) -> [review: 사람] -> send(발송)
- draft / check / review 는 여러 번 다시 실행해도 안전하다 (바깥으로 나가는 동작이 없다).
- 바깥으로 나가는 동작(고객 발송)은 send 노드 하나뿐이고, review 와 반드시 분리되어 있다.
"""
import json
import os
import re
import sqlite3
from typing import Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from policy import CATEGORY_KEYWORDS, POLICIES, RISK_WORDS

DB_PATH = os.environ.get("HITL_DB", "hitl.db")
CONF_THRESHOLD = 0.7      # 기준 1: 분류 확신도가 이 값 미만이면 멈춤
MAX_REDRAFT = 2           # '다시 판정' 상한

# 멈춤 기준 이름 -> 설명. evaluate.py 가 켜고 끄며 비교한다.
CRITERIA = {
    "low_confidence": f"분류 확신도 {CONF_THRESHOLD} 미만",
    "no_citation": "근거 규정을 인용하지 못한 답변",
    "risk_words": "위험 표현 포함 (" + ", ".join(RISK_WORDS[:4]) + " 등)",
}
ACTIVE = tuple(CRITERIA)  # 기본: 세 기준 모두 사용


# ---------------------------------------------------------------- 판정 (규칙 기반, LLM 선택)
def classify_rules(text: str) -> tuple[str, float]:
    scores = {c: sum(text.count(k) > 0 for k in kws) for c, kws in CATEGORY_KEYWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (top_cat, top), (_, second) = ranked[0], ranked[1]
    if top == 0:
        return "기타", 0.3
    conf = 0.55 + 0.2 * min(top, 2)
    if second >= top:         # 두 분류가 비슷하게 걸리면 확신이 떨어진다
        conf -= 0.2
    return top_cat, round(max(conf, 0.1), 2)


def classify_llm(text: str) -> Optional[tuple[str, float]]:
    """ANTHROPIC_API_KEY 가 있을 때만 LLM 으로 분류. 실패하면 None -> 규칙 기반으로 대체."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        cats = list(CATEGORY_KEYWORDS) + ["기타"]
        msg = client.messages.create(
            model=os.environ.get("HITL_LLM_MODEL", "claude-haiku-5-5"),
            max_tokens=100,
            messages=[{"role": "user", "content":
                       f"쇼핑몰 고객 문의를 분류하세요. 분류 후보: {cats}\n"
                       f'JSON 한 줄로만 답하세요: {{"category": "...", "confidence": 0~1}}\n문의: {text}'}],
        )
        data = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group())
        if data["category"] not in cats:
            return None
        return data["category"], float(data["confidence"])
    except Exception:
        return None


def classify(text: str) -> tuple[str, float]:
    return classify_llm(text) or classify_rules(text)


def retrieve_policy(text: str, category: str) -> Optional[dict]:
    best, best_hits = None, 0
    for p in POLICIES:
        hits = sum(k in text for k in p["keywords"])
        if p["category"] == category:
            hits += 0.5   # 같은 분류의 규정을 약간 우대
        if hits > best_hits and sum(k in text for k in p["keywords"]) > 0:
            best, best_hits = p, hits
    return best


def write_draft(policy: Optional[dict]) -> str:
    if policy is None:
        return "안녕하세요, 고객님. 문의 주셔서 감사합니다.\n문의하신 내용을 확인한 뒤 담당자가 별도로 안내드리겠습니다."
    return (f"안녕하세요, 고객님. 문의 주셔서 감사합니다.\n\n{policy['text']}\n\n"
            f"(근거: {policy['ref']})\n추가로 궁금하신 점이 있으면 언제든 말씀해 주세요.")


# ---------------------------------------------------------------- 멈춤 기준
def gate_reasons(text: str, confidence: float, policy: Optional[dict], active=ACTIVE) -> list[dict]:
    """계산 가능한 조건만 쓴다. 비어 있으면 자동 처리, 하나라도 있으면 사람에게 간다."""
    reasons = []
    if "low_confidence" in active and confidence < CONF_THRESHOLD:
        reasons.append({"code": "low_confidence",
                        "message": f"분류 확신도 {confidence} < {CONF_THRESHOLD}"})
    if "no_citation" in active and policy is None:
        reasons.append({"code": "no_citation", "message": "근거로 인용할 규정을 찾지 못함"})
    if "risk_words" in active:
        found = [w for w in RISK_WORDS if w in text]
        if found:
            reasons.append({"code": "risk_words", "message": f"위험 표현 포함: {', '.join(found)}"})
    return reasons


# ---------------------------------------------------------------- 그래프
class State(TypedDict, total=False):
    ticket_id: str
    text: str
    category: str
    confidence: float
    policy: Optional[dict]
    draft: str
    reasons: list
    instruction: str
    redraft_count: int
    decision: dict
    outcome: dict


def draft_node(state: State) -> dict:
    # '다시 판정'이면 담당자의 지시문도 판정 재료로 쓴다 (예: "불량 규정으로 답변해").
    material = state["text"] + " " + state.get("instruction", "")
    category, conf = classify(material)
    policy = retrieve_policy(material, category)
    return {"category": category, "confidence": conf, "policy": policy, "draft": write_draft(policy)}


def check_node(state: State) -> dict:
    reasons = gate_reasons(state["text"], state["confidence"], state.get("policy"))
    if state.get("redraft_count", 0) > 0:
        # 담당자가 지시해서 다시 만든 답변은 기준 통과 여부와 무관하게 사람이 한 번 더 확인한다.
        reasons.append({"code": "redrafted", "message": f"다시 판정 결과 재확인 ({state['redraft_count']}회째)"})
    return {"reasons": reasons}


def route_after_check(state: State) -> str:
    return "review" if state["reasons"] else "send"


def review_node(state: State) -> dict:
    # 주의: interrupt 이전에 바깥으로 나가는 동작을 두지 않는다. (재개 시 이 노드는 처음부터 다시 실행됨)
    policy = state.get("policy")
    answer = interrupt({
        "ticket_id": state["ticket_id"],
        "고객 문의": state["text"],
        "AI 분류": f"{state['category']} (확신도 {state['confidence']})",
        "근거 규정": f"{policy['ref']}: {policy['text']}" if policy else "없음",
        "AI 답변 초안": state["draft"],
        "멈춘 이유": [r["message"] for r in state["reasons"]],
        "통과시키면": "위 답변이 고객에게 이메일로 발송됩니다 (발송 후 회수 불가)",
        "can_redraft": state.get("redraft_count", 0) < MAX_REDRAFT,
    })
    return {"decision": answer}


def route_after_review(state: State) -> str:
    action = state["decision"]["action"]
    if action in ("approve", "edit"):
        return "send"
    if action == "redraft" and state.get("redraft_count", 0) < MAX_REDRAFT:
        return "prepare_redraft"
    if action == "reject":
        return "reject"
    return "review"   # 상한 초과 재판정 등 -> 다시 사람에게


def prepare_redraft_node(state: State) -> dict:
    return {"instruction": state["decision"].get("instruction", ""),
            "redraft_count": state.get("redraft_count", 0) + 1,
            "decision": None}


def send_node(state: State) -> dict:
    """유일하게 바깥으로 나가는 단계. 실제 서비스라면 여기서 메일 API를 호출한다."""
    d = state.get("decision")
    if d is None:
        mode, body = "auto", state["draft"]
    elif d["action"] == "edit":
        mode, body = "human_edited", d["text"]
    else:
        mode, body = "human_approved", state["draft"]
    record_outbox(state["ticket_id"], mode, body)
    return {"outcome": {"status": "sent", "mode": mode, "body": body}}


def reject_node(state: State) -> dict:
    reason = state["decision"].get("reason", "")
    record_outbox(state["ticket_id"], "rejected", "", reason)
    return {"outcome": {"status": "rejected", "mode": "rejected", "reason": reason}}


# ---------------------------------------------------------------- 저장소 (체크포인터 + 발송함)
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS tickets(
        ticket_id TEXT PRIMARY KEY, text TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS outbox(
        ticket_id TEXT PRIMARY KEY, mode TEXT, body TEXT, reason TEXT,
        at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    return conn


_outbox_conn = None


def record_outbox(ticket_id: str, mode: str, body: str, reason: str = "") -> None:
    global _outbox_conn
    if _outbox_conn is None:
        _outbox_conn = _conn()
    # ticket_id 가 PK 라서 같은 건이 두 번 실행돼도 발송 기록은 한 건 (멱등)
    _outbox_conn.execute("INSERT OR REPLACE INTO outbox(ticket_id, mode, body, reason) VALUES (?,?,?,?)",
                         (ticket_id, mode, body, reason))
    _outbox_conn.commit()


def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("draft", draft_node)
    g.add_node("check", check_node)
    g.add_node("review", review_node)
    g.add_node("prepare_redraft", prepare_redraft_node)
    g.add_node("send", send_node)
    g.add_node("reject", reject_node)
    g.add_edge(START, "draft")
    g.add_edge("draft", "check")
    g.add_conditional_edges("check", route_after_check, ["review", "send"])
    g.add_conditional_edges("review", route_after_review,
                            ["send", "prepare_redraft", "reject", "review"])
    g.add_edge("prepare_redraft", "draft")
    g.add_edge("send", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=checkpointer)


class Service:
    """웹 데모가 쓰는 얇은 래퍼. 체크포인터는 SQLite 파일이라 재시작해도 대기 건이 남는다."""

    def __init__(self, db_path: str = None):
        global DB_PATH, _outbox_conn
        if db_path:
            DB_PATH, _outbox_conn = db_path, None
        self.conn = _conn()
        self.graph = build_graph(SqliteSaver(self.conn))

    @staticmethod
    def _cfg(ticket_id):
        return {"configurable": {"thread_id": ticket_id}}

    def submit(self, ticket_id: str, text: str) -> dict:
        if ticket_id in self.all_ticket_ids():     # 같은 건을 두 번 접수해도 다시 실행하지 않는다
            return self.status(ticket_id)
        self.conn.execute("INSERT OR IGNORE INTO tickets(ticket_id, text) VALUES (?,?)", (ticket_id, text))
        self.conn.commit()
        self.graph.invoke({"ticket_id": ticket_id, "text": text}, self._cfg(ticket_id))
        return self.status(ticket_id)

    def respond(self, ticket_id: str, decision: dict) -> dict:
        self.graph.invoke(Command(resume=decision), self._cfg(ticket_id))
        return self.status(ticket_id)

    def status(self, ticket_id: str) -> dict:
        snap = self.graph.get_state(self._cfg(ticket_id))
        v = snap.values
        waiting = "review" in snap.next          # 다음 실행 단계가 남아 있으면 대기 중
        payload = None
        if waiting:
            for t in snap.tasks:
                for i in t.interrupts:
                    payload = i.value
        return {"ticket_id": ticket_id, "waiting": waiting, "payload": payload,
                "outcome": v.get("outcome"), "values": v}

    def all_ticket_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT ticket_id FROM tickets ORDER BY created, rowid")]

    def pending(self) -> list[dict]:
        return [s for s in (self.status(t) for t in self.all_ticket_ids()) if s["waiting"]]

    def outbox(self) -> list[dict]:
        rows = self.conn.execute("SELECT ticket_id, mode, body, reason, at FROM outbox ORDER BY at").fetchall()
        return [dict(zip(["ticket_id", "mode", "body", "reason", "at"], r)) for r in rows]

    def reset(self) -> None:
        """데모용: 모든 건과 체크포인트를 지운다."""
        for table in ("tickets", "outbox", "checkpoints", "writes"):
            try:
                self.conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()
