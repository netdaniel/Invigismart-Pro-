/* Exam-type catalogue - the browser half of server/examtypes.py.
 *
 * The solver runs in two places: on the server (the gated, authoritative path)
 * and in this page so the dashboard can redraw instantly. Both have to agree on
 * which grades a sitting covers and whether it respects the teaching timetable,
 * so the catalogue is duplicated here as plain data instead of being fetched -
 * the dashboard keeps working while the API is unreachable.
 *
 * server/tests/test_api.py parses the EXAM_TYPES literal below and compares it
 * to examtypes.EXAM_TYPES, so the two copies cannot drift apart silently.
 */
(function (global) {
  'use strict';

  var EXAM_TYPES = [
    {
      "id": "term2",
      "label": "Term 2 exam (June) - Grades 8 to 12",
      "shortLabel": "Term 2 exam",
      "grades": [8, 9, 10, 11, 12],
      "timetablePolicy": "optional",
      "considerTimetableByDefault": false,
      "summary": "Mid-year papers for the whole school. Lessons are suspended while papers are written, so all teachers are available for invigilation.",
      "timetableNote": "Optional: switched off by default, which is the 'all teachers available' sitting. Switch it on if some classes still run."
    },
    {
      "id": "gr12_prelim",
      "label": "Grade 12 preliminary exam",
      "shortLabel": "Gr 12 prelim",
      "grades": [12],
      "timetablePolicy": "required",
      "considerTimetableByDefault": true,
      "summary": "The trial run for the NSC. Only Grade 12 writes, and the rest of the school is still on timetable.",
      "timetableNote": "Required: a teacher who is teaching at that slot cannot invigilate, so the teaching roster is always consulted."
    },
    {
      "id": "gr12_final",
      "label": "Grade 12 final exam (NSC)",
      "shortLabel": "Gr 12 final",
      "grades": [12],
      "timetablePolicy": "required",
      "considerTimetableByDefault": true,
      "summary": "The NSC sitting. Only Grade 12 writes, and Grade 8-11 lessons continue alongside the papers.",
      "timetableNote": "Required: a teacher who is teaching at that slot cannot invigilate, so the teaching roster is always consulted."
    },
    {
      "id": "term4",
      "label": "Term 4 exam - Grades 8 to 11",
      "shortLabel": "Term 4 exam",
      "grades": [8, 9, 10, 11],
      "timetablePolicy": "optional",
      "considerTimetableByDefault": true,
      "summary": "End-of-year papers for Grades 8 to 11, written after the Grade 12 candidates have finished.",
      "timetableNote": "Optional: on by default because the grades that are not writing are still in class. Switch it off if the timetable is suspended."
    }
  ];

  function byId(id) {
    var key = String(id || '').trim().toLowerCase();
    for (var i = 0; i < EXAM_TYPES.length; i++) {
      if (EXAM_TYPES[i].id === key) return EXAM_TYPES[i];
    }
    return null;
  }

  /* "12", "Gr 12", "Grade 12", or the marker inside "Mathematics Gr12". */
  function parseGrade(value) {
    if (value === null || value === undefined || value === true || value === false) return null;
    var text = String(value).trim();
    var match = /^(?:grade|gr)?\.?\s*(\d{1,2})\b/i.exec(text) ||
                /\b(?:grade|gr)\.?\s*(\d{1,2})\b/i.exec(text);
    if (!match) return null;
    var grade = parseInt(match[1], 10);
    return grade >= 8 && grade <= 12 ? grade : null;
  }

  /* Grade 12 prelim/final rosters always respect the timetable; the other
     sittings fall back to their own default when the school says nothing. */
  function resolveTimetable(spec, considerTimetable) {
    if (!spec) return true;
    if (spec.timetablePolicy === 'required') return true;
    if (considerTimetable === undefined || considerTimetable === null) {
      return !!spec.considerTimetableByDefault;
    }
    return !!considerTimetable;
  }

  function timetableRequired(spec) {
    return !!spec && spec.timetablePolicy === 'required';
  }

  /* An ungraded paper cannot be checked, so it is allowed (the server warns). */
  function coversGrade(spec, exam) {
    if (!spec) return true;
    var grade = parseGrade(exam && exam.grade) || parseGrade(exam && exam.subject);
    return grade === null || spec.grades.indexOf(grade) !== -1;
  }

  function gradesText(grades) {
    var sorted = (grades || []).slice().sort(function (a, b) { return a - b; });
    if (!sorted.length) return '';
    if (sorted.length === 1) return 'Grade ' + sorted[0];
    var contiguous = true;
    for (var i = 1; i < sorted.length; i++) {
      if (sorted[i] !== sorted[i - 1] + 1) { contiguous = false; break; }
    }
    if (contiguous) return 'Grades ' + sorted[0] + ' to ' + sorted[sorted.length - 1];
    return 'Grades ' + sorted.join(', ');
  }

  function summaryLine(spec, considerTimetable) {
    if (!spec) return '';
    return 'Exam type: ' + spec.label + ' | ' + gradesText(spec.grades) +
      ' | Teaching timetable: ' + (considerTimetable ? 'considered' : 'not considered');
  }

  global.InvigismartExamTypes = {
    LIST: EXAM_TYPES,
    byId: byId,
    parseGrade: parseGrade,
    resolveTimetable: resolveTimetable,
    timetableRequired: timetableRequired,
    coversGrade: coversGrade,
    gradesText: gradesText,
    summaryLine: summaryLine
  };
})(typeof window !== 'undefined' ? window : this);
