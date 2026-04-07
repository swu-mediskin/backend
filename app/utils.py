from passlib.context import CryptContext

# 비밀번호 해싱을 위한 Passlib 컨텍스트 설정
pwd_context =CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str):
    """비밀번호 암호화"""
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    """비밀번호 검증"""
    return pwd_context.verify(plain_password, hashed_password)