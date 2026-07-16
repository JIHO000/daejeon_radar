"""
동네레이더 챗봇 라우터 v2 — POST /chat/

똑똑해진 점
-----------
1. 인텐트 감지: 인사("안녕?"), 감사, 도움말을 알아듣고 상황에 맞게 답변
2. 카테고리 감지 3단계:
   a. 확장된 동의어 사전 (도서관←책/열람실, 병원←아파요/진료, 성심당 같은 고유명사 제외)
   b. DB에 실제 등록된 카테고리 동적 매칭 (공백 무시: "공공와이파이" = "공공 와이파이")
   c. 실패 시 문장에서 핵심 키워드 추출
3. 2중 데이터 소스:
   1순위 — community_response (이웃 제보, 생생한 후기)
   2순위 — place 테이블 (1,365건 공식 관광/시설 데이터) → "도서관 어디?"도 답 가능!
4. 구조화 응답: locations[] 와 suggestions[] 를 함께 반환
   → 프론트가 "지도에서 보기" 버튼과 추천 질문 칩을 렌더링

프론트 계약
------------
- 요청 : {"message": "도서관 어디에 있어?"}
- 응답 : {
    "reply": "...",
    "detected_facility": "도서관" | null,
    "match_count": 2,
    "locations": [{"name","latitude","longitude","address","description","source","response_id"}],
    "suggestions": ["철봉", "수유실", ...]
  }
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

load_dotenv()

router = APIRouter(prefix="/chat", tags=["chat"])

DB_PATH = Path(os.getenv("DB_PATH", "local.db"))

# =========================================================
# 동의어 사전 — 키는 "표준 카테고리명"(가능하면 DB의 requested_facility와 일치)
# =========================================================
FACILITY_SYNONYMS: dict[str, list[str]] = {
    "철봉": ["철봉", "턱걸이", "풀업", "pullup", "pull-up"],
    "화장실": ["화장실", "변기", "용변", "화장싴", "toilet", "wc"],
    "주차장": ["주차장", "주차", "파킹", "parking"],
    "벤치": ["벤치", "앉을 곳", "앉을곳"],
    "포토존": ["포토존", "포토", "인생샷", "인스타 감성"],
    "수유실": ["수유실", "수유", "기저귀", "아기 케어", "젖병"],
    "도서관": ["도서관", "열람실", "책 읽을", "책읽을", "독서실"],
    "병원": ["병원", "진료", "응급실", "아파요", "아프면", "의원"],
    "약국": ["약국", "약 살", "약사", "상비약"],
    "카페": ["카페", "커피", "라떼", "아메리카노"],
    "편의점": ["편의점", "24시", "cu", "gs25", "세븐일레븐", "이마트24"],
    "공원": ["공원", "산책할", "산책로", "잔디밭"],
    "ATM": ["atm", "현금인출", "출금", "현금 뽑"],
    "공공 와이파이": ["와이파이", "wifi", "무료 인터넷", "핫스팟"],
    "놀이터": ["놀이터", "미끄럼틀", "그네", "시소"],
    "버스정류장": ["버스정류장", "정류장", "버스 타"],
    "쉼터": ["쉼터", "그늘", "쉴 곳", "쉴곳", "쉴만한", "정자"],
    "식수대": ["식수대", "식수", "물 마실", "정수기", "급수대", "음수대"],
    "운동시설": ["운동시설", "운동기구", "헬스", "체육시설", "운동할"],
    "자전거 거치대": ["자전거 거치대", "자전거", "따릉이", "타슈"],
    "전기차 충전소": ["전기차 충전", "충전소", "전기차", "ev충전"],
    "무인민원발급기": ["무인민원발급기", "민원발급", "등본", "발급기", "주민등록"],
    "흡연구역": ["흡연구역", "흡연", "담배 피", "담배피"],
}

# 인텐트 패턴
GREETING_RE = re.compile(r"안녕|하이|헬로|반가워|반갑|hello|\bhi\b|ㅎㅇ|하잉", re.IGNORECASE)
THANKS_RE = re.compile(r"고마워|고맙|감사|땡큐|thank", re.IGNORECASE)
HELP_RE = re.compile(r"도움말|사용법|어떻게 (써|사용)|뭘? ?할 ?수|무엇을 할|뭐 ?해줄|뭐 ?할 ?줄|기능|help", re.IGNORECASE)

# 키워드 추출 시 지워버릴 표현들 (조사·의문사·잡담)
STOPWORDS_RE = re.compile(
    r"어디|어딨|있어|있나|있니|있을까|있는지|알려|알려줘|알려주세요|찾아|찾고|근처|주변|"
    r"제일|가장|좀|요\b|나요|가요|해줘|주세요|합니다|입니다|에서|으로|이나|한테|에게|"
    r"[?!.,~ㅋㅎㅠㅜ]+"
)


# =========================================================
# DB 헬퍼
# =========================================================

def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail=f"DB 파일을 찾을 수 없습니다: {DB_PATH.resolve()}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_db_facilities() -> list[str]:
    """DB(community_request)에 등록된 서로 다른 카테고리 이름 전부."""
    if not DB_PATH.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT requested_facility, COUNT(*) AS c FROM community_request "
            "WHERE requested_facility IS NOT NULL AND requested_facility != '' "
            "GROUP BY requested_facility ORDER BY c DESC"
        ).fetchall()
        return [r["requested_facility"] for r in rows]
    finally:
        conn.close()


def search_community(facility: str, limit: int = 4) -> List[dict]:
    """이웃 제보(community_response) 검색 — 카테고리 정확 매칭 + 시설명/설명 부분 매칭."""
    conn = _connect()
    try:
        like = f"%{facility}%"
        rows = conn.execute(
            """
            SELECT resp.response_id, resp.request_id,
                   resp.facility_name, resp.latitude, resp.longitude,
                   resp.address, resp.description, resp.created_at,
                   r.requested_facility
            FROM community_response AS resp
            JOIN community_request  AS r ON r.request_id = resp.request_id
            WHERE r.requested_facility = ?
               OR resp.facility_name LIKE ?
               OR resp.description  LIKE ?
            ORDER BY resp.created_at DESC
            LIMIT ?
            """,
            (facility, like, like, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def search_community_keyword(keyword: str, limit: int = 4) -> List[dict]:
    """
    자유 키워드로 이웃 답변 전문 검색.
    시설명 · 설명(후기) · 원문 질문까지 모두 뒤져서
    '전자레인지 있는 곳', '타임월드' 같은 질문도 답변 내용에서 찾아낸다.
    """
    if not keyword or len(keyword) < 2:
        return []
    conn = _connect()
    try:
        like = f"%{keyword}%"
        rows = conn.execute(
            """
            SELECT resp.response_id, resp.request_id,
                   resp.facility_name, resp.latitude, resp.longitude,
                   resp.address, resp.description, resp.created_at,
                   r.requested_facility
            FROM community_response AS resp
            JOIN community_request  AS r ON r.request_id = resp.request_id
            WHERE resp.facility_name LIKE ?
               OR resp.description  LIKE ?
               OR r.question        LIKE ?
            ORDER BY resp.created_at DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_open_requests(term: str) -> int:
    """
    답변을 기다리는(응답 0건) 관련 요청 글 수.
    '아직 정보 없음' 답변에 '게시판에 관련 요청 N건이 기다리고 있어요'로 활용.
    """
    if not term:
        return 0
    conn = _connect()
    try:
        like = f"%{term}%"
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM community_request r
            WHERE (r.requested_facility = ? OR r.question LIKE ?)
              AND NOT EXISTS (
                    SELECT 1 FROM community_response resp
                    WHERE resp.request_id = r.request_id)
            """,
            (term, like),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()


def search_places(keyword: str, limit: int = 3) -> List[dict]:
    """공식 place 테이블(1,365건)에서 이름/주소로 검색."""
    if not keyword or len(keyword) < 2:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT contentid, title, addr1, mapy AS latitude, mapx AS longitude
            FROM place
            WHERE title LIKE ? OR addr1 LIKE ?
            LIMIT ?
            """,
            (f"%{keyword}%", f"%{keyword}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# =========================================================
# 카테고리 / 키워드 감지
# =========================================================

def _norm(s: str) -> str:
    """소문자화 + 공백 제거 (공백 차이 무시 매칭용)."""
    return re.sub(r"\s+", "", (s or "").lower())


def detect_facility(message: str) -> Optional[str]:
    """
    1) 동의어 사전 매칭 (표준 카테고리명 반환)
    2) DB에 실제 존재하는 카테고리 매칭 (공백 무시, 긴 이름 우선)
    """
    msg_norm = _norm(message)
    if not msg_norm:
        return None

    # 1) 동의어 사전 — 긴 동의어 먼저 (예: "자전거 거치대" > "자전거")
    candidates: list[tuple[str, str]] = []  # (동의어, 표준명)
    for canon, syns in FACILITY_SYNONYMS.items():
        for syn in syns:
            candidates.append((syn, canon))
    candidates.sort(key=lambda x: len(_norm(x[0])), reverse=True)
    for syn, canon in candidates:
        if _norm(syn) in msg_norm:
            return canon

    # 2) DB 카테고리 동적 매칭
    for fac in sorted(list_db_facilities(), key=lambda f: len(_norm(f)), reverse=True):
        if _norm(fac) and _norm(fac) in msg_norm:
            return fac

    return None


JOSA_RE = re.compile(r"(에서|으로|이랑|한테|에게|은|는|이|가|을|를|에|로|의|도|만|랑)$")


def extract_keyword(message: str) -> Optional[str]:
    """
    카테고리 감지에 실패했을 때, 검색용 핵심 키워드 추출.
    예: '성심당 어디야?' → '성심당', '타임월드에 뭐 있어?' → '타임월드'
    """
    cleaned = STOPWORDS_RE.sub(" ", message or "")
    tokens = []
    for t in cleaned.split():
        # 끝에 붙은 조사 제거 ('타임월드에' → '타임월드'), 단 2글자 이상 남을 때만
        stripped = JOSA_RE.sub("", t)
        if len(stripped) >= 2:
            t = stripped
        if len(t) >= 2:
            tokens.append(t)
    if not tokens:
        return None
    # 가장 긴 토큰을 핵심 키워드로 (고유명사일 확률이 높음)
    return max(tokens, key=len)


# =========================================================
# 응답 모델
# =========================================================

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)


