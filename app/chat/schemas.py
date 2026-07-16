"""POST /api/chat 요청·응답 스키마."""

from typing import Literal

from pydantic import BaseModel, Field

# 히스토리 유지 턴 수. RFP III-3-다 "대화 히스토리 유지" 대응.
# 10턴은 팀에서 정한 값 — 토큰 예산과 직결되므로 늘리기 전에 계산해볼 것.
MAX_HISTORY_TURNS = 10


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)

    history: list[ChatMessage] = Field(
        default_factory=list,
        description="이전 대화. 서버에서 최근 MAX_HISTORY_TURNS개만 사용한다.",
    )

    # ⚠️ FE와 조율 필요 — API 명세서 v1.0에는 없던 필드.
    # 화면 시안 ③번의 "지금 탄방역 근천데, 철봉 어디 있어?" +
    # "도보 6분" / "가까운 순" 을 구현하려면 사용자 좌표가 필요하다.
    # 없으면 거리 정렬 없이 키워드 검색만 한다.
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)


class Source(BaseModel):
    """
    답변의 근거. LLM이 생성하지 않는다 — 검색 결과를 그대로 담는다.
    FE는 이걸로 근거 카드/원문 링크를 그린다.
    """

    type: Literal["place", "report"]

    # place면 contentid(문자열), report면 response_id(정수 → 문자열로 통일)
    id: str
    title: str

    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    # 거리 정보 — 사용자 좌표가 있을 때만 채워진다
    distance_m: int | None = None
    walk_minutes: int | None = None

    # report일 때만: 원문 요청 글로 이동하기 위한 ID
    request_id: int | None = None

    # place일 때만: TourAPI 원본 이미지 URL (공공누리 3유형, 변경 금지)
    image_url: str | None = None


class ChatResponse(BaseModel):
    reply: str
    sources: list[Source] = Field(default_factory=list)

    # 컨텍스트가 0건이라 제보를 유도한 경우 True.
    # 시안 ③번의 "📡 레이더에 비는 곳이에요 → 요청 글로 올리기" 버튼 노출용.
    suggest_report: bool = False
