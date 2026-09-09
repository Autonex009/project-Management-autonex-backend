# Late Check-in Escalation Plan

## Overview
This plan outlines the steps required to implement a two-stage escalation process for employees who fail to check in by the required times:
1. **10:45 AM (Warning DM):** A direct Slack message to employees who haven't checked in, warning them that their names will be escalated to admins shortly.
2. **11:10 AM (Admin Report):** A single Slack message sent to a specific channel (e.g., `#late-check-in-list`) containing a compiled list of all employees who still haven't checked in.

## 1. Environment Variables (`.env`)
To ensure the timings are fully configurable (just like the other schedulers), we will add the following environment variables:

```env
# 10:45 AM Warning DM
LATE_WARNING_HOUR=10
LATE_WARNING_MINUTE=45

# 11:10 AM Admin Report
ADMIN_REPORT_HOUR=11
ADMIN_REPORT_MINUTE=10

# The Slack Channel ID for the Admin Group (e.g., "Late check in list")
LATE_CHECKIN_CHANNEL_ID="C1234567890" 
```
*(Note: The channel name "Late check in list" must be converted to its actual Slack Channel ID in the production `.env` file).*

## 2. Slack Service Updates (`app/services/slack_service.py`)
We need to create two new Slack message formatting functions:

1. `send_late_warning_message(employee_slack_user_id, employee_name)`
   - **Type:** Direct Message
   - **Content:** "Hi [Name], you haven't checked in yet. If you do not check in now, your name will be added to the late check-in list and sent to the admins in a few minutes. Please check in immediately."
   - Includes the "Check In Now" action button.

2. `send_admin_late_list(channel_id, late_employees)`
   - **Type:** Channel Message
   - **Content:** A formatted list (or blocks) of the employees who have not checked in today.
   - Example format: "*Daily Late Check-in Report (Total: 5)* \n• John Doe (john@autonex.ai)\n• Jane Doe (jane@autonex.ai)"

## 3. Scheduler Service Updates (`app/services/scheduler_service.py`)
We will add two new background jobs to the scheduler:

### Job A: `_scheduled_late_warning` (Runs at 10:45 AM)
- **Weekend/Holiday Check:** Immediately skips execution if today is a weekend or fixed holiday.
- **Optimized DB Query:** Queries the database using `NOT EXISTS` to efficiently return only active employees who do not have a `DailyCheckIn` today and do not have an approved `Leave` overlapping today.
- Loops through the remaining employees and calls `send_late_warning_message`.
- **Rate Limit Handling:** Implements `time.sleep(1.5)` between messages to respect Slack's 50 requests/min limit. Also wraps the call in a `try-except` block to catch `429 Too Many Requests` errors, parsing the `Retry-After` header to pause appropriately if the limit is hit.

### Job B: `_scheduled_admin_report` (Runs at 11:10 AM)
- **Weekend/Holiday Check:** Immediately skips execution if today is a weekend or fixed holiday.
- **Optimized DB Query:** Same efficient query as Job A to fetch missing check-ins.
- Compiles their names and emails into a list.
- **Message Chunking:** If the list of late employees is very large, splits the report into multiple messages or posts it in a thread to avoid exceeding Slack's message length limits.
- Calls `send_admin_late_list` to post the compiled report directly to the `LATE_CHECKIN_CHANNEL_ID`.

## 4. Execution Plan
1. Update `slack_service.py` with the two new messaging functions, including retry logic for rate limits and chunking logic.
2. Update `scheduler_service.py` to pull the new environment variables and register the two new tasks in `start_scheduler()`.
3. Ensure both jobs use the `today_ist()` utilities and check `is_weekend` / `is_fixed_holiday` before executing.
4. Test locally by overriding the environment variables to the current time.
