"""Exam types and the invigilation rules that follow from each one.

A school never invigilates "an exam" in the abstract. June papers suspend the
timetable, so every teacher can be used; Grade 12 prelims and finals run while
the rest of the school is still being taught, so a teacher standing in front of
a class at that slot is simply not available. Encoding that knowledge once, here,
keeps the solver, the HTTP API and the browser dashboard from disagreeing about
what a roster is allowed to contain.

Every roster request names exactly one exam type. The type decides:

  * which grades may appear on the roster (a Grade 12 prelim cannot carry a
    Grade 10 paper), and
  * whether the teaching timetable is respected - always for the two Grade 12
    types, a school choice for the Term 2 and Term 4 papers.

The catalogue is mirrored in ``assets/exam-types.js`` for the browser engine.
The test suite compares the two copies, so they cannot drift apart.
"""
import re

# How the teaching timetable is treated for a given exam type.
TIMETABLE_REQUIRED = "required"      # the browser may not switch it off
TIMETABLE_OPTIONAL = "optional"      # the school decides, per roster

# The order here is the order the dashboard and the API report.
EXAM_TYPES = [
    {
        "id": "term2",
        "label": "Term 2 exam (June) - Grades 8 to 12",
        "shortLabel": "Term 2 exam",
        "grades": [8, 9, 10, 11, 12],
        "timetablePolicy": TIMETABLE_OPTIONAL,
        "considerTimetableByDefault": False,
        "summary": (
            "Mid-year papers for the whole school. Lessons are suspended while "
            "papers are written, so all teachers are available for invigilation."
        ),
        "timetableNote": (
            "Optional: switched off by default, which is the 'all teachers "
            "available' sitting. Switch it on if some classes still run."
        ),
    },
    {
        "id": "gr12_prelim",
        "label": "Grade 12 preliminary exam",
        "shortLabel": "Gr 12 prelim",
        "grades": [12],
        "timetablePolicy": TIMETABLE_REQUIRED,
        "considerTimetableByDefault": True,
        "summary": (
            "The trial run for the NSC. Only Grade 12 writes, and the rest of the "
            "school is still on timetable."
        ),
        "timetableNote": (
            "Required: a teacher who is teaching at that slot cannot invigilate, "
            "so the teaching roster is always consulted."
        ),
    },
    {
        "id": "gr12_final",
        "label": "Grade 12 final exam (NSC)",
        "shortLabel": "Gr 12 final",
        "grades": [12],
        "timetablePolicy": TIMETABLE_REQUIRED,
        "considerTimetableByDefault": True,
        "summary": (
            "The NSC sitting. Only Grade 12 writes, and Grade 8-11 lessons "
            "continue alongside the papers."
        ),
        "timetableNote": (
            "Required: a teacher who is teaching at that slot cannot invigilate, "
            "so the teaching roster is always consulted."
        ),
    },
    {
        "id": "term4",
        "label": "Term 4 exam - Grades 8 to 11",
        "shortLabel": "Term 4 exam",
        "grades": [8, 9, 10, 11],
        "timetablePolicy": TIMETABLE_OPTIONAL,
        "considerTimetableByDefault": True,
        "summary": (
            "End-of-year papers for Grades 8 to 11, written after the Grade 12 "
            "candidates have finished."
        ),
        "timetableNote": (
            "Optional: on by default because the grades that are not writing are "
            "still in class. Switch it off if the timetable is suspended."
        ),
    },
]

_BY_ID = {spec["id"]: spec for spec in EXAM_TYPES}

# "Mathematics Gr12", "Maths Grade 12", "Grade 12 Maths" - the grade is often
# typed into the subject line, so it has to be readable from there as well.
_GRADE_MARKER = re.compile(r"\b(?:grade|gr)\.?\s*(\d{1,2})\b", re.IGNORECASE)
_EXPLICIT_GRADE = re.compile(r"^(?:grade|gr)?\.?\s*(\d{1,2})\b", re.IGNORECASE)


class ExamTypeError(ValueError):
    """The request does not match the exam type it names.

    Raised with a message written for a school administrator, not for a
    developer: the API turns it into a 400 with that text.
    """


def list_exam_types():
    """The catalogue as plain JSON-ready dicts (a fresh copy per call)."""
    return [dict(spec, grades=list(spec["grades"])) for spec in EXAM_TYPES]


