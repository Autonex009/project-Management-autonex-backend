import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

DATABASE_URL = "postgresql://postgres.ajcynsqzablkvbqeihda:Pm-portal%40123@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

db = SessionLocal()

# Find the user
res = db.execute(text("SELECT id FROM users WHERE name ILIKE '%testAccForOnboardingPipeline%';")).fetchone()
if res:
    user_id = res[0]
    print(f"Found user_id: {user_id}")
    
    # Find pipeline
    pipeline = db.execute(text(f"SELECT id, status FROM onboarding_pipeline WHERE candidate_id = {user_id} ORDER BY created_at DESC LIMIT 1;")).fetchone()
    if pipeline:
        print(f"Found pipeline: {pipeline[0]}, current status: {pipeline[1]}")
        # Update
        db.execute(text(f"UPDATE onboarding_pipeline SET status = 'pending_confirmation' WHERE id = {pipeline[0]};"))
        db.commit()
        print("Updated pipeline status to pending_confirmation.")
    else:
        print("No pipeline found for this user.")
else:
    print("User not found.")

