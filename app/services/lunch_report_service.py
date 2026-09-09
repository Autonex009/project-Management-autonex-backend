"""
Daily Lunch Order Report
- Generates a clean PDF (meal-plan segregated + floor grouped)
- Sends it at 11 AM IST
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from io import BytesIO
from typing import List, Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from sqlalchemy.orm import Session
from app.models.daily_checkin import DailyCheckIn
from app.models.employee import Employee
from app.services.email_service import try_send_lunch_report_email

IST = ZoneInfo("Asia/Kolkata")

MEAL_ORDER = ["full_meal", "no_rice", "dal_and_rice"]
MEAL_LABELS = {
    "full_meal": "Full Meal",
    "no_rice": "No Rice",
    "dal_and_rice": "Dal & Rice",
}
FLOOR_ORDER = ["7", "9", "17"]


def _get_today_lunch_data(db: Session) -> Dict:
    today = datetime.now(IST).date()

    rows = (
        db.query(DailyCheckIn, Employee.name)
        .join(Employee, DailyCheckIn.employee_id == Employee.id)
        .filter(
            DailyCheckIn.checkin_date == today,
            DailyCheckIn.work_mode == "WFO",
            DailyCheckIn.lunch_preference.isnot(None),
        )
        .all()
    )

    tiffin = defaultdict(lambda: defaultdict(list))   # meal -> floor -> [names]
    canteen = defaultdict(list)                       # floor -> [names]

    for chk, name in rows:
        floor = chk.office_floor or "—"
        if chk.lunch_preference == "order_tiffin" and chk.tiffin_type:
            tiffin[chk.tiffin_type][floor].append(name)
        elif chk.lunch_preference == "canteen":
            canteen[floor].append(name)

    # sort names
    for meal in tiffin:
        for fl in tiffin[meal]:
            tiffin[meal][fl].sort()
    for fl in canteen:
        canteen[fl].sort()

    return {
        "date": today,
        "tiffin": tiffin,
        "canteen": canteen,
    }


def _build_pdf(data: Dict) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TitleCenter",
        parent=styles["Heading1"],
        alignment=TA_CENTER,
        fontSize=16,
        spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="SubCenter",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=10,
        textColor=colors.grey,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="SectionHeader",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=colors.HexColor("#1e3a8a"),
        spaceBefore=14,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="MealHeader",
        parent=styles["Heading3"],
        fontSize=11,
        textColor=colors.HexColor("#065f46"),
        spaceBefore=10,
        spaceAfter=4,
    ))

    story = []

    # Title
    date_str = data["date"].strftime("%d %b %Y")
    story.append(Paragraph(f"Daily Lunch Order Summary – {date_str}", styles["TitleCenter"]))
    story.append(Paragraph(
        f"Generated at {datetime.now(IST).strftime('%I:%M %p IST')}",
        styles["SubCenter"]
    ))

    # Summary counts
    total_tiffin = sum(len(names) for meal in data["tiffin"].values() for names in meal.values())
    full = sum(len(n) for n in data["tiffin"].get("full_meal", {}).values())
    no_rice = sum(len(n) for n in data["tiffin"].get("no_rice", {}).values())
    dal = sum(len(n) for n in data["tiffin"].get("dal_and_rice", {}).values())
    total_canteen = sum(len(n) for n in data["canteen"].values())

    summary_data = [
        ["Total Tiffin Orders", str(total_tiffin)],
        ["  • Full Meal", str(full)],
        ["  • No Rice", str(no_rice)],
        ["  • Dal & Rice", str(dal)],
        ["Canteen", str(total_canteen)],
    ]
    summary_table = Table(summary_data, colWidths=[120*mm, 40*mm])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c7d2fe")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 8*mm))

    # ---------- 1. Order Tiffin ----------
    story.append(Paragraph("1. Order Tiffin (Action Required)", styles["SectionHeader"]))

    if total_tiffin == 0:
        story.append(Paragraph("No tiffin orders today.", styles["Normal"]))
    else:
        for meal_key in MEAL_ORDER:
            floors = data["tiffin"].get(meal_key, {})
            if not floors:
                continue

            story.append(Paragraph(MEAL_LABELS[meal_key], styles["MealHeader"]))

            # table header
            table_data = [["Floor", "Employee Name"]]
            for fl in FLOOR_ORDER:
                names = floors.get(fl, [])
                for name in names:
                    table_data.append([fl, name])
            # any other floor
            for fl, names in floors.items():
                if fl not in FLOOR_ORDER:
                    for name in names:
                        table_data.append([fl, name])

            t = Table(table_data, colWidths=[30*mm, 130*mm])
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d1fae5")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#065f46")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#a7f3d0")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdf4")]),
            ]))
            story.append(t)
            story.append(Spacer(1, 4*mm))

    # ---------- 2. Canteen ----------
    story.append(Paragraph("2. Canteen", styles["SectionHeader"]))

    if total_canteen == 0:
        story.append(Paragraph("No one selected Canteen today.", styles["Normal"]))
    else:
        table_data = [["Floor", "Employee Name"]]
        for fl in FLOOR_ORDER:
            for name in data["canteen"].get(fl, []):
                table_data.append([fl, name])
        for fl, names in data["canteen"].items():
            if fl not in FLOOR_ORDER:
                for name in names:
                    table_data.append([fl, name])

        t = Table(table_data, colWidths=[30*mm, 130*mm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#93c5fd")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eff6ff")]),
        ]))
        story.append(t)

    # Footer
    story.append(Spacer(1, 10*mm))
    story.append(Paragraph(
        "<font size='8' color='#6b7280'>"
        "Auto-generated from the Daily Check-in system. "
        "Only employees who checked in as <b>WFO</b> and selected a lunch preference are included."
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
) -> bool:
    """
    Main entry point.
    Sends the lunch report PDF to all given emails.
    """
    if not to_emails:
        to_emails = ["kisanjena40@gmail.com"]  

    data = _get_today_lunch_data(db)
    pdf_bytes = _build_pdf(data)

    date_str = data["date"].strftime("%d-%b-%Y")
    filename = f"Lunch_Order_Summary_{date_str}.pdf"

    html = f"""
    <p>Hi,</p>
    <p>Please find attached the <strong>Daily Lunch Order Summary</strong> for <strong>{data['date'].strftime('%d %b %Y')}</strong>.</p>
    <p>The PDF is segregated by <b>Meal Plan</b> and then by <b>Floor</b> for easy preparation.</p>
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