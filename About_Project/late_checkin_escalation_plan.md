# Late Check-in & Roster Escalation Plan

## Overview
This plan outlines an automated, self-correcting roster system driven by employee daily check-ins, along with a multi-stage escalation and reporting process via Slack. It ensures managers and admins receive highly accurate, project-wise staffing reports twice a day.

### Escalation Timeline
- **10:00 AM:** Standard Daily Check-in Reminder (Existing).
- **10:45 AM (Warning DM):** A direct Slack message to active employees who haven't checked in, warning them of impending escalation.
- **11:10 AM (Admin Report 1):** Detailed summary report sent to the Admin Slack channel.
- **12:00 PM:** PM/Lead check-in confirmation reminder (Existing).
- **6:30 PM (Admin Report 2):** Evening follow-up summary report sent to the Admin Slack channel.

## 1. Environment Variables (`.env`)
To ensure the timings are fully configurable, we require the following environment variables:
```env
LATE_WARNING_HOUR=10
LATE_WARNING_MINUTE=45

ADMIN_REPORT_HOUR=11
ADMIN_REPORT_MINUTE=10

EVENING_REPORT_HOUR=18
EVENING_REPORT_MINUTE=30

LATE_CHECKIN_CHANNEL_ID="C1234567890" 
```

---

## 2. Infrastructure Optimizations (Slack Service)

To support sending large, detailed reports to hundreds of employees without crashing, two core optimizations are required in `app/services/slack_service.py`.

### 2.1 Rate Limit Handling (`Retry-After`)
**The Problem:** Currently, if Slack throws a `429 Too Many Requests` error during the 10:45 AM Warning blasts, the system hard-sleeps for 10 seconds. This is inefficient and can still cause failures if Slack demands a 30-second backoff.
**The Optimization:** We will parse the exact `Retry-After` HTTP header from Slack.

*In-Depth Example:*
```python
# Custom Exception
class SlackRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds

# Inside _slack_request (using urllib)
try:
    response = urlopen(req)
except HTTPError as e:
    if e.code == 429:
        # Read the exact backoff time requested by Slack, default to 10 if missing
        retry_after = int(e.headers.get("Retry-After", 10))
        raise SlackRateLimitError(retry_after)
```
The Scheduler will catch `SlackRateLimitError`, sleep for exactly `retry_after` seconds, and safely resume.

### 2.2 Message Chunking (The 50-Block Limit)
**The Problem:** Slack has a strict hard limit of 50 UI blocks per message payload. Because our new Admin Report uses detailed Block Kit formatting for every project, a large company will easily exceed 50 blocks, crashing the API.
**The Optimization:** We will implement a chunking engine that sends blocks in batches of 40. Any overflow blocks will be sent as threaded replies to the main message.

*In-Depth Example:*
```python
def _send_chunked_blocks(channel_id: str, blocks: list):
    max_blocks = 40
    main_message_ts = None
    
    for i in range(0, len(blocks), max_blocks):
        chunk = blocks[i:i + max_blocks]
        payload = {"channel": channel_id, "blocks": chunk}
        
        # If this is chunk 2+, send it as a threaded reply to keep the channel clean
        if main_message_ts:
            payload["thread_ts"] = main_message_ts
            
        response = _slack_request("chat.postMessage", payload)
        
        # Save the timestamp of the first message to thread the rest
        if not main_message_ts:
            main_message_ts = response.get("ts")
```

---

## 3. The Stats Engine & Slack Format

To generate project-wise statistics, the Scheduler cannot rely on N+1 queries. We will build a unified Stats Engine.

