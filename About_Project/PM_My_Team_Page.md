# PM My Team Page Architecture & Optimizations

## Overview
The PM My Team Page (`MyTeamPage.jsx`) provides a comprehensive view of a Project Manager's direct reports, including their current active projects, contact information (like Slack DMs), and daily attendance (Leaves).

## Architectural Flaw: Silent Data Waste
The page relies on a massive aggregation endpoint (`employeeApi.getTeamData()`) which downloads hundreds of kilobytes of data. However, a significant portion of this data is completely ignored by the frontend.

### The Problematic Data Flow
When the page mounts, it executes:
1. `employeeApi.getTeamData()` - Fetches employees, projects, allocations, **and the entire history of leaves and WFH requests** for the team.
2. `leaveApi.getAll({ start_date: todayStr, end_date: todayStr })` - Fetches today's specific leaves.

The frontend then destructures the `teamData` object:
```javascript
const employees = teamData?.employees || [];
const scopedProjects = teamData?.projects || [];
const scopedAllocations = teamData?.allocations || [];
```
Noticeably missing are the `leaves` and `wfh_requests` arrays from `teamData`. The frontend downloads ~150+ full historical leave and WFH objects inside the `teamData` payload (costing ~21 seconds of backend latency), and immediately throws them in the trash, favoring the separate `leaveApi.getAll()` call instead.

## Database Schema (Required Entities)

To render the page efficiently, the backend must return only the data the table uses. The frontend requires:
- **Employee Profiles**: `id`, `name`, `email`, `designation`, `status`, `slack_user_id` (for the Slack DM button).
- **Projects & Allocations**: Only the sub-projects currently active and assigned to those employees.
- **Attendance**: A simple list of employee IDs who are on leave today.

## Known Issues, Performance Bottlenecks & Proposed Solutions

| Priority | Endpoint / Data | Size | Time | Problem Identified | Recommended Backend Solution | Solved |
|---|---|---|---|---|---|---|
| P0 | `GET /api/employees/team-data` | 178.7 KB | ~21.2 s | Over-fetched payload. Embeds full history of 153 leaves and 143 WFH requests, which the frontend code silently ignores. | **Strip History:** Remove `leaves` and `wfh_requests` from the `team-data` backend response entirely. Target ~40-60 KB. | ❌ |
| P1 | `GET /api/leaves?start_date=...` | 3.1 KB | ~7.4 s | High latency just to check who is on leave today. | **Migrate to Slim Endpoint:** Replace this with `GET /api/leaves/today-ids` to instantly resolve absent employees, or inject the `on_leave_today: true` flag directly into the `team-data` employee objects. | ❌ |
| P1 | Unused Profile Fields | — | — | Employee objects inside `team-data` return 22 fields, many of which (like `razorpay_email`, `working_hours_per_day`) are not used on the UI. | **Projection:** Only serialize the fields rendered on the My Team table and action menus (name, email, slack_user_id, status). | ❌ |

## Implementation Plan
1. **Refactor Backend `team-data`:** Update `app/api/employees.py` to completely stop joining and serializing `leaves` and `wfh_requests` history into the `team-data` payload. This will drastically reduce the 21-second latency.
2. **Optimize Daily Leave Check:** Instead of making a separate heavy `/api/leaves` call from `MyTeamPage.jsx`, migrate to the optimized `leaveApi.getTodayIds()` which returns a simple array of integers (employee IDs).
