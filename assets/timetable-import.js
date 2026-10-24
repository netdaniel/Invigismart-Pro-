/* Invigismart Pro - staff timetable import.
 *
 * School administration systems often export a weekly grid with one block per
 * teacher: a header row (teacher, Monday..Friday), then one row per period and
 * a cell such as "ENGHL - 08H". That is a different shape from the dashboard's
 * 4-column schedule CSV, so this module normalises both shapes into the same
 * workspace objects.
 *
 * The functions here are deliberately pure: no DOM, no localStorage and no
 * network. index.html decides when to merge the result into its state, and the
 * test suite can exercise the parser without a browser.
 */
(function (global) {
  'use strict';

  var WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];

  /* Subject codes used by South African school timetables. Keeping the full
     name in the workspace makes the staff directory readable and lets the
     solver's alias matching exclude the right teachers. Unknown codes are kept
     verbatim, so a school-specific abbreviation is never silently lost. */
  var SUBJECT_NAMES = {
    'ENGHL': 'English Home Language',
    'AFRFAL': 'Afrikaans First Additional Language',
    'MAT': 'Mathematics',
    'MLT': 'Mathematical Literacy',
    'LO': 'Life Orientation',
    'NS': 'Natural Sciences',
    'EMS': 'Economic and Management Sciences',
    'SS': 'Social Sciences',
    'TEC': 'Technology',
    'CA': 'Creative Arts',
    'BST': 'Business Studies',
    'TRSM': 'Tourism',
    'L SC': 'Life Sciences',
    'CST': 'Consumer Studies',
    'GEO': 'Geography',
    'HIS': 'History',
    'ECON': 'Economics',
    'PSC': 'Physical Sciences',
    'CAT': 'Computer Applications Technology',
    'TECH MATHS': 'Technical Mathematics',
    'TECH SCIENCE': 'Technical Sciences',
    'EGD': 'Engineering Graphics and Design',
    'ETC': 'Electrical Technology',
    'ACC': 'Accounting'
  };

  var NON_TEACHING = /^(register|registration|home\s*room|homeroom|tutor|assembly|lunch|break|interval|recess)\b/i;

  /* RFC-4180 CSV parser: quoted fields, escaped quotes ("") and CRLF/BOM. It
     mirrors the dashboard's importer and keeps this module dependency-free, so
     the grid parser can be exercised on its own outside a browser. */
  function csvToRows(text) {
    var src = String(text == null ? '' : text).replace(/^\uFEFF/, '');
    var rows = [];
    var row = [];
    var field = '';
    var inQuotes = false;
    var i;
    for (i = 0; i < src.length; i++) {
      var c = src.charAt(i);
      if (inQuotes) {
        if (c === '"') {
          if (src.charAt(i + 1) === '"') { field += '"'; i++; }
          else inQuotes = false;
        } else {
          field += c;
        }
      } else if (c === '"') {
        inQuotes = true;
      } else if (c === ',') {
        row.push(field); field = '';
      } else if (c === '\n') {
        row.push(field); field = ''; rows.push(row); row = [];
      } else if (c !== '\r') {
        field += c;
      }
    }
    if (field.length || row.length) { row.push(field); rows.push(row); }
    return rows.map(function (cells) {
      return cells.map(function (cell) { return String(cell).trim(); });
    }).filter(function (cells) {
      return cells.some(function (cell) { return cell !== ''; });
    });
  }

  function text(value) {
    return String(value == null ? '' : value).replace(/\u00a0/g, ' ');
  }

  /* Keep line breaks inside a cell (the source repeats the code and class on
     separate lines), but normalise the whitespace on each line. */
  function cellText(value) {
    return text(value).replace(/\r\n?/g, '\n').split('\n').map(function (line) {
      return line.replace(/[ \t]+/g, ' ').trim();
    }).filter(function (line) {
      return line !== '';
    }).join('\n');
  }

  function oneLine(value) {
    return text(value).replace(/\s+/g, ' ').trim();
  }

  function canonicalDay(value) {
    var compact = oneLine(value).toLowerCase().replace(/[^a-z]/g, '');
    if (!compact) return '';
    for (var i = 0; i < WEEKDAYS.length; i++) {
      var full = WEEKDAYS[i].toLowerCase();
      if (compact === full || (compact.length >= 3 && full.indexOf(compact) === 0)) {
        return WEEKDAYS[i];
      }
    }
    return '';
  }

  function cleanTeacherName(value) {
    var name = oneLine(value);
    name = name.replace(/^[,\s]+/, '').replace(/[,\s]+$/, '');
    return name.replace(/\s*,\s*/g, ', ');
  }

  function teacherKey(value) {
    return cleanTeacherName(value).toLowerCase().replace(/\s+/g, ' ').trim();
  }

  /* "08h00 - 08h55", "8:00-9:00" and "12h35- 13h30" all become one canonical
     label. Non-time labels (Lunch, Register Period, separator rows) return ''. */
  function normaliseTime(value) {
    var raw = oneLine(value);
    var match = raw.match(/^(\d{1,2})\s*[hH:.]\s*(\d{2})\s*[-\u2013\u2014]\s*(\d{1,2})\s*[hH:.]\s*(\d{2})$/);
    if (!match) return '';
    return match[1] + 'h' + match[2] + ' - ' + match[3] + 'h' + match[4];
  }

  function rowIsNonTeaching(row) {
    var hasText = false;
    for (var i = 1; i <= 5; i++) {
      var value = oneLine(row[i]);
      if (!value) continue;
      hasText = true;
      if (!NON_TEACHING.test(value)) return false;
    }
    return hasText;
  }

  function headerInfo(row) {
    if (!row) return null;
    var name = cleanTeacherName(row[0]);
    var days = {};
    var count = 0;
    for (var i = 1; i <= 5; i++) {
      var day = canonicalDay(row[i]);
      if (day) { days[i] = day; count++; }
    }
    if (!name || count < 3) return null;
    return { name: name, days: days };
  }

  function isStaffTimetableGrid(rows) {
    for (var i = 0; i < (rows || []).length; i++) {
      if (headerInfo(rows[i])) return true;
    }
    return false;
  }

  /* "ENGHL - 08H" -> { code: "ENGHL", klass: "08H" }. The first line is the
     authoritative one; the repeated lines below it are ignored. */
  function parseCell(value) {
    var lines = cellText(value).split('\n');
    var first = lines.length ? lines[0] : '';
    var match = first.match(/^(.+?)\s+[-\u2013\u2014]\s+(.+)$/);
    if (!match) return null;
    var code = oneLine(match[1]);
    var klass = oneLine(match[2]);
    if (!code || !klass) return null;
    return { code: code, klass: klass };
  }

  function subjectNamesFor(code) {
    return oneLine(code).split('/').map(function (part) {
      var label = oneLine(part);
      return SUBJECT_NAMES[label.toUpperCase()] || label;
    }).filter(Boolean);
  }

  function addUnique(list, value) {
    var key = oneLine(value).toLowerCase();
    for (var i = 0; i < list.length; i++) {
      if (oneLine(list[i]).toLowerCase() === key) return false;
    }
    list.push(value);
    return true;
  }

  function blockPeriods(rows, start, end) {
    var labels = [];
    var seen = {};
    for (var i = start + 1; i <= end; i++) {
      var row = rows[i] || [];
      var label = normaliseTime(row[0]);
      if (!label || rowIsNonTeaching(row) || seen[label]) continue;
      seen[label] = true;
      labels.push(label);
    }
    return labels;
  }

  /* Parse a timetable grid into a stable, DOM-free shape.
     Returns { teachers, periodOrder, stats }. */
  function parseStaffTimetable(rows) {
    rows = rows || [];
    var headers = [];
    var i;
    for (i = 0; i < rows.length; i++) {
      var info = headerInfo(rows[i]);
      if (info) headers.push({ index: i, name: info.name, days: info.days });
    }

    var stats = {
      blocks: headers.length,
      teachers: 0,
      slots: 0,
      subjects: 0,
      periods: 0,
      skippedBlocks: 0,
      skippedCells: 0,
      duplicateSlots: 0
    };
    if (!headers.length) return { teachers: [], periodOrder: [], stats: stats };

    /* Use the longest period list in the file. Empty rows still occupy a
       period, so a teacher with a free first period does not shift everyone
       else's P1/P2 numbering. */
    var periodOrder = [];
    for (i = 0; i < headers.length; i++) {
      var end = (i + 1 < headers.length) ? headers[i + 1].index - 1 : rows.length - 1;
      var candidate = blockPeriods(rows, headers[i].index, end);
      if (candidate.length > periodOrder.length) periodOrder = candidate;
    }
    var periodMap = {};
    for (i = 0; i < periodOrder.length; i++) periodMap[periodOrder[i]] = 'P' + (i + 1);

    var teachers = [];
    var byKey = {};
    var allSubjects = {};

    for (i = 0; i < headers.length; i++) {
      var header = headers[i];
      var blockEnd = (i + 1 < headers.length) ? headers[i + 1].index - 1 : rows.length - 1;
      var key = teacherKey(header.name);
      if (!key) { stats.skippedBlocks++; continue; }

      var teacher = byKey[key];
      if (!teacher) {
        teacher = { name: header.name, key: key, subjects: [], slots: [] };
        byKey[key] = teacher;
        teachers.push(teacher);
      }

      var slotSeen = {};
      for (var r = header.index + 1; r <= blockEnd; r++) {
        var row = rows[r] || [];
        var time = normaliseTime(row[0]);
        if (!time || rowIsNonTeaching(row)) continue;
        var period = periodMap[time];
        if (!period) {
          periodOrder.push(time);
          period = 'P' + periodOrder.length;
          periodMap[time] = period;
        }

        for (var col in header.days) {
          if (!Object.prototype.hasOwnProperty.call(header.days, col)) continue;
          var cell = cellText(row[col]);
          if (!cell || NON_TEACHING.test(oneLine(cell))) continue;
          var parsed = parseCell(cell);
          if (!parsed) { stats.skippedCells++; continue; }

          var names = subjectNamesFor(parsed.code);
          if (!names.length) { stats.skippedCells++; continue; }
          var slotKey = header.days[col] + '|' + period;
          if (slotSeen[slotKey]) { stats.duplicateSlots++; continue; }
          slotSeen[slotKey] = true;

          var subject = names.join(' / ');
          teacher.slots.push({
            day: header.days[col],
            period: period,
            time: time,
            klass: parsed.klass + ' ' + subject,
            subjectCode: parsed.code,
            classCode: parsed.klass
          });
          for (var n = 0; n < names.length; n++) {
            if (addUnique(teacher.subjects, names[n])) allSubjects[names[n]] = true;
          }
        }
      }
    }

    for (i = 0; i < teachers.length; i++) {
      teachers[i].subjects.sort(function (a, b) { return a.localeCompare(b); });
      stats.slots += teachers[i].slots.length;
    }
    stats.teachers = teachers.length;
    stats.periods = periodOrder.length;
    stats.subjects = Object.keys(allSubjects).length;
    return { teachers: teachers, periodOrder: periodOrder, stats: stats };
  }

  function clone(object) {
    var copy = {};
    for (var key in object) {
      if (Object.prototype.hasOwnProperty.call(object, key)) copy[key] = object[key];
    }
    return copy;
  }

  var fallbackSeq = 0;
  function fallbackId() {
    fallbackSeq++;
    return 'tt' + fallbackSeq;
  }

  function slotKey(schedule) {
    return schedule.teacherId + '|' + oneLine(schedule.day).toLowerCase() + '|' +
      oneLine(schedule.period).toUpperCase();
  }

  /* Merge a parsed grid into an existing workspace. Returns new arrays (the
     inputs are not mutated) plus counts for the import message. Re-importing
     the same file is therefore idempotent: existing busy slots are skipped. */
  function applyToWorkspace(parsed, teachers, schedules, makeId) {
    var nextId = typeof makeId === 'function' ? makeId : fallbackId;
    var outputTeachers = (teachers || []).map(function (teacher) {
      var copy = clone(teacher);
      copy.subjects = (teacher.subjects || []).slice();
      return copy;
    });
    var outputSchedules = (schedules || []).map(clone);
    var byKey = {};
    var j;

    for (j = 0; j < outputTeachers.length; j++) {
      byKey[teacherKey(outputTeachers[j].name)] = outputTeachers[j];
    }

    var stats = { teachersAdded: 0, teachersUpdated: 0, slotsAdded: 0, slotsSkipped: 0 };
    var idsByKey = {};

    for (j = 0; j < ((parsed && parsed.teachers) || []).length; j++) {
      var incoming = parsed.teachers[j];
      var key = teacherKey(incoming.name);
      var teacher = byKey[key];
      var isNew = false;
      if (!teacher) {
        teacher = { id: nextId(), name: incoming.name, subjects: [] };
        outputTeachers.push(teacher);
        byKey[key] = teacher;
        stats.teachersAdded++;
        isNew = true;
      }
      var before = teacher.subjects.length;
      for (var s = 0; s < incoming.subjects.length; s++) {
        addUnique(teacher.subjects, incoming.subjects[s]);
      }
      if (!isNew && teacher.subjects.length !== before) stats.teachersUpdated++;
      idsByKey[key] = teacher.id;
    }

    var occupied = {};
    for (j = 0; j < outputSchedules.length; j++) {
      occupied[slotKey(outputSchedules[j])] = true;
    }

    for (j = 0; j < ((parsed && parsed.teachers) || []).length; j++) {
      var source = parsed.teachers[j];
      var teacherId = idsByKey[teacherKey(source.name)];
      for (var k = 0; k < source.slots.length; k++) {
        var slot = source.slots[k];
        var record = {
          id: nextId(),
          teacherId: teacherId,
          day: slot.day,
          period: slot.period,
          klass: slot.klass,
          time: slot.time
        };
        var key2 = slotKey(record);
        if (occupied[key2]) { stats.slotsSkipped++; continue; }
        occupied[key2] = true;
        outputSchedules.push(record);
        stats.slotsAdded++;
      }
    }

    return {
      teachers: outputTeachers,
      schedules: outputSchedules,
      stats: stats
    };
  }

  global.InvigismartTimetable = {
    csvToRows: csvToRows,
    isStaffTimetableGrid: isStaffTimetableGrid,
    parseStaffTimetable: parseStaffTimetable,
    applyToWorkspace: applyToWorkspace,
    teacherKey: teacherKey,
    cleanTeacherName: cleanTeacherName,
    subjectNamesFor: subjectNamesFor,
    SUBJECT_NAMES: SUBJECT_NAMES
  };
})(typeof window !== 'undefined' ? window : this);
