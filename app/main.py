from fastapi import FastAPI, HTTPException, Depends, status
from sqlalchemy.orm import Session
from .database import engine, get_db
from . import models, schemas, utils
from app.models import User


# FastAPI 인스턴스 생성
app = FastAPI()

# 서버 시작 시 테이블 생성
models.Base.metadata.create_all(bind=engine)

# 루트 엔트포인트 정의
@app.get("/")
async def read_root():
    return {"message": "Hello, World!"}

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

    # DB에 저장
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user

@app.post("/login", response_model=schemas.Token)
def login(user_credentials: schemas.UserLogin, db: Session = Depends(get_db)):
    # 이메일로 사용자 찾기
    user = db.query(models.User).filter(models.User.email == user_credentials.email).first()
    
    # 사용자가 없거나 비밀번호가 틀리면 에러 던지기
    if not user:
        raise HTTPException(status_code=403, detail="이메일이 존재하지 않습니다.")
    
    # utils.verify_password(입력비번, DB저장비번)
    if not utils.verify_password(user_credentials.password, user.password):
        raise HTTPException(status_code=403, detail="비밀번호가 틀렸습니다.")

    # 로그인 성공 응답
    return {
        "message": "로그인 성공!",
        "user_id": user.id,
        "name": user.name
    }

@app.delete("/withdraw/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def withdraw_user(user_id: int, db: Session = Depends(get_db)):
    """
    특정 ID를 가진 사용자를 삭제(탈퇴)합니다.
    """
    # DB에서 해당 사용자 찾기
    user = db.query(User).filter(User.id == user_id).first()
    
    # 없으면 404 에러
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="해당 사용자를 찾을 수 없습니다."
        )

    # 삭제 진행
    try:
        db.delete(user)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="탈퇴 처리 중 오류가 발생했습니다."
        )
    
    return None

