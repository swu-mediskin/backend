from fastapi import FastAPI, HTTPException, Depends, status
from sqlalchemy.orm import Session
from .database import engine, get_db
from . import models, schemas, utils
from .models import User
from .auth import get_current_user
from . import auth


# FastAPI 인스턴스 생성
app = FastAPI()

# 서버 시작 시 테이블 생성
models.Base.metadata.create_all(bind=engine)

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

    # DB에 저장
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

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