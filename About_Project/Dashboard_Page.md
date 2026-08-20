# Backend Dashboard Architecture & Optimizations

## Overview
The `Dashboard.jsx` page (and its associated backend analytics endpoints) serves as the main administrative overview of the PM Portal. It aggregations system-wide data across Employees, Leaves, WFH Requests, Projects, and Analytics.

## Database Schema (Aggregated Entities)

### Employee (`employees`)
- `id` (INTEGER)
- `status` (STRING): active, inactive, archived
- `employee_type` (STRING): Intern, Full-Time, Contractor
- `designation` (STRING): Role titles used for the breakdown

### Leave & WFH (`leaves` / `wfh_requests`)
- `employee_id` (INTEGER)
- `start_date` / `end_date` / `wfh_date` (DATE)
- `status` (STRING): pending, approved, rejected

### Project (`daily_sheets` / `main_projects`)
- `project_status` (STRING): Used to count active/archived projects.
- `workforce_vendors` (JSON): 3rd party vendor names used for Dashboard breakdowns.
- `main_project_id` -> `MainProject.client` (STRING): Client organization names used for Dashboard breakdowns.

## Backend APIs
The Dashboard relies heavily on the `app/api/analytics.py` endpoints to aggregate system data:

- **`GET /api/analytics/dashboard/kpis`**
  Fetches total employee counts, total active/archived projects, and active "desk" metrics (people currently on Leave/WFH today). Now includes accurate `by_organisation` and `by_vendor` arrays.
- **`GET /api/analytics/summary`**
  Returns aggregated time-tracking and progress metadata for all active projects.
- **`GET /api/analytics/autonex/overview` & `kpis`**
  Returns time-series data for the 30-day sparklines and the "Most Active" panel.
- **`GET /api/sub-projects/paginated?limit=5`**
  Fetches 5 recent projects to render the quick-glance table.
- **`GET /api/notifications/unread-summary`**
  Returns unread counts and recent notifications.

## Key Logic and Data Flow
1. **Aggregating KPIs:** The backend calculates dashboard metrics by executing several `SQL GROUP BY` operations (e.g., grouping employees by status and designation, or grouping projects by their `project_status`).
2. **Current Desk Availability:** The backend filters `Leave` and `WFHRequest` tables by checking if `date.today()` falls between `start_date` and `end_date` with an `approved` status.
3. **Accurate Dashboard Breakdowns:** The backend explicitly calculates "Projects by Organisation" and "Projects by Vendor" across ALL active database projects, guaranteeing accuracy over frontend pagination limits.

## Known Issues, Performance Bottlenecks & Solutions Implemented

| Priority | Endpoint / Data | Size | Time | Problem Identified | Backend Solution Implemented | Solved |
|---|---|---|---|---|---|---|
| P0 | `GET /api/analytics/summary` | 32.8 KB -> 2.8 KB | 8.14 s -> <100ms | Payload was slimmed via `?fields=`, but backend still ran N+1 database queries internally, looping 55 times. | Built a "Fast Path" SQL `GROUP BY` that intercepts the query, completing in 1 database hit. | ✅ |
| P1 | `GET /api/sub-projects/paginated` | 6.3 KB | ~34 s | Huge overhead calculating capacities for simple dashboard list. | Created `?is_dashboard=true` projection that bypasses heavy math. | ✅ |
| P1 | `projectsByOrganisation` & `projectsByVendor` | — | — | Fake stats calculated by JS on frontend limits. | Implemented fast `SQL GROUP BY` and bundled into `dashboard-kpis`. | ✅ |
| P2 | `employees.by_work_model` | 2.1 KB | — | Wasteful payload array. | Omitted `by_work_model` from payload. | ✅ |
| P2 | `GET /api/notifications` | 12.8 KB | ~2.8 s | Full list of 50 notifications downloaded just for a layout badge. | Built `/api/notifications/unread-summary` to serve count + latest 5. | ✅ |


## Performance Comparison (Before vs After)
Based on network profiling before and after the architecture changes, here is the dramatic reduction in payload sizes and load times for the Dashboard:

| Endpoint / Data | Before (Payload -> Time) | After (Payload -> Time) | Net Improvement |
|---|---|---|---|
| **Analytics Summary** (/api/analytics/summary) | 32.8 KB -> ~7.7 s | 2.8 KB -> < 100 ms | **91% smaller, 70x faster** (via ?fields= + Fast Path SQL) |
| **Project List** (/api/sub-projects/paginated) | 6.3 KB -> ~34 s | 1.1 KB -> ~964 ms | **82% smaller, 35x faster** (via ?is_dashboard=true) |
| **Notifications** (/api/notifications/unread-summary) | 12.8 KB -> ~2.8 s | 1.5 KB -> ~2.7 s | **88% smaller** (via slim unread-summary projection) |
| **Skills Summary** (/api/skills/summary) | 1.9 KB -> ~1.2 s | 0 KB -> 0 s | **100% eliminated** (dead query removed) |
