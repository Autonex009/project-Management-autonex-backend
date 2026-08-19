# About Project Page

## Overview
The Project Page is the central hub for managing projects and their associated details. It fetches and displays project card data primarily from the `daily_sheets` database table (represented by the `DailySheet` / `Project` / `SubProject` models in the backend).

This page provides an interface to track progress, manpower allocation, project hierarchy, and time targets.

## Database Structure (`daily_sheets` table)
The underlying table for the Project Page is `daily_sheets`. It contains 40 columns:

### Core Identity & Hierarchy
- `id` (INTEGER): Primary Key
- `name` (TEXT): Name of the project/sheet
- `batch_name` (TEXT): Associated batch name
- `sub_project_id` (INTEGER): Foreign key to the parent `sub_projects` table
- `main_project_id` (INTEGER): Foreign key to the parent `main_projects` table
- `is_sub_project` (BOOLEAN): Flag indicating if this sheet acts as a sub-project
- `previous_daily_sheet_id` (INTEGER): Link to the previous sequence sheet

### Details & Classification
- `client` (TEXT): The exact name of the client
- `project_type` (TEXT): `POC` or `Full`
- `project_types` (JSON): Key-value pairs for categorization. Exact keys found in DB include: `Development`, `Object Segmentation Types`, `Data Modalities`, and `Annotation Types (By Data)`.
- `is_annotation` (BOOLEAN): `True` if it is an annotation project, `False` otherwise
- `priority` (TEXT): `P0`, `P1`, `P2`, `P3`, or `medium`
- `project_status` (TEXT): `poc`, `active`, `on-hold`, `completed`, or `cancelled`
- `sentiment` (TEXT): `GOOD`, `AVG`, or `None`

### Targets & Scheduling
- `start_date` (DATE): Start date of the project
- `end_date` (DATE): Target completion date
- `project_duration_weeks` (INTEGER): Duration in weeks
- `project_duration_days` (INTEGER): Duration in days
- `daily_target` (INTEGER): Target tasks per day
- `total_tasks` (INTEGER): Total tasks for the project
- `estimated_time_per_task` (FLOAT): Annotation time per task (in hours/minutes)
- `review_time_per_task` (FLOAT): Review time per task (in hours/minutes)
- `gearing_ratio` (FLOAT): Ratio between annotation and review

### Manpower & Staffing Requirements
- `required_manpower` (INTEGER): Total dynamically calculated headcount needed. Sums up the specific roles below.
- `autonex_annotators` (INTEGER): Requested annotators
- `autonex_reviewers` (INTEGER): Requested reviewers
- `team_lead_count` (INTEGER): Requested team leads
- `team_manager_count` (INTEGER): Requested project managers
- `others_count` (INTEGER): Requested other roles
- `developers_count` (INTEGER): Requested developers (for dev projects)
- `qc_count` (INTEGER): Requested QC (Historical)
- `annotators_total` (INTEGER): Vendor + Internal annotators (Informational)
- `workforce_reviewers` (INTEGER): External/Vendor reviewers
- `workforce_vendors` (JSON): List of vendor names

### Allocation & Integration
- `allocated_employees` (INTEGER): The number of active employees assigned to this project
- `assigned_employee_ids` (JSON): Cached list of PMs/Leads assigned
- `required_expertise` (JSON): Skills needed for the project
- `encord_project_hash` (TEXT): Encord integration ID

### Timestamps
- `created_at` (TIMESTAMP): Creation time
- `updated_at` (TIMESTAMP): Last updated time


## Backend APIs
The Project Page interacts with the following backend endpoints (defined in `app/api/projects.py` and newly optimized slim endpoints).

- **`GET /api/sub-projects/paginated`** (Optimized)
  Fetches a paginated, filtered, and sorted list of projects. Pagination (`.limit()` and `.offset()`) is pushed to the database level to ensure only the 12 visible projects are loaded and enriched in memory, dropping load times from 30 seconds to < 1.5 seconds.
- **`GET /api/sub-projects/kpi`** (Optimized)
  Fetches key performance indicators (KPIs) summarizing project status. It bypasses full project enrichment and leverages raw queries to instantly compute capacity and totals.
- **`POST /api/sub-projects/` & `PUT /api/sub-projects/{project_id}`**
  Creates/updates a project in the `daily_sheets` table, setting up initial hierarchy and role requirements.
- **Slim Endpoints (New & Optimized)**
  - `GET /api/employees/slim`: Drops payload from 188KB to 15KB for dropdown pickers.
  - `GET /api/allocations/slim`: Returns only `id`, `employee_id`, `sub_project_id`, and `role_tags`.
  - `GET /api/leaves/today-ids` & `GET /api/wfh/today-ids`: Return only simple arrays of employee IDs currently on leave or WFH today, dropping payloads by 99%.