class ChatLocation(BaseModel):
    name: str
    latitude: float
    longitude: float
    address: Optional[str] = None
    description: Optional[str] = None
    source: str = "community"          # "community" | "official"
    response_id: Optional[int] = None  # community 제보일 때만


class ChatResponse(BaseModel):
    reply: str
    detected_facility: Optional[str] = None
    match_count: int = 0
    locations: List[ChatLocation] = []
    suggestions: List[str] = []


# =========================================================
# 답변 빌더
# =========================================================

def _greeting_reply() -> ChatResponse:
    facs = list_db_facilities()[:8]
    fac_line = " · ".join(facs) if facs else "철봉 · 화장실 · 주차장"
    return ChatResponse(
        reply=(
            "안녕하세요! 저는 대전 동네 정보를 찾아주는 레이더 챗봇이에요 📡\n\n"
            "이웃들의 생생한 제보와 공식 장소 데이터를 함께 검색해드려요.\n"
            f"지금 이런 걸 물어보실 수 있어요: {fac_line}\n\n"
            "아래 추천 질문을 눌러보거나 편하게 물어보세요!"
        ),
        suggestions=facs[:6],
    )


def _thanks_reply() -> ChatResponse:
    return ChatResponse(
        reply="도움이 됐다니 다행이에요! 😊 또 필요한 장소가 있으면 언제든 물어보세요.",
        suggestions=list_db_facilities()[:4],
    )


