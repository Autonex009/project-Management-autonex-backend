"""HR Document Generation Service — Phase 2.

Pipeline (primary): docxtpl fills {{ placeholders }} in a .docx template,
then LibreOffice (headless) converts the filled .docx to a PDF.

Fallback: if LibreOffice is not installed, the old Jinja2 HTML → xhtml2pdf
path is attempted, and finally a plain ReportLab text render.
"""
import io
import os
import subprocess
import tempfile
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.employee_document import EmployeeDocument
from app.models.company_settings import CompanySetting
from app.services.salary_crypto import decrypt_salary
from app.services.document_storage_service import (
    upload_document,
    is_supabase_configured,
)

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "documents"
DOCX_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "docx"

# Default company metadata used when company_settings rows are missing
COMPANY_NAME_DEFAULT = "Autonex"
HR_SIGNATORY_DEFAULT = "HR Department"


# ── Jinja2 env ─────────────────────────────────────────────────────────────────
_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=True,
)


def _fmt_date(d) -> str:
    if d is None:
        return "—"
    if isinstance(d, (date, datetime)):
        return d.strftime("%d %B %Y")
    return str(d)


def _parse_money(value) -> float:
    """Parse a money value that may be a formatted string (e.g. '7,00,000' or '7,00,000/-')."""
    if value is None:
        return 0.0
    # Strip formatting characters: commas, rupee sign, trailing /-
    cleaned = str(value).replace(",", "").replace("/-", "").replace("₹", "").strip()
    return float(cleaned)


def _fmt_currency(value) -> str:
    """Format a number using Indian lakh notation (e.g. 1,20,000)."""
    if value is None:
        return "—"
    try:
        n = int(_parse_money(value))
        # Indian formatting: last 3 digits, then groups of 2
        s = str(n)
        if len(s) <= 3:
            return f"{s}/-"
        result = s[-3:]
        s = s[:-3]
        while len(s) > 2:
            result = s[-2:] + "," + result
            s = s[:-2]
        if s:
            result = s + "," + result
        return f"{result}/-"
    except (ValueError, TypeError):
        return str(value)


def _fmt_currency_words(value) -> str:
    """Convert a number into Indian Rupee words (e.g. 100000 -> One Lakh)."""
    if not value:
        return ""
    try:
        from num2words import num2words
        n = int(float(value))
        # Use lang='en_IN' to get Indian numbering system words (Lakhs, Crores, etc)
        words = num2words(n, lang='en_IN').title()
        return words.replace(" And ", " and ")
    except (ValueError, TypeError, ImportError):
        return ""


# ── Company context ─────────────────────────────────────────────────────────────
def _company_context(db: Session) -> dict:
    """Build the shared letterhead context from company_settings rows."""
    settings = {s.key: s.value for s in db.query(CompanySetting).all()}
    raw_address = settings.get("office_address", "")
    lines = [ln.strip() for ln in raw_address.splitlines() if ln.strip()]
    addr1 = lines[0] if lines else ""
    addr2 = ", ".join(lines[1:]) if len(lines) > 1 else ""

    return {
        "company_name": settings.get("company_name", COMPANY_NAME_DEFAULT),
        "hr_signatory": settings.get("hr_signatory", HR_SIGNATORY_DEFAULT),
        "company_address_line1": addr1,
        "company_address_line2": addr2,
        "office_address_line1": addr1,
        "office_address_line2": addr2,
        "issue_date": _fmt_date(date.today()),
    }


