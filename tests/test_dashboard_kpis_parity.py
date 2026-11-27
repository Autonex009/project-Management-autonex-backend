import pytest
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from app.db.database import Base
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.allocation import Allocation
from app.models.user import User
from app.utils.business_time import today_ist
from app.api.analytics import get_dashboard_kpis
from app.api.employees import employee_stats

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()

def test_dashboard_kpis_and_employee_stats_parity(db_session):
    today = today_ist()

    # 1. Active employee with allocation
    emp1 = Employee(id=1, name="Active Emp", email="active@test.com", status="active", designation="Annotator", employee_type="Full-Time")
    # 2. Idle employee (no allocation, no leave)
    emp2 = Employee(id=2, name="Idle Emp", email="idle@test.com", status="active", designation="Annotator", employee_type="Full-Time")
    # 3. Approved leave employee (has allocation)
    emp3 = Employee(id=3, name="Approved Leave Emp", email="app_leave@test.com", status="active", designation="Annotator", employee_type="Full-Time")
    # 4. Pending leave employee (has allocation)
    emp4 = Employee(id=4, name="Pending Leave Emp 1", email="pen_leave1@test.com", status="active", designation="Annotator", employee_type="Full-Time")
    # 5. Pending leave employee (no allocation)
    emp5 = Employee(id=5, name="Pending Leave Emp 2", email="pen_leave2@test.com", status="active", designation="Annotator", employee_type="Full-Time")
    # 6. Archived employee
    emp6 = Employee(id=6, name="Archived Emp", email="archived@test.com", status="archived", designation="Annotator", employee_type="Full-Time")

    db_session.add_all([emp1, emp2, emp3, emp4, emp5, emp6])
    db_session.commit()

    # Allocations for emp1, emp3, emp4
    alloc1 = Allocation(employee_id=1, sub_project_id=101, is_active=True)
    alloc3 = Allocation(employee_id=3, sub_project_id=101, is_active=True)
    alloc4 = Allocation(employee_id=4, sub_project_id=101, is_active=True)
    db_session.add_all([alloc1, alloc3, alloc4])
    db_session.commit()

    # Leaves today: emp3 approved, emp4 pending, emp5 pending
    leave3 = Leave(employee_id=3, start_date=today, end_date=today, status="approved")
    leave4 = Leave(employee_id=4, start_date=today, end_date=today, status="pending")
    leave5 = Leave(employee_id=5, start_date=today, end_date=today, status="pending")
    db_session.add_all([leave3, leave4, leave5])
    db_session.commit()

    # Admin user
    admin_user = User(id=999, name="Admin User", email="admin@test.com", password_hash="dummy", role="admin")

    # Get employee_stats
    emp_res = employee_stats(db=db_session)
    emp_status = emp_res["by_status"]

    # Get dashboard_kpis
    req = Request({"type": "http", "headers": []})
    resp = Response()
    kpi_res = get_dashboard_kpis(request=req, response=resp, db=db_session, current_user=admin_user)
    kpi_emp = kpi_res["employees"]

    # Both should report:
    # Total roster (non-archived): 5
    # Inactive (on leave - approved or pending): 3 (emp3, emp4, emp5)
    # Idle (no leave, not allocated): 1 (emp2)
    # Active (allocated, not on leave): 1 (emp1)
    assert emp_status["inactive"] == 3
    assert emp_status["idle"] == 1
    assert emp_status["active"] == 1
    assert emp_res["total"] == 5

    assert kpi_emp["inactive"] == 3
    assert kpi_emp["idle"] == 1
    assert kpi_emp["active"] == 1
    assert kpi_emp["total"] == 5

    # Full parity
    assert kpi_emp["inactive"] == emp_status["inactive"]
    assert kpi_emp["idle"] == emp_status["idle"]
    assert kpi_emp["active"] == emp_status["active"]
    assert kpi_emp["total"] == emp_res["total"]


def test_rejected_leaves_are_not_counted_in_inactive(db_session):
    today = today_ist()

    # Employee with rejected leave and no allocation -> should be Idle, NOT Inactive
    emp = Employee(id=10, name="Rejected Leave Emp", email="rej@test.com", status="active", designation="Annotator", employee_type="Full-Time")
    db_session.add(emp)
    db_session.commit()

    rej_leave = Leave(employee_id=10, start_date=today, end_date=today, status="rejected")
    db_session.add(rej_leave)
    db_session.commit()

    admin_user = User(id=999, name="Admin User", email="admin@test.com", password_hash="dummy", role="admin")

    req = Request({"type": "http", "headers": []})
    resp = Response()
    kpi_res = get_dashboard_kpis(request=req, response=resp, db=db_session, current_user=admin_user)
    kpi_emp = kpi_res["employees"]

    emp_res = employee_stats(db=db_session)

    # Both should report 0 inactive, 1 idle, 0 active
    assert kpi_emp["inactive"] == 0
    assert kpi_emp["idle"] == 1
    assert kpi_emp["active"] == 0
    assert kpi_emp["inactive"] == emp_res["by_status"]["inactive"]
    assert kpi_emp["idle"] == emp_res["by_status"]["idle"]