def get(exam_type_id):
    """Look up one exam type, or explain what the valid choices are."""
    key = str(exam_type_id or "").strip().lower()
    if not key:
        raise ExamTypeError(missing_message())
    spec = _BY_ID.get(key)
    if spec is None:
        raise ExamTypeError(
            "Unknown exam type %r. Choose one of: %s."
            % (exam_type_id, ", ".join(known["id"] for known in EXAM_TYPES))
        )
    return spec


def missing_message():
    return "An exam type is required. Choose one of: %s." % ", ".join(
        spec["id"] for spec in EXAM_TYPES
    )


def _coerce_grade(value):
    """Accept 12, "12", "Gr 12" or "Grade 12"; anything else is not a grade."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 8 <= value <= 12 else None
    match = _EXPLICIT_GRADE.match(str(value or "").strip())
    if not match:
        return None
    grade = int(match.group(1))
    return grade if 8 <= grade <= 12 else None


def grade_of(exam):
    """The grade a paper belongs to, or ``None`` when the data does not say.

    An explicit ``grade`` field wins. Older rows only carry "Mathematics Gr12",
    so the marker is parsed out of the subject before giving up.
    """
    if not isinstance(exam, dict):
        return None
    grade = _coerce_grade(exam.get("grade"))
    if grade is not None:
        return grade
    match = _GRADE_MARKER.search(str(exam.get("subject") or ""))
    if not match:
        return None
    return _coerce_grade(match.group(1))


def grades_text(grades):
    """'Grade 12', or 'Grades 8 to 11' when the scope is contiguous."""
    grades = sorted(grades)
    if len(grades) == 1:
        return "Grade %d" % grades[0]
    if grades == list(range(grades[0], grades[-1] + 1)):
        return "Grades %d to %d" % (grades[0], grades[-1])
    return "Grades " + ", ".join(str(g) for g in grades)


def covers_grade(exam_type_id, exam):
    """True when the paper's grade is inside this exam type's scope.

    A paper with no grade at all is not rejected - the subject may simply not
    spell it out - it is reported as a warning by :func:`validate`.
    """
    spec = get(exam_type_id)
    grade = grade_of(exam)
    return grade is None or grade in spec["grades"]


def resolve_timetable(spec, consider_timetable=None):
    """Decide whether this roster consults the teaching timetable.

    ``spec`` is a catalogue entry (not an id), so callers that already looked
    one up do not pay for a second lookup. The Grade 12 types cannot be talked
    out of it: passing ``False`` is an error rather than a silent override, so a
    stale client cannot produce a roster the school would have to redo.
    """
    if spec["timetablePolicy"] == TIMETABLE_REQUIRED:
        if consider_timetable is False:
            raise ExamTypeError(
                "The %s roster must consider the teaching timetable: a teacher "
                "who is teaching at that slot cannot invigilate." % spec["shortLabel"]
            )
        return True
    if consider_timetable is None:
        return bool(spec["considerTimetableByDefault"])
    return bool(consider_timetable)


def validate(spec, exams):
    """Check every paper against the grade scope; return non-blocking warnings.

    A paper outside the scope is a hard error - silently dropping it would let a
    school print a Grade 12 prelim roster that quietly omits the Grade 10
    papers. Missing grades cannot be checked, so they are reported and left to
    the exams office.
    """
    allowed = spec["grades"]
    if sorted(allowed) == list(range(8, 13)):
        return []                                   # whole-school sitting
    outside = []
    unknown = []
    for exam in exams or []:
        if not isinstance(exam, dict):
            continue
        grade = grade_of(exam)
        subject = str(exam.get("subject") or "Unnamed paper").strip()
        if grade is None:
            unknown.append(subject)
        elif grade not in allowed:
            outside.append("%s (Gr %d)" % (subject, grade))
    if outside:
        raise ExamTypeError(
            "%s only covers %s. Remove or re-scope: %s."
            % (spec["label"], grades_text(allowed), _summarise(outside))
        )
    if unknown:
        return [
            "%d paper(s) carry no grade, so the %s grade check was skipped: %s."
            % (len(unknown), spec["shortLabel"], _summarise(unknown))
        ]
    return []


def summary_line(spec, consider_timetable):
    """One-line provenance stamp for CSV exports and printed rosters."""
    return "Exam type: %s | %s | Teaching timetable: %s" % (
        spec["label"],
        grades_text(spec["grades"]),
        "considered" if consider_timetable else "not considered",
    )


def _summarise(items, limit=4):
    """Shorten a list for a one-line error or warning."""
    items = list(items)
    if len(items) <= limit:
        return "; ".join(items)
    return "%s; and %d more" % ("; ".join(items[:limit]), len(items) - limit)
