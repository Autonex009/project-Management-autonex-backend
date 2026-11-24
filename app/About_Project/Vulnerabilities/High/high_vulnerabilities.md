# 🚨 High Vulnerabilities Index

## Active High Vulnerabilities

| Vulnerability ID | Title | Status | Target Modules |
| :--- | :--- | :--- | :--- |
| **[High Vulnerability 01](./high_vulnerabilities_01_payroll_employee_type_desync_and_duplicate_accounts.md)** | **Payroll Identity Desynchronization, Duplicate Account Generation on Name Edits, and Stale Employment Type Drift** | ✅ Solved & Verified | `app/api/employees.py`, `app/api/payroll.py`, `app/models/payroll.py` |

---

### Summary of High Vulnerability 01
- **Reported By:** Reefa Kachhi (PM Portal)
- **Key Issues:**
  1. **Employee Type Not Updating:** Intern conversion to Full-Time does not update the Payroll tab (`Salary.employment_type` stays "Intern").
  2. **Duplicate Payroll Accounts on Name Updates:** Editing an employee's name creates a duplicate ₹0 payroll account and leaves the old account as an orphan with the actual salary.
- **Architectural Root Cause:** Identity coupling via mutable natural key (`full_name`) instead of immutable surrogate foreign key (`employee_id`).
- **Comprehensive SDE-3 Solution:**
  - **Tier 1:** Immediate zero-downtime surgical fix in API layer (atomic name & type sync, single-source-of-truth display, self-healing deduplication).
  - **Tier 2:** Relational modernization (Alembic migration adding `employee_id` Foreign Key to `salary` table with unique constraint).

*For the complete forensic investigation, code proof, and step-by-step diffs, see [high_vulnerabilities_01_payroll_employee_type_desync_and_duplicate_accounts.md](./high_vulnerabilities_01_payroll_employee_type_desync_and_duplicate_accounts.md).*
