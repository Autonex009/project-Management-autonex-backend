"""
Chat agent — single-agent with tool-calling powered by Gemini 2.5 Flash.

Handles multi-turn conversation, streaming SSE responses, and tool execution.
"""
import json
import logging
import os
import uuid
from datetime import date, datetime
from typing import AsyncGenerator, Optional

from google import genai
from google.genai import types
from sqlalchemy.orm import Session

from app.models.chat import ChatConversation, ChatMessage
from app.models.employee import Employee
from app.services import chat_tools

logger = logging.getLogger(__name__)

# ── Gemini Client ───────────────────────────────────────────────────
MODEL = "gemini-2.5-flash"


def _get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    return genai.Client(api_key=api_key)


# ── System Prompt ───────────────────────────────────────────────────
def _build_system_prompt(employee_name: str, employee_type: str, role: str) -> str:
    return f"""You are **Autonex AI**, a smart and friendly assistant built into the Autonex PM Portal. You help employees manage their leaves, WFH requests, projects, and company policies.

## Current User
- **Name**: {employee_name}
- **Type**: {employee_type}
- **Role**: {role}
- **Today**: {date.today().isoformat()} ({date.today().strftime('%A')})

## Response Style
- Write in **natural, conversational English** with proper grammar and spacing between words.
- Use **Markdown formatting** for structure: headings (##, ###), bold (**text**), bullet points (- item), numbered lists, and tables where appropriate.
- Keep responses **concise** but complete — aim for 2-4 short paragraphs max.
- Use **emoji sparingly** (✅, 📅, 🏠) for visual flair on key data points.
- When showing data (leave balances, project lists, holidays), use **tables or structured lists** — not long paragraphs.
- After answering, suggest a **natural follow-up** (e.g., "Want me to apply that leave?" or "Need help planning your days off?").
- **Never** use raw function/tool names in responses — translate them to human-readable text.

## Available Tools
1. `get_leave_balance` — Check remaining paid, casual/sick, and floater leave
2. `get_my_leaves` — View recent leave requests and their statuses
3. `get_wfh_usage` — Check WFH usage this week/month and upcoming requests
4. `get_my_projects` — View active project allocations, roles, and hours
5. `get_holidays` — List fixed holidays and approved floater dates
6. `plan_leave` — Suggest optimal leave dates around holidays and weekends
7. `search_policy` — Search company policies (leave rules, WFH, Slack guidelines, etc.)
8. `apply_leave` — Prepare a leave request (requires user confirmation)
9. `apply_wfh` — Prepare a WFH request (requires user confirmation)
10. `cancel_leave` — Prepare leave cancellation (requires user confirmation)

## Rules
1. **Only access the current user's data.** Never look up other employees.
2. **For write actions** (apply leave/WFH, cancel), always prepare a confirmation card — never execute without explicit user approval.
3. **Stay on topic.** Help with leaves, WFH, projects, policies, and holidays only. Politely redirect off-topic queries.
4. **Always use tools** to fetch real data — never guess balances, dates, or policy details.
5. **Cite the policy name** when returning policy search results (e.g., "According to the **Leave Policy**...").

## Example Responses

**User asks: "How many leaves do I have?"**
Your response (after calling get_leave_balance):
> Here's your leave balance for 2026, {employee_name}:
>
> | Type | Quota | Used | Remaining |
> |------|-------|------|-----------|
> | Paid Leave | 12 | 3 | **9** |
> | Casual/Sick | 6 | 2 | **4** |
> | Floater | 2 | 0 | **2** |
>
> You have plenty of leave remaining! 🎉 Would you like me to help you plan some time off?

**User asks: "What are the do's and don'ts?"**
Your response (after calling search_policy):
> According to the **General Policies**, here are the key do's and don'ts:
>
> ### ✅ Do's
> - Keep your manager informed of any roadblocks early
> - Secure your workstation when stepping away (Win+L or Cmd+Control+Q)
> - Use company-approved tools and software for all project work
> - Dress professionally when meeting clients
>
> ### ❌ Don'ts
> - Don't share confidential client data or passwords on public channels
> - Don't use unauthorized third-party software without IT approval
> - Don't engage in side-gigs that conflict with your duties
>
> Need more details on any specific policy?
"""