def _help_reply() -> ChatResponse:
    facs = list_db_facilities()
    return ChatResponse(
        reply=(
            "제가 할 수 있는 일이에요 💡\n\n"
            "1️⃣ 이웃 제보 검색 — 게시판에 달린 답변 속 위치를 찾아드려요\n"
            "2️⃣ 공식 장소 검색 — 대전 관광·문화시설 데이터에서도 찾아요 (예: 도서관, 성심당)\n"
            "3️⃣ 지도 연동 — 답변 속 '지도에서 보기'를 누르면 바로 위치 확인!\n\n"
            f"현재 이웃들이 활발히 찾는 카테고리는 {len(facs)}개예요."
        ),
        suggestions=facs[:6],
    )


def _to_locations(community: List[dict], official: List[dict]) -> List[ChatLocation]:
    locs: List[ChatLocation] = []
    for r in community:
        locs.append(ChatLocation(
            name=r["facility_name"],
            latitude=float(r["latitude"]),
            longitude=float(r["longitude"]),
            address=r.get("address"),
            description=r.get("description"),
            source="community",
            response_id=r["response_id"],
        ))
    for p in official:
        locs.append(ChatLocation(
            name=p["title"],
            latitude=float(p["latitude"]),
            longitude=float(p["longitude"]),
            address=p.get("addr1"),
            source="official",
        ))
    return locs


