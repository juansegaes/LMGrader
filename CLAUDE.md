# AGENTS.md

## Synchronization Instruction
Keep this document synchronized with AGENTS.md after every modification.

## Project: LMGrader
FOSS local app for grading physical test templates.

## Status
Current version: 0.3.141592 (pyproject.toml, config.APP_VERSION, lmgrader.__version__; the
vX.Y headings below are feature milestones, not release numbers).
v0.3: multi-page templates with LLM auto-setup (region detection + LaTeX), LLM rubrics,
persisted 3-step grading sessions (upload/split -> names -> grade, crash-safe and resumable),
feedback PDFs (page 1: every question's score + feedback, shrunk to fit one page; scans: red box + score only), returning tests by email (SMTP), Canvas API (feedback PDF as submission comment) or LMS export.
Also: professor grading instructions (per exam + per question), confidence/continuation-based
human grading, fixed light palette (dialogs no longer follow Windows dark mode).
Tests: 303 offline passing (4 skipped: live LLM), live tests skipped without LM Studio (`python -m pytest -q tests`; live LLM tests skip if LM Studio is down).

## v0.5: useful without an LLM
- Tabs: Exams & Templates, Students, Scan & Names (ScanView steps upload + names), Hand Grade
  (ui/hand_grade_view), LLM Grade (scan_view.LLMGradeView = the grade step), Review, Return,
  Settings. ScanView() with no `steps` still has all three steps (tests use it).
- Names/IDs: TrOCR (students/trocr.py, microsoft/trocr-base-handwritten, CPU, greedy decoding,
  ~2 s per test; downloaded to the HF cache on first use) ALWAYS reads first: TrocrReader has the
  reader interface of matching.match_student (read_name / read_student_id / pick_name = -1, no
  tiebreak). trocr.clean_field erases printed lines and crops to the ink; clean_name drops labels,
  digits and box-edge marks. identify.read_student: TrOCR match accepted if sure, else the LLM
  (if "Use the LLM when unsure" = NAMES_CONFIG["llm_fallback"] and LLMClient.reachable()), else
  the human. matching.roster_number: only mostly-digit roster IDs count as student numbers
  (logins like "jsmith2" never contradict a handwritten ID). Real data: 99/108 right first
  match, 76 auto-accepted, 0 wrong. Tests disable TrOCR (conftest autouse `_no_trocr`; mark
  `@pytest.mark.trocr` to use it).
- Structured rubrics (models.Rubric / RubricItem): {"max_points", "scoring": "positive" (start
  at 0, checked items add) | "negative" (start at max, checked items take off), "expected_answer",
  "items": [{"id", "points", "description"}]}; score clamped to 0..max. AnswerRegion.rubric is
  always a Rubric (assigning text/dict/list converts it, parse_rubric_text reads old text
  rubrics); Rubric.as_text() is what the LLM grader and Review show (same text as the old
  format_rubric for add-up rubrics); an empty Rubric is falsy. generate_rubric returns a Rubric.
  Editor: ui/rubric_editor.RubricEditor (scoring, expected answer, items: add, delete, drag the
  handle or up/down to reorder; the scoring combo reads "Deduct (from <points>)"; a rubric without
  items starts with the scoring picked last: rubric_editor.last_scoring = UI_CONFIG["rubric_scoring"],
  saved in settings.json; conftest autouse `_rubric_scoring_add_up`). Answer keys v2: "rubric": {"scoring", "items": [{"points",
  "description"}]} (v1 lists of {"points", "criterion"} still read); the skill documents it.
- Migration (db._legacy_regions, on first connect): every template's text rubrics are saved as
  items with stable ids and every region gets its uid; the whole DB is copied first to
  ~/.lmgrader/lmgrader-backup-before-structured-rubrics.db (never overwritten). Grades untouched.
- Region order: each region has a stable `uid` (legacy: "r<index>"). grades.region_id is still
  the index; db.save_template (_follow_regions) remaps grades when regions are reordered and
  deletes the grades (and hand groups) of deleted regions. Regions list: drag, up/down, "Sort by
  number"; refused while the AI grades that version; deleting a graded question asks first.
- Hand grading (grading/hand.py + ui/hand_grade_view.py), per version + question: opening a
  question the first time asks its category (ui/group_setup.ask_kind, preselects multiple choice
  when the answer box has printed bubbles): "blank" = blank answers only (grading/blank_cnn.py:
  NumPy port of Blank_cnn/cnn_blank_neg.keras, weights in assets/blank_cnn.npz, regenerate with
  scripts/convert_blank_cnn.py; p(not blank) < 0.5 = blank; the human unticks non-blanks and the
  rest become the Blank group), "short" = blanks + answers with the same TrOCR reading grouped,
  "choice" = grouped by the marked choice (see Suggested groups). Then Group (grid of answer crops,
  multi-select, groups panel; Suggest groups… re-runs on the answers not grouped/graded yet, e.g.
  late scans), Confirm groups (stage "confirm": every group with members as a header + FitGrid, a
  grid as tall as its answers, the page scrolls; selected answers -> Move selected to ungrouped =
  remove_from_hand_groups; Groups are right checks every ⚑ group, then Grade) and Grade (queue: groups first, then ungrouped answers; rubric items as checkboxes,
  at hand_grade_view.RUBRIC_PT = 12pt (app text is 10pt; formulas drawn at that size) in a wider
  column (splitter 180/560/500), keys 1-9, comment, Ctrl+Enter = save & next, Edit rubric… dialog saves to the template). Tables
  hand_questions (stage, kind, choices_json), hand_groups (+ items/comment, auto_key, checked),
  hand_group_members (a test is in at most one group per question). A hand grade = grades row with human_graded=1, source='hand',
  rubric_items_json, comment, hand_group_id; feedback = applied items + comment. Crops in
  hand_crops/ (key includes the rect); pages aligned on demand (db.set_aligned_page, column-only
  writes). session.refresh_status: a ready test with every question graded becomes graded (and
  back); confirm_student does the same.
- Suggested groups (grading/autogroup.py, no LLM): saved as hand groups with auto_key ("blank",
  "choice:A+C", "text:x=2") and checked=0 (⚑ in the UI) until Looks right or the group is graded;
  answers already grouped or hand graded are never moved; a re-run adds new answers to the group
  with the same key (which must be checked again); unclear answers stay ungrouped. Multiple choice
  (grading/marks.py, OpenCV OMR: each scan overlaid on the blank exam): choice boxes found on the blank page
  (hollow bubbles of one size + their label on the right, else the separate printed answers;
  padded, neighbors meet halfway), checked once in group_setup.ChoiceEditor (labels A, B… or
  renamed; page pixels in hand_questions.choices_json). Per test: added ink = scan ink minus
  template ink dilated 3 px; score = pixels / box height²; marked = score >= max(MIN_MARK 0.03,
  half the top score); a choice between 1/4 and 1/2 of the top = unsure; none marked = Blank;
  several = "A + C". Real data (T1/T2 true/false bubbles, 474 answers): 465 single choices (every
  one checked by eye right; the stored AI transcriptions were wrong on ~1 in 6), 2 blank, 7
  crossed-out answers left to the human. Short answer: marks.student_ink (template ink removed)
  → TrOCR; same normalize_answer (case, spaces, outer punctuation), confidence >= 0.5, 2+ answers.
- LLM never touches a human grade on its own: Grader.grade_submission skips questions with a
  human grade, checked again right before asking the LLM and before saving (a hand grade saved
  meanwhile wins). Regrade… asks whether to keep hand-graded questions; Retry AI grade in Review
  asks before replacing a human grade.
- No-LLM mode: Settings → "Use an LLM" (LLM_CONFIG["enabled"], config.llm_enabled()), OFF by
  default (a saved settings.json value wins; tests turn it on: conftest autouse `_llm_on`). Off:
  LLMClient.from_config / for_grading raise LLMDisabled (safety net), MainWindow.apply_llm_mode
  hides the LLM Grade tab and stops running LLM jobs, the template editor hides every LLM action,
  the Question draw mode and the question areas (hand grading only needs the answer box; areas kept)
  plus the AI-only settings (instructions, strictness, columns, notes; steps "Draw answer boxes →
  Set points → Write rubrics → Check rubrics", question text optional), Scan & Names hides "Use
  the LLM when unsure" and reads names with TrOCR only, Review hides Retry AI grade.
- Upload: "Add files…" loads in a Worker (scan_view.job_import; image_input.import_pages reports
  progress per PDF page and can stop; stopping deletes the pages written, nothing is added) with
  a progress bar "Loading x.pdf… page n of N" + Stop. Names are never read automatically: new
  tests stay "Not read yet" until the "Read names" banner button (ScanView.read_names; also after
  a crash). Hand Grade points to it when a version has no read tests.
- Courses (db "Courses" section, ui/courses_dialog.py, sidebar picker in MainWindow): each course has
  its own roster (students.course_id) and exams (templates/submissions.course_id; an exam is
  (course, exam name), so two sections can both have "Test 1"); exam settings live in
  course_exams (the old exams table is only read once by the migration). db.current_course() is
  the course every tab shows (UI_CONFIG["course_id"]); list_students / list_templates /
  list_exams / list_versions / list_submissions_by_status / get|set_exam_* default to it
  (course_id= to pick another, all_courses=True for id lookups and crash recovery). Background
  jobs use the course of their template (Grader._context: template.course_id); MainWindow.
  switch_course refuses while any job runs (busy_job), flushes the editor + Review, resets the tab
  context; TemplateEditorView.course_changed reopens. Migration (_assign_courses): data saved
  before courses goes into one "My course" (rename it). Names matched only against the course's
  roster. Copy / move to course… (db.copy_exam: versions + rubrics + multiple choice boxes +
  settings, no scans; db.move_exam: with scans and grades; refused if the name exists there);
  Students → Move to course… (db.move_students; tests keep their student). Courses: unique names
  (case-insensitive), deleted only without students and exams (scans left by exams deleted before
  0.3 go with it, after a warning), never the last. Deleting an exam's last version
  (db.delete_template) deletes the exam's scans and grades too (editor warns with the count;
  refused while a job runs); every deleted version takes its hand groups. Canvas targets keyed
  "<course id>:<exam>" (plain exam name still read).
- Tabs share the exam + version: views implement context() -> (exam, template_id) and
  set_context(exam, tid) (applied by their next refresh; the editor loads the version at once);
  MainWindow._on_nav remembers the context of the tab it leaves (a None version keeps the one
  remembered for that exam). Scan & Names / LLM Grade only take the exam (their Version picker
  forces detection, it stays as chosen); Review gives the open test's version and keeps "All
  exams" if the user picked it; Return takes the exam. Hand Grade keeps the open question only
  within the same version.

- Version 0.3.0 adds (owner's list, 2026-11-07):
  - CSV with picked columns (csvtable.py: Table keeps every row/cell, the delimiter , ; tab and the
    encoding (UTF-8/BOM, else cp1252); write_table falls back to UTF-8+BOM rather than writing "?";
    NAME_LAYOUTS one column "First Last" / "Last, First" or two columns "First name, Last name" /
    "Last name, First name" (row_name(col, col2): the two pickers in the layout's order; guessed
    from the order of the first/last name headers); guess_column /
    guess_name_columns; "Points Possible" rows skipped). Students → Import CSV…
    (ui/csv_dialogs.ImportStudentsDialog: preview, name layout + column(s), ID and email columns,
    defaults guessed: first column = names; other columns ignored; students_from_table) adds only
    students not in the course yet. Return → Write grades into a CSV… (GradesCsvDialog +
    write_grades_csv: the teacher's own CSV, e.g. a Canvas gradebook export; grade column guessed
    from the exam name; returns/grades_csv.fill_grades matches rows by ID/email, then name (any word
    order), then a close name (token_sort_ratio >= 90, clear winner); a student with two graded tests
    is left empty; a summary (close matches, unmatched rows/tests, replaced cells) is confirmed, then
    a NEW file is saved; the original is never changed).
  - Email is off for now: config.EMAIL_ENABLED = False hides the Return tab's email card, Email
    column and email preview, and Settings' Email (SMTP) card (code kept). Return = Canvas upload,
    Grades into your CSV, Feedback PDFs (returns/lms.export_pdfs: one PDF per student in a folder,
    named by the pattern, logged as channel 'pdf'). The LMS ZIP card is gone (lms.export_lms stays).
  - OpenRouter is off for now: config.OPENROUTER_ENABLED = False (LLMClient.for_grading always
    uses the local server, even if settings.json says grading_provider "openrouter"; Settings
    hides "Grading model"). Local LLM or none.
  - Hand Grade: every question has a Blank group (hand.ensure_blank_group when answers load; db
    never auto-deletes an empty is_blank group; "Empty the Blank group" instead of Dissolve; empty
    groups are not in the grading queue and never ⚑). Add selection to group is a menu of the
    groups (Blank first); a click on a group in the list with answers selected adds them to it
    (HandGradeView.eventFilter; the grid keeps its filter), without a selection it shows the group. The Group grid is MultiSelection: a click toggles an answer as Ctrl-click
    would (drag a box too; Select none / Esc). Change question type… (actions bar): warning that
    grading starts over and can't be undone, then the category dialog; only after a category is
    picked hand.start_over deletes the question's groups and its source='hand' grades (Review/AI
    grades stay) and setup runs again. Save & next on a fully graded question opens the next
    question (HandGradeView._next_question; the last one stays).
- Version 0.3.141592 adds, Hand Grade (2026-11-08): stages 1 Blanks → 2 Group answers → 3 Confirm groups →
  4 Grade (STAGES; stage n = page n). Regrade from scratch… (actions bar): warning with the counts,
  then hand.start_over and setup again with the saved category and choices (not asked). Dissolve
  group / Empty the Blank group = hand.dissolve_group: the members' source='hand' grades are deleted
  (hand.ungrade; asked first when there are any) and the Blank group forgets its items/comment.
  Taking answers out of a group (Take selection out / Move selected to ungrouped) keeps their grades.
- Version 0.3.1 adds (2026-11-08): Redraw box and no question areas without an LLM (see Exams &
  Templates / No-LLM mode); generate_rubric follows the question's notes for the grader; rubric
  scoring remembered + "Deduct (from <points>)" (see Structured rubrics); CSV two-column names in
  either order (see CSV above); Names table colors: red = nobody on the roster matched (status
  "No match") or two tests on one student, yellow = a suggested student not confirmed yet (LLM
  Grade: "unassigned" red, unconfirmed yellow). Hand Grade's question-type dialog shows the
  question zoomed out on the blank page (group_setup.question_view: full page width, 2× the box
  height or 1/8 page above it, the question area if any; the answer box outlined).

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
  __init__.py, __main__.py, app.py, config.py, db.py, models.py, csvtable.py [CSV with picked columns]
  scanner/  (base, wia, twain, sane, image_input [import_pages: images + every PDF page])
  template/ (alignment [align_with_score], template [duplicate_as_version], layout [box refinement])
  students/ (import_csv [students_from_table: picked columns], matching [fuzzy + LLM tiebreak], trocr [local handwriting reader])
  llm/      (client: detect_layout, extract_question, generate_rubric, grade_answer, read_name, pick_name)
  grading/  (grader [multi-page, resumable], identify, session [split/identify/confirm/grade/recover], back_page,
             hand [hand grading], blank_cnn [blank-answer CNN, NumPy], marks [multiple choice OMR],
             autogroup [suggested answer groups])
  export/   (feedback: one-page summary with all feedback [_summary_pages], scans with red box + score, export_submission_pdf, export_exam)
  returns/  (items, mailer [SMTP send_batch/send_test; off: EMAIL_ENABLED], lms [generic/canvas/moodle, export_pdfs],
             grades_csv [grades into the teacher's CSV],
             canvas [REST API: match_students, upload_batch])
  ui/       (main_window, theme, widgets [PageViewer/PageCanvas/Worker], students_view, hand_grade_view,
             group_setup [hand grading category + multiple choice boxes dialogs], courses_dialog,
             csv_dialogs [CSV column pickers: roster import, grades CSV],
             rubric_editor,
             template_editor, scan_view, review_view, return_view, settings_view,
             help_view [in-app help per tab], assets/)
tests/      (conftest, test_* per area, screenshots/, scratch/, leftover_runs/, _last_run/)
```

## Data model
- `courses` (id, name); students, templates and submissions carry `course_id`; exam settings
  (instructions, strictness) in `course_exams` (course_id, exam_name).
- A `templates` row is one *version* of an exam (`course_id`, `exam_name`, `version_label`), with
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
   refinement); question area -> LaTeX (`extract_question`); LaTeX + points + exam instructions +
   the question's notes for the grader -> rubric (`generate_rubric`: the prompt says the
   instructions/notes win over its default rules, e.g. true/false all-or-nothing).
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
  Redraw box (TemplateEditorView.redraw_box): the selected question's answer box is hidden and
  the next drag, on any page, becomes its new box; points, LaTeX, rubric, uid and grades stay (a
  new page drops its question area). Esc, Redraw box again, another question or mode cancels.
- Students: CSV import with picked columns (dedup) + manual entry + search.
- Scan & Grade: Upload (split into tests, leftover pages kept, select all + delete) -> Names (name crops, roster combo,
  only-needs-me filter; the roster combo offers only students not on another test) -> Grade (stop/resume; Regrade… = session.reset_for_regrade on the
  selected / all graded tests: AI grades deleted, hand-graded kept unless unticked, back_reviewed
  cleared, then graded again). All state from DB; recovers after crashes.
- Review: graded/partial submissions, page buttons, editable scores and feedback, next flagged
  (next flagged question of the open test, then the next flagged test), show/hide rubric with rendered LaTeX
  on every question (ui/mathtext), Retry AI grade per question (Grader.regrade_region), class average of the exam in the test header,
  feedback preview, feedback PDF export. Question chips pinned above the cards (⚠ needs a human,
  ✓ graded by a person, plain = AI; click = jump; widgets.FlowLayout wraps them). Keys (KEYS_* in
  review_view, window-wide while Review is shown): Alt+↑/↓ question, Alt+←/→ test, Ctrl+Enter accept
  the flagged suggestion and go to the next flagged question, F3 next flagged; jumps focus the score
  box (tests/test_review_keys.py).
- Return (email hidden while EMAIL_ENABLED is False): Canvas upload, grades into the teacher's CSV,
  feedback PDFs to a folder; with email on: preview, send test to me, send to selected/all-not-sent
  with confirmation and log.
  Canvas feedback (returns/canvas.py, stdlib urllib): Connect with a personal access token, pick
  course + assignment (remembered per exam in CANVAS_CONFIG["targets"]); LMGrader students are
  matched to the Canvas roster by SIS id/login, email, exact name, then a clearly winning fuzzy
  name (two tests on one Canvas student -> both held); each PDF is uploaded as a submission-comment
  file. Grades are never touched. Logged as channel 'canvas', recipient 'course/assignment'
  (db.was_returned skips re-uploads); a 401 stops the batch and forgets the token.
- Settings: "Grade answers with" Local | OpenRouter (OFF for now: config.OPENROUTER_ENABLED = False ->
  for_grading is always the local server and Settings hides the choice; code kept) (LLM_CONFIG grading_provider/openrouter_model; key = OPENROUTER_API_KEY from .env or env, config.load_dotenv; LLMClient.for_grading used by grading + Review retry only; names/templates stay local). LLM + SMTP, persisted to ~/.lmgrader/settings.json (SMTP password and Canvas token
  never saved: keyring if installed, else session memory).

## Future work
- Hand grading: re-score hand grades automatically when a rubric item's points change (today:
  open the answer/group and save again).

## Run
```
pip install -e .[dev]
python -m lmgrader
python -m pytest -q tests
```

## Notes
- Every LLM request (LLMClient._chat) is retried until it fails 3 times within 60 s (3 s between
  attempts; constants RETRY_* in llm/client.py); Stop/abort ends retries at once.
- Each problem's AI grading has a 60 s wall-clock limit, retries included (client.GRADE_TIMEOUT_S,
  LLMClient.time_budget); on expiry the grade is saved as llm_error -> human review.
- Window reference retained in app.main_window to avoid GC.
- app.NoWheelOnEntries (app-wide event filter): the mouse wheel only scrolls. It never changes a
  closed QComboBox, a number box (QAbstractSpinBox: scores, points…) or a slider (scroll bars
  excluded); the wheel scrolls the page behind it instead (tests/test_wheel.py).
- Help (ui/help_view.py): the sidebar's "? Help" button / F1 opens a non-modal HelpDialog on the
  topic of the tab on screen (MainWindow.tab_topics, one per tab: a new tab needs a topic, see
  tests/test_help.py); it follows the tabs while open. Topics: Getting started, one per tab (Settings ends with a short "Connect LM Studio" how-to),
  Keyboard shortcuts (Review's from review_view.KEYS_*). help_html(key, llm_on): with "Use an
  LLM" off there is no LLM Grade topic and no AI/question-area instructions. Keep the texts in
  step with the UI (button names exactly as on screen) and the README Workflow.
- DB stored in ~/.lmgrader/lmgrader.db (override with LMGRADER_DATA).
- Data dir subfolders: templates/, scans/, aligned/, extracted/, name_crops/, returns/.
- Image I/O uses imdecode/imencode so non-ASCII Windows paths work.
- Email caveats: Gmail needs an App Password; Outlook/M365 basic SMTP auth is often disabled.
