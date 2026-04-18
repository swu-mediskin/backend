from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, OperationalError
from jose import JWTError
from .database import engine, get_db
from . import models, schemas, utils
from .models import User
from .auth import get_current_user
from . import auth
from fastapi import Response
import logging


# FastAPI 인스턴스 생성
app = FastAPI()

# 서버 시작 시 테이블 생성
models.Base.metadata.create_all(bind=engine)

# 로깅 설정
logger = logging.getLogger("uvicorn.error")


# 일관된 HTTPException 핸들러
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # 인증/권한 관련 응답은 일관된 JSON 구조와 WWW-Authenticate 헤더 유지
    content = {"error": exc.detail}
    headers = getattr(exc, "headers", None)
    if headers:
        return JSONResponse(status_code=exc.status_code, content=content, headers=headers)
    return JSONResponse(status_code=exc.status_code, content=content)


# 요청 유효성 에러 핸들러
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("Request validation error: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "요청 데이터 유효성 검사 실패", "details": exc.errors()},
    )


@app.exception_handler(JWTError)
async def jwt_error_handler(request: Request, exc: JWTError):
    logger.warning("JWTError: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "토큰이 유효하지 않습니다."},
        headers={"WWW-Authenticate": "Bearer"},
    )


# SQLAlchemy 무결성/운영 오류 처리
@app.exception_handler(IntegrityError)
async def sqlalchemy_integrity_error_handler(request: Request, exc: IntegrityError):
    logger.exception("Database integrity error")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "데이터베이스 무결성 오류", "detail": str(exc.orig) if hasattr(exc, 'orig') else str(exc)},
    )


@app.exception_handler(OperationalError)
async def sqlalchemy_operational_error_handler(request: Request, exc: OperationalError):
    logger.exception("Database operational error")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "데이터베이스 연결 또는 운영 오류가 발생했습니다."},
    )


# 일반 예외 처리 (최후의 수단)
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "서버 내부 오류가 발생했습니다."},
    )

# 루트 엔트포인트 정의
@app.get("/")
async def read_root():
    return {"message": "Hello, World!"}

# 회원가입
@app.post("/signup", response_model=schemas.UserCreate)
def signup(user: schemas.UserCreate, db: Session = Depends(get_db)):
    # 이메일 중복 확인
    db_user = db.query(models.User).filter(models.User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="이미 가입된 이메일입니다.")
    
    print(f"DEBUG: incoming password is -> {user.password}")

    # 비밀번호 해싱
    hashed_pwd = utils.hash_password(user.password)

    # DB 모델 인스턴스 생성
    new_user = models.User(
        email=user.email,
        password=hashed_pwd,
        name=user.name,
        birth_year=user.birth_year,
        gender=user.gender
    )

    # DB에 저장 (무결성 오류 등 예외 처리)
    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
    except IntegrityError as ie:
        db.rollback()
        logger.exception("IntegrityError during signup")
        raise HTTPException(status_code=400, detail="이미 존재하는 데이터가 있습니다. 이메일을 확인해주세요.")
    except Exception as e:
        db.rollback()
        logger.exception("Unexpected error during signup: %s", e)
        raise HTTPException(status_code=500, detail="회원가입 처리 중 서버 오류가 발생했습니다.")

    return new_user

# 로그인
@app.post("/login")
def login(user_credentials: schemas.UserLogin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == user_credentials.email).first()
    
    if not user or not utils.verify_password(user_credentials.password, user.password):
        raise HTTPException(status_code=403, detail="이메일 또는 비밀번호가 틀렸습니다.")

    # Access Token 생성 (유저 ID를 문자열로 담아)
    access_token = auth.create_access_token(data={"sub": str(user.id)})

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "name": user.name
    }

# 회원 탈퇴
@app.delete("/withdraw", status_code=status.HTTP_204_NO_CONTENT)
def withdraw_user(
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(auth.get_current_user) # 토큰에서 현재 유저 가져오기
):
    """
    현재 로그인된 사용자를 탈퇴 처리합니다.
    """

    # 삭제 진행
    try:
        db.delete(current_user)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="탈퇴 처리 중 오류가 발생했습니다."
        )
    
    return None

# 내 정보 조회
@app.get("/users/me", response_model=schemas.UserResponse)
def read_user_me(current_user: models.User = Depends(get_current_user)):
    return current_user

# 특정 사용자 ID로 정보 조회
@app.get("/users/{user_id}", response_model=schemas.UserResponse)
def get_user_profile(user_id: int, db: Session = Depends(get_db)):
    # DB에서 해당 ID의 유저 검색
    user = db.query(models.User).filter(models.User.id == user_id).first()
    
    # 유저가 없으면 404 에러
    if not user:
        raise HTTPException(status_code=404, detail="해당 사용자를 찾을 수 없습니다.")
    
    return user

# 내 정보 수정 API
@app.patch("/users/me", response_model=schemas.UserResponse)
def update_my_info(
    updated_data: schemas.UserUpdate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    for key, value in updated_data.model_dump(exclude_unset=True).items():
        setattr(current_user, key, value)
    
    db.commit()
    db.refresh(current_user)
    return current_user