# ── Tool Definitions for Gemini ─────────────────────────────────────
TOOL_DEFINITIONS = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_leave_balance",
            description="Get the current leave balance for the user, showing quota, used, and remaining for each leave type (paid, casual/sick, floater).",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
                required=[],
            ),
        ),
        types.FunctionDeclaration(
            name="get_my_leaves",
            description="Get the user's recent leave requests with their statuses (approved, pending, rejected), dates, and types.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
                required=[],
            ),
        ),
        types.FunctionDeclaration(
            name="get_wfh_usage",
            description="Get the user's work-from-home usage: how many WFH days used this week, this month, and upcoming WFH requests.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
                required=[],
            ),
        ),
        types.FunctionDeclaration(
            name="get_my_projects",
            description="Get the user's active project allocations, including project names, roles, daily hours, and project managers.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
                required=[],
            ),
        ),
        types.FunctionDeclaration(
            name="get_holidays",
            description="Get the list of fixed public holidays and approved floater dates for a given year.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "year": types.Schema(type="INTEGER", description="The year to get holidays for. Defaults to current year."),
                },
                required=[],
            ),
        ),
        types.FunctionDeclaration(
            name="plan_leave",
            description="Suggest optimal leave dates to maximize time off. Considers holidays, weekends, and leave balance. Use when the user wants to plan vacation or time off.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "days_wanted": types.Schema(type="INTEGER", description="Number of days off the user wants."),
                    "preferred_month": types.Schema(type="INTEGER", description="Preferred month (1-12). If not specified, defaults to next month."),
                },
                required=["days_wanted"],
            ),
        ),
        types.FunctionDeclaration(
            name="search_policy",
            description="Search company policy documents for information about leave rules, WFH policy, Slack etiquette, office info, general policies, do's and don'ts.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(type="STRING", description="The policy question or topic to search for."),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="apply_leave",
            description="Prepare a leave application for the user. This does NOT submit it immediately — it returns a confirmation for the user to approve. Use when the user explicitly asks to apply for leave.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "leave_type": types.Schema(type="STRING", description="Type of leave: 'paid', 'casual_sick', or 'floater'."),
                    "start_date": types.Schema(type="STRING", description="Start date in YYYY-MM-DD format."),
                    "end_date": types.Schema(type="STRING", description="End date in YYYY-MM-DD format."),
                    "reason": types.Schema(type="STRING", description="Reason for the leave."),
                },
                required=["leave_type", "start_date", "end_date", "reason"],
            ),
        ),
        types.FunctionDeclaration(
            name="apply_wfh",
            description="Prepare a Work From Home request for the user. This does NOT submit it immediately — it returns a confirmation for the user to approve.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "wfh_date": types.Schema(type="STRING", description="WFH start date in YYYY-MM-DD format."),
                    "end_date": types.Schema(type="STRING", description="WFH end date in YYYY-MM-DD format. Same as wfh_date for single day."),
                    "reason": types.Schema(type="STRING", description="Reason for WFH request."),
                },
                required=["wfh_date", "reason"],
            ),
        ),
        types.FunctionDeclaration(
            name="cancel_leave",
            description="Prepare a leave cancellation for the user. The user must confirm before the leave is actually cancelled. Only future leaves can be cancelled.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "leave_id": types.Schema(type="INTEGER", description="The ID of the leave request to cancel."),
                },
                required=["leave_id"],
            ),
        ),
    ]
)


