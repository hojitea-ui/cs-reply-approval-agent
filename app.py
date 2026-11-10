"""Streamlit 웹 데모. 실행: streamlit run app.py"""
import pandas as pd
import streamlit as st

from agent import ACTIVE, CONF_THRESHOLD, CRITERIA, MAX_REDRAFT, Service
from samples import SAMPLES

st.set_page_config(page_title="CS 답변 승인 에이전트", page_icon="🛡️", layout="wide")


@st.cache_resource
def get_service() -> Service:
    return Service()


svc = get_service()
BADGE = {"risk_words": "🔴", "no_citation": "🟠", "low_confidence": "🟡", "redrafted": "🔁"}
MODE_COLOR = {"자동 발송": "#e8f5e9", "승인 후 발송": "#e3f2fd", "수정 후 발송": "#fff8e1", "반려": "#ffebee"}
MODE_LABEL = {"auto": "자동 발송", "human_approved": "승인 후 발송",
              "human_edited": "수정 후 발송", "rejected": "반려"}

# ------------------------------------------------------------------ 사이드바
with st.sidebar:
    st.header("문의 접수")
    if st.button("샘플 30건 한꺼번에 접수", use_container_width=True):
        with st.spinner("에이전트 실행 중..."):
            for tid, text, _ in SAMPLES:
                svc.submit(tid, text)
        st.rerun()
    with st.form("new_ticket", clear_on_submit=True):
        text = st.text_area("직접 입력", placeholder="예: 소비자원에 신고할 거예요. 환불해 주세요.")
        if st.form_submit_button("접수") and text.strip():
            svc.submit(f"U{len(svc.all_ticket_ids()) + 1:03d}", text.strip())
            st.rerun()
    st.divider()
    st.subheader("멈춤 기준 (하나라도 걸리면 사람에게)")
    for code, desc in CRITERIA.items():
        st.markdown(f"- **{code}**: {desc}")
    st.caption(f"다시 판정은 건당 최대 {MAX_REDRAFT}회")
    st.divider()
    if st.button("데모 초기화", use_container_width=True):
        svc.reset()
        st.rerun()

st.title("🛡️ 고객 문의 자동 답변 · 사람 승인 에이전트")
st.caption("위험한 건만 발송 직전에 멈추고, 나머지는 자동으로 보냅니다. 대기 건은 SQLite 체크포인터에 저장되어 앱을 껐다 켜도 남습니다.")

# ------------------------------------------------------------------ 요약
ids = svc.all_ticket_ids()
pending = svc.pending()
outbox = svc.outbox()
auto_n = sum(o["mode"] == "auto" for o in outbox)
human_n = sum(o["mode"] != "auto" for o in outbox)
c1, c2, c3, c4 = st.columns(4)
c1.metric("접수", len(ids))
c2.metric("자동 처리(사람 없이)", auto_n)
c3.metric("승인 대기", len(pending))
c4.metric("사람이 처리 완료", human_n)

tab_pending, tab_done, tab_eval, tab_how = st.tabs(["승인 대기", "처리 완료", "기준 비교", "동작 원리"])

