# Check-in Feature Enhancements Plan

## UI Refinements
- [✓] **Remove Header in Admin Page**: Remove the title "Company Check-ins" and subtitle "Monitor daily attendance and work locations..." from `AdminCheckInsPage.jsx`.
- [✓] **Standardize KPI Cards**: Replace the hardcoded `div`-based KPI cards in `AdminCheckInsPage.jsx` with the `StatCard` component used in `TeamCheckInsPage.jsx` for consistency.
- [✓] **Fix Layout Padding**: Remove the `max-w-[1200px] mx-auto py-6` classes in `AdminCheckInsPage.jsx` to inherit the standard padding and layout from `AppShellLayout` (`<div className="space-y-4">`).

## New Features
- [✓] **Smart Slack Reminder (10:00 AM IST)**: 
   - Utilize the existing `apscheduler` or `arq` setup in the backend.
   - Create a cron task that runs daily at 10:00 AM IST.
   - **Edge Case Logic:** 
     - Automatically skip weekends (Saturdays and Sundays).
     - Cross-reference with the `leaves` table to ensure employees on **Approved Leave** today are explicitly skipped and not bothered.
   - Send a DM via `slack_service.py` reminding remaining active employees to check in.
- [✓] **Global Filters**: 
   - Add a global filter button on the Check-in pages.
   - Filter by **Project**, **Status** (Checked In / Not Yet), and **Time** (After 10:00 AM / Custom Time - applies exclusively to late check-ins).
   - Update API endpoints to support these new query parameters.

---

## Architecture Review & History Tabs (Points 6 & 7) [COMPLETED]

To future-proof the application as it scales past 5,000+ employees, we have successfully adopted a full enterprise-grade **Hybrid Partitioning + Materialized View Strategy**. This guarantees lightning-fast grid generation for the Weekly, Monthly, and Custom Range history tabs without over-burdening the database.

### 1. Database Storage Structure & Exact Table Examples

**Current State (Partitioning + 1 Materialized View)**
The logical structure of the database was successfully migrated to:

**Table 1: `daily_checkins` (Master Partitioned Table)**
PostgreSQL splits the master table into physical monthly tables (e.g., `daily_checkins_2026_09`).
* **Automated Maintenance**: A scheduled `apscheduler` job runs on the 25th of every month to automatically create the next month's partition. Zero manual DBA intervention needed.
* **How data is stored:**
  | id | employee_id | checkin_date | work_mode | checked_in_at | pm_confirmed_by |
  |----|-------------|--------------|-----------|---------------|-----------------|
  | 1  | 105 (John)  | 2026-09-02   | WFO       | 10:02:00      | 42              |
  | 2  | 105 (John)  | 2026-09-03   | WFH       | 09:58:00      | NULL            |


**Table 2: `historical_checkins_matrix` (New Materialized View)**
This read-only structure is generated at the end of each month. It permanently stores the pivoted matrix data so the backend doesn't compute it on the fly for historical tabs.
* **How data is stored:** Pre-aggregated JSON objects mapping exact dates to check-in times and work modes (e.g., `10:02 🏢` for WFO).
* **Example:**
  | employee_id | month_year | checkin_matrix (JSONB) |
  |-------------|------------|------------------------|
  | 105 (John)  | 2026-08    | `{"1": {"time": "10:05", "mode": "WFO"}, "2": {"time": "09:50", "mode": "WFH"}, ...}` |

### 2. Query Strategy by View Type

#### A. Daily View (Today or specific Past Day)
* **Used by**: Admin and PMs (`Today's Roster` tab).
* **Format required**: `Employee | Projects | Status | Mode | Checked in | Confirmed by PM`
* **Strategy**: We run a **Standard Flat Relational Query** against the `daily_checkins` partition. Because it's filtered to a single specific day, scanning the partition for detailed metadata is incredibly fast and 100% real-time.

#### B. Weekly, Monthly, & Custom Tabs (Matrix / Heatmap)
* **Format required**: 
  * Weekly: `Name | Mon(2) | Tue(3) | ... | Sun(8)`
  * Monthly: `Name | 1 | 2 | ... | 31`
  * Custom Range: `Name | Start Date | ... | End Date`
* **UI/UX Upgrade:** Matrix cells use context markers based on `work_mode` (e.g., `10:02 🏢` for WFO, `09:58 🏠` for WFH) so PMs can instantly visually parse attendance modes. The frontend uses a shared `DatePicker` component for clean, consistent date selection.
* **Custom Range Performance Limits:** A maximum 90-day window is enforced on the frontend to prevent browser memory exhaustion and excessive API polling when rendering massive grids.
* **Strategy**: We use the **Hybrid Strategy**:
  1. **Current Month (Live)**: We query the live `daily_checkins` partition using PostgreSQL JSON aggregation: `jsonb_object_agg`.
  2. **Past Months (Historical)**: We query the **Materialized View** (`historical_checkins_matrix`).
  3. **Cross-Month Fetching (Frontend Magic)**: If a Custom Range or Week spans across multiple months (e.g., Aug 31 to Sept 6), the frontend `HistoryMatrix.jsx` seamlessly detects this, fires parallel API requests for `2026-08` and `2026-09`, and stitches the resulting JSON dicts back together into a single unified timeline. No complex backend join logic is required.
