# PM Allocation Page Architecture & Optimizations

## Overview
The PM Allocation Page is the central operational screen for project managers to assign their team members to active sub-projects. It relies heavily on highly aggregated table data that merges project details, required manpower, current slots, and daily employee availability.

## Database Schema (Associated Entities)

### Allocation (`allocations`)
- `id` (INTEGER)
- `employee_id` (INTEGER)
- `sub_project_id` (INTEGER)
- `role_tags` (JSON)

### Sub-Project (`sub_projects`)
- `id` (INTEGER)
- `name` (STRING)
- `required_manpower` (INTEGER)
- `required_expertise` (JSON)

## Backend APIs
The page interacts with an aggregated pagination API and a project dropdown API. While the payload shape of the main table is excellent, the backend processing time is unacceptably slow.

- **`GET /api/allocations/page`**
  Fetches the core allocation table. Shape is highly optimized (project info, required vs assigned manpower, WFO/WFH counts, and a preview array of avatars).
- **`GET /api/sub-projects`**
  Fetches projects for the "Select Project" allocation dropdown.

## Key Logic and Data Flow
1. **Aggregated Headcounts:** The backend merges multiple tables (`sub_projects`, `allocations`, `leaves`, `wfh_requests`) to instantly show a PM how many people are in the office vs on leave for a specific project.
2. **Project Selection:** The UI relies on a dropdown populated by `/api/sub-projects` to choose where to allocate a user.

## Known Issues, Performance Bottlenecks & Proposed Solutions

| Priority | Endpoint / Data | Size | Time | Problem Identified | Recommended Backend Solution | Solved |
|---|---|---|---|---|---|---|
| P0 | `GET /api/allocations/page?...` | 6.5 KB | ~22.4 s | Excellent slim payload, but extreme 22+ second latency. Blocks the entire allocation surface. | Severe backend bottleneck. Investigate heavy SQL joins, missing indexes, or Python N+1 loops calculating headcounts. Target < 500 ms. | ❌ |
| P0 | `GET /api/sub-projects` | 6.5 KB | ~23.9 s | Fetches full 46-field project objects for a simple dropdown. Very slow (23.9s). | Implement a slim projection `?fields=id,name,required_manpower,required_expertise` for dropdowns. Target < 200 ms. | ❌ |
| P2 | `GET /api/notifications/unread-summary` | 1.3 KB | ~5.7 s | Fetches layout notifications but suffers from slow response times. | Improve backend speed. Increase frontend `staleTime` to avoid aggressive focus refetching. | ❌ |
