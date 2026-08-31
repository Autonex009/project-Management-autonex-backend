import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

DATABASE_URL = "postgresql://postgres.ajcynsqzablkvbqeihda:Pm-portal%40123@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

db = SessionLocal()
# Find the candidate
user = db.execute(text("SELECT id FROM users WHERE name ILIKE '%testAccForOnboardingPipeline%';")).fetchone()
if user:
    # Update pipeline record
    db.execute(text(f"UPDATE onboarding_pipeline SET status = 'day_5_pending' WHERE candidate_id = {user.id};"))
    db.commit()
    print("Successfully updated candidate to day_5_pending!")
else:
    print("User not found.")
