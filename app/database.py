import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# 1. .env 파일 로드
load_dotenv()

# 2. 환경 변수에서 DB 접속 정보 가져오기
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

# 3. SQLAlchemy 연결 URL 생성 (MySQL용)
# 포트는 아까 수정했던 3307이 아닌, 컨테이너 내부 통신용인 3306으로 연결됩니다.
# (도커 네트워크 안에서는 서비스 이름인 'db'로 통신 가능)
SQLALCHEMY_DATABASE_URL = f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}@db:3306/{DB_NAME}"

# 4. 데이터베이스 엔진 생성
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,  # 연결 유효성 체크 (끊김 방지)
    pool_recycle=3600    # 1시간마다 연결 갱신
)

# 5. 세션 설정 (DB와 대화하는 창구)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 6. 모델 클래스가 상속받을 기본 클래스
Base = declarative_base()

# 7. DB 세션을 가져오기 위한 의존성 함수
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()