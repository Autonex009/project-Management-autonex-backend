import copy
import math
from datetime import datetime, timezone
from difflib import SequenceMatcher
import random
from typing import Any, Dict, List, Optional, Tuple


class AssessmentValidationError(Exception):
    """Raised when an assessment blueprint or candidate payload fails validation."""
    pass


def _parse_int(val: Any, field_name: str) -> int:
    if val is None or isinstance(val, bool):
        raise AssessmentValidationError(f"'{field_name}' must be an integer.")
    if isinstance(val, float) and not val.is_integer():
        raise AssessmentValidationError(f"'{field_name}' must be an integer.")
    try:
        return int(val)
    except (ValueError, TypeError) as exc:
        raise AssessmentValidationError(f"'{field_name}' must be a valid integer.") from exc


def _parse_finite_float(val: Any, field_name: str) -> float:
    if val is None or isinstance(val, bool):
        raise AssessmentValidationError(f"'{field_name}' must be a number.")
    try:
        parsed = float(val)
    except (ValueError, TypeError) as exc:
        raise AssessmentValidationError(f"'{field_name}' must be a valid number.") from exc
    if not math.isfinite(parsed):
        raise AssessmentValidationError(f"'{field_name}' must be finite.")
    return parsed


def validate_blueprint_for_publish(
    test_type: str,
    test_config: Any,
    passing_score: Optional[float] = None,
) -> None:
    """
    Validates blueprint completeness and structural integrity when publishing.
    Drafts can bypass this, but published tests must strictly comply.
    """
    if test_type not in ("mcq", "typing"):
        raise AssessmentValidationError(
            f"Unsupported test_type '{test_type}'. Only 'mcq' and 'typing' are supported in Phase 1."
        )

    if not isinstance(test_config, dict):
        raise AssessmentValidationError("test_config must be a JSON object.")

    if test_type == "mcq":
        if passing_score is None:
            raise AssessmentValidationError(
                "MCQ assessments require an explicit passing_score between 0 and 100."
            )
        eff_passing = _parse_finite_float(passing_score, "passing_score")
        if not (0.0 <= eff_passing <= 100.0):
            raise AssessmentValidationError("MCQ assessments require passing_score between 0 and 100.")

        categories = test_config.get("categories")
        if categories is not None and not isinstance(categories, list):
            raise AssessmentValidationError("test_config.categories must be a list.")

        if not categories:
            questions = test_config.get("questions")
            if not isinstance(questions, list) or not questions:
                raise AssessmentValidationError(
                    "MCQ assessment must define at least one category or question pool."
                )
            categories = [{
                "category_id": "default",
                "name": "General",
                "pick_count": len(questions),
                "questions": questions,
            }]

        all_question_ids = set()
        for idx, cat in enumerate(categories):
            if not isinstance(cat, dict):
                raise AssessmentValidationError(f"Category at index {idx} must be an object.")
            cat_name = str(cat.get("name") or f"Category #{idx + 1}")
            if not cat.get("category_id"):
                raise AssessmentValidationError(f"Category '{cat_name}' is missing 'category_id'.")

            pick_count = _parse_int(cat.get("pick_count"), f"{cat_name}.pick_count")
            questions = cat.get("questions", [])
            if not isinstance(questions, list):
                raise AssessmentValidationError(f"Category '{cat_name}' questions must be a list.")

            if pick_count <= 0:
                raise AssessmentValidationError(f"Category '{cat_name}' must have pick_count > 0.")
            if len(questions) < pick_count:
                raise AssessmentValidationError(
                    f"Category '{cat_name}' requires at least {pick_count} questions, but only {len(questions)} provided."
                )

            for q_idx, q in enumerate(questions):
                if not isinstance(q, dict):
                    raise AssessmentValidationError(
                        f"Question #{q_idx + 1} in category '{cat_name}' must be an object."
                    )
                qid = q.get("question_id")
                if qid is None or str(qid).strip() == "":
                    raise AssessmentValidationError(f"Question in category '{cat_name}' missing 'question_id'.")
                qid_str = str(qid)
                if qid_str in all_question_ids:
                    raise AssessmentValidationError(
                        f"Duplicate question_id '{qid_str}' detected across assessment categories."
                    )
                all_question_ids.add(qid_str)

                options = q.get("options")
                if not isinstance(options, list) or len(options) < 2:
                    raise AssessmentValidationError(f"Question '{qid_str}' must have at least 2 options.")

                # A published test must never serve a blank question or a blank choice.
                q_text = q.get("question")
                if not isinstance(q_text, str) or not q_text.strip():
                    raise AssessmentValidationError(
                        f"Question '{qid_str}' in category '{cat_name}' has an empty prompt."
                    )
                if any(not isinstance(o, str) or not o.strip() for o in options):
                    raise AssessmentValidationError(
                        f"Question '{qid_str}' in category '{cat_name}' has an empty option; every option needs text."
                    )

                correct_idx = _parse_int(q.get("correct_index"), f"{qid_str}.correct_index")
                if not (0 <= correct_idx < len(options)):
                    raise AssessmentValidationError(
                        f"Question '{qid_str}' has invalid correct_index: {correct_idx}."
                    )

                if "marks" in q and q["marks"] is not None:
                    marks = _parse_finite_float(q["marks"], f"{qid_str}.marks")
                    if marks <= 0:
                        raise AssessmentValidationError(f"Question '{qid_str}' marks must be > 0.")

    elif test_type == "typing":
        raw_passage = test_config.get("passage_text")
        if not isinstance(raw_passage, str) or len(raw_passage.strip().split()) < 20:
            raise AssessmentValidationError("Typing test requires a passage of at least 20 words.")

        target_wpm = _parse_finite_float(test_config.get("target_wpm"), "target_wpm")
        if target_wpm <= 0:
            raise AssessmentValidationError("Typing test requires target_wpm > 0.")

        if "min_accuracy_percent" in test_config and test_config["min_accuracy_percent"] is not None:
            min_acc = _parse_finite_float(test_config["min_accuracy_percent"], "min_accuracy_percent")
            if not (0.0 < min_acc <= 100.0):
                raise AssessmentValidationError(
                    "Typing test requires min_accuracy_percent > 0 and <= 100."
                )


