"""Server-side roster solver.

This is the gated "money operation": the balancing/constraint logic lives on the
server and is only reachable with a verified, unexpired entitlement. Ported to
mirror the browser implementation exactly (1:30 ratio, no own-subject
invigilation, skip teachers with a clashing class, balance load, no
double-booking within a single exam slot).
"""
import math
import re
from datetime import datetime, timezone

import examtypes

DEFAULT_RATIO = 30

# Timetables and exam papers rarely spell a subject the same way. The importer
# stores full names, while a school may type "Maths" or "ENGHL" on an exam row.
# Canonicalising both sides to one family keeps the no-own-subject rule working
# across those spellings instead of silently rostering a teacher onto their own
# paper.
_SUBJECT_ALIASES = {
    "eng": "english",
    "english": "english",
    "eng hl": "english",
    "enghl": "english",
    "english hl": "english",
    "english home language": "english",
    "eng fal": "english",
    "engfal": "english",
    "english fal": "english",
    "english first additional language": "english",
    "afr": "afrikaans",
    "afrikaans": "afrikaans",
    "afr fal": "afrikaans",
    "afrfal": "afrikaans",
    "afrikaans fal": "afrikaans",
    "afrikaans first additional language": "afrikaans",
    "math": "mathematics",
    "maths": "mathematics",
    "mathematics": "mathematics",
    "mat": "mathematics",
    "math lit": "mathematical literacy",
    "maths lit": "mathematical literacy",
    "mathematical literacy": "mathematical literacy",
    "mlt": "mathematical literacy",
    "ml": "mathematical literacy",
    "physical science": "physical sciences",
    "physical sciences": "physical sciences",
    "phys sci": "physical sciences",
    "phys sciences": "physical sciences",
    "psc": "physical sciences",
    "physics": "physical sciences",
    "life science": "life sciences",
    "life sciences": "life sciences",
    "l sc": "life sciences",
    "lsc": "life sciences",
    "life orientation": "life orientation",
    "lo": "life orientation",
    "accounting": "accounting",
    "acc": "accounting",
    "business studies": "business studies",
    "bst": "business studies",
    "bus stud": "business studies",
    "economics": "economics",
    "econ": "economics",
    "geography": "geography",
    "geo": "geography",
    "history": "history",
    "his": "history",
    "tourism": "tourism",
    "trsm": "tourism",
    "consumer studies": "consumer studies",
    "cst": "consumer studies",
    "computer applications technology": "computer applications technology",
    "cat": "computer applications technology",
    "technical mathematics": "technical mathematics",
    "tech maths": "technical mathematics",
    "tech math": "technical mathematics",
    "tec maths": "technical mathematics",
    "tec math": "technical mathematics",
    "technical sciences": "technical sciences",
    "tech sciences": "technical sciences",
    "tech science": "technical sciences",
    "tec sciences": "technical sciences",
    "engineering graphics and design": "engineering graphics and design",
    "egd": "engineering graphics and design",
    "natural sciences": "natural sciences",
    "natural science": "natural sciences",
    "ns": "natural sciences",
    "economic and management sciences": "economic and management sciences",
    "ems": "economic and management sciences",
    "social sciences": "social sciences",
    "social science": "social sciences",
    "ss": "social sciences",
    "technology": "technology",
    "tec": "technology",
    "creative arts": "creative arts",
    "ca": "creative arts",
    "electrical technology": "electrical technology",
    "etc": "electrical technology",
}


def _subject_key(value):
    """Reduce a subject label to a comparable family name.

    Grade/class markers are dropped so "Mathematical Literacy Gr12" and
    "Maths Lit" meet on the same key. Unknown subjects keep their normalised
    spelling, which still gives exact matching for school-specific labels.
    """
    text = str(value or "").lower()
    text = re.sub(r"\b(?:grade|gr)\.?\s*\d+\b", " ", text)
    text = re.sub(r"\b\d+[a-z]?\b", " ", text)
    text = re.sub(r"[^a-z]+", " ", text)
    text = " ".join(text.split())
    return _SUBJECT_ALIASES.get(text, text) if text else ""


def _subject_keys(values):
    """Canonical subject families for one teacher's subject list."""
    keys = set()
    for value in values or []:
        for part in re.split(r"[/;,]", str(value or "")):
            key = _subject_key(part)
            if key:
                keys.add(key)
    return keys


