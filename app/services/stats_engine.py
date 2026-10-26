from sqlalchemy.orm import Session
from sqlalchemy import func, not_
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any

import app.models.sub_project
import app.models.parent_project
from app.models.employee import Employee
from app.models.daily_checkin import DailyCheckIn
from app.models.allocation import Allocation
from app.models.project import Project
from app.models.leave import Leave

IST = ZoneInfo("Asia/Kolkata")

def _generate_admin_report_payload(db: Session, target_date=None) -> Dict[str, Any]:
    """
    Generates a high-performance aggregated dictionary containing Overall Stats 
    and Project-Wise stats using SQL-level aggregation and in-memory manager resolution.
    """
    if not target_date:
        target_date = datetime.now(IST).date()

    # Base queries for active employees and those on leave (approved + applied/pending)
    active_employees_sq = db.query(Employee.id).filter(Employee.status == "active")
    
    on_leave_subq = db.query(Leave.employee_id).filter(
        Leave.status.in_(["approved", "pending"]),
        Leave.start_date <= target_date,
        Leave.end_date >= target_date
    )

    # 1. OVERALL AGGREGATION (Pure SQL)
    # Total active employees NOT on leave
    total_active_expected = db.query(func.count(Employee.id)).filter(
        Employee.status == "active",
        not_(Employee.id.in_(on_leave_subq))
    ).scalar() or 0

    # Total check-ins today (from active, not on leave)
    total_checked_in = db.query(func.count(DailyCheckIn.id)).filter(
        DailyCheckIn.checkin_date == target_date,
        DailyCheckIn.employee_id.in_(active_employees_sq),
        not_(DailyCheckIn.employee_id.in_(on_leave_subq))
    ).scalar() or 0

    total_pending = total_active_expected - total_checked_in

    # Earliest check-in and Average time
    # Note: SQLite/Postgres avg on timestamps can be tricky, so we fetch the raw times and average in python
    checkins_today = db.query(
        DailyCheckIn.checked_in_at,
        DailyCheckIn.work_mode,
        Employee.id,
        Employee.name,
        Employee.email,
        Employee.designation,
        DailyCheckIn.mood
    ).join(Employee, DailyCheckIn.employee_id == Employee.id).filter(
        DailyCheckIn.checkin_date == target_date,
        Employee.status == "active",
        not_(Employee.id.in_(on_leave_subq))
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

    late_checkin_raw = []
    low_sentiment_list = []
    
    for c_at_utc, w_mode, emp_id, emp_name, emp_email, emp_desig, mood in checkins_today:
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
            late_checkin_raw.append((emp_id, emp_name, emp_email, emp_desig, c_at_ist.strftime("%I:%M %p")))

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

    # 2. MANAGER & LEAD RESOLUTION (Zero fallback, identical to app/api/employees.py)
    all_projects = db.query(Project.id, Project.name, Project.assigned_employee_ids).filter(Project.project_status == "active").all()
    ds_by_id = {p.id: p for p in all_projects}
    all_ds_ids = [p.id for p in all_projects]

    project_alloc_rows = (
        db.query(Allocation.sub_project_id, Allocation.employee_id, Allocation.role_tags)
        .filter(Allocation.sub_project_id.in_(all_ds_ids), Allocation.is_active == True)
        .all()
    ) if all_ds_ids else []

    candidate_emp_ids = set()
    for ds in all_projects:
        for raw_id in (ds.assigned_employee_ids or []):
            try:
                candidate_emp_ids.add(int(raw_id))
            except (TypeError, ValueError):
                pass

    LEAD_TAG_TERMS = {"team lead", "lead", "tl"}
    company_lead_ids = {
        r[0] for r in db.query(Employee.id).filter(
            func.lower(Employee.designation).in_(["team lead", "lead", "tl"])
        ).all()
    }

    for _, a_emp_id, role_tags in project_alloc_rows:
        if a_emp_id:
            is_tagged_lead = any((t or "").strip().lower() in LEAD_TAG_TERMS for t in (role_tags or []))
            if is_tagged_lead or (a_emp_id in company_lead_ids):
                candidate_emp_ids.add(a_emp_id)

    emp_meta = {}
    if candidate_emp_ids:
        records = db.query(Employee.id, Employee.name, Employee.designation).filter(
            Employee.id.in_(candidate_emp_ids)
        ).all()
        emp_meta = {
            e.id: {
                "name": e.name,
                "designation": (e.designation or "").strip().lower()
            } for e in records
        }

    ds_pm_ids = {}
    ds_lead_ids = {}
    all_resolved_mgr_ids = set()

    for ds in all_projects:
        assigned_ids = set()
        for raw_id in (ds.assigned_employee_ids or []):
            try:
                assigned_ids.add(int(raw_id))
            except (TypeError, ValueError):
                pass

        pm_ids = {i for i in assigned_ids if emp_meta.get(i, {}).get("designation") not in LEAD_TAG_TERMS}
        lead_ids = {i for i in assigned_ids if emp_meta.get(i, {}).get("designation") in LEAD_TAG_TERMS}

        for sub_id, a_emp_id, role_tags in project_alloc_rows:
            if sub_id == ds.id and a_emp_id:
                is_lead_tag = any((t or "").strip().lower() in LEAD_TAG_TERMS for t in (role_tags or []))
                is_lead_desig = emp_meta.get(a_emp_id, {}).get("designation") in LEAD_TAG_TERMS
                if is_lead_tag or is_lead_desig:
                    lead_ids.add(a_emp_id)

        ds_pm_ids[ds.id] = pm_ids
        ds_lead_ids[ds.id] = lead_ids
        all_resolved_mgr_ids.update(pm_ids | lead_ids)

    mgr_by_id = {eid: emp_meta[eid]["name"] for eid in all_resolved_mgr_ids if eid in emp_meta}

    # Fetch Pending List
    checked_in_subq = db.query(DailyCheckIn.employee_id).filter(DailyCheckIn.checkin_date == target_date)
    pending_employees = db.query(Employee.id, Employee.name, Employee.email, Employee.designation).filter(
        Employee.status == "active",
        not_(Employee.id.in_(checked_in_subq)),
        not_(Employee.id.in_(on_leave_subq))
    ).all()

    active_admins = db.query(Employee).filter(Employee.designation.ilike("Admin"), Employee.status == "active").all()
    admin_names = [a.name for a in active_admins]

    # Map allocations for pending and late employees
    target_emp_ids = list(set([e.id for e in pending_employees] + [r[0] for r in late_checkin_raw]))
    target_allocs = db.query(Allocation).filter(
        Allocation.employee_id.in_(target_emp_ids),
        Allocation.is_active == True
    ).all() if target_emp_ids else []

    emp_projects = {eid: [] for eid in target_emp_ids}
    emp_pms = {eid: set() for eid in target_emp_ids}
    emp_leads = {eid: set() for eid in target_emp_ids}

    for alloc in target_allocs:
        ds = ds_by_id.get(alloc.sub_project_id)
        if not ds:
            continue
        if ds.name not in emp_projects[alloc.employee_id]:
            emp_projects[alloc.employee_id].append(ds.name)
        for pid in ds_pm_ids.get(ds.id, set()):
            if pid in mgr_by_id:
                emp_pms[alloc.employee_id].add(mgr_by_id[pid])
        for lid in ds_lead_ids.get(ds.id, set()):
            if lid in mgr_by_id:
                emp_leads[alloc.employee_id].add(mgr_by_id[lid])

    def resolve_emp_proj_and_mgr(eid, designation):
        assigned_projs = emp_projects.get(eid, [])
        if not assigned_projs:
            proj_str = "—"
        elif len(assigned_projs) <= 2:
            proj_str = ", ".join(assigned_projs)
        else:
            proj_str = f"{assigned_projs[0]}, {assigned_projs[1]} (+{len(assigned_projs) - 2})"

        pms_for_emp = emp_pms.get(eid, set())
        leads_for_emp = emp_leads.get(eid, set())

        desig = (designation or "").lower()
        if desig in ("admin", "hr"):
            mgrs = []
        elif "program manager" in desig or desig == "pm":
            mgrs = admin_names.copy()
        elif pms_for_emp:
            mgrs = sorted(list(pms_for_emp))
        elif leads_for_emp:
            mgrs = sorted(list(leads_for_emp))
        elif assigned_projs:
            mgrs = ["Unassigned"]
        else:
            mgrs = []

        mgr_str = ", ".join(mgrs) if mgrs else "—"
        return proj_str, mgr_str

    pending_list = []
    for emp in pending_employees:
        proj_str, mgr_str = resolve_emp_proj_and_mgr(emp.id, emp.designation)
        pending_list.append((emp.name, emp.email, proj_str, mgr_str))

    late_checkin_list = []
    for eid, emp_name, emp_email, emp_desig, time_str in late_checkin_raw:
        proj_str, mgr_str = resolve_emp_proj_and_mgr(eid, emp_desig)
        late_checkin_list.append((emp_name, emp_email, proj_str, mgr_str, time_str))

    # 3. PROJECT-WISE AGGREGATION (Pure SQL grouping)
    project_stats = {}
    for p in all_projects:
        p_pms = sorted([mgr_by_id[i] for i in ds_pm_ids.get(p.id, set()) if i in mgr_by_id])
        p_leads = sorted([mgr_by_id[i] for i in ds_lead_ids.get(p.id, set()) if i in mgr_by_id])
        if p_pms:
            pm_lead_str = ", ".join(p_pms)
        elif p_leads:
            pm_lead_str = ", ".join(p_leads)
        else:
            pm_lead_str = "Unassigned"

        project_stats[p.id] = {
            "name": p.name,
            "pm_lead": pm_lead_str,
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
        not_(Allocation.employee_id.in_(on_leave_subq))
    ).group_by(Allocation.sub_project_id).all()
    
    for p_id, count in allocations:
        if p_id in project_stats:
            project_stats[p_id]["allocated_total"] = count
            project_stats[p_id]["pending"] = count # Initially pending is total

    # Fetch Check-ins mapped to allocations
    emp_checkin_times = {emp_email: c_at_utc.astimezone(IST) for c_at_utc, _, _, _, emp_email, _, _ in checkins_today}
    
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
