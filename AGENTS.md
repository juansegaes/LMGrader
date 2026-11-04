# AGENTS.md

## Synchronization Instruction
Keep this document synchronized with CLAUDE.md after every modification.

## Development Workflow
Use subagents sparingly (owner's preference, updated): do the work directly; spawn agents only for
large, clearly independent pieces. Run `python -m pytest -q tests --ignore=tests/test_live_llm.py`
after changes; UI changes get a screenshot in tests/screenshots/ that is checked visually.

## Project: LMGrader
FOSS local alternative to Gradescope for grading physical test templates.

## Status
v0.3: multi-page templates with LLM auto-setup (region detection + LaTeX), LLM rubrics,
persisted 3-step grading sessions (upload/split -> names -> grade, crash-safe and resumable),
feedback PDFs (page 1: every question's score + feedback, shrunk to fit one page; scans: red box + score only), returning tests by email (SMTP), Canvas API (feedback PDF as submission comment) or LMS export.
Also: professor grading instructions (per exam + per question), confidence/continuation-based
human grading, fixed light palette (dialogs no longer follow Windows dark mode).
v0.4: Stop buttons on every LLM job, auto-setup pauses for box confirmation before LaTeX, blank
(back) pages in the scan layout, optional column count, student-ID field (ID-first matching),
back-page work routing, move/resize of existing boxes, sub-question context (main question stem).
Real data (private fixtures): 20/20 students identified automatically.
Tests: 192 offline passing (`python -m pytest -q tests --ignore=tests/test_live_llm.py`);
live tests need LMGRADER_LIVE=1 (+ LMGRADER_REAL_LIVE=1 for the private scans).

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
  template/ (alignment [align_with_score], template [duplicate_as_version], layout [box refinement],
             answer_key [JSON key from the .tex; `python -m lmgrader.template.answer_key f.json|f.tex` checks],
             tex_import [questions/solutions parsed straight from a .tex])
  assets/answer_key_skill/SKILL.md  (Claude Code skill = the answer-key format spec; packaged)
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
  `pages_json` (multi-page), `name_rect_json` + `name_page`, `id_rect_json` + `id_page`,
  `scan_layout_json` (template page index or -1 = blank/back page), `columns`, `stems_json`,
  optional `version_rect_json` + `version_page` (where the exam states its version).
- Max points: every new question (auto-detected or drawn) starts at config.DEFAULT_POINTS = 1;
  printed points are ignored; the professor changes it per question ("Max points").
- Region (in `regions_json`): problem, group, points, page, rect (answer box), question_rect,
  question_text (fixed LaTeX), rubric (fixed), grading_notes, answer_key (professor's expected
  answer from an imported key; generate_rubric(answer_key=...) uses it instead of solving).
- Sub-questions: a label like "1a"/"Q1 (b)" (layout.parse_label) makes the region a part of
  question 1. Its shared stem lives once in `TestTemplate.stems["1"]`; question_text holds only
  the part. Question boxes: the first part's box holds the stem + part (a); boxes of later parts
  hold only their own part (refine_layout no longer stretches them up to the stem).
- Submissions: `status` new -> identified -> ready -> grading -> graded (or error), `exam_name`,
  `scan_pages_json`/`back_pages_json`/`aligned_pages_json`, `name_read`, `name_match_score`,
  `name_confirmed`, `source`, `version_read` (field: X / layout / manual), `version_confirmed`
  (human-picked version; re-reading never changes it). Grades (one per region, saved immediately) store `transcription`
  and `next_part_notes`.
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
   The question sent is template.question_context(t, i): for part (b) it is the main question's
   stem + earlier parts (context only) + "Part to grade: (b)"; rubrics use the same context
   (RUBRIC_PARTS_NOTE: criteria only for the part to grade). Rubric rule: true/false questions get
   no partial credit (one all-or-nothing criterion worth all the points).
   Next-part notes: parts are graded in part order (grader.grading_order). A part with later parts
   also returns `next_part_notes` (GRADE_SCHEMA_NEXT: the student's result, right or wrong, and the
   correct one); it is saved on the grade (grades.next_part_notes) and every later part receives
   the earlier parts' notes (prior_part_notes) with a "don't penalize a carried-forward error
   twice" rule. Shown in Review as "Note for later parts".
   Only the FIRST part of a question (editor.first_parts) is extracted with `extract_part` ->
   {stem, part}; later parts use extract_question(part_of=(num, letter)) and are never asked for
   a stem. A stem is never overwritten once set. Question extraction is PLAIN TEXT, not JSON
   (extract_part replies "MAIN QUESTION:\n...\nPART (a):\n..."; split_part_reply parses it, strips
   NONE / "(b)" labels, never stores raw JSON). Looping output (is_repetitive) is retried once
   strictly, then cut (cut_repetition); transcription replies are capped at 1200 tokens.
   LaTeX in JSON replies (rubrics, grades): models write single backslashes, which JSON decodes as
   control chars (\begin -> backspace+"egin") or rejects (\sqrt); _extract_json runs
   _fix_latex_escapes first. template.repair_part_json fixes old text on load (raw JSON, damaged
   LaTeX via repair_latex_text, looping text cleared for re-extraction). Images sent to the model are capped by
   llm.client.prepare_crop (<= 1600 px side, <= MAX_IMAGE_PIXELS): larger ones crashed LM
   Studio's Vulkan engine.
4. Human grading rules (grader.apply_review_rules, independent of the model's own flag):
   continuation phrases in the transcription (config.CONTINUATION_PATTERNS, EN+ES) or the model's
   continues_elsewhere flag, confidence < LLM_CONFIG["review_confidence"], ink on back page, poor
   alignment, missing page, LLM errors -> "Needs human grade". Settings option "full marks"
   (full_marks_skip_review, off by default; full_marks_min_confidence, default 0.3): a full-marks
   grade skips only the low-confidence rule unless confidence < that floor (grader.review_settings).
   "full_marks_ignore_back" (ON by default): a full-marks grade gets no work_on_back flags; when
   every back-flagged question got full marks the submission's back_reviewed is set automatically,
   so the test isn't flagged for its back page. "See back" phrases (work_continues_elsewhere) still flag.
   Saving Settings runs grader.reapply_full_marks_rules: both options also apply to grades already
   saved (strips low_confidence/work_on_back reasons from full-marks, non-human grades; other
   reasons stay). Review shows the AI score as a
   suggestion; accepting/editing sets human_graded. Feedback PDFs print "Pending review" for
   unresolved grades.

## Features implemented
- Answer keys from LaTeX: the owner writes exams in .tex with Claude Code. Claude (skill
  `lmgrader-answer-key`, installed from the editor's "How to make one" to
  ~/.claude/skills/lmgrader-answer-key/) writes `<exam>.lmgrader.json` (format
  "lmgrader-answer-key" v1: questions/parts with label, stem, text, points, answer, type
  open|true_false|multiple_choice, rubric, notes; exam-wide instructions). Editor "Import answer
  key…" matches items to boxes by label (answer_key.match_key: "1a" == "Q1 (a)"), previews a
  summary (checkbox "Import the rubrics from the key", apply_answer_key(rubrics=...)), then fills
  LaTeX, stems, points, answers, notes; rubrics come from the key when ticked (true_false /
  multiple_choice: all-or-nothing), otherwise "Generate rubrics" writes them from the key's answer.
  The skill writes a rubric for every open question and verifies computable answers with Python
  (SymPy/NumPy; asks before installing).
- Import .tex (no LLM, template/tex_import.py: parse_tex/load_tex -> the same AnswerKey, same
  import dialog): exam class (\question[pts], parts/\part[pts], solution* envs -> answer,
  choices/checkboxes with \CorrectChoice -> multiple_choice) or the largest top-level enumerate
  (\item = question, nested enumerate = parts, "(4 pts)" points). Inlines \input/\include,
  strips comments, expands \newcommand/\def/\DeclareMathOperator (never \question/\part/\item),
  drops layout (\vspace, \fillin, \makeemptybox, ...), figures -> "[Figure]", "True or false" /
  "Verdadero o falso" -> true_false. AnswerKey.warnings (no solutions, whole-question points,
  subparts) are shown in the dialog. The CLI checker also reads .tex. Boxes still come from detection/drawing. test_skill_example_is_valid keeps the
  SKILL.md example importable.
- Exams & Templates: multi-page exams with versions, "Auto-setup with LLM", page selector,
  manual drawing (answer, question, name, ID, version fields), LaTeX + rubric generation, guided steps, autosave.
  Answer-key re-import: if the version has notes/answers the new key lacks, the editor asks
  Replace (apply_answer_key(replace=True) clears them) / Keep / Cancel. Box editing on the canvas
  (PageCanvas.active_key): click a box (answer/question/name/ID) to select, drag handles to resize,
  drag inside to move, arrows nudge (Shift x10), Shift+drag draws over boxes. "Main question"
  panel for parts. New versions always start with the outline of an existing version (boxes,
  labels, points, notes, name/ID, layout; duplicate_as_version(keep_text=False)); LaTeX/rubrics are
  copied only if the user says the questions are the same. "Copy boxes from another version"
  fills an existing version. Rubrics: per-question "Redo from scratch", "Redo all rubrics from
  scratch" (run_ai(..., redo=True)).
- Students: CSV import (dedup) + manual entry + search.
- Scan & Grade: Upload (split into tests, leftover pages kept, select all + delete) -> Names (name crops, roster combo,
  only-needs-me filter) -> Grade (stop/resume; Regrade… = session.reset_for_regrade on the
  selected / all graded tests: AI grades deleted, hand-graded kept unless unticked, back_reviewed
  cleared, then graded again). All state from DB; recovers after crashes.
- Review: graded/partial submissions, page buttons, editable scores and feedback, next flagged,
  feedback preview, feedback PDF export. "Next flagged" scrolls to the next question of the open
  test needing a human (below the view / after the last jump), then moves to the next flagged test
  and scrolls to its first one. Every card has Show/Hide rubric (open by default when flagged) with LaTeX drawn by
  ui/mathtext.latex_html (mathtext PNGs as data-URI <img>, drawn at display size), and "Retry AI grade"
  (ReviewView.retry_grade -> Grader.regrade_region in a Worker: same inputs as the first run via
  Grader._grade_region, replaces the saved grade in place; confirms first if human-graded; on an
  LLM error the old grade is kept). The detail
  header shows the class average (ReviewView.class_average: fully graded tests of the same exam,
  all versions, pending AI suggestions included, ignores Show/search filters; only % when versions
  have different totals); it updates as scores are edited. Every question card has "Full page" (aligned front page,
  answer box outlined and scrolled into view) and "Back of page" (back of that sheet) buttons;
  ReviewView.view_page shows them in one reused non-modal window (+ "Open in image viewer").
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
- Light/dark mode: sidebar toggle + Settings → Appearance (Light / Dark / Follow Windows), saved as
  settings.json "ui.theme". theme.LIGHT / theme.DARK color sets; apply_theme() rewrites the module
  colors (theme.ACCENT, ...), palette and QSS at runtime. Never hard-code colors in views: use
  theme.<NAME> (read at build/refresh time) or an objectName styled in the QSS.

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