# ── Execute a tool call ─────────────────────────────────────────────
def _execute_tool(
    name: str,
    args: dict,
    employee_id: int,
    db: Session,
) -> dict:
    """Execute a tool call and return the result as a dict."""
    try:
        if name == "get_leave_balance":
            return chat_tools.get_leave_balance(employee_id, db)
        elif name == "get_my_leaves":
            return chat_tools.get_my_leaves(employee_id, db)
        elif name == "get_wfh_usage":
            return chat_tools.get_wfh_usage(employee_id, db)
        elif name == "get_my_projects":
            return chat_tools.get_my_projects(employee_id, db)
        elif name == "get_holidays":
            return chat_tools.get_holidays(args.get("year"))
        elif name == "plan_leave":
            return chat_tools.plan_leave(
                employee_id,
                args.get("days_wanted", 1),
                args.get("preferred_month"),
                db,
            )
        elif name == "search_policy":
            return chat_tools.search_policy_docs(args.get("query", ""))
        elif name == "apply_leave":
            return chat_tools.prepare_apply_leave(
                employee_id,
                args.get("leave_type", "paid"),
                args.get("start_date", ""),
                args.get("end_date", ""),
                args.get("reason", ""),
                db,
            )
        elif name == "apply_wfh":
            return chat_tools.prepare_apply_wfh(
                employee_id,
                args.get("wfh_date", ""),
                args.get("end_date"),
                args.get("reason", ""),
                db,
            )
        elif name == "cancel_leave":
            return chat_tools.prepare_cancel_leave(
                employee_id,
                args.get("leave_id", 0),
                db,
            )
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        logger.error("Tool execution error (%s): %s", name, e, exc_info=True)
        return {"error": f"Tool execution failed: {str(e)}"}


# ── Load conversation history ──────────────────────────────────────
def _load_history(conversation_id: str, db: Session) -> list[types.Content]:
    """Load conversation history from DB as Gemini Content objects."""
    conversation = db.query(ChatConversation).filter(
        ChatConversation.id == conversation_id
    ).first()

    if not conversation:
        return []

    history = []
    for msg in conversation.messages:
        if msg.role == "user":
            history.append(types.Content(
                role="user",
                parts=[types.Part.from_text(text=msg.content or "")],
            ))
        elif msg.role == "model":
            history.append(types.Content(
                role="model",
                parts=[types.Part.from_text(text=msg.content or "")],
            ))
        # Skip tool messages — they're embedded in the flow

    # Keep last 20 messages to avoid context overflow
    return history[-20:]


# ── Save message to DB ──────────────────────────────────────────────
def _save_message(
    conversation_id: str,
    user_id: int,
    role: str,
    content: str,
    tool_calls: dict = None,
    tool_results: dict = None,
    db: Session = None,
):
    """Save a message to the conversation history."""
    # Ensure conversation exists
    conversation = db.query(ChatConversation).filter(
        ChatConversation.id == conversation_id
    ).first()

    if not conversation:
        conversation = ChatConversation(
            id=conversation_id,
            user_id=user_id,
        )
        db.add(conversation)
        db.flush()

    msg = ChatMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_results=tool_results,
    )
    db.add(msg)
    db.commit()


