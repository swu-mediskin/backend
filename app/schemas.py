from pydantic import BaseModel, EmailStr, Field
from typing import Optional

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