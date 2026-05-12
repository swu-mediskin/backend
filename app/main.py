from fastapi import FastAPI, HTTPException, Depends, status, Request, File, UploadFile
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
import shutil
import os
from uuid import uuid4
import requests


# FastAPI 인스턴스 생성
app = FastAPI()

# 이미지가 저장될 경로 설정
UPLOAD_DIR = "static/uploads"

# 폴더 없으면 자동 생성
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

from fastapi.staticfiles import StaticFiles

# http://아이피:8080/static/uploads/파일명 으로 접근
app.mount("/static", StaticFiles(directory="static"), name="static")

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


# 일반 예외 처리
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "서버 내부 오류가 발생했습니다."},
    )

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

# 이미지 업로드
@app.post("/upload-skin-image", status_code=status.HTTP_201_CREATED)
async def upload_skin_image(
    file: UploadFile = File(...), 
    current_user: models.User = Depends(auth.get_current_user), # 로그인한 사람만 업로드 가능
    db: Session = Depends(get_db)
):
    """
    카메라로 찍은 사진을 서버에 저장합니다.
    """
    # 파일 확장자 체크 
    extension = file.filename.split(".")[-1].lower()
    if extension not in ["jpg", "jpeg", "png"]:
        raise HTTPException(status_code=400, detail="이미지 파일(jpg, png)만 업로드 가능합니다.")

    # 파일 이름 고유화
    filename = f"{uuid4()}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    # 파일 실제 저장
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

            new_image = models.SkinImage(
                user_id=current_user.id,
                file_path=file_path
            )
            db.add(new_image)
            db.commit()
            db.refresh(new_image)
    except Exception as e:
        logger.exception("파일 저장 중 오류 발생")
        raise HTTPException(status_code=500, detail="이미지 저장에 실패했습니다.")

    return {
        "message": "이미지 업로드 성공",
        "file_path": file_path,
        "user_email": current_user.email
    }

# 이미지 + 메타데이터 업로드
@app.post("/upload-skin", status_code=status.HTTP_201_CREATED)
async def upload_skin_and_diagnose(
    file: UploadFile = File(...), 
    
    age: int = 0,
    gender: str = "MALE",
    region: str = "FACE",
    smoke: bool = False,
    drink: bool = False,
    pesticide: bool = False,
    skin_cancer_history: bool = False,
    cancer_history: bool = False,
    fitspatrick: str = "1",
    diameter_1: float = 0.0,
    diameter_2: float = 0.0,
    itch: bool = False,
    grew: bool = False,
    hurt: bool = False,
    changed: bool = False,
    bleed: bool = False,
    elevation: bool = False,
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    # 이미지 저장 로직
    filename = f"{uuid4()}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 진단 기록 생성 (Diagnosis 테이블)
    new_diagnosis = models.Diagnosis(user_id=current_user.id, image_path=file_path)
    db.add(new_diagnosis)
    db.commit()
    db.refresh(new_diagnosis)

    # 사용자 메타데이터 저장 (UserMetadata 테이블)
    new_metadata = models.UserMetadata(
        diagnosis_id=new_diagnosis.id,
        age=age, gender=gender, region=region, smoke=smoke, drink=drink,
        pesticide=pesticide, skin_cancer_history=skin_cancer_history,
        cancer_history=cancer_history, fitspatrick=fitspatrick,
        diameter_1=diameter_1, diameter_2=diameter_2,
        itch=itch, grew=grew, hurt=hurt, changed=changed,
        bleed=bleed, elevation=elevation
    )
    db.add(new_metadata)
    db.commit()

    # 데이터 가공
    ai_meta_data = {
        "age": str(age),
        "gender": gender.upper(),
        "region": region.upper(),
        "smoke": "YES" if smoke else "NO",
        "drink": "YES" if drink else "NO",
        "pesticide": "YES" if pesticide else "NO",
        "skin_cancer_history": "YES" if skin_cancer_history else "NO",
        "cancer_history": "YES" if cancer_history else "NO",
        "itch": "YES" if itch else "NO",
        "grew": "YES" if grew else "NO",
        "hurt": "YES" if hurt else "NO",
        "changed": "YES" if changed else "NO",
        "bleed": "YES" if bleed else "NO",
        "elevation": "YES" if elevation else "NO",
        "fitspatrick": str(fitspatrick),
        "diameter_1": str(diameter_1),
        "diameter_2": str(diameter_2)
    }

    # AI 서버 호출 및 결과 받기
    try:
        AI_SERVER_URL = "http://3.37.47.74:8080/predict" 
        with open(file_path, "rb") as f:
            response = requests.post(
                AI_SERVER_URL, 
                data=ai_meta_data, 
                files={"file": (filename, f, file.content_type)}
            )
        
        ai_result = response.json()
        
        # DB에 분석 결과 업데이트
        new_diagnosis.result_class = ai_result.get("predicted_class")
        new_diagnosis.result_prob = ai_result.get("probability")
        db.commit()

        return {
            "message": "분석 완료",
            "diagnosis_id": new_diagnosis.id,
            "result": ai_result
        }
    except Exception as e:
        return {"error": "AI 서버 연결 실패", "details": str(e)}