def invigilators_required(students, ratio=DEFAULT_RATIO):
    try:
        count = int(students)
    except (TypeError, ValueError):
        count = 0
    return max(1, math.ceil(count / float(ratio)))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def generate_roster(
    teachers,
    schedules,
    exams,
    ratio=DEFAULT_RATIO,
    exam_type=None,
    consider_timetable=None,
):
    """Balance invigilation load under the rules of one exam type.

    ``exam_type`` is optional here so the module can be exercised on its own,
    but the HTTP API insists on it: a roster without a sitting has no grade
    scope and no timetable policy, and the school would not know which rules it
    was generated under.

    ``consider_timetable`` is resolved by the exam type. The two Grade 12 types
    always consult the timetable (asking to ignore it raises), Term 2 defaults
    to "all teachers available", and Term 4 defaults to teaching-aware. When the
    timetable is ignored the schedules are dropped entirely rather than merely
    skipped, so the busy-slot rule cannot leak back in.
    """
    teachers = list(teachers or [])
    schedules = list(schedules or [])
    exams = list(exams or [])

    spec = examtypes.get(exam_type) if exam_type is not None else None
    warnings = []
    if spec is not None:
        warnings.extend(examtypes.validate(spec, exams))
        use_timetable = examtypes.resolve_timetable(spec, consider_timetable)
        if use_timetable and not schedules:
            # Not an error: a school may genuinely have no timetable loaded yet.
            # Saying so is the difference between "the rule was applied" and
            # "the rule looked applied because nobody was flagged".
            warnings.append(
                "No teaching timetable is loaded: every teacher was treated as "
                "free, so the timetable rule could not exclude anyone."
            )
    else:
        use_timetable = True
    if not use_timetable:
        schedules = []

    load = {t.get("id"): 0 for t in teachers}
    booked = set()
    assignments = []

    ordered = sorted(
        exams,
        key=lambda e: (
            str(e.get("day") or "") + str(e.get("period") or ""),
            -_int(e.get("students")),
        ),
    )

    for exam in ordered:
        needed = invigilators_required(exam.get("students"), ratio)
        subject_key = _subject_key(exam.get("subject"))
        day = str(exam.get("day") or "")
        period = str(exam.get("period") or "")

        eligible = []
        for teacher in teachers:
            tid = teacher.get("id")
            if not tid:
                continue
            if (day, period, tid) in booked:
                continue
            if subject_key and subject_key in _subject_keys(teacher.get("subjects")):
                continue
            busy = any(
                sc.get("teacherId") == tid
                and str(sc.get("day") or "") == day
                and str(sc.get("period") or "").upper() == period.upper()
                for sc in schedules
            )
            if busy:
                continue
            eligible.append(teacher)

        eligible.sort(key=lambda t: (load.get(t.get("id"), 0), str(t.get("name") or "")))

        chosen = [t.get("id") for t in eligible[:needed]]
        if len(chosen) < needed:
            warnings.append(
                "%s (%s %s): only %d/%d invigilators available."
                % (exam.get("subject"), day, period, len(chosen), needed)
            )
        for tid in chosen:
            load[tid] = load.get(tid, 0) + 1
            booked.add((day, period, tid))
        assignments.append({"examId": exam.get("id"), "teacherIds": chosen})

    return {
        "assignments": assignments,
        "warnings": warnings,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # The exam type travels with the result so the browser, the CSV export
        # and the printed master roster all state the same sitting and rules.
        "examType": spec["id"] if spec else None,
        "examTypeLabel": spec["label"] if spec else None,
        "gradeScope": list(spec["grades"]) if spec else [],
        "considerTimetable": use_timetable,
        "summary": {
            "exams": len(ordered),
            "teachers": len(teachers),
            "assigned": sum(len(a["teacherIds"]) for a in assignments),
            "required": sum(invigilators_required(e.get("students"), ratio) for e in ordered),
        },
    }


def _csv_field(value):
    """Quote one CSV cell (RFC 4180), mirroring the browser's exporter.

    A leading = + - @ or tab/CR is prefixed with an apostrophe, so a teacher or
    subject name can never be executed as a spreadsheet formula.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    if any(ch in text for ch in (",", '"', "\r", "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def export_roster_csv(
    teachers,
    exams,
    assignments,
    ratio=DEFAULT_RATIO,
    exam_type=None,
    consider_timetable=None,
):
    """Render a generated roster as CSV - formatting only, no solver.

    The assignments are passed in exactly as the school saw them, so the export
    can never drift from what is on screen. This is the clean, unwatermarked
    artifact: the backend only reaches it for a paid subscription, never for a
    free evaluation.

    When an exam type is supplied the file opens with a ``#`` provenance line
    naming the sitting, its grade scope and whether teaching was respected, and
    every paper carries its grade. A printed roster without that context is
    ambiguous a term later, which is exactly when schools re-use it.
    """
    names = {}
    for teacher in teachers or []:
        tid = teacher.get("id")
        if tid:
            names[tid] = str(teacher.get("name") or "")

    exams_by_id = {}
    for exam in exams or []:
        eid = exam.get("id")
        if eid:
            exams_by_id[eid] = exam

    rows = []
    if exam_type is not None:
        spec = examtypes.get(exam_type)
        rows.append(
            ["# " + examtypes.summary_line(spec, examtypes.resolve_timetable(spec, consider_timetable))]
        )
    rows.append(
        ["Date", "Day", "Period", "Subject", "Grade", "Students", "Required", "Invigilators"]
    )
    for assignment in assignments or []:
        exam = exams_by_id.get(assignment.get("examId"))
        if not exam:
            continue
        invigilators = assignment.get("teacherIds") or []
        grade = examtypes.grade_of(exam)
        rows.append(
            [
                exam.get("date") or "",
                exam.get("day") or "",
                exam.get("period") or "",
                exam.get("subject") or "",
                grade if grade is not None else "",
                exam.get("students") if exam.get("students") is not None else "",
                invigilators_required(exam.get("students"), ratio),
                "; ".join(names.get(tid, "Unknown") for tid in invigilators),
            ]
        )
    return "\r\n".join(",".join(_csv_field(cell) for cell in row) for row in rows)