def generate_candidate_exam_paper(
    test_type: str,
    test_config: Dict[str, Any],
    passing_score: Optional[float] = None,
    proctoring_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generates an immutable question_snapshot for a new candidate sitting.
    Freezes test_type, passing_score, proctoring_config, and sampled questions/passages.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    frozen_proctoring = copy.deepcopy(proctoring_config or {})

    if test_type == "typing":
        raw_wpm = test_config.get("target_wpm")
        try:
            target_wpm = float(raw_wpm) if raw_wpm is not None else 40.0
        except (ValueError, TypeError):
            target_wpm = 40.0

        raw_acc = test_config.get("min_accuracy_percent")
        try:
            min_acc = float(raw_acc) if raw_acc is not None else 95.0
        except (ValueError, TypeError):
            min_acc = 95.0

        return {
            "test_type": "typing",
            "target_wpm": target_wpm,
            "min_accuracy_percent": min_acc,
            "passage_text": test_config.get("passage_text", "") or "",
            "proctoring_config": frozen_proctoring,
            "generated_at": now_iso,
        }

    # MCQ Path: Category-pooled sampling
    mode = test_config.get("mode", "category_pooled")
    shuffle_options = test_config.get("shuffle_options", True)
    shuffle_questions = test_config.get("shuffle_questions", True)

    categories = test_config.get("categories", [])
    if not categories and "questions" in test_config:
        categories = [{
            "category_id": "default",
            "name": "General",
            "pick_count": len(test_config["questions"]),
            "questions": test_config["questions"],
        }]

    selected_questions = []
    category_meta = []
    total_marks = 0.0

    for cat in categories:
        cat_id = cat.get("category_id", "default")
        cat_name = cat.get("name", "General")
        pick_count = int(cat.get("pick_count", 0))
        pool = cat.get("questions", [])

        if len(pool) < pick_count:
            raise ValueError(
                f"Category '{cat_name}' has {len(pool)} questions, fewer than pick_count {pick_count}"
            )

        sampled = random.sample(pool, pick_count)

        for q in sampled:
            q_copy = copy.deepcopy(q)
            q_copy["category_id"] = cat_id
            q_copy["category_name"] = cat_name
            marks = float(q_copy.get("marks", 1.0))
            total_marks += marks

            if shuffle_options and "options" in q_copy:
                orig_options = list(q_copy["options"])
                orig_correct = int(q_copy.get("correct_index", 0))
                n = len(orig_options)
                if n > 1:
                    perm = random.sample(range(n), n)
                    q_copy["options"] = [orig_options[i] for i in perm]
                    q_copy["correct_index"] = perm.index(orig_correct)

            selected_questions.append(q_copy)

        category_meta.append({
            "category_id": cat_id,
            "name": cat_name,
            "picked_count": pick_count,
        })

    if shuffle_questions:
        random.shuffle(selected_questions)

    eff_passing = passing_score if passing_score is not None else test_config.get("passing_score")
    if eff_passing is None:
        eff_passing = 80.0

    return {
        "test_type": "mcq",
        "mode": mode,
        "passing_score": float(eff_passing),
        "proctoring_config": frozen_proctoring,
        "generated_at": now_iso,
        "total_questions": len(selected_questions),
        "total_marks": total_marks,
        "categories": category_meta,
        "questions": selected_questions,
    }


def sanitize_snapshot_for_candidate(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strips answer keys, internal explanations, and internal proctoring thresholds
    before sending snapshot to candidate UI.
    """
    clean_snapshot = copy.deepcopy(snapshot)
    clean_snapshot.pop("proctoring_config", None)
    if clean_snapshot.get("test_type") == "mcq":
        for q in clean_snapshot.get("questions", []):
            q.pop("correct_index", None)
            q.pop("explanation", None)
            q.pop("internal_notes", None)
    return clean_snapshot


def sanitize_result_summary_for_candidate(result_summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Strips question-level diagnostics and anti-cheat detection flags
    before sending evaluation output to candidate-facing views.
    """
    if result_summary is None:
        return None
    clean = copy.deepcopy(result_summary)
    clean.pop("item_diagnostics", None)
    clean.pop("is_flagged", None)
    clean.pop("flag_reason", None)

    # Candidate-friendly "7 / 10 right" totals derived from the aggregate category breakdown,
    # so no per-question detail ever has to be exposed (also works for already-stored attempts).
    breakdown = clean.get("category_breakdown")
    if clean.get("test_type", "mcq") == "mcq" and isinstance(breakdown, dict):
        cats = [c for c in breakdown.values() if isinstance(c, dict)]
        clean["correct_count"] = sum(int(c.get("correct_count") or 0) for c in cats)
        clean["total_questions"] = sum(int(c.get("total_questions") or 0) for c in cats)
    return clean


def validate_draft_payload(snapshot: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """
    Validates draft autosave payload or submit payload against frozen attempt snapshot.
    Raises AssessmentValidationError on malformed values.
    """
    test_type = snapshot.get("test_type", "mcq")
    answers = payload.get("answers", {})

    if test_type == "mcq":
        if not isinstance(answers, dict):
            raise AssessmentValidationError("Answers must be a JSON object mapping question_id to option index.")

        valid_q_map = {str(q["question_id"]): len(q.get("options", [])) for q in snapshot.get("questions", [])}
        for q_id, ans in answers.items():
            str_qid = str(q_id)
            if str_qid not in valid_q_map:
                raise AssessmentValidationError(f"Unknown question_id '{q_id}' in answers.")
            if isinstance(ans, bool) or not isinstance(ans, int):
                raise AssessmentValidationError(f"Answer for '{q_id}' must be an integer option index.")
            opt_count = valid_q_map[str_qid]
            if not (0 <= ans < opt_count):
                raise AssessmentValidationError(
                    f"Answer index {ans} out of bounds for question '{q_id}' (0 to {opt_count - 1})."
                )

    elif test_type == "typing":
        if not isinstance(answers, dict):
            raise AssessmentValidationError("Typing data must be a JSON object.")

        allowed_typing_keys = {"typed_text", "keystroke_count"}
        unexpected_keys = set(answers.keys()) - allowed_typing_keys
        if unexpected_keys:
            raise AssessmentValidationError(
                f"Unsupported field(s) in typing answers: {', '.join(sorted(unexpected_keys))}."
            )

        typed_text = answers.get("typed_text", "")
        if not isinstance(typed_text, str):
            raise AssessmentValidationError("typed_text must be a string.")

        passage_len = len(snapshot.get("passage_text", ""))
        max_allowed_len = max(passage_len * 2, 500)
        if len(typed_text) > max_allowed_len:
            raise AssessmentValidationError(f"typed_text exceeds maximum length cap ({max_allowed_len} chars).")

        max_allowed_keys = max(passage_len * 10, 10000)
        keystroke_count = answers.get("keystroke_count")
        if keystroke_count is not None and (
            isinstance(keystroke_count, bool)
            or not isinstance(keystroke_count, int)
            or keystroke_count < 0
            or keystroke_count > max_allowed_keys
        ):
            raise AssessmentValidationError(
                f"keystroke_count must be an integer between 0 and {max_allowed_keys}."
            )


def evaluate_mcq_attempt(
    question_snapshot: Dict[str, Any],
    submitted_answers: Dict[str, Any]
) -> Tuple[float, bool, Dict[str, Any]]:
    """
    Pure CPU in-memory evaluation (< 0.1ms) against frozen question_snapshot.
    """
    total_marks_possible = float(question_snapshot.get("total_marks", 0.0))
    if total_marks_possible <= 0:
        total_marks_possible = sum(float(q.get("marks", 1.0)) for q in question_snapshot.get("questions", []))

    total_marks_obtained = 0.0
    category_stats = {}

    for cat in question_snapshot.get("categories", []):
        category_stats[cat["category_id"]] = {
            "category_name": cat["name"],
            "total_questions": 0,
            "correct_count": 0,
            "score": 0.0,
            "max_score": 0.0,
            "accuracy_percent": 0.0,
        }

    item_diagnostics = []

    for q in question_snapshot.get("questions", []):
        q_id = str(q["question_id"])
        cat_id = q.get("category_id", "default")
        q_marks = float(q.get("marks", 1.0))
        correct_idx = q.get("correct_index")

        if cat_id not in category_stats:
            category_stats[cat_id] = {
                "category_name": q.get("category_name", "General"),
                "total_questions": 0,
                "correct_count": 0,
                "score": 0.0,
                "max_score": 0.0,
                "accuracy_percent": 0.0,
            }

        cat = category_stats[cat_id]
        cat["total_questions"] += 1
        cat["max_score"] += q_marks

        candidate_ans = submitted_answers.get(q_id)
        if candidate_ans is None and q.get("question_id") in submitted_answers:
            candidate_ans = submitted_answers[q["question_id"]]

        is_correct = False
        if candidate_ans is not None and not isinstance(candidate_ans, bool):
            try:
                ans_idx = int(candidate_ans)
                if ans_idx == int(correct_idx):
                    total_marks_obtained += q_marks
                    cat["correct_count"] += 1
                    cat["score"] += q_marks
                    is_correct = True
            except (ValueError, TypeError):
                pass

        item_diagnostics.append({
            "question_id": q_id,
            "question": q.get("question"),
            "options": list(q.get("options") or []),
            "category_id": cat_id,
            "category_name": cat["category_name"],
            "user_choice": candidate_ans,
            "correct_index": correct_idx,
            "is_correct": is_correct,
            "marks": q_marks if is_correct else 0.0,
        })

    for cat in category_stats.values():
        if cat["max_score"] > 0:
            cat["accuracy_percent"] = round((cat["score"] / cat["max_score"]) * 100.0, 2)

    score_percent = round((total_marks_obtained / total_marks_possible) * 100.0, 2) if total_marks_possible > 0 else 0.0
    raw_passing = question_snapshot.get("passing_score")
    passing_score = float(raw_passing if raw_passing is not None else 80.0)
    passed = score_percent >= passing_score

    result_summary = {
        "test_type": "mcq",
        "total_marks_obtained": total_marks_obtained,
        "total_marks_possible": total_marks_possible,
        "passing_score": passing_score,
        "category_breakdown": category_stats,
        "item_diagnostics": item_diagnostics,
    }

    return score_percent, passed, result_summary


def evaluate_typing_attempt(
    question_snapshot: Dict[str, Any],
    submitted_data: Dict[str, Any],
    time_spent_seconds: float = 0.0
) -> Tuple[float, bool, Dict[str, Any]]:
    """
    Evaluates typing test performance using SequenceMatcher tokenized block alignment.
    Prevents short-snippet gaming by requiring substantive passage word coverage.
    """
    passage_text = str(question_snapshot.get("passage_text", "")).strip()
    typed_text = str(submitted_data.get("typed_text", "")).strip()

    passage_words = passage_text.split()
    typed_words = typed_text.split()

    total_words_typed = len(typed_words)
    total_passage_words = len(passage_words)

    matcher = SequenceMatcher(None, passage_words, typed_words, autojunk=False)
    blocks = matcher.get_matching_blocks()

    correct_word_idx = {b.b + k for b in blocks for k in range(b.size)}
    correct_words = len(correct_word_idx)
    # Count only inter-word spaces (n - 1) so single-word correct_chars cannot exceed total_chars_typed
    correct_chars = sum(len(typed_words[i]) for i in correct_word_idx) + max(correct_words - 1, 0)

    total_chars_typed = len(typed_text)
    accuracy_percent = round((correct_words / total_words_typed * 100.0), 2) if total_words_typed > 0 else 0.0

    # Enforce substantive passage completion: >= 50% of passage words (min 20 words for longer texts, full passage if <= 20 words)
    min_words_required = (
        total_passage_words
        if total_passage_words <= 20
        else max(20, math.ceil(total_passage_words * 0.5))
    )
    if total_passage_words <= 0:
        min_words_required = 1
    coverage_ratio = min(total_words_typed / min_words_required, 1.0) if min_words_required > 0 else 0.0
    has_min_coverage = total_words_typed >= min_words_required

    minutes = max(float(time_spent_seconds) / 60.0, 1.0 / 60.0)
    gross_wpm = round((total_chars_typed / 5.0) / minutes, 1)
    net_wpm = round((correct_chars / 5.0) / minutes, 1)

    target_wpm = float(question_snapshot.get("target_wpm", 40.0))
    raw_min_acc = question_snapshot.get("min_accuracy_percent")
    min_accuracy = float(raw_min_acc if raw_min_acc is not None else 95.0)

    passed = has_min_coverage and (net_wpm >= target_wpm) and (accuracy_percent >= min_accuracy)

    if target_wpm > 0:
        wpm_factor = min(net_wpm / target_wpm, 1.0)
        acc_factor = 1.0 if accuracy_percent >= min_accuracy else (accuracy_percent / 100.0)
        score_percent = round(wpm_factor * coverage_ratio * acc_factor * 100.0, 2)
    else:
        score_percent = 0.0

    try:
        keystroke_count = int(submitted_data.get("keystroke_count") or 0)
    except (ValueError, TypeError):
        keystroke_count = 0

    is_flagged = False
    flag_reason = None
    if total_chars_typed > 80:
        if keystroke_count < (total_chars_typed * 0.4):
            is_flagged = True
            flag_reason = "suspicious_bulk_paste_keystrokes_missing"

    result_summary = {
        "test_type": "typing",
        "net_wpm": net_wpm,
        "gross_wpm": gross_wpm,
        "accuracy_percent": accuracy_percent,
        "correct_words": correct_words,
        "total_words_typed": total_words_typed,
        "min_words_required": min_words_required,
        "completion_percent": round(coverage_ratio * 100.0, 2),
        "target_wpm": target_wpm,
        "min_accuracy_percent": min_accuracy,
        "is_flagged": is_flagged,
        "flag_reason": flag_reason,
    }
    return score_percent, passed, result_summary


def evaluate_candidate_attempt(
    question_snapshot: Dict[str, Any],
    submitted_data: Dict[str, Any],
    time_spent_seconds: float = 0.0
) -> Tuple[float, bool, Dict[str, Any]]:
    """
    Polymorphic grader dispatcher: delegates to MCQ or Typing grader based on test_type.
    """
    test_type = question_snapshot.get("test_type", "mcq")
    if test_type == "typing":
        return evaluate_typing_attempt(question_snapshot, submitted_data, time_spent_seconds)
    return evaluate_mcq_attempt(question_snapshot, submitted_data)
