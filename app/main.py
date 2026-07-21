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
from datetime import datetime
from typing import Optional
from fastapi import Query
import re
from fastapi import APIRouter
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from app.schemas import NaturalLanguageRequest
from app.service import extract_and_translate_via_gemini

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

# 회원 가입 시 받는 메타데이터
@app.post("/user/metadata/basic", status_code=status.HTTP_201_CREATED)
async def save_basic_metadata(
    smoke: Optional[bool] = Query(None),
    drink: Optional[bool] = Query(None),
    pesticide: Optional[bool] = Query(None),
    skin_cancer_history: Optional[bool] = Query(None),
    cancer_history: Optional[bool] = Query(None),
    fitspatrick: str = Query(default="0"),
    
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    # 🌟 나이 자동 계산: 현재 연도 - 유저의 출생 연도
    current_year = datetime.now().year
    calculated_age = current_year - current_user.birth_year

    existing_metadata = db.query(models.UserMetadata).filter(
        models.UserMetadata.diagnosis_id == None, 
        models.UserMetadata.gender == current_user.gender
    ).first()
    
    if existing_metadata:
        existing_metadata.smoke = smoke
        existing_metadata.drink = drink
        existing_metadata.pesticide = pesticide
        existing_metadata.skin_cancer_history = skin_cancer_history
        existing_metadata.cancer_history = cancer_history
        existing_metadata.fitspatrick = fitspatrick
        existing_metadata.age = calculated_age  
    else:
        # 없다면 새로 한 줄 생성
        new_metadata = models.UserMetadata(
            diagnosis_id=None,
            age=calculated_age,  
            gender=getattr(current_user, "gender", "MALE"),
            region="FACE",
            smoke=smoke,
            drink=drink,
            pesticide=pesticide,
            skin_cancer_history=skin_cancer_history,
            cancer_history=cancer_history,
            fitspatrick=fitspatrick
        )
        db.add(new_metadata)
        
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("기본 정보 저장 중 에러 발생")
        raise HTTPException(status_code=500, detail=f"저장 실패: {str(e)}")
        
    return {"message": "기본 정보 저장 완료"}

# 기초 메타데이터 조회
@app.get("/user/metadata/basic", status_code=status.HTTP_200_OK)
async def get_basic_metadata(
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    existing_metadata = db.query(models.UserMetadata).filter(
        models.UserMetadata.diagnosis_id == None, 
        models.UserMetadata.gender == current_user.gender
    ).first()
    
    if not existing_metadata:
        raise HTTPException(
            status_code=404, 
            detail="등록된 기본 정보가 없습니다. 최초 설문을 먼저 진행해주세요."
        )

    from datetime import datetime
    current_year = datetime.now().year
    calculated_age = current_year - current_user.birth_year

    return {
        "smoke": existing_metadata.smoke,
        "drink": existing_metadata.drink,
        "pesticide": existing_metadata.pesticide,
        "skin_cancer_history": existing_metadata.skin_cancer_history,
        "cancer_history": existing_metadata.cancer_history,
        "fitspatrick": existing_metadata.fitspatrick,
        "age": calculated_age
    }

# 기초 메타데이터 수정 
@app.patch("/user/metadata/basic", status_code=status.HTTP_200_OK)
async def update_basic_metadata(
    smoke: bool = None,
    drink: bool = None,
    pesticide: bool = None,
    skin_cancer_history: bool = None,
    cancer_history: bool = None,
    fitspatrick: str = 0,
    
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db)
):
    # 수정할 기존 기본 정보 찾기
    existing_metadata = db.query(models.UserMetadata).filter(
        models.UserMetadata.diagnosis_id == None, 
        models.UserMetadata.gender == current_user.gender
    ).first()
    
    # 만약 기초 정보가 아예 없다면 에러 반환
    if not existing_metadata:
        raise HTTPException(status_code=404, detail="수정할 기본 정보가 존재하지 않습니다. 먼저 등록해주세요.")

    if smoke is not None: existing_metadata.smoke = smoke
    if drink is not None: existing_metadata.drink = drink
    if pesticide is not None: existing_metadata.pesticide = pesticide
    if skin_cancer_history is not None: existing_metadata.skin_cancer_history = skin_cancer_history
    if cancer_history is not None: existing_metadata.cancer_history = cancer_history
    if fitspatrick is not None: existing_metadata.fitspatrick = fitspatrick

    from datetime import datetime
    current_year = datetime.now().year
    existing_metadata.age = current_year - current_user.birth_year

    try:
        db.commit()
        db.refresh(existing_metadata)
    except Exception as e:
        db.rollback()
        logger.exception("기본 정보 수정 중 에러 발생")
        raise HTTPException(status_code=500, detail=f"수정 실패: {str(e)}")
        
    return {
        "message": "기본 정보가 성공적으로 수정되었습니다.",
        "updated_data": {
            "smoke": existing_metadata.smoke,
            "drink": existing_metadata.drink,
            "pesticide": existing_metadata.pesticide,
            "skin_cancer_history": existing_metadata.skin_cancer_history,
            "cancer_history": existing_metadata.cancer_history,
            "fitspatrick": existing_metadata.fitspatrick,
            "age": existing_metadata.age
        }
    }

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
    region: str = "FACE",
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

    user_basic_info = db.query(models.UserMetadata).filter(
        models.UserMetadata.diagnosis_id == None,
        models.UserMetadata.gender == current_user.gender  
    ).order_by(models.UserMetadata.id.desc()).first()

    smoke = user_basic_info.smoke if user_basic_info else False
    drink = user_basic_info.drink if user_basic_info else False
    pesticide = user_basic_info.pesticide if user_basic_info else False
    skin_cancer_history = user_basic_info.skin_cancer_history if user_basic_info else False
    cancer_history = user_basic_info.cancer_history if user_basic_info else False
    fitspatrick = user_basic_info.fitspatrick if user_basic_info else "1"

    # 사용자 메타데이터 저장 (UserMetadata 테이블)
    new_metadata = models.UserMetadata(
        diagnosis_id=new_diagnosis.id,
        age=age, 
        gender=getattr(current_user, "gender", "MALE"), 
        region=region, 
        smoke=smoke, 
        drink=drink,
        pesticide=pesticide, 
        skin_cancer_history=skin_cancer_history,
        cancer_history=cancer_history, 
        fitspatrick=fitspatrick,
        diameter_1=diameter_1, 
        diameter_2=diameter_2,
        itch=itch, grew=grew, hurt=hurt, changed=changed,
        bleed=bleed, elevation=elevation
    )
    db.add(new_metadata)
    db.commit()

    # 데이터 가공
    ai_meta_data = {
        "age": str(age),
        "gender": getattr(current_user, "gender", "MALE").upper(),
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
        new_diagnosis.result_class = ai_result.get("prediction")
        new_diagnosis.result_prob = ai_result.get("confidence")
        db.commit()

        return {
            "message": "분석 완료",
            "diagnosis_id": new_diagnosis.id,
            "result": ai_result
        }
    except Exception as e:
        return {"error": "AI 서버 연결 실패", "details": str(e)}
    
# 자연어 메타데이터 추출
# 1. 클라이언트가 보낼 자연어 데이터 스키마
class NaturalLanguageInput(BaseModel):
    text: str  # 예: "저는 25세 여성이고, 얼굴에 붉은 반점이 생겼어요. 흡연은 안 합니다."

# 2. 추출된 메타데이터 결과를 담을 스키마
class ExtractedMetadata(BaseModel):
    age: Optional[int] = None
    gender: Optional[str] = None
    region: Optional[str] = None
    smoke: Optional[bool] = None
    # 필요하면 drink, fitspatrick 등 추가 가능

def parse_metadata_from_text(text: str) -> dict:
    extracted = {
        "age": None,
        "gender": None,
        "region": None,
        "smoke": None
    }
    
    # 1. 나이 추출 (예: 25세, 30대, 45살)
    age_match = re.search(r'(\d+)(세|살|대)', text)
    if age_match:
        extracted["age"] = int(age_match.group(1))
        
    # 2. 성별 추출
    if re.search(r'(여성|여자|여)', text):
        extracted["gender"] = "FEMALE"
    elif re.search(r'(남성|남자|남)', text):
        extracted["gender"] = "MALE"
        
    # 3. 발생 부위 추출
    region_match = re.search(r'(얼굴|팔|다리|등|배|가슴|목|손|발|두피)', text)
    if region_match:
        # DB에 저장하는 규격에 맞춰 영문으로 매핑해주면 더 좋아!
        region_map = {"얼굴": "FACE", "팔": "ARM", "다리": "LEG", "등": "BACK"}
        korean_region = region_match.group(1)
        extracted["region"] = region_map.get(korean_region, korean_region)
        
    # 4. 흡연 여부 추출
    if re.search(r'(비흡연|안 피|안피|금연)', text):
        extracted["smoke"] = False
    elif re.search(r'(흡연|담배|피웁)', text):
        extracted["smoke"] = True
        
    return extracted

@app.post("/api/extract-metadata", response_model=ExtractedMetadata)
async def extract_metadata_api(input_data: NaturalLanguageInput):
    try:
        result = parse_metadata_from_text(input_data.text)
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 추출 중 오류 발생: {str(e)}")

@app.post("/diagnosis/process-text", status_code=status.HTTP_200_OK, tags=["Diagnosis"])
async def process_user_natural_language(payload: NaturalLanguageRequest):
    # 1. 서비스 레이어 호출 (제미나이를 통한 텍스트 분석 및 변환)
    processed_result = extract_and_translate_via_gemini(payload.description_text)
    
    # 2. 필수 데이터 유효성 검증 (나이, 성별 가드 로직)
    if processed_result.age is None or processed_result.gender == "UNKNOWN":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="입력된 내용에서 필수 정보(나이, 성별)를 추출할 수 없습니다. 더 자세히 입력해 주세요."
        )

    # 3. 결과 반환 (추후 DB 저장이나 BioMedCLIP 서버 전송 로직은 이 아래에 추가)
    return {
        "message": "제미나이 기반 메타데이터 추출 및 영문 프롬프트 변환 성공",
        "original_text": payload.description_text,
        "extracted_data": processed_result.model_dump()
    }