# ── Employee context ────────────────────────────────────────────────────────────
def _employee_context(emp: Employee, db: Session) -> dict:
    salary = None
    if emp.base_salary_enc:
        try:
            salary = decrypt_salary(emp.base_salary_enc)
        except Exception:
            salary = None
    elif emp.base_salary:
        salary = emp.base_salary

    monthly_gross = salary or 0
    annual_ctc = monthly_gross * 12
    # Simple in-hand estimate: 80% of gross (rough TDS/PF deduction)
    monthly_inhand = int(monthly_gross * 0.80)
    annual_inhand = monthly_inhand * 12

    return {
        "employee_name": emp.name,
        "employee_email": emp.email,
        "designation": emp.designation or "Intern",
        "employee_type": emp.employee_type or "Intern",
        "start_date": _fmt_date(emp.created_at),
        "end_date": _fmt_date(emp.internship_end_date),
        "effective_date": _fmt_date(date.today()),
        "stipend": _fmt_currency(monthly_gross) if monthly_gross else "As discussed",
        "monthly_gross": _fmt_currency(monthly_gross) if monthly_gross else "—",
        "annual_ctc": _fmt_currency(annual_ctc) if annual_ctc else "—",
        "monthly_inhand": _fmt_currency(monthly_inhand) if monthly_inhand else "—",
        "annual_inhand": _fmt_currency(annual_inhand) if annual_inhand else "—",
    }


# ── DOCX template map ───────────────────────────────────────────────────────────
DOCX_TEMPLATE_MAP = {
    "internship_offer_letter": "internship_offer_letter.docx",
    "fulltime_offer_letter": "fulltime_offer_letter.docx",
    "internship_completion_certificate": "internship_completion_certificate.docx",
    "experience_letter": "experience_letter.docx",
    "salary_structure": "salary_structure.docx",
}


def _detect_libreoffice() -> Optional[str]:
    """Return the soffice executable path if LibreOffice is installed."""
    # Common Mac paths
    candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/local/bin/soffice",
        "/usr/bin/soffice",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    # Also check PATH
    import shutil
    found = shutil.which("soffice")
    return found