## Key Logic and Data Flow
1. **Required Manpower Calculation:** The `required_manpower` is not a manually typed value. Whenever a project is created or updated, the backend runs `_autonex_headcount()` to sum `autonex_annotators + autonex_reviewers + team_lead_count + team_manager_count + others_count + developers_count`.
2. **Current Manpower vs Required Manpower:** The UI displays "Manpower required" (from the DB) next to "Manpower current" (calculated dynamically based on the active allocations).
3. **Hierarchy:** While the data is stored in `daily_sheets`, it is conceptually linked to `sub_projects` and `main_projects` to form a 3-level hierarchy (MainProject -> SubProject -> DailySheet).
4. **Allocation Connection & Synchronization (Auto-Syncing):** 
   - **How data is stored:** The `allocations` table is the true source of truth. When an employee is assigned, updated, or removed via the Allocations API, the backend immediately triggers `sync_project_allocations()`. This function counts active allocations and precisely overwrites the `allocated_employees`, `team_lead_count`, `team_manager_count`, and `assigned_employee_ids` (PMs) on the `daily_sheets` table. This guarantees the manpower limits never fall out of sync with actual staffing.
   - **How it reflects in the popover:** When the user hovers or clicks the project popover on the Projects Page, the frontend relies on the new lightweight `/api/allocations/slim` endpoint to fetch the list of `employee_ids` actively tied to that `sub_project_id`. It then maps these IDs to the slim roster (`/api/employees/slim`) to display names, avatars, and specific roles without freezing the browser.

## Known Issues, Performance Bottlenecks & Solutions Implemented

| Priority | Endpoint / Issue | Before (Size / Time) | Problem Identified (The Bottleneck) | Backend Solution Implemented | Solved |
|---|---|---|---|---|---|
| P0 | `GET /api/sub-projects/kpi` | 218 B / ~27.0 s | Filtering and capacity calculation did massive N+1 queries in Python loops for all 300+ projects. | Implemented `bulk_compute_capacity()` which calculates capacity for all active projects via 3 fast bulk queries. | ✅ |
| P0 | `GET /api/sub-projects/paginated` | 15.1 KB / ~15.2 s | Sliced the final list, but the internal filtering loop still loaded and enriched 300+ projects via heavy DB queries. | Rewrote `_get_filtered_enriched_projects` to use `project_scope.managed_projects_of_employee` to filter projects instantly. | ✅ |
| P3 | **504 Timeouts (App Load)** | Failed | Database queries took so long (30s) that the network connection timed out before the server finished. | Resolved inherently by the N+1 Database Query bulk fix; page now responds in milliseconds. | ✅ |
| P0 | **Allocation Sync Bug** | Data Corruption | `team_manager_count` and `team_lead_count` in `daily_sheets` would drift out of sync when allocations changed. | Injected `sync_project_allocations()` into `POST`, `PUT`, `DELETE` of `api/allocations.py` to auto-sync PM/Lead counts perfectly from allocations. | ✅ |
| P0 | `GET /api/allocations` | 170.5 KB / ~1.6 s | Massive payload (309 rows) filled with unused metadata (time_distribution, override_flag) just to count heads. | Created `GET /api/allocations/slim` returning only `{id, employee_id, sub_project_id, role_tags}`. | ✅ |
| P0 | `GET /api/leaves` | 134.4 KB / ~0.5 s | UI downloaded 318 historical leave records just to check who is off *today* (to show a tiny UI badge). | Created `GET /api/leaves/today-ids` which returns an ultra-light array of `employee_ids` on leave today. | ✅ |
| P0 | `GET /api/wfh` | 92.1 KB / ~1.1 s | Downloaded full history of WFH records to show a "today" badge. | Created `GET /api/wfh/today-ids` which returns an ultra-light array of `employee_ids` WFH today. | ✅ |
| P1 | `GET /api/employees` (Active & Archived) | 188 KB / ~2.1 s | Returned massive 22-field full employee profiles (e.g. `razorpay_email`) just to populate name dropdowns. | Created `GET /api/employees/slim` returning only `{id, name, designation, status, skills}`. | ✅ |
| P1 | `GET /api/guidelines` | 23.4 KB / ~1.5 s | Loaded all 59 guidelines up-front even for projects not on current page. | Needs frontend UI update to lazy-load or use a `has_guideline` flag. | ❌ |
| P2 | `GET /api/notifications` | 12.8 KB / ~1.9 s | Full payload fetched for 50 notifications just for a tiny unread badge in the top-right corner. | Needs `GET /api/notifications/unread-count`. | ❌ |
| P2 | `GET /api/projects` | 24.0 KB / ~1.0 s | Some fields (description, counts) less critical for list view. | Acceptable size for now; can slim later if needed. | ❌ |

---

### Impact Analysis (Before vs After Update)

| Metric / Endpoint | Before Backend Fixes | After Backend Fixes | Improvement |
|---|---|---|---|
| **Project Page Load Time** | **~26 to 31 Seconds** (Constant 504s) | **~3.6 Seconds** (No 504s) | **~96% Faster** 🚀 |
| **Employee Dropdowns Payload** | **~188 KB** (`/api/employees` + `archived`) | **~15 KB** (`/api/employees/slim`) | **92% Smaller** 📉 |
| **Leave & WFH Payload** | **~226 KB** (Full history) | **< 1 KB** (Only today's IDs) | **99% Smaller** 📉 |
| **Allocations Payload** | **~170 KB** (Full metadata) | **~20 KB** (Slim projection) | **88% Smaller** 📉 |
| **Database Queries Fired** | **~1,200+** per refresh | **< 10** per refresh | Massive CPU relief |
| **Manpower Syncing** | Manually running scripts to fix | **100% Automated & Accurate** | Zero Data Corruption |