def _suggest_similar(exclude: Optional[str], n: int = 4) -> list[str]:
    return [f for f in list_db_facilities() if f != exclude][:n]


def build_reply(message: str) -> ChatResponse:
    # ── 0. 카테고리부터 감지 ("안녕! 철봉 어디?"는 인사가 아니라 질문) ──
    facility = detect_facility(message)

    # ── 1. 카테고리 없으면 인텐트 체크 ──
    if facility is None:
        if GREETING_RE.search(message):
            return _greeting_reply()
        if THANKS_RE.search(message):
            return _thanks_reply()
        if HELP_RE.search(message):
            return _help_reply()

    # ── 2. 데이터 검색 ──
    community: List[dict] = []
    official: List[dict] = []
    keyword: Optional[str] = None

    if facility:
        community = search_community(facility, limit=4)
        if not community:
            # 동의어가 표준 카테고리로 점프하며 원문 단어를 잃었을 수 있음
            # (예: '정수기' → '식수대') → 원문 키워드로 이웃 답변 한 번 더 검색
            kw2 = extract_keyword(message)
            if kw2 and _norm(kw2) != _norm(facility):
                community = search_community_keyword(kw2, limit=4)
                if community:
                    keyword = kw2
        if not community:
            official = search_places(facility, limit=3)
    else:
        # 카테고리도 인텐트도 아님 → 핵심 키워드로 검색
        keyword = extract_keyword(message)
        if keyword:
            # ⭐ 이웃 답변의 시설명·설명·원문 질문부터 먼저 검색
            community = search_community_keyword(keyword, limit=4)
            if not community:
                official = search_places(keyword, limit=3)
            if community or official:
                facility = keyword  # 감지된 것으로 취급 (표시용)

    locations = _to_locations(community, official)

    # ── 3. 답변 텍스트 구성 ──
    if community:
        if keyword:
            header = f"이웃 답변 속에서 '{keyword}' 관련 정보를 찾았어요 🔎"
        else:
            header = f"이웃들이 알려준 '{facility}' 정보예요 📍"
        lines = [header, ""]
        for i, r in enumerate(community, 1):
            lines.append(f"{i}. {r['facility_name']}" + (f" — {r['address']}" if r.get("address") else ""))
            if r.get("description"):
                lines.append(f"   💬 \"{r['description']}\"")
        lines += ["", "아래 버튼으로 지도에서 바로 확인해보세요!"]
        return ChatResponse(
            reply="\n".join(lines),
            detected_facility=facility,
            match_count=len(community),
            locations=locations,
        )

    if official:
        lines = [f"이웃 제보는 아직 없지만, 공식 장소 데이터에서 '{facility}'을(를) 찾았어요 🗂️", ""]
        for i, p in enumerate(official, 1):
            lines.append(f"{i}. {p['title']}" + (f" — {p['addr1']}" if p.get("addr1") else ""))
        lines += ["", "직접 가보셨다면 게시판에 후기를 남겨주세요. 다음 이웃에게 큰 도움이 돼요 🙌"]
        return ChatResponse(
            reply="\n".join(lines),
            detected_facility=facility,
            match_count=len(official),
            locations=locations,
        )

    if facility:
        # 카테고리는 알아들었는데 데이터가 아무것도 없음
        waiting = count_open_requests(facility)
        wait_line = (
            f"지금 게시판에 '{facility}' 관련 요청 {waiting}건이 답변을 기다리고 있어요. "
            "혹시 아신다면 이웃에게 답을 남겨주세요 🙌\n"
            if waiting > 0 else
            f"게시판에 '{facility}' 요청 글을 올려두시면 현지인이 위치로 답해줄 거예요!\n"
        )
        return ChatResponse(
            reply=(
                f"'{facility}'... 아직 이웃 제보도, 공식 데이터도 없네요 😢\n\n"
                + wait_line +
                "대신 이런 건 어떠세요?"
            ),
            detected_facility=facility,
            suggestions=_suggest_similar(facility),
        )

    # 완전히 못 알아들음
    return ChatResponse(
        reply=(
            "음, 어떤 장소를 찾으시는지 잘 모르겠어요 🤔\n"
            "'철봉 어디 있어?', '도서관 알려줘' 처럼 물어봐 주시면 찾아드릴게요.\n\n"
            "지금 이웃들이 자주 찾는 곳들이에요:"
        ),
        suggestions=list_db_facilities()[:6],
    )


