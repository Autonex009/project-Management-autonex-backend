import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

DATABASE_URL = "postgresql://postgres.ajcynsqzablkvbqeihda:Pm-portal%40123@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

db = SessionLocal()
res = db.execute(text("SELECT u.id, u.name, u.role, u.employee_id, e.designation, e.skills FROM users u LEFT JOIN employees e ON u.employee_id = e.id WHERE u.name ILIKE '%testAccForOnboardingPipeline%';")).fetchall()
print(res)
