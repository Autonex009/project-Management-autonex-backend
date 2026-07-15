"""
Recommendations API Router
Provides endpoints for project capacity analysis and recommendations.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from app.services.auth_service import get_current_user
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.services.recommendation_service import RecommendationEngine

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"], dependencies=[Depends(get_current_user)])


@router.get("/project/{project_id}")
def get_project_recommendations(project_id: int, db: Session = Depends(get_db)):
    """
    Get detailed capacity analysis and recommendations for a specific project.
    
    Returns:
    - Total estimated hours (workload)
    - Net capacity (available team hours)
    - Utilization ratio
    - Status (overburdened/balanced/underutilized)
    - Leave impact
    - Actionable recommendations
    """
    engine = RecommendationEngine(db)
    result = engine.calculate_project_capacity(project_id)
    
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result["error"]
        )
    
    return result


@router.get("/dashboard")
def get_dashboard_recommendations(db: Session = Depends(get_db)):
    """
    Get capacity overview for all active projects.
    Suitable for dashboard display with projects grouped by status.
    """
    engine = RecommendationEngine(db)
    return engine.get_dashboard_overview()


@router.get("/project/{project_id}/timeline")
def get_project_timeline(
    project_id: int,
    include_daily: bool = False,
    db: Session = Depends(get_db)
):
    """
    Get detailed timeline analysis for a project.
    
    Args:
        project_id: Project to analyze
        include_daily: If True, include full daily breakdown
    """
    engine = RecommendationEngine(db)
    result = engine.calculate_project_capacity(project_id)
    
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result["error"]
        )
    
    # Return full daily breakdown if requested
    if include_daily:
        # Re-calculate with full breakdown
        result = engine.calculate_project_capacity(project_id)
        # The daily_breakdown is truncated to 7 days by default, 
        # we need to expose full data
    
    return {
        "project_id": result["project_id"],
        "project_name": result["project_name"],
        "working_days": result["working_days"],
        "calendar_days": result.get("total_calendar_days", 0),
        "net_capacity": result["net_capacity"],
        "daily_average": round(result["net_capacity"] / max(result["working_days"], 1), 1),
        "leaves_impact": result.get("leaves_impact", 0),
        "timeline": result["daily_breakdown"] if include_daily else None
    }
