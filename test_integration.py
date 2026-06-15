"""
Comprehensive integration test script to stress-test the backend API,
validating all allocations and onboarding routes and logic.
"""
import subprocess
import time
import requests
import sys
import os

PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"

def run_tests():
    # 1. Login as Admin
    print("\n--- [Admin Auth] Logging in as admin... ---")
    admin_login_res = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": "admin@autonex.com",
        "password": "admin123"
    })
    assert admin_login_res.status_code == 200, f"Admin login failed: {admin_login_res.text}"
    admin_token = admin_login_res.json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    print("✓ Admin login successful!")

    # 2. Login as Employee (Anjali Gupta)
    print("\n--- [Employee Auth] Logging in as employee (Anjali)... ---")
    emp_login_res = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": "anjali.gupta@autonex.com",
        "password": "emp123"
    })
    assert emp_login_res.status_code == 200, f"Employee login failed: {emp_login_res.text}"
    emp_data = emp_login_res.json()
    emp_token = emp_data["token"]
    emp_user_id = emp_data["user"]["id"]
    emp_headers = {"Authorization": f"Bearer {emp_token}"}
    print(f"✓ Employee login successful! User ID: {emp_user_id}")

    # ==========================================
    # ALLOCATIONS ENDPOINTS TESTS
    # ==========================================
    print("\n==========================================")
    print("RUNNING ALLOCATION TESTS...")
    print("==========================================")

    # A1. Get all employees status
    print("\n--- [Allocations] Getting employee status... ---")
    status_res = requests.get(f"{BASE_URL}/api/allocations/employee-status", headers=admin_headers)
    assert status_res.status_code == 200
    print("✓ Employee allocation status retrieved!")

    # A2. Get existing allocations
    print("\n--- [Allocations] Getting allocations... ---")
    allocs_res = requests.get(f"{BASE_URL}/api/allocations", headers=admin_headers)
    assert allocs_res.status_code == 200
    initial_allocations = allocs_res.json()
    print(f"✓ Found {len(initial_allocations)} allocations initially.")

    # A3. Validate a valid allocation
    print("\n--- [Allocations] Validating a valid allocation... ---")
    val_payload = {
        "employee_id": 3, # Rahul Verma
        "sub_project_id": 2, # Yutori - Batch 43
        "total_daily_hours": 4,
        "active_start_date": "2026-06-01",
        "active_end_date": "2026-06-30",
        "time_distribution": {"Annotation": 4}
    }
    val_res = requests.post(f"{BASE_URL}/api/allocations/validate", json=val_payload, headers=admin_headers)
    assert val_res.status_code == 200
    val_data = val_res.json()
    print(f"✓ Validation response: is_valid={val_data['is_valid']}, warnings={val_data['warnings']}")
    assert val_data["is_valid"] is True

    # A4. Validate an invalid allocation (Sum-zero check mismatch)
    print("\n--- [Allocations] Validating invalid allocation (Time distribution mismatch)... ---")
    bad_val_payload = val_payload.copy()
    bad_val_payload["time_distribution"] = {"Annotation": 5} # 5 hrs but total_daily_hours is 4
    bad_val_res = requests.post(f"{BASE_URL}/api/allocations/validate", json=bad_val_payload, headers=admin_headers)
    assert bad_val_res.status_code == 200
    bad_val_data = bad_val_res.json()
    print(f"✓ Bad validation response: is_valid={bad_val_data['is_valid']}, errors={bad_val_data['errors']}")
    assert bad_val_data["is_valid"] is False

    # A5. Validate leave overlap warning
    print("\n--- [Allocations] Validating allocation overlapping with leave... ---")
    # Ravi Tiwari (ID 9, linked to Employee ID 9) has leave from today + 20 days. Let's find his User record
    # Actually employee Vikram Singh (employee_id=5, User ID is 6 is Anjali, wait let's get employees list to find someone on leave)
    # Let's just create an allocation for employee_id=16 (Divya Menon, who is on-leave) or employee_id=3 (Rahul, has casual leave at today+14)
    # Let's allocate Rahul Verma (employee_id=3) on today + 14 days
    today = date_after(0)
    leave_day = date_after(14)
    leave_val_payload = {
        "employee_id": 3, # Employee ID 3 is Rahul Verma
        "sub_project_id": 2,
        "total_daily_hours": 4,
        "active_start_date": leave_day,
        "active_end_date": leave_day,
        "time_distribution": {"Annotation": 4}
    }
    leave_val_res = requests.post(f"{BASE_URL}/api/allocations/validate", json=leave_val_payload, headers=admin_headers)
    assert leave_val_res.status_code == 200
    leave_val_data = leave_val_res.json()
    print(f"✓ Leave overlap response: is_valid={leave_val_data['is_valid']}, warnings={leave_val_data['warnings']}")
    # It should have warning about leave conflict
    assert any("leave" in w.lower() or "conflict" in w.lower() for w in leave_val_data["warnings"]), "Expected leave conflict warning"

    # A6. Create allocation (Without double-booking)
    print("\n--- [Allocations] Creating new allocation... ---")
    create_payload = {
        "employee_id": 5, # Vikram Singh
        "sub_project_id": 2, # Yutori - Batch 43
        "total_daily_hours": 2,
        "active_start_date": "2026-07-01",
        "active_end_date": "2026-07-10",
        "role_tags": ["Annotation"],
        "time_distribution": {"Annotation": 2}
    }
    create_res = requests.post(f"{BASE_URL}/api/allocations", json=create_payload, headers=admin_headers)
    assert create_res.status_code == 200, f"Failed to create allocation: {create_res.text}"
    new_alloc = create_res.json()
    new_alloc_id = new_alloc["id"]
    print(f"✓ Created allocation with ID: {new_alloc_id}")

    # A7. Double booking block & override behavior
    print("\n--- [Allocations] Attempting double booking to trigger 409 conflict... ---")
    # Rahul has 8 hrs capacity per day. Let's try to allocate 8 additional hours on same dates
    db_payload = {
        "employee_id": 3,
        "sub_project_id": 3,
        "total_daily_hours": 8,
        "active_start_date": "2026-07-01",
        "active_end_date": "2026-07-10",
        "role_tags": ["Annotation"],
        "time_distribution": {"Annotation": 8}
    }
    db_res = requests.post(f"{BASE_URL}/api/allocations", json=db_payload, headers=admin_headers)
    print(f"✓ Double booking response status: {db_res.status_code}")
    assert db_res.status_code == 409, f"Expected 409, got {db_res.status_code}: {db_res.text}"
    assert db_res.json()["detail"]["requires_override"] is True

    # Now create with override_flag = True
    print("\n--- [Allocations] Creating with override flag... ---")
    override_payload = db_payload.copy()
    override_payload["override_flag"] = True
    override_payload["override_reason"] = "Needed for urgent project support"
    override_res = requests.post(f"{BASE_URL}/api/allocations", json=override_payload, headers=admin_headers)
    assert override_res.status_code == 200, f"Failed to create allocation with override: {override_res.text}"
    overbooked_alloc = override_res.json()
    overbooked_alloc_id = overbooked_alloc["id"]
    print(f"✓ Override successful! Created allocation ID: {overbooked_alloc_id}")

    # A8. Update allocation
    print("\n--- [Allocations] Updating allocation... ---")
    update_res = requests.put(f"{BASE_URL}/api/allocations/{new_alloc_id}", json={
        "total_daily_hours": 3,
        "time_distribution": {"Annotation": 3}
    }, headers=admin_headers)
    assert update_res.status_code == 200, f"Update failed: {update_res.text}"
    print("✓ Updated hours to 3 successfully!")

    # A9. Delete allocations
    print("\n--- [Allocations] Deleting allocations... ---")
    del1 = requests.delete(f"{BASE_URL}/api/allocations/{new_alloc_id}", headers=admin_headers)
    assert del1.status_code == 200
    del2 = requests.delete(f"{BASE_URL}/api/allocations/{overbooked_alloc_id}", headers=admin_headers)
    assert del2.status_code == 200
    print("✓ Allocations cleaned up successfully!")


    # ==========================================
    # ONBOARDING ENDPOINTS TESTS
    # ==========================================
    print("\n==========================================")
    print("RUNNING ONBOARDING TESTS...")
    print("==========================================")

    # O1. Retrieve modules list
    print("\n--- [Onboarding] Getting modules... ---")
    modules_res = requests.get(f"{BASE_URL}/api/onboarding/modules", headers=emp_headers)
    assert modules_res.status_code == 200
    modules = modules_res.json()
    print(f"✓ Found {len(modules)} published modules.")
    
    # We should have 11 modules from the seed
    assert len(modules) == 11, f"Expected 11 modules, found {len(modules)}"
    m1_id = modules[0]["id"]
    m2_id = modules[1]["id"]
    print(f"Module 1 ID: {m1_id}, Title: {modules[0]['title']}")
    print(f"Module 2 ID: {m2_id}, Title: {modules[1]['title']}")

    # O2. Check candidate dashboard lock sequence
    print("\n--- [Onboarding] Checking candidate dashboard sequence locks... ---")
    dash_res = requests.get(f"{BASE_URL}/api/onboarding/candidates/{emp_user_id}/dashboard", headers=emp_headers)
    assert dash_res.status_code == 200
    dash_data = dash_res.json()
    
    m1_dash = next(m for m in dash_data["modules"] if m["id"] == m1_id)
    m2_dash = next(m for m in dash_data["modules"] if m["id"] == m2_id)
    print(f"Module 1 '{m1_dash['title']}' - Locked: {m1_dash['locked']}")
    print(f"Module 2 '{m2_dash['title']}' - Locked: {m2_dash['locked']}")
    
    assert m1_dash["locked"] is False, "Module 1 should be unlocked"
    assert m2_dash["locked"] is True, "Module 2 should be locked initially"

    # O3. Access locked Module 2 details -> should return 403 Forbidden
    print("\n--- [Onboarding] Getting details of locked Module 2 (Expect 403)... ---")
    m2_detail_res = requests.get(f"{BASE_URL}/api/onboarding/modules/{m2_id}", headers=emp_headers)
    print(f"✓ Status code: {m2_detail_res.status_code}")
    assert m2_detail_res.status_code == 403, f"Expected 403, got {m2_detail_res.status_code}"

    # O4. Get details of unlocked Module 1 -> should succeed (200)
    print("\n--- [Onboarding] Getting details of unlocked Module 1... ---")
    m1_detail_res = requests.get(f"{BASE_URL}/api/onboarding/modules/{m1_id}", headers=emp_headers)
    assert m1_detail_res.status_code == 200
    m1_detail = m1_detail_res.json()
    s1_id = m1_detail["sections"][0]["id"]
    print(f"✓ Module 1 details retrieved. Section 1.1 ID: {s1_id}")

    # O5. Submit progress on locked Module 2 section -> should return 403 Forbidden
    # Let's get sections of Module 2 via admin token since candidate is blocked from get_module
    m2_admin_res = requests.get(f"{BASE_URL}/api/onboarding/modules/{m2_id}", headers=admin_headers)
    m2_admin_detail = m2_admin_res.json()
    s2_id = m2_admin_detail["sections"][0]["id"]
    
    print("\n--- [Onboarding] Recording progress on locked Module 2 section (Expect 403)... ---")
    rec_locked_res = requests.post(f"{BASE_URL}/api/onboarding/progress/section", json={
        "user_id": emp_user_id,
        "module_id": m2_id,
        "section_id": s2_id
    }, headers=emp_headers)
    print(f"✓ Status code: {rec_locked_res.status_code}")
    assert rec_locked_res.status_code == 403

    # O6. Record progress on unlocked Module 1 section (has 0 quiz questions, so we can do it directly)
    print("\n--- [Onboarding] Recording progress on unlocked Module 1 section... ---")
    rec_ok_res = requests.post(f"{BASE_URL}/api/onboarding/progress/section", json={
        "user_id": emp_user_id,
        "module_id": m1_id,
        "section_id": s1_id
    }, headers=emp_headers)
    assert rec_ok_res.status_code == 200, f"Progress recording failed: {rec_ok_res.text}"
    print("✓ Module 1 section marked complete!")

    # O7. Check candidate dashboard again -> Module 2 should be unlocked now!
    print("\n--- [Onboarding] Checking candidate dashboard again for unlock validation... ---")
    dash_res2 = requests.get(f"{BASE_URL}/api/onboarding/candidates/{emp_user_id}/dashboard", headers=emp_headers)
    dash_data2 = dash_res2.json()
    m2_dash2 = next(m for m in dash_data2["modules"] if m["id"] == m2_id)
    print(f"Module 2 '{m2_dash2['title']}' - Locked: {m2_dash2['locked']}")
    assert m2_dash2["locked"] is False, "Module 2 should now be unlocked!"

    # O8. Candidate gets Module 2 details -> should now succeed (200)
    print("\n--- [Onboarding] Getting details of now unlocked Module 2... ---")
    m2_detail_res2 = requests.get(f"{BASE_URL}/api/onboarding/modules/{m2_id}", headers=emp_headers)
    assert m2_detail_res2.status_code == 200
    m2_detail2 = m2_detail_res2.json()
    s2_id = m2_detail2["sections"][0]["id"]
    s2_questions = m2_detail2["sections"][0]["questions"]
    print(f"✓ Module 2 details retrieved. Section 2.1 ID: {s2_id}, Questions count: {len(s2_questions)}")

    # O9. Submit quiz with wrong answers (fail threshold < 50%)
    print("\n--- [Onboarding] Submitting quiz for Section 2.1 with incorrect answers (fail)... ---")
    # Option indices are 0-based.
    # Q1 correct index is 0 (Their project manager or team lead) - we submit 1
    # Q2 correct index is 2 (The Annotate section on the left sidebar) - we submit 0
    fail_answers = [
        {"question_id": s2_questions[0]["id"], "chosen_index": 1},
        {"question_id": s2_questions[1]["id"], "chosen_index": 0}
    ]
    quiz_fail_res = requests.post(f"{BASE_URL}/api/onboarding/quiz/submit", json={
        "user_id": emp_user_id,
        "section_id": s2_id,
        "answers": fail_answers
    }, headers=emp_headers)
    assert quiz_fail_res.status_code == 200
    fail_data = quiz_fail_res.json()
    print(f"✓ Score: {fail_data['score']}% (Expected 0%)")
    assert fail_data["score"] == 0

    # O10. Attempt to record section progress with failed quiz score -> should raise 400 Bad Request
    print("\n--- [Onboarding] Marking section complete with failed quiz (Expect 400)... ---")
    rec_fail_res = requests.post(f"{BASE_URL}/api/onboarding/progress/section", json={
        "user_id": emp_user_id,
        "module_id": m2_id,
        "section_id": s2_id
    }, headers=emp_headers)
    print(f"✓ Status code: {rec_fail_res.status_code}")
    assert rec_fail_res.status_code == 400, f"Expected 400, got {rec_fail_res.status_code}"

    # O11. Submit quiz with correct answers (pass threshold >= 50%)
    print("\n--- [Onboarding] Submitting quiz with correct answers (pass)... ---")
    # Q1 correct index: 0, Q2 correct index: 2
    pass_answers = [
        {"question_id": s2_questions[0]["id"], "chosen_index": 0},
        {"question_id": s2_questions[1]["id"], "chosen_index": 2}
    ]
    quiz_pass_res = requests.post(f"{BASE_URL}/api/onboarding/quiz/submit", json={
        "user_id": emp_user_id,
        "section_id": s2_id,
        "answers": pass_answers
    }, headers=emp_headers)
    assert quiz_pass_res.status_code == 200
    pass_data = quiz_pass_res.json()
    print(f"✓ Score: {pass_data['score']}% (Expected 100%)")
    assert pass_data["score"] == 100

    # O12. Record progress now -> should succeed (200)
    print("\n--- [Onboarding] Marking section complete after passing quiz... ---")
    rec_pass_res = requests.post(f"{BASE_URL}/api/onboarding/progress/section", json={
        "user_id": emp_user_id,
        "module_id": m2_id,
        "section_id": s2_id
    }, headers=emp_headers)
    assert rec_pass_res.status_code == 200, f"Progress recording failed: {rec_pass_res.text}"
    print("✓ Module 2 section completed!")

    # O13. Check dashboard again -> Module 3 should be unlocked!
    print("\n--- [Onboarding] Checking candidate dashboard again for Module 3 unlock... ---")
    dash_res3 = requests.get(f"{BASE_URL}/api/onboarding/candidates/{emp_user_id}/dashboard", headers=emp_headers)
    dash_data3 = dash_res3.json()
    m3_dash = next(m for m in dash_data3["modules"] if m["title"].startswith("Task Finding"))
    print(f"Module 3 '{m3_dash['title']}' - Locked: {m3_dash['locked']}")
    assert m3_dash["locked"] is False, "Module 3 should now be unlocked!"


    # ==========================================
    # ONBOARDING ADMIN/PM DASHBOARD TESTS
    # ==========================================
    print("\n==========================================")
    print("RUNNING ONBOARDING ADMIN & PM ENDPOINTS...")
    print("==========================================")

    # AD1. Analytics dashboard
    print("\n--- [Admin Onboarding] Getting analytics dashboard... ---")
    an_dash_res = requests.get(f"{BASE_URL}/api/onboarding/analytics/dashboard", headers=admin_headers)
    assert an_dash_res.status_code == 200
    an_dash = an_dash_res.json()
    print(f"✓ Metrics: {an_dash['metrics']}")

    # AD2. Full analytics details
    print("\n--- [Admin Onboarding] Getting full analytics details... ---")
    an_full_res = requests.get(f"{BASE_URL}/api/onboarding/analytics/full", headers=admin_headers)
    assert an_full_res.status_code == 200
    an_full = an_full_res.json()
    print(f"✓ KPIs: {an_full['kpis']}")

    # AD3. Onboarding Reports list
    print("\n--- [Admin Onboarding] Getting onboarding reports... ---")
    rep_res = requests.get(f"{BASE_URL}/api/onboarding/reports", headers=admin_headers)
    assert rep_res.status_code == 200
    reports_data = rep_res.json()
    print(f"✓ Reports data for {len(reports_data)} candidates retrieved.")

    # AD4. Export CSV reports
    print("\n--- [Admin Onboarding] Exporting CSV reports... ---")
    export_res = requests.get(f"{BASE_URL}/api/onboarding/reports/export", headers=admin_headers)
    assert export_res.status_code == 200
    assert "text/csv" in export_res.headers.get("content-type", "")
    csv_content = export_res.text
    print(f"✓ Exported CSV successfully! Size: {len(csv_content)} bytes")
    # Verify CSV has headers
    assert "Candidate Name" in csv_content

    # AD5. Download sample template Excel files
    print("\n--- [Admin Onboarding] Downloading quiz sample Excel template... ---")
    ex_res = requests.get(f"{BASE_URL}/api/onboarding/modules/quiz-sample-excel", headers=admin_headers)
    # Check if openpyxl is installed and returned 200 or 501
    print(f"✓ Quiz sample download response status: {ex_res.status_code}")
    assert ex_res.status_code in (200, 501)

    print("\n--- [Admin Onboarding] Downloading team contact sample Excel template... ---")
    ex_team_res = requests.get(f"{BASE_URL}/api/onboarding/team/sample-excel", headers=admin_headers)
    print(f"✓ Team contact sample download response status: {ex_team_res.status_code}")
    assert ex_team_res.status_code in (200, 501)

    # AD6. Team contacts CRUD
    print("\n--- [Admin Onboarding] Seeding team contacts list... ---")
    contacts_res = requests.get(f"{BASE_URL}/api/onboarding/team", headers=emp_headers)
    assert contacts_res.status_code == 200
    contacts = contacts_res.json()
    print(f"✓ Retrieved {len(contacts)} team contacts.")
    
    print("\n--- [Admin Onboarding] Creating new team contact... ---")
    new_contact_payload = {
        "name": "Jane Doe",
        "role": "Lead Reviewer",
        "department": "Review Team",
        "email": "jane.doe@company.com",
        "linkedin": "https://linkedin.com/in/jane-doe",
        "slack": "U1234567"
    }
    create_contact_res = requests.post(f"{BASE_URL}/api/onboarding/team", json=new_contact_payload, headers=admin_headers)
    assert create_contact_res.status_code == 201
    created_contact = create_contact_res.json()
    contact_id = created_contact["id"]
    print(f"✓ Team contact created! ID: {contact_id}")

    update_contact_res = requests.put(f"{BASE_URL}/api/onboarding/team/{contact_id}", json={
        "name": "Jane Doe",
        "role": "Principal Quality Assurance Reviewer",
        "department": "Review Team",
        "email": "jane.doe@company.com",
        "linkedin": "https://linkedin.com/in/jane-doe",
        "slack": "U1234567"
    }, headers=admin_headers)
    assert update_contact_res.status_code == 200
    print("✓ Team contact updated successfully!")

    print("\n--- [Admin Onboarding] Deleting team contact... ---")
    del_contact_res = requests.delete(f"{BASE_URL}/api/onboarding/team/{contact_id}", headers=admin_headers)
    assert del_contact_res.status_code == 204
    print("✓ Team contact deleted successfully!")

    # PM1. PM mentees lists
    print("\n--- [PM Onboarding] Retrieving PM mentees list... ---")
    # PM User Priya Sharma has user_id = 3 (Wait, from seed Arjun Mehta is ID 2, Priya Sharma is ID 3, Test Manager is ID 4)
    # Let's hit the endpoint for mentor_id = 3
    mentees_res = requests.get(f"{BASE_URL}/api/onboarding/mentors/3/mentees", headers=admin_headers)
    assert mentees_res.status_code == 200
    mentees = mentees_res.json()
    print(f"✓ Retrieved {len(mentees)} mentees for PM Arjun/Priya.")

    print("\n" + "=" * 50)
    print("🎉 ALL STRESS & INTEGRATION ENDPOINT TESTS PASSED SUCCESSFULLY!")
    print("=" * 50)


def date_after(days):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


if __name__ == "__main__":
    # Start the backend server as a subprocess
    backend_dir = r"c:\TANMAY\AutonexAI\git-live\development\autonex009\project-Management-autonex-backend"
    python_exe = os.path.join(backend_dir, "venv", "Scripts", "python.exe")
    
    print("Running seed_local_db.py to reset database state...")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run([python_exe, "seed_local_db.py"], cwd=backend_dir, env=env, check=True)
    
    print("Starting backend local server...")
    server_process = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=backend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for server to boot
    print("Waiting 3 seconds for server to start...")
    time.sleep(3)
    
    try:
        run_tests()
    except AssertionError as exc:
        print(f"\n❌ TEST FAILURE: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\n❌ UNEXPECTED ERROR: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("\nStopping backend local server...")
        server_process.terminate()
        server_process.wait()
        print("Server stopped.")
