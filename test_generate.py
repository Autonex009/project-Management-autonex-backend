import asyncio
from sqlalchemy.orm import Session
from app.db.database import SessionLocal, Base, engine
from app.models.user import User  # IMPORT ALL MODELS
from app.models.employee import Employee
from app.models.employee_document import EmployeeDocument
from app.services.document_service import generate_document

db = SessionLocal()
try:
    generate_document(employee_id=1, doc_type="internship_offer_letter", db=db)
    print("Success")
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    db.close()
