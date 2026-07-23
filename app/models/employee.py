from sqlalchemy import Column, Integer, String, Float, Text, TIMESTAMP, JSON, ForeignKey
from sqlalchemy.sql import func

from app.db.database import Base



class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)
    
    name = Column(Text, nullable=False)
    email = Column(Text, nullable=False, unique=True)
    razorpay_email = Column(Text, nullable=True)
    phone = Column(String(32), nullable=True)
    employee_type = Column(Text, nullable=False)  # Full-time, Part-time, Intern, Contract
    
    # Designation: Program Manager, Annotator, Developer, QA, Reviewer
    designation = Column(Text, default="Annotator")
    
    working_hours_per_day = Column(Float, nullable=False, default=8.0)
    weekly_availability = Column(Float, nullable=False, default=40.0)
    
    # Store skills as JSON array: ["Python", "Data Analysis", ...]
    skills = Column(JSON, nullable=True)

    slack_user_id = Column(String(64), nullable=True)

    # Encord account email/identity. Matches encord_daily_time_spent.user_email so
    # analytics can resolve an Encord user to this employee's real name.
    encord_id = Column(Text, nullable=True, index=True)

    # Profile picture URL (uploaded file under /uploads/avatars or a remote Slack/Razorpay URL)
    avatar_url = Column(Text, nullable=True)

    productivity_baseline = Column(Float, nullable=False, default=1.0)

    # Monthly base salary (CTC) — used for payroll calculation.
    # Stored ENCRYPTED in base_salary_enc; the plaintext column is retired (kept
    # only for the migration and always left NULL). See services/salary_crypto.py.
    base_salary = Column(Float, nullable=True)
    base_salary_enc = Column(Text, nullable=True)

    status = Column(Text, default="active")  # active, inactive, on-leave
    mentor_id = Column(Integer, ForeignKey("employees.id"), nullable=True)


    # ── Intern → Full-time conversion audit ──────────────────────────
    # Set when an intern is promoted in-place to a full-time employee. The
    # timestamp also acts as the cutoff for payroll: paid leave taken BEFORE it
    # keeps the monthly intern entitlement; leave on/after it uses the annual quota.
    previous_employee_type = Column(Text, nullable=True)
    converted_to_fulltime_at = Column(TIMESTAMP, nullable=True)
    converted_by = Column(Integer, nullable=True)  # user_id of the admin who promoted

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now()
    )