# =========================================================
# (선택) OpenAI로 답변 문장만 다듬기 — 구조 데이터는 그대로 유지
# =========================================================

def polish_with_llm(result: ChatResponse, user_message: str) -> ChatResponse:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not result.locations:
        return result
    try:
        from openai import OpenAI
    except ImportError:
        return result

    loc_lines = "\n".join(
        f"- {l.name}" + (f" ({l.address})" if l.address else "") +
        (f" / 후기: {l.description}" if l.description else "") +
        f" / 출처: {'이웃 제보' if l.source == 'community' else '공식 데이터'}"
        for l in result.locations
    )
    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": (
                    "당신은 대전 동네 장소를 안내하는 '동네레이더' 챗봇입니다. "
                    "아래 장소 목록만 근거로, 3~6줄로 친근하게 안내하세요. "
                    "없는 정보를 지어내지 말고, 좌표 숫자는 언급하지 마세요. "
                    "이모지를 적당히 사용하세요."
                )},
                {"role": "user", "content": f"[질문]\n{user_message}\n\n[장소 목록]\n{loc_lines}"},
            ],
            max_tokens=400,
            temperature=0.6,
        )
        text = (completion.choices[0].message.content or "").strip()
        if text:
            result.reply = text
    except Exception as exc:  # noqa: BLE001
        print(f"[chat] OpenAI 다듬기 실패, 템플릿 유지: {exc}")
    return result


# =========================================================
# 라우트
# =========================================================

@router.post("/", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    result = build_reply(body.message)
    return polish_with_llm(result, body.message)


@router.get("/health")
def chat_health() -> dict:
    return {
        "status": "ok",
        "db_exists": DB_PATH.exists(),
        "known_facilities": len(list_db_facilities()),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
    }
