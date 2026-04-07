from pydantic import BaseModel, EmailStr
from typing import Optional

class UserBase(BaseModel):
    email: EmailStr
    name: str
    birth_year: int
    gender: str

# 회원가입할 때 서버로 보낼 데이터
class UserCreate(BaseModel):
    email: EmailStr   # 이메일 형식 검증
    password: str     # 비밀번호
    name: str         # 이름
    birth_year: int   # 생년
    gender: str       # 성별

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