def _fill_docx_template(doc_type: str, context: dict) -> bytes:
    """Fill a .docx template with context values using docxtpl.
    Returns the filled .docx as bytes.
    """
    from docxtpl import DocxTemplate  # type: ignore

    template_file = DOCX_TEMPLATE_MAP.get(doc_type)
    if not template_file:
        raise ValueError(f"No .docx template for doc_type: {doc_type}")

    template_path = DOCX_TEMPLATES_DIR / template_file
    if not template_path.exists():
        raise ValueError(f".docx template not found: {template_path}")

    tpl = DocxTemplate(str(template_path))
    tpl.render(context)

    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def _convert_docx_to_pdf(docx_bytes: bytes, libreoffice_path: str) -> bytes:
    """Convert a .docx file (as bytes) to PDF using LibreOffice headless.
    Returns PDF bytes.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "document.docx")
        pdf_path = os.path.join(tmpdir, "document.pdf")

        # Write docx bytes to temp file
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        # Run LibreOffice in headless mode
        result = subprocess.run(
            [
                libreoffice_path,
                "--headless",
                "--convert-to", "pdf",
                docx_path,
                "--outdir", tmpdir,
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice conversion failed: {result.stderr.decode()}"
            )

        if not os.path.exists(pdf_path):
            raise RuntimeError("LibreOffice did not produce a PDF output file")

        with open(pdf_path, "rb") as f:
            return f.read()



TEMPLATE_MAP = {
    "internship_offer_letter": "internship_offer_letter.html",
    "fulltime_offer_letter": "fulltime_offer_letter.html",
    "internship_completion_certificate": "internship_completion_certificate.html",
    "experience_letter": "experience_letter.html",
    "salary_structure": "salary_structure.html",
}


def _render_html(doc_type: str, context: dict) -> str:
    template_file = TEMPLATE_MAP.get(doc_type)
    if not template_file:
        raise ValueError(f"No template for doc_type: {doc_type}")
    try:
        tmpl = _jinja_env.get_template(template_file)
    except TemplateNotFound:
        raise ValueError(f"Template file not found: {template_file}")

    # Build doc_ref string
    context.setdefault(
        "doc_ref",
        f"AUTONEX/{doc_type.upper()[:6]}/{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
    )
    return tmpl.render(**context)


# ── HTML → PDF ──────────────────────────────────────────────────────────────────
def _html_to_pdf(html: str) -> bytes:
    """Convert rendered HTML to PDF bytes.

    Tries xhtml2pdf first (best CSS support), falls back to a ReportLab
    plain-text render if unavailable.
    """
    try:
        from xhtml2pdf import pisa  # type: ignore

        buf = io.BytesIO()
        result = pisa.CreatePDF(html, dest=buf)
        if result.err:
            raise RuntimeError(f"xhtml2pdf conversion error: {result.err}")
        return buf.getvalue()
    except ImportError:
        pass

    # ── Fallback: ReportLab canvas plain-text render ─────────────────────
    # Strips tags and produces a minimal but valid PDF.
    import re
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable

    # Strip HTML tags for plain text
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=25 * mm,
        rightMargin=25 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph(text[:8000], styles["Normal"]),
    ]
    doc.build(story)
    return buf.getvalue()


# ── Main public function ────────────────────────────────────────────────────────
def generate_document(
    employee_id: int,
    doc_type: str,
    db: Session,
    uploaded_by: Optional[int] = None,
    dynamic_data: Optional[dict] = None,
) -> EmployeeDocument:
    """Generate a PDF document for an employee and persist the record.

    Returns the newly created EmployeeDocument row.
    Raises ValueError for invalid doc_type or missing employee.
    Raises RuntimeError for storage failures when Supabase is configured.

    dynamic_data: Optional dict of HR-entered values (e.g. stipend, ctc, performance_variable)
                  that override the defaults built from the employee's database record.
    """
    if doc_type == "org_policy":
        raise ValueError(
            "org_policy documents must be manually uploaded by HR — they cannot be auto-generated."
        )

    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise ValueError(f"Employee {employee_id} not found")

    # Build context from database
    ctx = {}
    ctx.update(_company_context(db))
    ctx.update(_employee_context(emp, db))

    # Override defaults with HR-entered dynamic values from the modal
    if dynamic_data:
        # Handle duration to end_date calculation
        if "duration_months" in dynamic_data:
            try:
                months = int(dynamic_data["duration_months"])
                d = emp.created_at.date() if isinstance(emp.created_at, datetime) else emp.created_at
                
                # Add months natively without external libraries
                target_month = d.month - 1 + months
                target_year = d.year + target_month // 12
                target_month = target_month % 12 + 1
                
                target_day = d.day
                while True:
                    try:
                        end_date = d.replace(year=target_year, month=target_month, day=target_day)
                        break
                    except ValueError:
                        target_day -= 1
                        
                dynamic_data["end_date"] = _fmt_date(end_date)
                dynamic_data["duration"] = f"{months} month{'s' if months != 1 else ''}"
            except (ValueError, TypeError, AttributeError):
                pass
                
        ctx.update({k: v for k, v in dynamic_data.items() if v is not None and v != ""})

        # Calculate CTC breakdown components if annual_ctc is present
        # Structure:
        #   Fixed Pay Annual = annual_ctc (the CTC IS the fixed pay)
        #   Performance Bonus & Retention Bonus are IN ADDITION to CTC
        #   Total Package = annual_ctc + performance_bonus + retention_bonus
        #
        #   Fixed Pay breakdown (% of annual_ctc):
        #     Basic Salary        = 50%
        #     HRA                 = 25%
        #     LTA                 = 10%
        #     Special Allowance   = 15% (balancer, ensures total = 100%)
        if "annual_ctc" in ctx and ctx["annual_ctc"] not in (None, "", "—", "As discussed"):
            try:
                # Use _parse_money to safely handle formatted strings like "7,00,000/-"
                annual_ctc_val = _parse_money(ctx["annual_ctc"])
                perf_var = _parse_money(ctx.get("performance_variable") or 0)
                ret_bonus = _parse_money(ctx.get("retention_bonus") or 0)

                # Bonuses are ON TOP of CTC — fixed pay equals CTC directly
                fixed_pay_annual = annual_ctc_val
                # Monthly = annual / 12, truncated (matches Indian payroll convention)
                fixed_pay_monthly = int(fixed_pay_annual / 12)

                ctx["fixed_pay_annual"] = fixed_pay_annual
                ctx["fixed_pay_monthly"] = fixed_pay_monthly

                # Total package = CTC + bonuses (shown separately in the document)
                total_package = annual_ctc_val + perf_var + ret_bonus
                ctx["total_package"] = total_package

                # Compute annual components first, then derive monthly by truncating
                basic_annual = 0.50 * fixed_pay_annual
                ctx["basic_salary_annual"] = basic_annual
                ctx["basic_salary_monthly"] = int(basic_annual / 12)

                hra_annual = 0.25 * fixed_pay_annual
                ctx["hra_annual"] = hra_annual
                ctx["hra_monthly"] = int(hra_annual / 12)

                lta_annual = 0.10 * fixed_pay_annual
                ctx["lta_annual"] = lta_annual
                ctx["lta_monthly"] = int(lta_annual / 12)

                special_annual = 0.15 * fixed_pay_annual
                ctx["special_allowance_annual"] = special_annual
                ctx["special_allowance_monthly"] = int(special_annual / 12)

            except (ValueError, TypeError):
                pass

        # Apply Indian currency formatting to monetary fields entered by HR
        _MONEY_KEYS = {
            "stipend", "annual_ctc", "performance_variable", "retention_bonus",
            "monthly_gross", "monthly_inhand", "annual_inhand",
            "fixed_pay_annual", "fixed_pay_monthly",
            "basic_salary_annual", "basic_salary_monthly",
            "hra_annual", "hra_monthly",
            "lta_annual", "lta_monthly",
            "special_allowance_annual", "special_allowance_monthly",
            "total_package",
        }
        for key in _MONEY_KEYS:
            if key in ctx and ctx[key] not in (None, "", "—", "As discussed"):
                # Also generate a _words variant for the raw integer before formatting it
                words = _fmt_currency_words(ctx[key])
                if words:
                    ctx[f"{key}_words"] = words
                ctx[key] = _fmt_currency(ctx[key])

    # Add a unique document reference code
    ctx.setdefault(
        "doc_ref",
        f"AUTONEX/{doc_type.upper()[:6]}/{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
    )

    # ── DOCX → PDF pipeline (primary) ───────────────────────────────────────
    libreoffice_path = _detect_libreoffice()
    if libreoffice_path and doc_type in DOCX_TEMPLATE_MAP:
        docx_bytes = _fill_docx_template(doc_type, ctx)
        pdf_bytes = _convert_docx_to_pdf(docx_bytes, libreoffice_path)
    else:
        # ── HTML → PDF fallback (used when LibreOffice is not installed) ────
        html = _render_html(doc_type, ctx)
        pdf_bytes = _html_to_pdf(html)

    # Determine version
    latest = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.employee_id == employee_id,
            EmployeeDocument.doc_type == doc_type,
        )
        .order_by(EmployeeDocument.version.desc())
        .first()
    )
    next_version = (latest.version + 1) if latest else 1

    # TEMPORARY: Local storage fallback unconditionally calls upload_document
    stored_path = upload_document(
        employee_id=employee_id,
        doc_type=doc_type,
        version=next_version,
        file_bytes=pdf_bytes,
        content_type="application/pdf",
    )

    # Deactivate any current active version
    db.query(EmployeeDocument).filter(
        EmployeeDocument.employee_id == employee_id,
        EmployeeDocument.doc_type == doc_type,
        EmployeeDocument.is_active == True,
    ).update({"is_active": False})

    # Persist record
    doc = EmployeeDocument(
        employee_id=employee_id,
        doc_type=doc_type,
        file_url=stored_path,
        file_name=f"{doc_type}_v{next_version}.pdf",
        version=next_version,
        source="generated",
        uploaded_by=uploaded_by,
        is_active=True,
        generated_at=datetime.now(timezone.utc),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    return doc
