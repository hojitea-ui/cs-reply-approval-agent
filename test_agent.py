"""중단/재개/보관 동작 검증. 실행: python test_agent.py"""
import os, tempfile
from agent import Service

db = os.path.join(tempfile.mkdtemp(), "t.db")
s = Service(db)

# 1) 안전한 건은 사람 없이 끝까지 자동 처리
st = s.submit("A1", "비밀번호를 잊어버렸어요. 어떻게 재설정하나요?")
assert not st["waiting"] and st["outcome"]["mode"] == "auto", st["outcome"]

# 2) 위험한 건은 발송 전에 멈추고, 발송함에 아무것도 없다
st = s.submit("B1", "소비자원에 신고하겠습니다. 택배가 안 와요")
assert st["waiting"], "멈춰야 함"
assert all(o["ticket_id"] != "B1" for o in s.outbox()), "멈춘 동안 발송되면 안 됨"
st2 = s.submit("C1", "쿠폰이 적용이 안 돼요")
assert st2["waiting"]

# 3) 프로세스 재시작을 흉내: 새 Service 인스턴스로 대기 건이 남아 있는지
s2 = Service(db)
assert {p["ticket_id"] for p in s2.pending()} == {"B1", "C1"}

# 4) 재개: 수정 후 승인 / 반려 / 다시 판정
st = s2.respond("B1", {"action": "edit", "text": "고객님 불편을 드려 죄송합니다. 담당자가 직접 연락드립니다."})
assert st["outcome"]["mode"] == "human_edited" and "담당자가 직접" in st["outcome"]["body"]
st = s2.respond("C1", {"action": "reject", "reason": "쿠폰 규정이 없어 담당자 직접 답변"})
assert st["outcome"]["status"] == "rejected"

# 5) 다시 판정: 지시문으로 규정을 바꾸고, 상한(2회) 이후엔 다시 사람에게
s2.submit("D1", "배송이 이상해요 그리고 사이즈도 안 맞아요")
st = s2.respond("D1", {"action": "redraft", "instruction": "교환 규정으로 답변해"})
assert s2.status("D1")["values"]["redraft_count"] == 1
assert st["waiting"] and "재확인" in str(st["payload"]["멈춘 이유"]), "재판정 결과는 사람이 다시 봐야 함"
st = s2.respond("D1", {"action": "approve"})
assert st["outcome"]["mode"] == "human_approved"
# 대기 건 목록에서 처리된 건은 빠진다
assert "B1" not in {p["ticket_id"] for p in s2.pending()}
# 발송함은 건당 1건 (멱등)
ids = [o["ticket_id"] for o in s2.outbox()]
assert len(ids) == len(set(ids))
print("ALL OK", [ (o["ticket_id"], o["mode"]) for o in s2.outbox()])
