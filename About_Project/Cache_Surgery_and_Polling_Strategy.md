# Global State Optimization Strategy (Cache Surgery & ETag Polling)

### 1. API Response Check

I checked the pprove_leave and 
eject_leave endpoints in ackend/app/api/leaves.py.
The API **does not** return the full leave object. It returns a minimal dictionary like this:

`json
{
    "message": "Leave approved",
    "leave_id": 123,
    "status": "approved",
    "razorpay_applied": true
}
`

**Good News:** Even though it's not the full object, this is actually **all we need** for the cache surgery! We only need the leave_id to find the row in the frontend cache, and status/
azorpay_applied to update those specific fields. The rest of the row (dates, reasons, etc.) hasn't changed, so we can just merge these updates into the existing row.

---

### Our Action Plan

Since we want to fix the freezing page issue first, here is the recommended plan:

#### Phase 1: The Immediate Fix (Cache Surgery)

1. Edit LeavesPage.jsx and PMLeavesPage.jsx (or whichever component holds the mutations).
2. Remove queryClient.invalidateQueries(["leaves"]) from the onSuccess callback of the approve/reject mutations.
3. Replace it with queryClient.setQueryData which will take the { leave_id, status, razorpay_applied } from the backend response and instantly update that specific row in the UI.
**Result:** Clicking approve updates the row instantly. The other 9 rows are not touched. No 70-second page reload.

#### Phase 2: Handling changes by other Admins (ETag Polling)

Since we aren't using WebSockets, to ensure the page eventually catches updates made by *other* admins, we will add React Query's built-in background polling (
efetchInterval: 60000 - e.g., 1 minute). To ensure we don't blindly pull heavy data:

**1. The Fast "Version Check" on the Backend**
When the /api/leaves endpoint is hit, before it does any of the heavy, slow SQL joins, the backend will run a tiny, lightning-fast query. For example: SELECT MAX(updated_at) FROM leaves.
This will act as our "version number" (ETag) or "Last-Modified" timestamp for the current data state.

**2. The Initial Request (Status 200)**
- The frontend requests the leaves for the first time.
- The backend runs the fast check, then runs the heavy query, and sends the massive payload back to the browser.
- **Crucial step:** The backend attaches the ETag (or Last-Modified) header to the response.

**3. The Polling Request (Conditional Fetch)**
- One minute later, React Query automatically polls the backend.
- The browser automatically includes the header If-None-Match: <ETag> (or If-Modified-Since) using the value it saved from the first request.

**4. The Short-Circuit (Status 304)**
- The backend receives the poll request and reads the header.
- The backend runs only the lightning-fast MAX(updated_at) query.
- It compares the result to the ETag/timestamp the frontend sent.
- **If they match:** The backend immediately stops and sends back a 304 Not Modified response with an empty body. It takes milliseconds, costs almost zero CPU, and transfers zero data.
- **If they don't match (someone else approved a leave):** The backend knows the data is stale. It proceeds to run the heavy query and returns a 200 OK with the fresh data and the new ETag.

### Summary of What Needs to be Done

- **Frontend Task 1:** Implement Cache Surgery (setQueryData) in mutation callbacks.
- **Backend Task:** Update the /api/leaves endpoint to calculate an ETag/Last-Modified hash (using a fast MAX(updated_at) DB query) and return early with 304 Not Modified if the frontend's header matches it.
- **Frontend Task 2:** Add 
efetchInterval to the useQuery hooks in the frontend to trigger the conditional background polling.
