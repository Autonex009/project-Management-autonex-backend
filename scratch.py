from app.db.database import SessionLocal
from app.models.employee import Employee
from app.models.parent_project import MainProject
db = SessionLocal()

tls = db.query(Employee).filter(Employee.designation == 'Team Lead').limit(5).all()
for tl in tls:
    print(f"TL: {tl.name} (ID: {tl.id})")
    projs = db.query(MainProject).filter(MainProject.program_manager_ids.op('@>')([tl.id])).all()
    for p in projs:
        print(f"  Project: {p.name} (IDs: {p.program_manager_ids})")
