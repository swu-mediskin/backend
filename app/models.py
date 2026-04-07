from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, index=True)
    password = Column(String(255))
    name = Column(String(255))
    birth_year = Column(Integer)
    gender = Column(String(50))  # SQL의 enum 대응
    created_at = Column(Date)
    updated_at = Column(Date)
    deleted_at = Column(Date, nullable=True)

    diagnoses = relationship("Diagnosis", back_populates="user")

class Disease(Base):
    __tablename__ = "diseases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name_ko = Column(String(255))
    name_en = Column(String(255))
    definition = Column(Text)
    symptoms = Column(Text)
    guidelines = Column(Text)

    results = relationship("AIAnalysisResult", back_populates="disease")

class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    image_path = Column(String(500))
    region = Column(String(100)) # SQL의 enum 대응
    created_at = Column(Date)

    user = relationship("User", back_populates="diagnoses")
    analysis_result = relationship("AIAnalysisResult", back_populates="diagnosis", uselist=False)
    # 예약어 metadata 대신 user_metadata 사용
    user_metadata = relationship("UserMetadata", back_populates="diagnosis", uselist=False)

class AIAnalysisResult(Base):
    __tablename__ = "ai_analysis_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    disease_id = Column(Integer, ForeignKey("diseases.id"), nullable=False)
    diagnosis_id = Column(Integer, ForeignKey("diagnoses.id"), nullable=False)
    classification_class = Column(String(100)) # SQL의 enum 대응
    probability = Column(Float)
    medgemma_report = Column(Text)
    risk_signs_description = Column(Text)
    recommendations = Column(Text)

    disease = relationship("Disease", back_populates="results")
    diagnosis = relationship("Diagnosis", back_populates="analysis_result")

class UserMetadata(Base):
    __tablename__ = "user_metadata"

    id = Column(Integer, primary_key=True, autoincrement=True)
    diagnosis_id = Column(Integer, ForeignKey("diagnoses.id"), nullable=False)
    
    # SQL 파일의 타입(boolean, enum 등) 반영
    smoke = Column(Boolean)
    drink = Column(Boolean)
    background_father = Column(Boolean)
    background_mother = Column(Boolean)
    age = Column(Integer)
    pesticide = Column(Boolean)
    gender = Column(String(50)) # enum
    skin_cancer_history = Column(Boolean)
    cancer_history= Column(Boolean)
    has_piped_water = Column(Boolean)
    has_sewage_system = Column(Boolean)
    fitspatrick = Column(String(50)) # enum
    region = Column(String(100)) # enum
    diameter_1 = Column(Boolean)
    diameter_2 = Column(Boolean)
    diagnostic = Column(String(100)) # enum
    itch = Column(Boolean)
    grew = Column(Boolean)
    hurt = Column(Boolean)
    changed = Column(Boolean)
    bleed = Column(Boolean)
    elevation = Column(Boolean)
    biopsed = Column(Boolean) # 생검 여부

    diagnosis = relationship("Diagnosis", back_populates="user_metadata")