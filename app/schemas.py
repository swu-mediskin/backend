from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Dict, List, Literal, Optional

class UserBase(BaseModel):
    email: EmailStr
    name: str
    birth_year: int
    gender: str

# 회원가입할 때 서버로 보낼 데이터
class UserCreate(BaseModel):
    email: EmailStr   # 이메일 형식 검증
    password: str= Field(..., description="비밀번호", example="password123")     # 비밀번호
    name: str         # 이름
    birth_year: int = Field(..., description="출생 연도", example=2003)   # 생년
    gender: str = Field(    # 성별
        ..., 
        description="성별 (M: 남성, F: 여성)", 
        pattern="^(M|F)$",
        example="F"
    )

# 회원가입 성공 시 돌려줄 응답
class UserResponse(UserBase):
    id: int

    class Config:
        from_attributes = True

# 로그인할 때 서버로 보낼 데이터
class UserLogin(BaseModel):
    email: EmailStr
    password: str

# 로그인 성공 시 돌려줄 응답 
class Token(BaseModel):
    message: str
    user_id: int
    name: str

# 정보 수정
class UserUpdate(BaseModel):
    name: Optional[str] = None
    birth_year: Optional[int] = Field(None, description="수정할 출생 연도", example=2003)
    gender: Optional[str] = Field(None, description="수정할 성별", pattern="^(M|F)$", example="F")
    password: Optional[str] = Field(None, description="수정할 비밀번호", example="newpassword123")

class ExtractedMedicalData(BaseModel):
    age: Optional[int] = Field(None, description="환자의 나이 (숫자만 추출)")
    gender: Optional[str] = Field(None, description="환자의 성별. MALE, FEMALE, UNKNOWN 중 하나로 정규화")
    region: Optional[str] = Field(
        None,
        description="피부 병변이 발생한 신체 부위. FACE, BACK, CHEST, NOSE, FOREARM, ARM, NECK, EAR, HAND, THIGH, ABDOMEN, LIP, FOOT, SCALP 중 가장 가까운 하나(영문 대문자)로 정규화",
    )
    smoke: Optional[bool] = Field(None, description="흡연 여부")
    drink: Optional[bool] = Field(None, description="음주 여부")
    itch: Optional[bool] = Field(None, description="가려움 여부")
    pain: Optional[bool] = Field(None, description="통증 여부")
    growth: Optional[bool] = Field(None, description="병변이 커졌는지 여부")
    clinical_prompt: str = Field(
        ..., 
        description="BioMedCLIP 모델 입력용 영문 임상 문장. 없는 정보나 false인 증상은 문장에 절대 포함 금지."
    )

class NaturalLanguageRequest(BaseModel):
    description_text: str

class ExtractedMedicalData(BaseModel):
    age: Optional[int] = Field(None, description="환자의 나이 (숫자만 추출)")
    gender: Optional[str] = Field(None, description="환자의 성별. MALE, FEMALE, UNKNOWN 중 하나로 정규화")
    region: Optional[str] = Field(
        None,
        description="피부 병변이 발생한 신체 부위. FACE, BACK, CHEST, NOSE, FOREARM, ARM, NECK, EAR, HAND, THIGH, ABDOMEN, LIP, FOOT, SCALP 중 가장 가까운 하나(영문 대문자)로 정규화",
    )
    smoke: Optional[bool] = Field(None, description="흡연 여부")
    drink: Optional[bool] = Field(None, description="음주 여부")
    itch: Optional[bool] = Field(None, description="가려움 여부")
    pain: Optional[bool] = Field(None, description="통증 여부")
    growth: Optional[bool] = Field(None, description="병변이 커졌는지 여부")
    clinical_prompt: str = Field(
        ..., 
        description="BioMedCLIP 모델 입력용 영문 임상 문장. 없는 정보나 false인 증상은 문장에 절대 포함 금지."
    )

class NaturalLanguageRequest(BaseModel):
    description_text: str

# 개별 대화 메시지 정의
class ChatMessage(BaseModel):
    role: Literal["user", "model"]  # 'user' = 환자, 'model' = AI
    text: str  # 대화 내용

    # 'assistant'로 보내도 Gemini 형식('model')으로 변환
    @field_validator("role", mode="before")
    @classmethod
    def normalize_role(cls, v):
        return "model" if v == "assistant" else v

# API 요청 스키마
class DiagnosisRequest(BaseModel):
    # 지금까지의 전체 대화를 배열로 받음 (환자 메시지로 시작하고 끝나야 함)
    messages: List[ChatMessage] = Field(..., min_length=1)

    # 입력 안 들어오면 기본값(null 또는 '미상') 자동 적용
    age: Optional[int] = Field(default=None, description="환자 나이")
    gender: Optional[str] = Field(default="미상", description="환자 성별 (M/F/미상)")

    # 문진 종료 시 AI 서버(/predict)에 함께 보낼 피부 사진 (/upload-skin-image 응답의 image_id)
    image_id: int = Field(..., description="업로드한 피부 이미지 ID")

    @field_validator("messages")
    @classmethod
    def starts_and_ends_with_user(cls, v):
        if v[0].role != "user" or v[-1].role != "user":
            raise ValueError("대화는 환자(user) 메시지로 시작하고 끝나야 합니다.")
        return v

# 채팅 응답 스키마 (Gemini 구조화 출력 형식으로도 사용)
class ChatResponse(BaseModel):
    reply: str = Field(..., description="환자에게 보여줄 AI의 한국어 답변")
    is_complete: bool = Field(..., description="문진에 필요한 정보가 충분히 모였는지 여부")
    extracted_data: Optional[ExtractedMedicalData] = Field(
        None, description="is_complete가 true일 때만 채워지는 최종 분석 결과"
    )

# AI 서버(/predict) 예측 결과
class PredictionResult(BaseModel):
    prediction: str = Field(..., description="예측 클래스 코드 (NEV/BCC/SCC/MEL)")
    prediction_name: str = Field(..., description="예측 클래스 한국어 이름")
    confidence: float = Field(..., description="예측 확률 (0~1)")
    all_probabilities: Dict[str, float] = Field(..., description="클래스별 확률 (0~1)")

# /api/diagnose 응답 (Gemini 응답 + 종료 시점의 AI 예측 결과)
class DiagnosisResponse(ChatResponse):
    prediction: Optional[PredictionResult] = Field(
        None, description="is_complete가 true일 때만 채워지는 AI 서버 예측 결과"
    )
