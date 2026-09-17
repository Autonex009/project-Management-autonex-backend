from sqlalchemy.orm import Session
from sqlalchemy import func, not_
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any

from app.models.employee import Employee
from app.models.daily_checkin import DailyCheckIn
from app.models.allocation import Allocation
from app.models.project import Project
from app.models.leave import Leave

IST = ZoneInfo("Asia/Kolkata")

def _generate_admin_report_payload(db: Session, target_date=None) -> Dict[str, Any]:
    """
    Generates a high-performance aggregated dictionary containing Overall Stats 
    and Project-Wise stats using SQL-level aggregation.
    """
    if not target_date:
        target_date = datetime.now(IST).date()

    # Base queries for active employees and those on leave
    active_employees_sq = db.query(Employee.id).filter(Employee.status == "active").subquery()
    
    on_leave_sq = db.query(Leave.employee_id).filter(
        Leave.status == "approved",
        Leave.start_date <= target_date,
        Leave.end_date >= target_date
    ).subquery()

    # 1. OVERALL AGGREGATION (Pure SQL)
    # Total active employees NOT on leave
    total_active_expected = db.query(func.count(Employee.id)).filter(
        Employee.status == "active",
        not_(Employee.id.in_(on_leave_sq))
    ).scalar() or 0

    # Total check-ins today (from active, not on leave)
    total_checked_in = db.query(func.count(DailyCheckIn.id)).filter(
        DailyCheckIn.checkin_date == target_date,
        DailyCheckIn.employee_id.in_(active_employees_sq),
        not_(DailyCheckIn.employee_id.in_(on_leave_sq))
    ).scalar() or 0

    total_pending = total_active_expected - total_checked_in

    # Earliest check-in and Average time
    # Note: SQLite/Postgres avg on timestamps can be tricky, so we fetch the raw times and average in python
    checkins_today = db.query(
        DailyCheckIn.checked_in_at,
        DailyCheckIn.work_mode,
        Employee.name,
        Employee.email,
        DailyCheckIn.mood
    ).join(Employee, DailyCheckIn.employee_id == Employee.id).filter(
        DailyCheckIn.checkin_date == target_date,
        Employee.status == "active",
        not_(Employee.id.in_(on_leave_sq))
    ).all()

    first_checkin_wfo = None
    first_checkin_wfh = None
    
    time_sum_seconds_wfo = 0
    valid_times_wfo = 0
    time_sum_seconds_wfh = 0
    valid_times_wfh = 0

    dist_before_9 = 0
    dist_9_to_10 = 0
    dist_10_to_11 = 0
    dist_11_to_12 = 0
    dist_after_12 = 0

    late_checkin_list = []
    low_sentiment_list = []
    
    for c_at_utc, w_mode, emp_name, emp_email, mood in checkins_today:
        c_at_ist = c_at_utc.astimezone(IST)
        mode = (w_mode or "WFO").upper()
        
        if mood in ("low", "stressed"):
            low_sentiment_list.append((emp_name, emp_email, str(mood).title()))
        
        # Track first
        if mode == "WFO":
            if not first_checkin_wfo or c_at_ist < first_checkin_wfo["time"]:
                first_checkin_wfo = {"name": emp_name, "time": c_at_ist}
        else:
            if not first_checkin_wfh or c_at_ist < first_checkin_wfh["time"]:
                first_checkin_wfh = {"name": emp_name, "time": c_at_ist}
            
        # Average
        sec = (c_at_ist.hour * 3600 + c_at_ist.minute * 60 + c_at_ist.second)
        if mode == "WFO":
            time_sum_seconds_wfo += sec
            valid_times_wfo += 1
        else:
            time_sum_seconds_wfh += sec
            valid_times_wfh += 1
        
        # Distribution
        hour = c_at_ist.hour
        if hour < 9:
            dist_before_9 += 1
        elif hour == 9:
            dist_9_to_10 += 1
        elif hour == 10:
            dist_10_to_11 += 1
        elif hour == 11:
            dist_11_to_12 += 1
        else:
            dist_after_12 += 1
            late_checkin_list.append((emp_name, emp_email, c_at_ist.strftime("%I:%M %p")))

    def format_avg(total_sec, count):
        if count == 0: return "—"
        avg_sec = int(total_sec / count)
        avg_h = avg_sec // 3600
        avg_m = (avg_sec % 3600) // 60
        ampm = "AM" if avg_h < 12 else "PM"
        display_h = avg_h if 0 < avg_h <= 12 else (12 if avg_h == 0 else avg_h - 12)
        return f"{display_h:02d}:{avg_m:02d} {ampm}"

    avg_time_wfo = format_avg(time_sum_seconds_wfo, valid_times_wfo)
    avg_time_wfh = format_avg(time_sum_seconds_wfh, valid_times_wfh)

    def format_first(fc):
        if not fc: return "None"
        return f"{fc['name']} at {fc['time'].strftime('%I:%M %p')}"

    # Fetch Pending List
    checked_in_sq = db.query(DailyCheckIn.employee_id).filter(DailyCheckIn.checkin_date == target_date).subquery()
    pending_employees = db.query(Employee.name, Employee.email).filter(
        Employee.status == "active",
        not_(Employee.id.in_(checked_in_sq)),
        not_(Employee.id.in_(on_leave_sq))
    ).all()
    pending_list = [(emp.name, emp.email) for emp in pending_employees]


    # 2. PROJECT-WISE AGGREGATION (Pure SQL grouping)
    # We want: Project Name, total allocations, total checkins, pending
    
    # Query all active allocations mapped to project names
    project_stats = {}
    projects = db.query(Project.id, Project.name).filter(Project.project_status == "active").all()
    
    # Map project IDs to names and initialize stats
    for p_id, p_name in projects:
        project_stats[p_id] = {
            "name": p_name,
            "allocated_total": 0,
            "checked_in": 0,
            "pending": 0,
            "dist_before_9": 0,
            "dist_9_to_10": 0,
            "dist_10_to_11": 0,
            "dist_11_to_12": 0,
            "dist_after_12": 0,
        }

    # Fetch allocations (active, not on leave)
    allocations = db.query(Allocation.sub_project_id, func.count(Allocation.employee_id)).filter(
        Allocation.is_active == True,
        not_(Allocation.employee_id.in_(on_leave_sq))
    ).group_by(Allocation.sub_project_id).all()
    
    for p_id, count in allocations:
        if p_id in project_stats:
            project_stats[p_id]["allocated_total"] = count
            project_stats[p_id]["pending"] = count # Initially pending is total

    # Fetch Check-ins mapped to allocations
    # A user might have multiple project allocations. For daily reporting, we map their check-in
    emp_checkin_times = {emp_email: c_at_utc.astimezone(IST) for c_at_utc, _, _, emp_email, _ in checkins_today}
    
    emp_to_allocs = db.query(Employee.email, Allocation.sub_project_id).join(
        Allocation, Employee.id == Allocation.employee_id
    ).filter(
        Allocation.is_active == True,
        Employee.status == "active"
    ).all()

    for emp_email, p_id in emp_to_allocs:
        if p_id in project_stats and emp_email in emp_checkin_times:
            p_stat = project_stats[p_id]
            p_stat["checked_in"] += 1
            p_stat["pending"] -= 1
            
            # Distribution
            hour = emp_checkin_times[emp_email].hour
            if hour < 9:
                p_stat["dist_before_9"] += 1
            elif hour == 9:
                p_stat["dist_9_to_10"] += 1
            elif hour == 10:
                p_stat["dist_10_to_11"] += 1
            elif hour == 11:
                p_stat["dist_11_to_12"] += 1
            else:
                p_stat["dist_after_12"] += 1

    # Filter out projects with 0 allocations
    active_projects = [stats for stats in project_stats.values() if stats["allocated_total"] > 0]
    
    # Sort projects by most pending check-ins
    active_projects.sort(key=lambda x: x["pending"], reverse=True)

    return {
        "overall": {
            "total_active": total_active_expected,
            "total_checked_in": total_checked_in,
            "total_pending": total_pending,
            "avg_time_wfo": avg_time_wfo,
            "avg_time_wfh": avg_time_wfh,
            "first_checkin_wfo": format_first(first_checkin_wfo),
            "first_checkin_wfh": format_first(first_checkin_wfh),
            "distribution": {
                "before_9": dist_before_9,
                "9_to_10": dist_9_to_10,
                "10_to_11": dist_10_to_11,
                "11_to_12": dist_11_to_12,
                "after_12": dist_after_12
            }
        },
        "projects": active_projects,
        "late_list": late_checkin_list,
        "pending_list": pending_list,
        "low_sentiment_list": low_sentiment_list
    }