### 3.1 The Optimized Query Engine (`scheduler_service.py`)
We will create a helper function `_generate_admin_report_payload(db)`.
1. **Fetch Roster:** Query the `allocations` table where `is_active = True`. (This represents today's expected roster).
2. **Fetch Check-ins:** Query `DailyCheckIn` for today.
3. **In-Memory Grouping:** Loop through the allocations, grouping employees by `sub_project_id`. Check if their ID exists in the Check-ins array. 
4. **Result:** An aggregated dictionary containing Overall Stats and Project-Wise stats.

### 3.2 Slack Demo Format
```text
📊 *Daily Check-in & Summary Report*

*1. Overall Check-in Summary*
• 👥 *Total Active Employees:* 150 (excludes employees on leave)
• ✅ *Total Checked-in:* 120
• ⏳ *Total Pending:* 30
• ⏱️ *Average Check-in Time:* 09:42 AM
• 🥇 *First Check-in:* Jane Doe at 07:15 AM

*Check-in Time Distribution:*
  ◽ *Before 9:00 AM:* 45 employees
  ◽ *9:00 AM - 10:00 AM:* 50 employees
  ◽ *10:00 AM - 11:00 AM:* 20 employees
  🟥 *After 11:00 AM:* 5 employees

━━━━━━━━━━━━━━━━━━━━━━

*2. Project-Wise Summary*

📁 *Project: Apollo Data Annotation*
• *PM:* John Smith (Check-in: 09:30 AM ✅)
• *Team Lead:* Sarah Connor (Check-in: 08:45 AM ✅)
• *Check-ins:* 35 / 40 (5 Pending) | *Avg Time:* 09:15 AM
• *Distribution:* Before 9: 15 | 9-10: 15 | 10-11: 4 | After 11: 1

━━━━━━━━━━━━━━━━━━━━━━

*3. Pending Check-ins (Action Required)*
There are currently *30 employees* who have not checked in today.

[ 🖱️ View Detailed Pending List ]  <-- Slack Action Button redirecting to Frontend
```

---

## 4. Scheduler Service Execution (`app/services/scheduler_service.py`)

We will manage two specific jobs for reporting:

### Job A & B: `_scheduled_admin_report` (11:10 AM) & `_scheduled_evening_report` (6:30 PM)
- Both jobs call the `_generate_admin_report_payload(db)` stats engine.
- They pass the resulting dictionary to `send_admin_late_list(stats_payload)`.
- The Slack service formats the blocks and safely chunks them to prevent 50-block limit crashes.

---

## 5. Advanced Optimizations (Enterprise Scalability)

To push the roster system to be highly optimized and reliable, the following advanced optimizations will be applied:

### 5.1 SQL-Level Aggregation
Instead of fetching all check-ins and allocations into Python's memory to group them manually, we will offload the math to the database. The Stats Engine will use an optimized SQLAlchemy `JOIN` query with `GROUP_BY` and `COUNT()` so that PostgreSQL performs the heavy lifting and returns only the aggregated summary.

### 5.2 Asynchronous Slack Blasts
Currently, iterating over hundreds of employees to send direct messages using synchronous `urllib.request` blocks the scheduler thread for minutes. We will upgrade the Slack messaging functions to use asynchronous calls (`asyncio` / `httpx` / `aiohttp`), significantly reducing the total time taken to send warning blasts while strictly adhering to Slack's concurrency limits and preventing the APScheduler queue from stalling.

### 5.3 Chunk Transmission Resiliency
# Late Check-in & Roster Escalation Plan

## Overview
This plan outlines an automated, self-correcting roster system driven by employee daily check-ins, along with a multi-stage escalation and reporting process via Slack. It ensures managers and admins receive highly accurate, project-wise staffing reports twice a day.

### Escalation Timeline
- **10:00 AM:** Standard Daily Check-in Reminder (Existing).
- **10:45 AM (Warning DM):** A direct Slack message to active employees who haven't checked in, warning them of impending escalation.
- **11:10 AM (Admin Report 1):** Detailed summary report sent to the Admin Slack channel.
- **12:00 PM:** PM/Lead check-in confirmation reminder (Existing).
- **6:30 PM (Admin Report 2):** Evening follow-up summary report sent to the Admin Slack channel.

## 1. Environment Variables (`.env`)
To ensure the timings are fully configurable, we require the following environment variables:
```env
LATE_WARNING_HOUR=10
LATE_WARNING_MINUTE=45

ADMIN_REPORT_HOUR=11
ADMIN_REPORT_MINUTE=10

EVENING_REPORT_HOUR=18
EVENING_REPORT_MINUTE=30

LATE_CHECKIN_CHANNEL_ID="C1234567890" 
```

---

## 2. Infrastructure Optimizations (Slack Service)

To support sending large, detailed reports to hundreds of employees without crashing, two core optimizations are required in `app/services/slack_service.py`.

### 2.1 Rate Limit Handling (`Retry-After`)
**The Problem:** Currently, if Slack throws a `429 Too Many Requests` error during the 10:45 AM Warning blasts, the system hard-sleeps for 10 seconds. This is inefficient and can still cause failures if Slack demands a 30-second backoff.
**The Optimization:** We will parse the exact `Retry-After` HTTP header from Slack.

*In-Depth Example:*
```python
# Custom Exception
class SlackRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds

# Inside _slack_request (using urllib)
try:
    response = urlopen(req)
except HTTPError as e:
    if e.code == 429:
        # Read the exact backoff time requested by Slack, default to 10 if missing
        retry_after = int(e.headers.get("Retry-After", 10))
        raise SlackRateLimitError(retry_after)
```
The Scheduler will catch `SlackRateLimitError`, sleep for exactly `retry_after` seconds, and safely resume.

### 2.2 Message Chunking (The 50-Block Limit)
**The Problem:** Slack has a strict hard limit of 50 UI blocks per message payload. Because our new Admin Report uses detailed Block Kit formatting for every project, a large company will easily exceed 50 blocks, crashing the API.
**The Optimization:** We will implement a chunking engine that sends blocks in batches of 40. Any overflow blocks will be sent as threaded replies to the main message.

*In-Depth Example:*
```python
def _send_chunked_blocks(channel_id: str, blocks: list):
    max_blocks = 40
    main_message_ts = None
    
    for i in range(0, len(blocks), max_blocks):
        chunk = blocks[i:i + max_blocks]
        payload = {"channel": channel_id, "blocks": chunk}
        
        # If this is chunk 2+, send it as a threaded reply to keep the channel clean
        if main_message_ts:
            payload["thread_ts"] = main_message_ts
            
        response = _slack_request("chat.postMessage", payload)
        
        # Save the timestamp of the first message to thread the rest
        if not main_message_ts:
            main_message_ts = response.get("ts")
```

---

## 3. The Stats Engine & Slack Format

To generate project-wise statistics, the Scheduler cannot rely on N+1 queries. We will build a unified Stats Engine.

### 3.1 The Optimized Query Engine (`scheduler_service.py`)
We will create a helper function `_generate_admin_report_payload(db)`.
1. **Fetch Roster:** Query the `allocations` table where `is_active = True`. (This represents today's expected roster).
2. **Fetch Check-ins:** Query `DailyCheckIn` for today.
3. **In-Memory Grouping:** Loop through the allocations, grouping employees by `sub_project_id`. Check if their ID exists in the Check-ins array. 
4. **Result:** An aggregated dictionary containing Overall Stats and Project-Wise stats.

### 3.2 Slack Demo Format
```text
📊 *Daily Check-in & Summary Report*

*1. Overall Check-in Summary*
• 👥 *Total Active Employees:* 150 (excludes employees on leave)
• ✅ *Total Checked-in:* 120
• ⏳ *Total Pending:* 30
• ⏱️ *Average Check-in Time:* WFO: 09:42 AM | WFH: 10:15 AM
• 🥇 *First Check-in:* WFO: Jane Doe at 07:15 AM | WFH: John Smith at 08:00 AM

*Check-in Time Distribution:*
  ◽ *Before 9:00 AM:* 45 employees
  ◽ *9:00 AM - 10:00 AM:* 50 employees
  ◽ *10:00 AM - 11:00 AM:* 20 employees
  ◽ *11:00 AM - 12:00 PM:* 3 employees
  🟥 *After 12:00 PM:* 2 employees

━━━━━━━━━━━━━━━━━━━━━━

*2. Project-Wise Summary* (Now renders natively as a Slack Table)

| Project Name | Check-ins | Pending | Distribution (<9 | 9-10 | 10-11 | 11-12 | >12) |
| :--- | :---: | :---: | :--- |
| Apollo Data Annotation | 35/40 | 5 | 15 \| 15 \| 4 \| 1 \| 0 |
| Project Verde | 10/10 | 0 | 5 \| 3 \| 2 \| 0 \| 0 |

━━━━━━━━━━━━━━━━━━━━━━

*3. Action Required (Pending/Late tables)*

*Checked In Late (After 11:00 AM) (2)*
| S.No | Employee Name | Email | Time |
| :--- | :--- | :--- | :---: |
| 1 | Alice Worker | alice@example.com | 11:45 AM |

*Pending (Not Checked In Yet) (5)*
| S.No | Employee Name | Email |
| :--- | :--- | :--- |
| 1 | Bob Missing | bob@example.com |
```

---

## 4. Scheduler Service Execution (`app/services/scheduler_service.py`)

We will manage two specific jobs for reporting:

### Job A & B: `_scheduled_admin_report` (11:10 AM) & `_scheduled_evening_report` (6:30 PM)
- Both jobs call the `_generate_admin_report_payload(db)` stats engine.
- They pass the resulting dictionary to `send_admin_late_list(stats_payload)`.
- The Slack service formats the blocks and safely chunks them to prevent 50-block limit crashes.

---

## 5. Advanced Optimizations (Enterprise Scalability)

To push the roster system to be highly optimized and reliable, the following advanced optimizations will be applied:

### 5.1 SQL-Level Aggregation
Instead of fetching all check-ins and allocations into Python's memory to group them manually, we will offload the math to the database. The Stats Engine will use an optimized SQLAlchemy `JOIN` query with `GROUP_BY` and `COUNT()` so that PostgreSQL performs the heavy lifting and returns only the aggregated summary.

### 5.2 Asynchronous Slack Blasts
Currently, iterating over hundreds of employees to send direct messages using synchronous `urllib.request` blocks the scheduler thread for minutes. We will upgrade the Slack messaging functions to use asynchronous calls (`asyncio` / `httpx` / `aiohttp`), significantly reducing the total time taken to send warning blasts while strictly adhering to Slack's concurrency limits and preventing the APScheduler queue from stalling.

### 5.3 Chunk Transmission Resiliency
In the event of a network glitch or a `502 Bad Gateway` from Slack during a multi-chunk report transmission, we will implement a retry mechanism (e.g., using `tenacity`). If a specific chunk (e.g., chunk 2 of 5) fails, it will automatically retry without failing the entire report, guaranteeing complete delivery.

---

## 6. Phase Execution Plan

1. **Phase 1: Slack Infrastructure:** (Completed)
   - ✅ Update `_slack_request` with exact `Retry-After` parsing.
   - ✅ Refactor `send_admin_late_list` to handle Block Kit rendering and Threaded Chunking.
2. **Phase 2: The Stats Engine & Scheduler** (Completed)
- ✅ Write the optimized SQL-level join query for project-wise statistics.
- ✅ Update the `11:10 AM` job and create the `6:30 PM` job to consume this engine.

**Phase 3: Advanced Optimizations** (Completed)
- ✅ Upgrade Slack DM functions to use asynchronous messaging (`httpx` / `aiohttp`) for better concurrency.
- ✅ Add `tenacity` retries to the Threaded Chunking logic to handle transient Slack errors gracefully.
