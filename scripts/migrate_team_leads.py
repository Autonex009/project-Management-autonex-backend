"""One-off: make the stored data agree with how the app already reads it.

Introducing the Team Lead role left three surfaces disagreeing about the same project:

* the **card** resolved managers and leads by designation, so it was right;
* the **edit modal** read ``assigned_employee_ids`` raw, so converted managers appeared in
  the Program Manager box and Team Lead was empty;
* the **Allocations page** showed neither, because managers and leads hold no allocation
  row unless someone made one.

The reading code has since been fixed, so nothing here changes behaviour — it makes the
stored rows honest, which matters for raw SQL and anything outside this app.

Two changes per project:

1. **Move converted managers.** Anyone in ``assigned_employee_ids`` whose designation is now
   "Team Lead" is removed from that column. The column goes back to meaning exactly what its
   readers assume: this project's managers.
2. **Allocate managers and leads.** Each gets an allocation row if they lack one, so they
   appear on the Allocations page and in the manpower strip. Leads are tagged "Team Lead";
   managers are not.

Deliberately NOT done:

* No project is left without a manager *by this script* — if every occupant of the slot was
  converted, they are still moved out (the app already reads it that way), but the project
  is listed loudly at the end so you can assign a real PM.
* Nothing is deleted. Existing allocations are left alone, including their hours.

Usage::

    .venv/Scripts/python.exe scripts/migrate_team_leads.py            # dry run, writes nothing
    .venv/Scripts/python.exe scripts/migrate_team_leads.py --apply    # commit the changes

Take a database backup before ``--apply``.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.main  # noqa: F401  — registers every model in the mapper registry
from app.db.database import SessionLocal
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.project import DailySheet
from app.services import project_scope

TEAM_LEAD_TAG = "Team Lead"
# Matches how the app decides a project's manager is really a lead now.
LEAD_DESIGNATIONS = project_scope.TEAM_LEAD_TAGS


def _normalise(value):
    return (value or "").strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this the script only reports what it would do.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        employees = {e.id: e for e in db.query(Employee).all()}
        projects = db.query(DailySheet).all()
        allocations = db.query(Allocation).all()

        # (employee_id, project_id) -> allocation, so we never add a second row for someone
        # who is already on the project.
        existing = {(a.employee_id, a.sub_project_id): a for a in allocations}

        moved_rows = 0
        new_allocations = 0
        tagged_existing = 0
        projects_touched = 0
        left_without_manager = []

        for project in projects:
            slot = project_scope._as_int_set(project.assigned_employee_ids)
            leads_from_slot = {
                i
                for i in slot
                if _normalise(getattr(employees.get(i), "designation", None))
                in LEAD_DESIGNATIONS
            }
            managers = slot - leads_from_slot

            # Leads already recorded the proper way, via a tagged allocation.
            leads_from_allocations = {
                a.employee_id
                for a in allocations
                if a.sub_project_id == project.id
                and any(_normalise(t) in LEAD_DESIGNATIONS for t in (a.role_tags or []))
            }
            all_leads = leads_from_slot | leads_from_allocations

            # Collected, then printed under one heading. Printing as we go put a
            # project's allocate lines under the *previous* project's heading whenever it
            # had no leads to move — which read as the same person being allocated twice.
            lines: list[str] = []
            no_manager = False

            if leads_from_slot:
                moved_rows += len(leads_from_slot)
                names = [
                    f"#{i} {getattr(employees.get(i), 'name', '(off roster)')}"
                    for i in sorted(leads_from_slot)
                ]
                lines.append(f"    move out of manager slot : {', '.join(names)}")
                if not managers:
                    no_manager = True
                    left_without_manager.append((project.id, project.name))
                    lines.append("    !! no manager left on this project")
                if args.apply:
                    project.assigned_employee_ids = sorted(managers)

            # Everyone who runs this project should be visible on the Allocations page.
            for employee_id in sorted(managers | all_leads):
                if employee_id not in employees:
                    continue  # off the roster; a stale id, not someone to allocate
                is_lead = employee_id in all_leads
                current = existing.get((employee_id, project.id))
                if current is None:
                    new_allocations += 1
                    lines.append(
                        f"    allocate {'lead   ' if is_lead else 'manager'} : "
                        f"#{employee_id} {employees[employee_id].name}"
                    )
                    if args.apply:
                        db.add(
                            Allocation(
                                employee_id=employee_id,
                                sub_project_id=project.id,
                                total_daily_hours=8,
                                role_tags=[TEAM_LEAD_TAG] if is_lead else [],
                                time_distribution={},
                                active_start_date=project.start_date,
                                active_end_date=project.end_date,
                                # The capacity guard is about booking someone's working day;
                                # recording who runs a project is not that, and several of
                                # these people already run others.
                                override_flag=True,
                                override_reason="Backfilled by migrate_team_leads",
                            )
                        )
                elif is_lead and not any(
                    _normalise(t) in LEAD_DESIGNATIONS for t in (current.role_tags or [])
                ):
                    # Already allocated, but the row does not say they lead it.
                    tagged_existing += 1
                    lines.append(
                        f"    tag as lead        : #{employee_id} {employees[employee_id].name}"
                    )
                    if args.apply:
                        current.role_tags = [*(current.role_tags or []), TEAM_LEAD_TAG]

            if lines:
                print(f"[{project.id}] {project.name[:44]}")
                for line in lines:
                    print(line)

            if lines:
                projects_touched += 1

        print()
        print("-" * 60)
        print(f"projects affected            : {projects_touched}")
        print(f"leads moved out of PM slot   : {moved_rows}")
        print(f"allocations to create        : {new_allocations}")
        print(f"existing rows to tag as lead : {tagged_existing}")
        if left_without_manager:
            print()
            print(f"!! {len(left_without_manager)} project(s) will have NO manager - assign one:")
            for pid, name in left_without_manager:
                print(f"     [{pid}] {name}")

        if args.apply:
            db.commit()
            print()
            print("COMMITTED.")
        else:
            print()
            print("Dry run - nothing written. Re-run with --apply to commit.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())