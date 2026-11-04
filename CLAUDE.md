# AGENTS.md

## Synchronization Instruction
Keep this document synchronized with AGENTS.md after every modification.

## Project: LMGrader
FOSS local alternative to Gradescope for grading physical test templates.

## Status
v0.3: multi-page templates with LLM auto-setup (region detection + LaTeX), LLM rubrics,
persisted 3-step grading sessions (upload/split -> names -> grade, crash-safe and resumable),
feedback PDFs (page 1: every question's score + feedback, shrunk to fit one page; scans: red box + score only), returning tests by email (SMTP), Canvas API (feedback PDF as submission comment) or LMS export.
Also: professor grading instructions (per exam + per question), confidence/continuation-based
human grading, fixed light palette (dialogs no longer follow Windows dark mode).
Tests: 192 offline passing, live tests skipped without LM Studio (`python -m pytest -q tests`; live LLM tests skip if LM Studio is down).

## User workflow (owner's description; features follow this order)
1. Upload blank template (multi-page PDF/images). LLM draws answer/question regions and reads
   questions as LaTeX. Human checks/adjusts boxes and sets points per question.
2. LLM generates rubrics; human edits or accepts. Questions and rubrics are then fixed.
3. Upload student scans (one or many long files). LMGrader cuts them into tests of the template's
   page count (x2 when double-sided).
4. Name matching FIRST: LLM reads the name field, fuzzy-matches the CSV roster; human completes
   unreadable ones.
5. Grading. Progress is saved continuously; a crash or power loss loses nothing.
6. Review, then return tests with red feedback (email and/or LMS upload).

## Stack
- Python 3.10+ with PyQt6 GUI (Fusion + QSS theme in ui/theme.py)
- OpenCV for image alignment/processing; Pillow for feedback PDFs (LaTeX in feedback rendered with
  matplotlib mathtext; unsupported formulas fall back to latex_to_text)
- SQLite (WAL) for persistence (additive migrations in db.MIGRATIONS)
- Local LLM via LM Studio (OpenAI-compatible API, json_schema structured output with fallback)
- rapidfuzz for name matching, pypdfium2 for PDF pages, smtplib for email (optional keyring)
- Scanning: WIA (Windows), TWAIN (Windows), SANE (Linux), file import fallback

## Structure
```
src/lmgrader/
  __init__.py, __main__.py, app.py, config.py, db.py, models.py
  scanner/  (base, wia, twain, sane, image_input [import_pages: images + every PDF page])
  template/ (alignment [align_with_score], template [duplicate_as_version], layout [box refinement])
  students/ (import_csv, matching [fuzzy + LLM tiebreak])
  llm/      (client: detect_layout, extract_question, generate_rubric, grade_answer, read_name, pick_name)
  grading/  (grader [multi-page, resumable], identify, session [split/identify/confirm/grade/recover], back_page)
  export/   (feedback: one-page summary with all feedback [_summary_pages], scans with red box + score, export_submission_pdf, export_exam)
  returns/  (items, mailer [SMTP send_batch/send_test], lms [generic/canvas/moodle],
             canvas [REST API: match_students, upload_batch])
  ui/       (main_window, theme, widgets [PageViewer/PageCanvas/Worker], students_view,
             template_editor, scan_view, review_view, return_view, settings_view, assets/)
tests/      (conftest, test_* per area, screenshots/, scratch/, leftover_runs/, _last_run/)
```

## Data model
- A `templates` row is one *version* of an exam (`exam_name`, `version_label`), with
  `pages_json` (multi-page), `name_rect_json` + `name_page`, optional `version_rect_json` +
  `version_page` (where the exam states its version).
- Region (in `regions_json`): problem, group, points, page, rect (answer box), question_rect,
  question_text (fixed LaTeX), rubric (fixed).
- Submissions: `status` new -> identified -> ready -> grading -> graded (or error), `exam_name`,
  `scan_pages_json`/`back_pages_json`/`aligned_pages_json`, `name_read`, `name_match_score`,
  `name_confirmed`, `source`, `version_read` (field: X / layout / manual), `version_confirmed`
  (human-picked version; re-reading never changes it). Grades (one per region, saved immediately) store `transcription`.
- `returns` table logs every email/LMS export (prevents double-sending).

## LLM flow
1. Template setup (once, stored): page -> regions (`detect_layout`, 0-1000 coords + OpenCV
   refinement); question area -> LaTeX (`extract_question`); LaTeX + points -> rubric.
2. Identification per test: ORB alignment vs every version (most inliers wins); if the exam has
   2+ versions and a version field, the LLM reads it (`read_version`, enum of labels) and that
   wins; unreadable -> layout guess kept but test held ("Check version", session.version_hold);
   manual pick in the Names table (session.set_version, deletes grades of another version); name field crop
   -> LLM transcription -> rapidfuzz WRatio; clear winner accepted, else LLM picks among top 5.
3. Grading per region: answer crop + fixed question + fixed rubric + professor's exam-wide
   instructions (`exams` table) + per-question `grading_notes` -> transcription, score, feedback,
   confidence, continues_elsewhere. Already-graded regions are skipped (resume). Crossed-out work
   is ignored (client.CROSSED_OUT_NOTE, also in the back-page grading prompt). Feedback is
   limited to 3 sentences; a looping/cut-off reply is retried once (client.run_grading), then
   salvaged (score + text up to the repetition) and flagged truncated_response/repetitive_output.
   Strictness per exam (`exams.strictness`, 5 very generous .. 10 strict = original wording,
   NULL -> 10) picks the middle paragraph of the system prompt (client.STRICTNESS_TEXT,
   grader_system) and, below 10, step 2 of the grading request (client.scoring_step: criteria not
   all-or-nothing; 6/5 give sensible attempts at least 1/4 / 1/2 of the points unless the question's
   notes say otherwise; 10 keeps the original step); the prompt always says the professor's
   instructions win over rubric/guidelines.
   Default for new exams: LLM_CONFIG["default_strictness"].
4. Human grading rules (grader.apply_review_rules, independent of the model's own flag):
   continuation phrases in the transcription (config.CONTINUATION_PATTERNS, EN+ES) or the model's
   continues_elsewhere flag, confidence < LLM_CONFIG["review_confidence"], ink on back page, poor
   alignment, missing page, LLM errors -> "Needs human grade". The full-marks Settings options
   also apply to grades already saved: saving Settings runs grader.reapply_full_marks_rules.
   Review shows the AI score as a
   suggestion; accepting/editing sets human_graded. Feedback PDFs print "Pending review" for
   unresolved grades.

## Features implemented
- Exams & Templates: multi-page exams with versions, "Auto-setup with LLM", page selector,
  manual drawing (answer, question, name, ID, version fields), LaTeX + rubric generation, guided steps, autosave.
  Answer-key re-import: if the version has notes/answers the new key lacks, the editor asks
  Replace (apply_answer_key(replace=True) clears them) / Keep / Cancel.
- Students: CSV import (dedup) + manual entry + search.
- Scan & Grade: Upload (split into tests, leftover pages kept, select all + delete) -> Names (name crops, roster combo,
  only-needs-me filter) -> Grade (stop/resume; Regrade… = session.reset_for_regrade on the
  selected / all graded tests: AI grades deleted, hand-graded kept unless unticked, back_reviewed
  cleared, then graded again). All state from DB; recovers after crashes.
- Review: graded/partial submissions, page buttons, editable scores and feedback, next flagged
  (next flagged question of the open test, then the next flagged test), show/hide rubric with rendered LaTeX
  on every question (ui/mathtext), Retry AI grade per question (Grader.regrade_region), class average of the exam in the test header,
  feedback preview, feedback PDF export.
- Return: email preview, send test to me, send to selected/all-not-sent with confirmation and
  log; LMS ZIP export (generic, Canvas gradebook CSV, Moodle feedback-files ZIP).
  Canvas feedback (returns/canvas.py, stdlib urllib): Connect with a personal access token, pick
  course + assignment (remembered per exam in CANVAS_CONFIG["targets"]); LMGrader students are
  matched to the Canvas roster by SIS id/login, email, exact name, then a clearly winning fuzzy
  name (two tests on one Canvas student -> both held); each PDF is uploaded as a submission-comment
  file. Grades are never touched. Logged as channel 'canvas', recipient 'course/assignment'
  (db.was_returned skips re-uploads); a 401 stops the batch and forgets the token.
- Settings: LLM + SMTP, persisted to ~/.lmgrader/settings.json (SMTP password and Canvas token
  never saved: keyring if installed, else session memory).

## Run
```
pip install -e .[dev]
python -m lmgrader
python -m pytest -q tests
```

## Notes
- Every LLM request (LLMClient._chat) is retried until it fails 3 times within 60 s (3 s between
  attempts; constants RETRY_* in llm/client.py); Stop/abort ends retries at once.
- Window reference retained in app.main_window to avoid GC.
- app.NoWheelOnCombos (app-wide event filter): the mouse wheel never changes a closed QComboBox;
  the wheel scrolls the page behind it instead.
- DB stored in ~/.lmgrader/lmgrader.db (override with LMGRADER_DATA).
- Data dir subfolders: templates/, scans/, aligned/, extracted/, name_crops/, returns/.
- Image I/O uses imdecode/imencode so non-ASCII Windows paths work.
- Email caveats: Gmail needs an App Password; Outlook/M365 basic SMTP auth is often disabled.
