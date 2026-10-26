"""
Daily Lunch Order Report
- Generates a clean PDF (Menu-Wise Segregated + Physical Signature Box)
- Finalized strictly at 11:00 AM IST
"""
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo
from collections import defaultdict
from io import BytesIO
from typing import List, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from sqlalchemy.orm import Session
from app.models.daily_checkin import DailyCheckIn
from app.models.employee import Employee
from app.services.email_service import try_send_lunch_report_email

IST = ZoneInfo("Asia/Kolkata")


def _get_today_lunch_data(db: Session, target_date: Optional[date] = None) -> Dict:
    """Queries WFO check-ins and groups orders strictly by Menu Item with employee details."""
    today = target_date or datetime.now(IST).date()

    rows = (
        db.query(DailyCheckIn, Employee.name)
        .join(Employee, DailyCheckIn.employee_id == Employee.id)
        .filter(
            DailyCheckIn.checkin_date == today,
            DailyCheckIn.work_mode == "WFO",
            DailyCheckIn.lunch_preference.in_(["canteen", "order_tiffin"]),
        )
        .all()
    )

    # Group by Menu Option: menu_name -> list of employee dicts
    menu_groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    legacy_map = {
        "full_meal": "Full Meal",
        "no_rice": "No Rice",
        "dal_and_rice": "Dal & Rice",
    }

    for chk, name in rows:
        raw_name = (chk.tiffin_type or "Standard Canteen Meal").strip()
        menu_name = legacy_map.get(raw_name, raw_name)
        menu_groups[menu_name].append({
            "name": name,
            "floor": chk.office_floor or "—",
        })

    # Sort employees alphabetically under each menu option
    for menu_name in menu_groups:
        menu_groups[menu_name].sort(key=lambda x: x["name"].lower())

    return {
        "date": today,
        "menu_groups": dict(menu_groups),
        "total_count": len(rows),
    }


def _build_pdf(data: Dict) -> bytes:
    """Builds the printable Menu-Wise Lunch Sheet with signature columns."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TitleCenter",
        parent=styles["Heading1"],
        alignment=TA_CENTER,
        fontSize=16,
        spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name="SubCenter",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=10,
        textColor=colors.grey,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        name="MenuHeader",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=12,
        spaceAfter=4,
        keepWithNext=True,  # Prevents orphan headers at the bottom of pages
    ))

    story = []
    date_str = data["date"].strftime("%d %b %Y")

    # Header
    story.append(Paragraph(f"Daily Lunch Order Sheet – {date_str}", styles["TitleCenter"]))
    story.append(Paragraph(
        f"Finalized at 11:00 AM IST • Total Orders: {data['total_count']}",
        styles["SubCenter"]
    ))

    # Summary Table by Menu Option
    summary_data = [["Menu Option", "Total Orders"]]
    for menu_name, items in sorted(data["menu_groups"].items()):
        summary_data.append([menu_name, str(len(items))])
    summary_data.append(["Total Canteen Meals", str(data["total_count"])])

    summary_table = Table(summary_data, colWidths=[120 * mm, 40 * mm])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f1f5f9")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 6 * mm))

    # Menu-Wise Tables with Signature Column
    if not data["menu_groups"]:
        story.append(Paragraph("No canteen orders placed today.", styles["Normal"]))
    else:
        for menu_name, items in sorted(data["menu_groups"].items()):
            story.append(Paragraph(f"<b>{menu_name}</b> ({len(items)} orders)", styles["MenuHeader"]))

            table_data = [["Sr.", "Employee Name", "Floor", "Signature"]]
            for idx, item in enumerate(items, 1):
                table_data.append([
                    str(idx),
                    item["name"],
                    f"Floor {item['floor']}",
                    "",  # Blank box for employee physical signature
                ])

            t = Table(table_data, colWidths=[12 * mm, 75 * mm, 23 * mm, 50 * mm])
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#334155")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (2, 0), (2, -1), "CENTER"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]))
            story.append(t)
            story.append(Spacer(1, 5 * mm))

    # Footer
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "<font size='8' color='#64748b'>"
        "Confidential • Autonex Operations • Please collect signature of each employee upon food handout."
        "</font>",
        styles["Normal"]
    ))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


def generate_and_send_lunch_report(
    db: Session,
    to_emails: list[str] | None = None,
    target_date: Optional[date] = None,
) -> bool:
    """
    Main entry point.
    Sends the lunch report PDF to all given emails.
    """
    if not to_emails:
        primary = os.getenv("LUNCH_REPORT_PRIMARY_EMAIL", "jadhavashish061@gmail.com")
        fallback = os.getenv("LUNCH_REPORT_FALLBACK_EMAIL", "kisanjena40@gmail.com")
        to_emails = list(dict.fromkeys([primary, fallback]))

    data = _get_today_lunch_data(db, target_date=target_date)
    pdf_bytes = _build_pdf(data)

    date_str = data["date"].strftime("%d-%b-%Y")
    filename = f"Lunch_Order_Summary_{date_str}.pdf"

    html = f"""
    <p>Hi,</p>
    <p>Please find attached the <strong>Daily Lunch Order Summary</strong> for <strong>{data['date'].strftime('%d %b %Y')}</strong>.</p>
    <p>The PDF is organized strictly by <b>Menu Option</b> with dedicated signature boxes for food handout collection.</p>
    <p>Regards,<br>Autonex System</p>
    """

    all_success = True
    for email in to_emails:
        ok = try_send_lunch_report_email(
            to_email=email,
            to_name="Lunch Ops",
            subject=f"Daily Lunch Order Summary – {data['date'].strftime('%d %b %Y')}",
            html_body=html,
            pdf_bytes=pdf_bytes,
            pdf_filename=filename,
        )
        if not ok:
            all_success = False

    return all_success
