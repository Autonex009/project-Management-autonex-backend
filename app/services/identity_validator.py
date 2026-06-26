from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from app.models.employee import Employee
from app.models.user import User
from app.models.signup_request import SignupRequest

def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    # Keep only digits
    digits = "".join(c for c in phone if c.isdigit())
    return digits if digits else None

def check_duplicate_identity(
    db: Session,
    email: str | None = None,
    phone: str | None = None,
    exclude_employee_id: int | None = None,
    exclude_user_id: int | None = None,
    exclude_signup_request_id: int | None = None,
):
    """
    Check if any existing employee, user, or signup request belongs to the same physical individual
    using the provided email, phone number, etc.
    """
    normalized_incoming_phone = normalize_phone(phone)

    # Find the existing employee if updating an existing record (by employee_id or user_id)
    existing_employee = None
    if exclude_employee_id:
        existing_employee = db.query(Employee).filter(Employee.id == exclude_employee_id).first()
    elif exclude_user_id:
        user = db.query(User).filter(User.id == exclude_user_id).first()
        if user and user.employee_id:
            existing_employee = db.query(Employee).filter(Employee.id == user.employee_id).first()

    # 1. Check existing Employees
    employees = db.query(Employee).all()
    for emp in employees:
        if exclude_employee_id and emp.id == exclude_employee_id:
            continue
        
        # Check email match (case-insensitive)
        if email:
            incoming_email_lower = email.lower().strip()
            if emp.email and emp.email.lower().strip() == incoming_email_lower:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An employee with this email ({emp.email}) already exists."
                )
            if emp.razorpay_email and emp.razorpay_email.lower().strip() == incoming_email_lower:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An employee with this personal email ({emp.razorpay_email}) already exists."
                )
        
        # Check phone match
        if normalized_incoming_phone:
            emp_phone_normalized = normalize_phone(emp.phone)
            if emp_phone_normalized:
                if emp_phone_normalized == normalized_incoming_phone or (
                    len(emp_phone_normalized) >= 10 and len(normalized_incoming_phone) >= 10 and 
                    emp_phone_normalized[-10:] == normalized_incoming_phone[-10:]
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"An employee with this phone number ({emp.phone}) already exists."
                    )

    # 2. Check existing Users
    users = db.query(User).all()
    for usr in users:
        if exclude_user_id and usr.id == exclude_user_id:
            continue
        if exclude_employee_id and usr.employee_id == exclude_employee_id:
            continue
        
        if email:
            incoming_email_lower = email.lower().strip()
            if usr.email and usr.email.lower().strip() == incoming_email_lower:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A user account with this email ({usr.email}) already exists."
                )

    # 3. Check pending or approved Signup Requests
    requests = db.query(SignupRequest).filter(SignupRequest.status.in_(["pending", "approved"])).all()
    for req in requests:
        if exclude_signup_request_id and req.id == exclude_signup_request_id:
            continue
        
        # If this signup request belongs to the employee being updated, skip it.
        if existing_employee:
            is_own_request = False
            if existing_employee.email and req.email and req.email.lower().strip() == existing_employee.email.lower().strip():
                is_own_request = True
            if not is_own_request and existing_employee.phone and req.phone:
                req_phone_normalized = normalize_phone(req.phone)
                emp_phone_normalized = normalize_phone(existing_employee.phone)
                if req_phone_normalized and emp_phone_normalized and (
                    req_phone_normalized == emp_phone_normalized or (
                        len(req_phone_normalized) >= 10 and len(emp_phone_normalized) >= 10 and
                        req_phone_normalized[-10:] == emp_phone_normalized[-10:]
                    )
                ):
                    is_own_request = True
            if is_own_request:
                continue

        # Check email match
        if email:
            incoming_email_lower = email.lower().strip()
            if req.email and req.email.lower().strip() == incoming_email_lower:
                status_str = "pending review" if req.status == "pending" else "approved"
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A signup request with this email is already {status_str}."
                )
        
        # Check phone match
        if normalized_incoming_phone:
            req_phone_normalized = normalize_phone(req.phone)
            if req_phone_normalized:
                if req_phone_normalized == normalized_incoming_phone or (
                    len(req_phone_normalized) >= 10 and len(normalized_incoming_phone) >= 10 and
                    req_phone_normalized[-10:] == normalized_incoming_phone[-10:]
                ):
                    status_str = "pending review" if req.status == "pending" else "approved"
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"A signup request with this phone number is already {status_str}."
                    )

def check_duplicate_user_for_employee(db: Session, employee_id: int, exclude_user_id: int | None = None):
    """
    Ensure that an employee does not already have a linked User account.
    """
    if not employee_id:
        return
    existing_user = db.query(User).filter(User.employee_id == employee_id).first()
    if existing_user:
        if exclude_user_id and existing_user.id == exclude_user_id:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This employee record is already linked to user account: {existing_user.email}."
        )