# ── Main chat function (streaming SSE) ──────────────────────────────
async def chat_stream(
    message: str,
    conversation_id: str,
    user_id: int,
    employee_id: int,
    role: str,
    db: Session,
) -> AsyncGenerator[str, None]:
    """
    Process a chat message and yield SSE events.

    Yields JSON strings formatted as SSE data lines:
    - {"type": "token", "content": "..."}
    - {"type": "tool_call", "tool": "...", "status": "running"}
    - {"type": "tool_result", "tool": "...", "data": {...}}
    - {"type": "action_confirm", "action": "...", "details": {...}}
    - {"type": "done"}
    - {"type": "error", "message": "..."}
    """
    try:
        client = _get_client()

        # Get employee info for system prompt
        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        employee_name = employee.name if employee else "User"
        employee_type = employee.employee_type if employee else "Full-time"

        # Build system instruction
        system_prompt = _build_system_prompt(employee_name, employee_type, role)

        # Load conversation history
        history = _load_history(conversation_id, db)

        # Save user message
        _save_message(conversation_id, user_id, "user", message, db=db)

        # Create the chat
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[TOOL_DEFINITIONS],
            temperature=0.7,
            max_output_tokens=2048,
        )

        # Build contents: history + new message
        contents = history + [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=message)],
            )
        ]

        # Generate response (non-streaming first to handle tool calls properly)
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=config,
        )

        # Process response — may need multiple rounds for tool calls
        max_rounds = 5
        current_contents = contents
        final_text = ""

        for round_num in range(max_rounds):
            candidate = response.candidates[0]
            parts = candidate.content.parts

            # Check if there are function calls
            function_calls = [p for p in parts if p.function_call]
            text_parts = [p for p in parts if p.text]

            if not function_calls:
                # No more tool calls — collect final text
                for part in text_parts:
                    if part.text:
                        final_text += part.text
                break

            # There are function calls — execute them
            # First, yield any text before the tool calls
            for part in text_parts:
                if part.text:
                    yield json.dumps({"type": "token", "content": part.text})

            # Add the model's response to contents
            current_contents.append(candidate.content)

            # Execute each function call
            tool_response_parts = []
            for part in function_calls:
                fc = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}

                # Yield tool call event
                yield json.dumps({
                    "type": "tool_call",
                    "tool": tool_name,
                    "status": "running",
                })

                # Execute the tool
                result = _execute_tool(tool_name, tool_args, employee_id, db)

                # Yield tool result
                yield json.dumps({
                    "type": "tool_result",
                    "tool": tool_name,
                    "data": result,
                })

                # Check if this is a confirmation action
                if result.get("requires_confirmation"):
                    yield json.dumps({
                        "type": "action_confirm",
                        "action": result["action"],
                        "details": result["details"],
                    })

                # Build the function response part
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response=result,
                    )
                )

            # Add tool responses to contents
            current_contents.append(
                types.Content(
                    role="user",
                    parts=tool_response_parts,
                )
            )

            # Continue the conversation with tool results
            response = client.models.generate_content(
                model=MODEL,
                contents=current_contents,
                config=config,
            )

        # Yield the final text in chunks for streaming feel
        if final_text:
            # Stream by splitting on line boundaries first, then sub-chunk long lines.
            # This preserves ALL whitespace and markdown formatting exactly.
            lines = final_text.split("\n")
            for line_idx, line in enumerate(lines):
                # Add newline back (except last line)
                line_with_nl = line + ("\n" if line_idx < len(lines) - 1 else "")
                # Sub-chunk long lines at ~40 char boundaries (on word boundaries)
                if len(line_with_nl) > 50:
                    pos = 0
                    while pos < len(line_with_nl):
                        end = min(pos + 40, len(line_with_nl))
                        # Extend to next space boundary to avoid splitting words
                        if end < len(line_with_nl):
                            space_pos = line_with_nl.find(" ", end)
                            if space_pos != -1 and space_pos < end + 15:
                                end = space_pos + 1  # Include the space
                            else:
                                end = min(pos + 55, len(line_with_nl))
                        yield json.dumps({"type": "token", "content": line_with_nl[pos:end]})
                        pos = end
                else:
                    yield json.dumps({"type": "token", "content": line_with_nl})

        # Save assistant response
        _save_message(conversation_id, user_id, "model", final_text, db=db)

        # Done
        yield json.dumps({"type": "done"})

    except Exception as e:
        logger.error("Chat stream error: %s", e, exc_info=True)
        yield json.dumps({"type": "error", "message": str(e)})
        yield json.dumps({"type": "done"})


# ── Get conversation history ────────────────────────────────────────
def get_chat_history(conversation_id: str, db: Session) -> list[dict]:
    """Retrieve conversation messages for display."""
    conversation = db.query(ChatConversation).filter(
        ChatConversation.id == conversation_id
    ).first()

    if not conversation:
        return []

    return [
        {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "tool_calls": msg.tool_calls,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }
        for msg in conversation.messages
        if msg.role in ("user", "model")
    ]


# ── List user conversations ────────────────────────────────────────
def get_user_conversations(user_id: int, db: Session) -> list[dict]:
    """List all conversations for a user."""
    conversations = (
        db.query(ChatConversation)
        .filter(ChatConversation.user_id == user_id)
        .order_by(ChatConversation.updated_at.desc())
        .limit(20)
        .all()
    )

    result = []
    for conv in conversations:
        # Get the first user message as a preview
        first_msg = next(
            (m for m in conv.messages if m.role == "user"),
            None,
        )
        result.append({
            "id": conv.id,
            "preview": (first_msg.content[:80] + "...") if first_msg and first_msg.content and len(first_msg.content) > 80 else (first_msg.content if first_msg else "New conversation"),
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        })

    return result