# ------------------------------------------------------------------ 승인 대기
with tab_pending:
    if not pending:
        st.info("대기 중인 건이 없습니다. 사이드바에서 샘플을 접수해 보세요.")
    else:
        left, right = st.columns([1, 2])
        with left:
            st.subheader(f"대기 목록 ({len(pending)})")
            st.caption("🔴 위험 표현 · 🟠 규정 없음 · 🟡 확신도 낮음 · 🔁 재판정 (위험한 건이 위로)")

            def badges(p):
                codes = [r["code"] for r in p["values"]["reasons"]]
                return "".join(BADGE[c] for c in BADGE if c in codes)

            pending.sort(key=lambda p: -len({r["code"] for r in p["values"]["reasons"]} & {"risk_words"}))
            labels = {p["ticket_id"]: f"{badges(p)} {p['ticket_id']} · {p['values']['text'][:20]}…" for p in pending}
            chosen = st.radio("건 선택", list(labels), format_func=labels.get, label_visibility="collapsed")
        with right:
            p = next(x for x in pending if x["ticket_id"] == chosen)
            pl = p["payload"]
            st.subheader(f"{chosen} 검토")
            # 판단에 가장 중요한 두 가지(왜 멈췄나 / 통과시키면 무슨 일이 생기나)를 맨 위에 둔다.
            top1, top2 = st.columns(2)
            top1.warning("**⏸ 멈춘 이유**\n\n" + "\n\n".join("· " + m for m in pl["멈춘 이유"]))
            top2.error("**⚠ 통과시키면**\n\n" + pl["통과시키면"])
            st.markdown("**고객 문의 원문**")
            st.info(pl["고객 문의"])
            a, b = st.columns(2)
            a.markdown("**AI 분류**"); a.write(pl["AI 분류"])
            b.markdown("**근거 규정**"); b.write(pl["근거 규정"])
            st.markdown("**AI 답변 초안**")
            st.code(pl["AI 답변 초안"], language=None)

            st.divider()
            st.markdown("##### 담당자 응답")
            options = ["승인", "수정 후 승인", "반려", "다시 판정"]
            action = st.radio("응답", options, horizontal=True, key=f"act_{chosen}", label_visibility="collapsed")
            decision, ok = None, True
            if action == "승인":
                decision = {"action": "approve"}
            elif action == "수정 후 승인":
                edited = st.text_area("수정할 답변", value=pl["AI 답변 초안"], height=180, key=f"edit_{chosen}")
                decision = {"action": "edit", "text": edited}
                ok = bool(edited.strip())
            elif action == "반려":
                reason = st.text_input("반려 사유 (필수 — 기준을 고칠 재료가 됩니다)", key=f"rej_{chosen}")
                decision = {"action": "reject", "reason": reason}
                ok = bool(reason.strip())
            else:
                if pl["can_redraft"]:
                    ins = st.text_input("AI에게 줄 지시 (예: 교환 규정으로 답변해)", key=f"ins_{chosen}")
                    decision = {"action": "redraft", "instruction": ins}
                    ok = bool(ins.strip())
                else:
                    st.caption(f"다시 판정은 {MAX_REDRAFT}회까지입니다. 다른 응답을 선택하세요.")
                    ok = False
            if st.button("응답 보내기", type="primary", disabled=not ok, key=f"send_{chosen}"):
                svc.respond(chosen, decision)
                st.rerun()

# ------------------------------------------------------------------ 처리 완료
with tab_done:
    if not outbox:
        st.info("아직 처리된 건이 없습니다.")
    else:
        texts = {t: svc.status(t)["values"].get("text", "") for t in ids}
        df = pd.DataFrame([{"건": o["ticket_id"], "처리": MODE_LABEL.get(o["mode"], o["mode"]),
                            "문의": texts.get(o["ticket_id"], ""), "발송 내용/반려 사유": o["body"] or o["reason"],
                            "시각": o["at"]} for o in outbox])
        show = st.multiselect("처리 방식", list(MODE_LABEL.values()), default=list(MODE_LABEL.values()))
        view = df[df["처리"].isin(show)]
        styled = view.style.map(lambda v: f"background-color: {MODE_COLOR.get(v, '')}; color: #222", subset=["처리"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

# ------------------------------------------------------------------ 기준 비교
with tab_eval:
    st.markdown("같은 30건(사람이 라벨링)으로 기준을 바꿔 가며 센 결과입니다. 자세한 해석은 REPORT.md 참고.")
    try:
        st.markdown(open("eval_result.md", encoding="utf-8").read())
    except FileNotFoundError:
        st.warning("`python evaluate.py` 를 먼저 실행하세요.")

# ------------------------------------------------------------------ 동작 원리
with tab_how:
    st.markdown(f"""
```
문의 접수 → draft(분류·규정 검색·초안) → check(멈춤 기준) ─ 안 걸림 → send(자동 발송)
                                                   └ 걸림 → review(interrupt: 여기서 멈춤)
                                                              ├ 승인 / 수정 후 승인 → send
                                                              ├ 반려(사유) → reject
                                                              └ 다시 판정(지시) → draft → check → review
```
- `review` 는 `interrupt()` 로 멈추고, 담당자의 응답은 `Command(resume=...)` 로 같은 `thread_id`(=문의 ID)에 들어갑니다.
- 발송은 `send` 노드 한 곳뿐이며 `review` 와 분리되어 있어, 멈춘 단계가 재실행되어도 고객에게 중복 발송되지 않습니다.
- 체크포인터는 SQLite 파일(`hitl.db`)입니다. 다음 실행 단계(`next`)가 `review` 이면 승인 대기 건입니다.
""")
