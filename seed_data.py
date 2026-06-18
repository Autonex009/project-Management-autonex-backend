"""
Comprehensive seed script to populate the Autonex database with realistic sample data.
Usage: python seed_data.py
"""
import os
import sys
from datetime import date, timedelta
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Load environment
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set in .env")
    sys.exit(1)

# Handle Neon PostgreSQL SSL
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
Session = sessionmaker(bind=engine)

# ────────────────────────────────────────────────────
# Step 1: Import models and create tables
# ────────────────────────────────────────────────────
from app.db.database import Base
from app.models.employee import Employee
from app.models.project import DailySheet
from app.models.sub_project import SubProject
from app.models.parent_project import MainProject
from app.models.allocation import Allocation
from app.models.leave import Leave
from app.models.skill import Skill
from app.models.user import User
from app.models.guideline import Guideline
from app.services.auth_service import hash_password

print("🔧 Dropping and recreating all tables...")
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
print("✅ Tables created!\n")

db = Session()

try:
    # ────────────────────────────────────────────────────
    # Step 2: Seed Skills
    # ────────────────────────────────────────────────────
    print("📚 Seeding skills...")
    skill_names = [
        "Data Annotation", "Python", "React", "Node.js", "SQL",
        "Machine Learning", "Product Management", "QA Testing",
        "UI/UX Design", "DevOps", "Data Analysis", "Robotics",
        "NLP", "Computer Vision", "Project Management"
    ]
    skills = []
    for name in skill_names:
        s = Skill(name=name)
        db.add(s)
        skills.append(s)
    db.flush()
    print(f"   ✓ {len(skills)} skills added\n")

    # ────────────────────────────────────────────────────
    # Step 3: Seed Employees
    # ────────────────────────────────────────────────────
    print("👥 Seeding employees...")
    employees_data = [
        # Program Managers
        {"name": "Arjun Mehta", "email": "arjun.mehta@autonex.com", "employee_type": "Full-time",
         "designation": "Program Manager", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Project Management", "Product Management", "Data Analysis"], "productivity_baseline": 1.0, "status": "active"},
        {"name": "Priya Sharma", "email": "priya.sharma@autonex.com", "employee_type": "Full-time",
         "designation": "Program Manager", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Project Management", "Machine Learning", "Python"], "productivity_baseline": 1.1, "status": "active"},
        
        # Developers
        {"name": "Rahul Verma", "email": "rahul.verma@autonex.com", "employee_type": "Full-time",
         "designation": "Developer", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Python", "React", "Node.js", "SQL"], "productivity_baseline": 1.2, "status": "active"},
        {"name": "Sneha Patil", "email": "sneha.patil@autonex.com", "employee_type": "Full-time",
         "designation": "Developer", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["React", "Node.js", "UI/UX Design"], "productivity_baseline": 1.0, "status": "active"},
        {"name": "Vikram Singh", "email": "vikram.singh@autonex.com", "employee_type": "Full-time",
         "designation": "Developer", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Python", "Machine Learning", "DevOps", "SQL"], "productivity_baseline": 1.3, "status": "active"},
        
        # Annotators
        {"name": "Anjali Gupta", "email": "anjali.gupta@autonex.com", "employee_type": "Full-time",
         "designation": "Annotator", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Data Annotation", "NLP", "Data Analysis"], "productivity_baseline": 1.1, "status": "active"},
        {"name": "Deepak Kumar", "email": "deepak.kumar@autonex.com", "employee_type": "Full-time",
         "designation": "Annotator", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Data Annotation", "Computer Vision"], "productivity_baseline": 0.9, "status": "active"},
        {"name": "Kavita Nair", "email": "kavita.nair@autonex.com", "employee_type": "Full-time",
         "designation": "Annotator", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Data Annotation", "NLP", "Machine Learning"], "productivity_baseline": 1.0, "status": "active"},
        {"name": "Ravi Tiwari", "email": "ravi.tiwari@autonex.com", "employee_type": "Part-time",
         "designation": "Annotator", "working_hours_per_day": 4, "weekly_availability": 20,
         "skills": ["Data Annotation", "Data Analysis"], "productivity_baseline": 0.8, "status": "active"},
        {"name": "Meera Joshi", "email": "meera.joshi@autonex.com", "employee_type": "Full-time",
         "designation": "Annotator", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Data Annotation", "Computer Vision", "Robotics"], "productivity_baseline": 1.0, "status": "active"},
        
        # QA
        {"name": "Amit Deshmukh", "email": "amit.deshmukh@autonex.com", "employee_type": "Full-time",
         "designation": "QA", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["QA Testing", "Python", "Data Analysis"], "productivity_baseline": 1.0, "status": "active"},
        {"name": "Neha Saxena", "email": "neha.saxena@autonex.com", "employee_type": "Full-time",
         "designation": "QA", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["QA Testing", "Data Annotation", "SQL"], "productivity_baseline": 1.1, "status": "active"},
        
        # Reviewers
        {"name": "Suresh Iyer", "email": "suresh.iyer@autonex.com", "employee_type": "Full-time",
         "designation": "Reviewer", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Data Annotation", "NLP", "Machine Learning", "QA Testing"], "productivity_baseline": 1.2, "status": "active"},
        {"name": "Lakshmi Reddy", "email": "lakshmi.reddy@autonex.com", "employee_type": "Full-time",
         "designation": "Reviewer", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Data Annotation", "Computer Vision", "Data Analysis"], "productivity_baseline": 1.0, "status": "active"},
        
        # Intern
        {"name": "Karan Patel", "email": "karan.patel@autonex.com", "employee_type": "Intern",
         "designation": "Annotator", "working_hours_per_day": 6, "weekly_availability": 30,
         "skills": ["Data Annotation", "Python"], "productivity_baseline": 0.7, "status": "active"},
        
        # On-leave employee
        {"name": "Divya Menon", "email": "divya.menon@autonex.com", "employee_type": "Full-time",
         "designation": "Developer", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Python", "React", "DevOps"], "productivity_baseline": 1.1, "status": "on-leave"},
        # Seeded test manager account
        {"name": "Test Manager", "email": "vinayak.shukla@autonexai360.com", "employee_type": "Full-time",
         "designation": "Program Manager", "working_hours_per_day": 8, "weekly_availability": 40,
         "skills": ["Project Management", "QA Testing", "Data Analysis"], "productivity_baseline": 1.0,
         "status": "active", "phone": "+91-9876543210"},
    ]

    employees = []
    for emp_data in employees_data:
        emp = Employee(**emp_data)
        db.add(emp)
        employees.append(emp)
    db.flush()
    print(f"   ✓ {len(employees)} employees added\n")

    # ────────────────────────────────────────────────────
    # Step 4: Seed Main Projects (Parent Projects)
    # ────────────────────────────────────────────────────
    print("📁 Seeding main projects (parent projects)...")
    today = date.today()

    main_projects_data = [
        {"name": "Yutori", "program_manager_id": employees[0].id,
         "description": "Large-scale Japanese NLP data annotation project with multiple batches",
         "client": "Yutori Corp", "global_start_date": today - timedelta(days=60),
         "tentative_duration_months": 12, "status": "active"},
        {"name": "AutoDrive Vision", "program_manager_id": employees[1].id,
         "description": "Autonomous driving dataset annotation - LiDAR and camera data",
         "client": "AutoDrive Inc.", "global_start_date": today - timedelta(days=30),
         "tentative_duration_months": 8, "status": "active"},
        {"name": "MedScan AI", "program_manager_id": employees[0].id,
         "description": "Medical imaging annotation for diagnostic AI models",
         "client": "HealthTech Labs", "global_start_date": today + timedelta(days=15),
         "tentative_duration_months": 6, "status": "active"},
    ]

    main_projects = []
    for mp_data in main_projects_data:
        mp = MainProject(**mp_data)
        db.add(mp)
        main_projects.append(mp)
    db.flush()
    print(f"   ✓ {len(main_projects)} main projects added\n")

    # ────────────────────────────────────────────────────
    # Step 5: Seed Sub-Projects
    # ────────────────────────────────────────────────────
    print("📂 Seeding sub-projects (NEW intermediate level)...")
    new_sub_projects_data = [
        # Yutori sub-projects
        {"main_project_id": main_projects[0].id, "name": "Yutori NLP Batches",
         "client": "Yutori Corp", "pm_id": employees[0].id,
         "description": "All NLP annotation batches for Yutori",
         "start_date": today - timedelta(days=60), "duration_days": 365, "status": "active"},
        # AutoDrive sub-projects
        {"main_project_id": main_projects[1].id, "name": "AutoDrive Labeling",
         "client": "AutoDrive Inc.", "pm_id": employees[1].id,
         "description": "LiDAR and camera data annotation",
         "start_date": today - timedelta(days=30), "duration_days": 240, "status": "active"},
        # MedScan sub-projects
        {"main_project_id": main_projects[2].id, "name": "MedScan Imaging",
         "client": "HealthTech Labs", "pm_id": employees[0].id,
         "description": "Medical imaging annotation for diagnostic AI",
         "start_date": today + timedelta(days=15), "duration_days": 180, "status": "active"},
    ]

    new_sub_projects = []
    for nsp_data in new_sub_projects_data:
        nsp = SubProject(**nsp_data)
        db.add(nsp)
        new_sub_projects.append(nsp)
    db.flush()
    print(f"   ✓ {len(new_sub_projects)} sub-projects added\n")

    # ────────────────────────────────────────────────────
    # Step 6: Seed Daily Sheets (formerly Sub-Projects)
    # ────────────────────────────────────────────────────
    print("📋 Seeding daily sheets...")
    sub_projects_data = [
        # Yutori batches
        {"name": "Yutori - Batch 42", "client": "Yutori Corp", "project_type": "Annotation",
         "total_tasks": 5000, "estimated_time_per_task": 0.5,
         "required_expertise": ["Data Annotation", "NLP"],
         "assigned_employee_ids": [employees[5].id, employees[6].id, employees[7].id],
         "sub_project_id": new_sub_projects[0].id,
         "main_project_id": main_projects[0].id, "batch_name": "Batch 42", "is_sub_project": True,
         "start_date": today - timedelta(days=45), "end_date": today - timedelta(days=5),
         "daily_target": 200, "project_duration_weeks": 6, "project_duration_days": 40,
         "required_manpower": 3, "allocated_employees": 3, "priority": "high", "project_status": "completed"},
        
        {"name": "Yutori - Batch 43", "client": "Yutori Corp", "project_type": "Annotation",
         "total_tasks": 8000, "estimated_time_per_task": 0.5,
         "required_expertise": ["Data Annotation", "NLP"],
         "assigned_employee_ids": [employees[5].id, employees[7].id, employees[8].id, employees[14].id],
         "sub_project_id": new_sub_projects[0].id,
         "main_project_id": main_projects[0].id, "batch_name": "Batch 43", "is_sub_project": True,
         "start_date": today - timedelta(days=10), "end_date": today + timedelta(days=40),
         "daily_target": 250, "project_duration_weeks": 7, "project_duration_days": 50,
         "required_manpower": 4, "allocated_employees": 4, "priority": "high", "project_status": "active"},
        
        {"name": "Yutori - Batch 44", "client": "Yutori Corp", "project_type": "Annotation",
         "total_tasks": 10000, "estimated_time_per_task": 0.5,
         "required_expertise": ["Data Annotation", "NLP", "Machine Learning"],
         "assigned_employee_ids": [],
         "sub_project_id": new_sub_projects[0].id,
         "main_project_id": main_projects[0].id, "batch_name": "Batch 44", "is_sub_project": True,
         "start_date": today + timedelta(days=35), "end_date": today + timedelta(days=100),
         "daily_target": 300, "project_duration_weeks": 9, "project_duration_days": 65,
         "required_manpower": 5, "allocated_employees": 0, "priority": "medium", "project_status": "active"},

        # AutoDrive Vision sub-projects
        {"name": "AD Vision - LiDAR Labeling", "client": "AutoDrive Inc.", "project_type": "Annotation",
         "total_tasks": 3000, "estimated_time_per_task": 1.0,
         "required_expertise": ["Data Annotation", "Computer Vision"],
         "assigned_employee_ids": [employees[6].id, employees[9].id],
         "sub_project_id": new_sub_projects[1].id,
         "main_project_id": main_projects[1].id, "batch_name": "LiDAR Phase 1", "is_sub_project": True,
         "start_date": today - timedelta(days=20), "end_date": today + timedelta(days=60),
         "daily_target": 50, "project_duration_weeks": 11, "project_duration_days": 80,
         "required_manpower": 3, "allocated_employees": 2, "priority": "high", "project_status": "active"},
        
        {"name": "AD Vision - Camera Data QC", "client": "AutoDrive Inc.", "project_type": "QA",
         "total_tasks": 1500, "estimated_time_per_task": 0.8,
         "required_expertise": ["QA Testing", "Computer Vision"],
         "assigned_employee_ids": [employees[10].id],
         "sub_project_id": new_sub_projects[1].id,
         "main_project_id": main_projects[1].id, "batch_name": "Camera QC", "is_sub_project": True,
         "start_date": today - timedelta(days=10), "end_date": today + timedelta(days=50),
         "daily_target": 30, "project_duration_weeks": 8, "project_duration_days": 60,
         "required_manpower": 2, "allocated_employees": 1, "priority": "medium", "project_status": "active"},
        
        # MedScan AI sub-projects
        {"name": "MedScan - X-Ray Annotation", "client": "HealthTech Labs", "project_type": "Annotation",
         "total_tasks": 2000, "estimated_time_per_task": 1.5,
         "required_expertise": ["Data Annotation", "Data Analysis"],
         "assigned_employee_ids": [],
         "sub_project_id": new_sub_projects[2].id,
         "main_project_id": main_projects[2].id, "batch_name": "X-Ray Phase 1", "is_sub_project": True,
         "start_date": today + timedelta(days=15), "end_date": today + timedelta(days=105),
         "daily_target": 30, "project_duration_weeks": 13, "project_duration_days": 90,
         "required_manpower": 4, "allocated_employees": 0, "priority": "medium", "project_status": "active"},
        
        # Standalone sub-project (no parent)
        {"name": "Internal Tool Development", "client": "Autonex", "project_type": "Development",
         "total_tasks": 50, "estimated_time_per_task": 4.0,
         "required_expertise": ["Python", "React", "SQL"],
         "assigned_employee_ids": [employees[2].id, employees[3].id],
         "main_project_id": None, "batch_name": None, "is_sub_project": False,
         "start_date": today - timedelta(days=15), "end_date": today + timedelta(days=45),
         "daily_target": 2, "project_duration_weeks": 8, "project_duration_days": 60,
         "required_manpower": 2, "allocated_employees": 2, "priority": "low", "project_status": "active"},
    ]

    daily_sheets = []
    for sp_data in sub_projects_data:
        sp = DailySheet(**sp_data)
        db.add(sp)
        daily_sheets.append(sp)
    db.flush()
    print(f"   ✓ {len(daily_sheets)} daily sheets added\n")

    # ────────────────────────────────────────────────────
    # Step 6: Seed Allocations
    # ────────────────────────────────────────────────────
    print("📊 Seeding allocations...")
    allocations_data = [
        # Yutori Batch 43 allocations
        {"employee_id": employees[5].id, "sub_project_id": daily_sheets[1].id,
         "total_daily_hours": 8, "role_tags": ["Annotation"], "time_distribution": {"Annotation": 8},
         "active_start_date": daily_sheets[1].start_date, "active_end_date": daily_sheets[1].end_date},
        {"employee_id": employees[7].id, "sub_project_id": daily_sheets[1].id,
         "total_daily_hours": 6, "role_tags": ["Annotation", "Review"], "time_distribution": {"Annotation": 4, "Review": 2},
         "active_start_date": daily_sheets[1].start_date, "active_end_date": daily_sheets[1].end_date},
        {"employee_id": employees[8].id, "sub_project_id": daily_sheets[1].id,
         "total_daily_hours": 4, "role_tags": ["Annotation"], "time_distribution": {"Annotation": 4},
         "active_start_date": daily_sheets[1].start_date + timedelta(days=5), "active_end_date": daily_sheets[1].end_date},
        {"employee_id": employees[14].id, "sub_project_id": daily_sheets[1].id,
         "total_daily_hours": 6, "role_tags": ["Annotation"], "time_distribution": {"Annotation": 6},
         "active_start_date": daily_sheets[1].start_date, "active_end_date": daily_sheets[1].end_date},

        # AD Vision - LiDAR allocations
        {"employee_id": employees[6].id, "sub_project_id": daily_sheets[3].id,
         "total_daily_hours": 6, "role_tags": ["Annotation"], "time_distribution": {"Annotation": 6},
         "active_start_date": daily_sheets[3].start_date, "active_end_date": daily_sheets[3].end_date},
        {"employee_id": employees[9].id, "sub_project_id": daily_sheets[3].id,
         "total_daily_hours": 8, "role_tags": ["Annotation", "QC"], "time_distribution": {"Annotation": 6, "QC": 2},
         "active_start_date": daily_sheets[3].start_date, "active_end_date": daily_sheets[3].end_date},

        # AD Vision - Camera QC allocation
        {"employee_id": employees[10].id, "sub_project_id": daily_sheets[4].id,
         "total_daily_hours": 8, "role_tags": ["QC"], "time_distribution": {"QC": 8},
         "active_start_date": daily_sheets[4].start_date, "active_end_date": daily_sheets[4].end_date},

        # Internal Tool allocations
        {"employee_id": employees[2].id, "sub_project_id": daily_sheets[6].id,
         "total_daily_hours": 8, "role_tags": ["Development"], "time_distribution": {"Development": 8},
         "active_start_date": daily_sheets[6].start_date, "active_end_date": daily_sheets[6].end_date},
        {"employee_id": employees[3].id, "sub_project_id": daily_sheets[6].id,
         "total_daily_hours": 8, "role_tags": ["Development", "Design"], "time_distribution": {"Development": 5, "Design": 3},
         "active_start_date": daily_sheets[6].start_date, "active_end_date": daily_sheets[6].end_date},
        
        # Reviewer allocations (cross-project)
        {"employee_id": employees[12].id, "sub_project_id": daily_sheets[1].id,
         "total_daily_hours": 4, "role_tags": ["Review"], "time_distribution": {"Review": 4},
         "active_start_date": daily_sheets[1].start_date, "active_end_date": daily_sheets[1].end_date},
        {"employee_id": employees[13].id, "sub_project_id": daily_sheets[3].id,
         "total_daily_hours": 4, "role_tags": ["Review"], "time_distribution": {"Review": 4},
         "active_start_date": daily_sheets[3].start_date, "active_end_date": daily_sheets[3].end_date},
    ]

    allocations = []
    for alloc_data in allocations_data:
        alloc = Allocation(**alloc_data)
        db.add(alloc)
        allocations.append(alloc)
    db.flush()
    print(f"   ✓ {len(allocations)} allocations added\n")

    # ────────────────────────────────────────────────────
    # Step 7: Seed Leaves
    # ────────────────────────────────────────────────────
    print("🏖️  Seeding leaves...")
    leaves_data = [
        {"employee_id": employees[15].id, "leave_type": "vacation",
         "start_date": today - timedelta(days=5), "end_date": today + timedelta(days=10)},
        {"employee_id": employees[8].id, "leave_type": "sick",
         "start_date": today + timedelta(days=3), "end_date": today + timedelta(days=5)},
        {"employee_id": employees[2].id, "leave_type": "casual",
         "start_date": today + timedelta(days=14), "end_date": today + timedelta(days=14)},
        {"employee_id": employees[5].id, "leave_type": "personal",
         "start_date": today + timedelta(days=20), "end_date": today + timedelta(days=22)},
    ]

    leaves = []
    for leave_data in leaves_data:
        lv = Leave(**leave_data)
        db.add(lv)
        leaves.append(lv)
    db.flush()
    print(f"   ✓ {len(leaves)} leaves added\n")

    # ────────────────────────────────────────────────────
    # Step 8: Seed Users (Authentication)
    # ────────────────────────────────────────────────────
    print("🔐 Seeding users...")
    users_data = [
        # Admin user
        {"name": "Admin User", "email": "admin@autonex.com",
         "password_hash": hash_password("admin123"), "role": "admin",
         "employee_id": None, "skills": []},
        # PM users (linked to PM employees)
        {"name": "Arjun Mehta", "email": "arjun.mehta@autonex.com",
         "password_hash": hash_password("pm123"), "role": "pm",
         "employee_id": employees[0].id, "skills": ["Project Management", "Product Management"]},
        {"name": "Priya Sharma", "email": "priya.sharma@autonex.com",
         "password_hash": hash_password("pm123"), "role": "pm",
         "employee_id": employees[1].id, "skills": ["Project Management", "Machine Learning"]},
        {"name": "Test Manager", "email": "vinayak.shukla@autonexai360.com",
         "password_hash": hash_password("pm123"), "role": "pm",
         "employee_id": employees[-1].id, "skills": ["Project Management", "QA Testing", "Data Analysis"]},
        # Employee users
        {"name": "Rahul Verma", "email": "rahul.verma@autonex.com",
         "password_hash": hash_password("emp123"), "role": "employee",
         "employee_id": employees[2].id, "skills": ["Python", "React"]},
        {"name": "Anjali Gupta", "email": "anjali.gupta@autonex.com",
         "password_hash": hash_password("emp123"), "role": "employee",
         "employee_id": employees[5].id, "skills": ["Data Annotation", "NLP"]},
        {"name": "Amit Deshmukh", "email": "amit.deshmukh@autonex.com",
         "password_hash": hash_password("emp123"), "role": "employee",
         "employee_id": employees[10].id, "skills": ["QA Testing", "Python"]},
    ]

    users = []
    for user_data in users_data:
        u = User(**user_data)
        db.add(u)
        users.append(u)
    db.flush()
    print(f"   ✓ {len(users)} users added")
    print(f"     Admin:    admin@autonex.com / admin123")
    print(f"     PM:       arjun.mehta@autonex.com / pm123")
    print(f"     PM:       vinayak.shukla@autonexai360.com / pm123")
    print(f"     Employee: rahul.verma@autonex.com / emp123\n")

    # ────────────────────────────────────────────────────
    # Commit everything
    # ────────────────────────────────────────────────────
    db.commit()

    print("=" * 50)
    print("🎉 DATABASE SEEDED SUCCESSFULLY!")
    print("=" * 50)
    print(f"   Skills:        {len(skills)}")
    print(f"   Employees:     {len(employees)}")
    print(f"   Main Projects: {len(main_projects)}")
    print(f"   Sub-Projects:  {len(new_sub_projects)}")
    print(f"   Daily Sheets:  {len(daily_sheets)}")
    print(f"   Allocations:   {len(allocations)}")
    print(f"   Leaves:        {len(leaves)}")
    print(f"   Users:         {len(users)}")
    print("=" * 50)

except Exception as e:
    db.rollback()
    print(f"\n❌ Error seeding database: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    db.close()
