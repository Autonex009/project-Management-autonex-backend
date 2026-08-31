# PM Project Page Architecture & Optimizations

## Overview
The PM Projects Page (`ProjectsPage.jsx`) provides a detailed, card-based interface for managing active Sub-Projects. While it utilizes backend pagination, it still suffers from substantial latency (~4.1 seconds per page) and inefficient data hydration loops on the client-side.

## Architectural Flaw: Inefficient Data Hydration
The page paginates the projects list correctly using `subProjectApi.getPaginated()`, but it simultaneously executes multiple heavy "global" queries to hydrate the cards:
1. `allocationApi.getSlim()` - Downloads **all** allocations globally (307 rows) to manually calculate project capacity on the frontend.
2. `employeeApi.getSlim()` - Downloads **all** active employees (306 rows) just to resolve PM and Team Lead names on the project cards.
3. `employeeApi.getSlim({ status: "archived" })` - Due to a broken backend filter, this ignores the `status` query and downloads the **exact same 306 active employees again**, wasting 59.4 KB and polling the React Query cache.
4. `guidelineApi.getAll()` - Downloads **all 60 global guidelines** up-front, even if they belong to projects not visible on the current page.

## Database Schema (Required Entities)

To render the page efficiently, the backend must return a fully enriched card projection. The frontend requires:
- **Card Overviews**: `id`, `name`, `client`, `project_status`, `required_manpower`, `sentiment`, `start_date`, `end_date`, `pm_ids`, `team_lead_ids`.
- **Pre-computed Capacity**: The current headcount of active allocations.
- **Guideline Links**: The file URL and title for attached docs.

## Known Issues, Performance Bottlenecks & Proposed Solutions

| Priority | Endpoint / Data | Size | Time | Problem Identified | Recommended Backend Solution | Solved |
|---|---|---|---|---|---|---|
| P0 | `GET /api/sub-projects/paginated` | 6.5 KB | ~4.1 s | Fetches 46 fields per project when cards only need ~12 fields. | **Card Projection:** Implement `?view=card` in the backend to strip unused metadata (e.g. total_tasks, gearing_ratio). | ❌ |
| P0 | `GET /api/employees/slim?status=archived` | 59.4 KB | ~1.0 s | Backend filter is broken. It ignores `status=archived` and returns all 306 active employees again, duplicating the payload. | **Fix DB Filter:** Update `/api/employees/slim` to explicitly enforce `Employee.status == status` in the SQLAlchemy query. | ❌ |
| P1 | `GET /api/employees/slim` | 59.4 KB | ~1.5 s | Downloads 306 full profiles (including large `skills` JSON arrays). | **Strip Arrays:** Remove the `skills` array from the `slim` projection. Fetch full profiles only when opening the allocation modal. | ❌ |
| P1 | `GET /api/guidelines` | 23.8 KB | ~1.6 s | Downloads 60 global guidelines blindly on mount. | **Project-Scoped Guidelines:** Lazy-load this data (`/api/guidelines?sub_project_ids=1,2,3`) based on the visible cards. | ❌ |
| P2 | `GET /api/sub-projects/kpi` | 0.2 KB | ~2.6 s | Tiny payload but blocks the top KPI stat cards for 2.6s. | **Database Optimization:** Investigate the endpoint for missing indexes or Python-level aggregation loops. | ❌ |

## Implementation Plan
1. **Fix `status=archived` Filter:** Update `app/api/employees.py` so the slim endpoint properly filters by archived status, saving 59 KB instantly.
2. **Optimize `getPaginated` Card View:** Add a `view=card` parameter to the `sub-projects/paginated` endpoint that only returns the fields rendered on the card, dramatically speeding up DB retrieval.
3. **Lazy-Load Guidelines:** Refactor the frontend to only fetch guidelines if a project card requires them, rather than fetching the entire database up-front.
