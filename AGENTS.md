# AGENTS.md

## Synchronization Instruction
AGENTS.md and CLAUDE.md hold the same text: after every modification, copy it to the other file.
The details live in docs/DEVELOPER_NOTES.md: read the section for the area you are changing first,
and update it in the same change when behavior changes.

## Development Workflow
Use subagents sparingly (owner's preference, updated): do the work directly; spawn agents only for
large, clearly independent pieces. Run `python -m pytest -q tests --ignore=tests/test_live_llm.py`
after changes; UI changes get a screenshot in tests/screenshots/ that is checked visually.

## Project: LMGrader
FOSS local app for grading physical test templates (PyQt6 desktop app; useful with or without a
local LLM in LM Studio).
Current version: 0.5 (pyproject.toml, config.APP_VERSION, lmgrader.__version__; the vX.Y
headings in docs/DEVELOPER_NOTES.md are feature milestones, not release numbers).
Tests: 311 offline passing (4 skipped: live LLM); live tests need LMGRADER_LIVE=1
(+ LMGRADER_REAL_LIVE=1 for the private scans).
Tabs: Exams & Templates, Students, Scan & Names, Hand Grade, LLM Grade, Review, Return, Settings.

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
  template/ (alignment [align_with_score], template [duplicate_as_version], layout [box refinement],
             answer_key [JSON key from the .tex; `python -m lmgrader.template.answer_key f.json|f.tex` checks],
             tex_import [questions/solutions parsed straight from a .tex])
  assets/answer_key_skill/SKILL.md  (Claude Code skill = the answer-key format spec; packaged)
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
docs/DEVELOPER_NOTES.md  (detailed behavior, data model, LLM flow, per-feature notes)
```

## Run
```
pip install -e .[dev]
python -m lmgrader
python -m pytest -q tests
```

## Rules that are easy to break
- All state is in SQLite (~/.lmgrader/lmgrader.db, override with LMGRADER_DATA) and saved as work
  happens; everything must survive a crash. Schema changes are additive migrations (db.MIGRATIONS).
- The LLM never touches a human grade on its own (Grader.grade_submission checks right before
  asking the LLM and before saving); Regrade… and Retry AI grade ask before replacing one.
- "Use an LLM" is OFF by default (config.llm_enabled()): every LLM action must be hidden or guarded
  when it is off (LLMClient.from_config / for_grading raise LLMDisabled). Email
  (config.EMAIL_ENABLED) and OpenRouter (config.OPENROUTER_ENABLED) are switched off; code kept.
- Everything belongs to a course: list_students / list_templates / list_exams / list_versions /
  list_submissions_by_status / get|set_exam_* default to db.current_course(); background jobs use
  their template's course_id.
- grades.region_id is the region's index; regions also have a stable `uid`, and db.save_template
  remaps grades when regions are reordered and deletes those of deleted regions.
- UI: never hard-code colors (theme.<NAME> or a QSS objectName); every tab needs a help topic
  (tests/test_help.py); help texts use button names exactly as on screen.
- Image I/O uses imdecode/imencode (non-ASCII Windows paths); images sent to the model go through
  llm.client.prepare_crop (larger ones crashed LM Studio's Vulkan engine).
- Tests: conftest autouse fixtures `_no_trocr` (mark `@pytest.mark.trocr` to use TrOCR), `_llm_on`,
  `_rubric_scoring_add_up`.

## Where the details are (docs/DEVELOPER_NOTES.md)
- Tabs and their shared exam/version context, Upload, Names/IDs (TrOCR first, then the LLM, then
  the human), structured rubrics + their migration, region order, hand grading, suggested groups
  and multiple-choice OMR, no-LLM mode, courses, CSV import/export: "v0.5: useful without an LLM"
  (incl. the "Version 0.3.x adds" items).
- Tables and columns: "Data model".
- Layout detection, identification, grading prompts, strictness, sub-questions and next-part
  notes, human-review rules: "LLM flow".
- Answer keys and .tex import, template editor, Students, Scan & Grade, Review, Return (Canvas,
  PDFs, email), Settings, light/dark theme: "Features implemented".
- LLM retries and timeouts, mouse-wheel filter, Help dialog, data folders, email caveats: "Notes".
