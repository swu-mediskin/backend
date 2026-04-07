# 1. 파이썬 3.11 슬림 버전 이미지를 기반으로 시작 (용량이 가볍고 빨라)
FROM python:3.11-slim

# 2. 컨테이너 내부의 작업 디렉토리를 /app으로 설정
WORKDIR /app

# 3. 환경 변수 설정 (파이썬이 출력물을 버퍼링 없이 즉시 보여주게 함)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 4. 시스템 패키지 업데이트 및 필요한 라이브러리 설치 (MySQL 연결용)
RUN apt-get update && apt-get install -y \
    build-essential \
    libmariadb-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 5. requirements.txt 파일을 먼저 복사 (캐싱 효율을 높이기 위해)
COPY requirements.txt .

# 6. 파이썬 패키지 설치
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 7. 현재 폴더의 모든 소스 코드를 컨테이너의 /app으로 복사
COPY . .

# 8. 서버가 사용할 8080번 포트를 외부에 공개
EXPOSE 8080

# 9. 서버 실행 명령어 (gunicorn을 사용해 uvicorn 워커로 실행)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "-k", "uvicorn.workers.UvicornWorker", "app.main:app"]