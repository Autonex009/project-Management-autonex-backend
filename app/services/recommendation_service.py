"""
Intelligent Recommendation Service
Calculates real capacity using actual availability, removing hardcoded assumptions.
Implements the algorithm from Module 3 of Autonex Architecture V2.

Thresholds (per-employee average daily hours):
  - Overburdened:  > 8.5h
  - Balanced:      7.5h – 8.5h
  - Underutilized: < 7.5h
"""
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import date, timedelta
from typing import List, Dict, Any, Optional
from collections import defaultdict

from app.models.parent_project import ParentProject  # Import first for relationship
from app.models.project import Project
from app.models.allocation import Allocation
from app.models.leave import Leave
from app.models.employee import Employee


class RecommendationEngine:
    """
    Calculates project capacity based on:
    - Project timeline (working days only)
    - Employee allocations with active date ranges
    - Leave records (with replacement suggestions)
    """
    
    def __init__(self, db: Session):
        self.db = db
    
    def _is_weekend(self, d: date) -> bool:
        """Check if date is a weekend (Saturday=5, Sunday=6)."""
        return d.weekday() >= 5
    
    def _get_working_days(self, start: date, end: date) -> List[date]:
        """Generate list of working days (excluding weekends) between dates."""
        working_days = []
        current = start
        while current <= end:
            if not self._is_weekend(current):
                working_days.append(current)
            current += timedelta(days=1)
        return working_days
    
    def _is_date_in_leave(self, check_date: date, leaves: List[Leave]) -> bool:
        """Check if a date falls within any leave record."""
        for leave in leaves:
            if leave.start_date and leave.end_date:
                if leave.start_date <= check_date <= leave.end_date:
                    return True
        return False
    
    def _is_date_in_allocation_range(
        self,
        check_date: date,
        alloc: Allocation,
        project_start: date,
        project_end: date
    ) -> bool:
        """
        Check if date is within allocation's active range.
        Uses project dates as fallback if allocation has no specific range.
        """
        alloc_start = alloc.active_start_date or project_start
        alloc_end = alloc.active_end_date or project_end
        return alloc_start <= check_date <= alloc_end
    
    def _find_replacement_candidates(
        self,
        employee_on_leave: Employee,
        project: Project,
        existing_employee_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """
        Find available employees who could replace someone on leave.
        Matches by skills and checks they aren't already fully allocated.
        """
        required_skills = project.required_expertise or []
        
        # Get all active employees not already on this project
        candidates = self.db.query(Employee).filter(
            Employee.status == 'active',
            Employee.id.notin_(existing_employee_ids)
        ).all()
        
        suggestions = []
        for candidate in candidates:
            candidate_skills = candidate.skills or []
            
            # Check skill match
            matching_skills = [
                skill for skill in required_skills
                if any(cs.lower() == skill.lower() for cs in candidate_skills)
            ]
            
            if not matching_skills and required_skills:
                continue
            
            # Check current allocation load
            current_allocations = self.db.query(Allocation).filter(
                Allocation.employee_id == candidate.id
            ).all()
            total_allocated_hours = sum(
                (a.total_daily_hours or 8) for a in current_allocations
            )
            max_capacity = candidate.working_hours_per_day or 8
            available_hours = max(0, max_capacity - total_allocated_hours)
            
            if available_hours > 0:
                suggestions.append({
                    "employee_id": candidate.id,
                    "employee_name": candidate.name,
                    "designation": candidate.designation,
                    "matching_skills": matching_skills,
                    "available_hours": available_hours,
                    "skill_match_ratio": len(matching_skills) / max(len(required_skills), 1)
                })
        
        # Sort by skill match ratio descending, then available hours descending
        suggestions.sort(key=lambda x: (-x["skill_match_ratio"], -x["available_hours"]))
        return suggestions[:5]  # Top 5 candidates
    
    def calculate_project_capacity(self, project_id: int) -> Dict[str, Any]:
        """
        Main algorithm: Calculate real capacity for a project.
        
        Algorithm Steps:
        1. Generate timeline array (project.start_date to project.end_date)
        2. For each Date in Timeline:
           - Skip if Weekend
           - For each Employee in Allocation List:
             * Check if Date within their active_date_range
             * Check if Date inside any Leave_Record
             * Add Employee.total_daily_hours to net_capacity
        3. Calculate per-employee average daily required hours
        4. Apply thresholds: >8.5h overburdened, 7.5-8.5h balanced, <7.5h underutilized
        """
        # Get project
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return {"error": f"Project with ID {project_id} not found"}
        
        # Get all allocations for this project
        allocations = self.db.query(Allocation).filter(
            Allocation.sub_project_id == project_id
        ).all()
        
        # Capacity needs a finite timeline; open-ended sub-projects (no end date)
        # can't be scheduled, so report a neutral "no_end_date" status instead of
        # crashing on date arithmetic.
        if not project.end_date:
            return {
                "project_id": project_id,
                "project_name": project.name,
                "status": "no_end_date",
                "message": "Set an end date for this sub-project to compute capacity.",
                "total_estimated_hours": round((project.total_tasks or 0) * (project.estimated_time_per_task or 0), 1),
                "net_capacity": 0,
                "working_days": 0,
                "total_calendar_days": 0,
                "utilization_ratio": 0,
                "avg_daily_hours_per_employee": 0,
                "team_size": len(allocations),
                "leaves_impact": 0,
                "leave_impact_details": [],
                "replacement_suggestions": [],
            }

        working_days = self._get_working_days(project.start_date, project.end_date)
        total_working_days = len(working_days)
        total_estimated_hours = project.total_tasks * project.estimated_time_per_task
        
        if not allocations:
            return {
                "project_id": project_id,
                "project_name": project.name,
                "total_estimated_hours": total_estimated_hours,
                "net_capacity": 0,
                "working_days": total_working_days,
                "total_calendar_days": (project.end_date - project.start_date).days + 1,
                "utilization_ratio": 999.0,
                "avg_daily_hours_per_employee": 0,
                "status": "overburdened",
                "team_size": 0,
                "leaves_impact": 0,
                "leave_impact_details": [],
                "replacement_suggestions": [],
                "recommendations": ["No team members allocated to this project — assign employees to begin work"],
                "daily_breakdown": []
            }
        
        # Get employee IDs and their leave records
        employee_ids = [a.employee_id for a in allocations]
        leaves_by_employee = defaultdict(list)
        
        all_leaves = self.db.query(Leave).filter(
            Leave.employee_id.in_(employee_ids)
        ).all()
        
        for leave in all_leaves:
            leaves_by_employee[leave.employee_id].append(leave)
        
        # Get employee details
        employees = {
            e.id: e for e in self.db.query(Employee).filter(
                Employee.id.in_(employee_ids)
            ).all()
        }
        
        # Step 1-2: Daily sweep
        net_capacity = 0
        daily_breakdown = []
        leave_days_by_employee = defaultdict(int)  # Track leave days per employee
        
        for work_date in working_days:
            day_capacity = 0
            day_details = {
                "date": work_date.isoformat(),
                "employees": []
            }
            
            for alloc in allocations:
                employee = employees.get(alloc.employee_id)
                if not employee:
                    continue
                
                # Check 1: Is date within allocation's active range?
                if not self._is_date_in_allocation_range(
                    work_date, alloc, project.start_date, project.end_date
                ):
                    continue
                
                # Check 2: Is date inside any leave record?
                if self._is_date_in_leave(work_date, leaves_by_employee[alloc.employee_id]):
                    leave_days_by_employee[alloc.employee_id] += 1
                    day_details["employees"].append({
                        "employee_id": alloc.employee_id,
                        "employee_name": employee.name,
                        "hours": 0,
                        "status": "on_leave"
                    })
                    continue
                
                # Add hours to capacity
                hours = alloc.total_daily_hours or int(alloc.weekly_hours_allocated or 0) // 5 or 8
                day_capacity += hours
                day_details["employees"].append({
                    "employee_id": alloc.employee_id,
                    "employee_name": employee.name,
                    "hours": hours,
                    "status": "available"
                })
            
            day_details["total_hours"] = day_capacity
            daily_breakdown.append(day_details)
            net_capacity += day_capacity
        
        # Step 3: Calculate per-employee average daily required hours
        team_size = len(allocations)
        
        if team_size > 0 and total_working_days > 0:
            # Total hours of work per employee across the project
            hours_per_employee = total_estimated_hours / team_size
            # Daily required hours per employee (spread over working days)
            avg_daily_hours_per_employee = hours_per_employee / total_working_days
        else:
            avg_daily_hours_per_employee = 0
        
        # Step 4: Calculate utilization ratio
        if net_capacity > 0:
            utilization_ratio = total_estimated_hours / net_capacity
        else:
            utilization_ratio = 999.0
        
        # Step 5: Determine status based on per-employee daily hours thresholds
        if avg_daily_hours_per_employee > 8.5:
            status = "overburdened"
            overage = round(avg_daily_hours_per_employee - 8.0, 1)
            recommendations = [
                f"Each employee averages {round(avg_daily_hours_per_employee, 1)}h/day — exceeds 8.5h threshold",
                f"Team is overloaded by {overage}h per person per day",
                "Consider adding more team members or extending the deadline"
            ]
        elif avg_daily_hours_per_employee >= 7.5:
            status = "balanced"
            recommendations = [
                f"Each employee averages {round(avg_daily_hours_per_employee, 1)}h/day — within optimal range",
                "Resource allocation is well balanced"
            ]
        elif avg_daily_hours_per_employee > 0:
            status = "underutilized"
            spare = round(8.0 - avg_daily_hours_per_employee, 1)
            recommendations = [
                f"Each employee averages {round(avg_daily_hours_per_employee, 1)}h/day — below 7.5h threshold",
                f"Team has ~{spare}h spare capacity per person per day",
                "Consider reassigning some team members to other projects"
            ]
        else:
            status = "overburdened"
            recommendations = ["No capacity available — project needs staffing"]
        
        # Step 6: Leave impact analysis and replacement suggestions
        total_leave_days = sum(leave_days_by_employee.values())
        leave_impact_details = []
        replacement_suggestions = []
        
        for emp_id, leave_day_count in leave_days_by_employee.items():
            if leave_day_count > 0:
                employee = employees.get(emp_id)
                emp_leaves = leaves_by_employee[emp_id]
                
                # Find which leaves overlap with the project
                overlapping_leaves = []
                for leave in emp_leaves:
                    if leave.start_date and leave.end_date:
                        overlap_start = max(leave.start_date, project.start_date)
                        overlap_end = min(leave.end_date, project.end_date)
                        if overlap_start <= overlap_end:
                            overlapping_leaves.append({
                                "leave_type": leave.leave_type,
                                "start_date": leave.start_date.isoformat(),
                                "end_date": leave.end_date.isoformat(),
                                "working_days_lost": leave_day_count
                            })
                
                detail = {
                    "employee_id": emp_id,
                    "employee_name": employee.name if employee else "Unknown",
                    "working_days_on_leave": leave_day_count,
                    "leaves": overlapping_leaves
                }
                leave_impact_details.append(detail)
                
                # Find replacement candidates for this employee
                candidates = self._find_replacement_candidates(
                    employee, project, employee_ids
                )
                if candidates:
                    replacement_suggestions.append({
                        "for_employee_id": emp_id,
                        "for_employee_name": employee.name if employee else "Unknown",
                        "leave_days": leave_day_count,
                        "candidates": candidates
                    })
        
        # Add leave-related recommendations
        if total_leave_days > 0:
            hours_lost = total_leave_days * 8  # Approximate
            recommendations.append(
                f"⚠️ {total_leave_days} employee-days lost to leaves ({hours_lost}h capacity reduction)"
            )
            if replacement_suggestions:
                recommendations.append(
                    f"💡 Replacement candidates available for {len(replacement_suggestions)} employee(s) on leave"
                )
        
        return {
            "project_id": project_id,
            "project_name": project.name,
            "total_estimated_hours": round(total_estimated_hours, 1),
            "net_capacity": net_capacity,
            "working_days": total_working_days,
            "total_calendar_days": (project.end_date - project.start_date).days + 1,
            "utilization_ratio": round(utilization_ratio, 3),
            "avg_daily_hours_per_employee": round(avg_daily_hours_per_employee, 2),
            "status": status,
            "team_size": team_size,
            "leaves_impact": total_leave_days,
            "leave_impact_details": leave_impact_details,
            "replacement_suggestions": replacement_suggestions,
            "recommendations": recommendations,
            "daily_breakdown": daily_breakdown[:7]  # Return first week only for summary
        }
    
    def get_dashboard_overview(self) -> Dict[str, Any]:
        """
        Get capacity overview for all active projects.
        Returns summary suitable for dashboard display.
        """
        projects = self.db.query(Project).filter(
            Project.project_status == "active"
        ).all()
        
        overview = {
            "total_projects": len(projects),
            "overburdened": [],
            "balanced": [],
            "underutilized": []
        }
        
        for project in projects:
            result = self.calculate_project_capacity(project.id)
            if "error" not in result and result.get("status") != "no_end_date":
                summary = {
                    "project_id": project.id,
                    "project_name": project.name,
                    "utilization_ratio": result["utilization_ratio"],
                    "avg_daily_hours_per_employee": result.get("avg_daily_hours_per_employee", 0),
                    "status": result["status"],
                    "team_size": result.get("team_size", 0),
                    "leaves_impact": result.get("leaves_impact", 0)
                }
                
                if result["status"] in ["overburdened"]:
                    overview["overburdened"].append(summary)
                elif result["status"] in ["balanced"]:
                    overview["balanced"].append(summary)
                else:
                    overview["underutilized"].append(summary)
        
        return overview
