# LMGrader

FOSS local app for grading physical test templates.

## Features
- Desktop GUI (PyQt6) for scanning tests and defining answer boxes on a blank template.
- Exams with multiple versions (A, B, C…); scans are matched to the right version automatically.
- Works without an LLM: names are read on your computer, and every question can be graded by
  hand: answers are grouped and each group is graded once with rubric items.
  LMGrader suggests the groups on your computer: blank answers (a small model), multiple choice by
  the bubble or answer each test marked (compared with the blank exam), and short answers that
  read the same (handwriting reader). You check every suggested group before it is graded. The LLM is off until you tick *Use an LLM* in
  Settings: until then LMGrader never contacts one (the LLM tab and AI buttons are hidden).
- Courses: each course has its own roster and its own exams; pick the course in the sidebar and
  every tab shows only that course. Two sections can give the same test: *Copy / move to
  course…* copies an exam (pages, boxes, questions, rubrics) to another course. Exams made before
  courses are in "My course" (rename it under *Manage courses…*).
- CSV files as you have them: import a roster from any CSV (e.g. a gradebook export with other
  assignments) and pick the name column(s), ID and email; write an exam's grades into a column of
  your own CSV (matched by student ID, then by name) and save it as a new file.
- Switching tabs keeps the exam and version you were working on.
- Structured rubrics: each question has rubric items (points + description), scored by adding
  up from 0 or deducting from full marks. Type them, import them from a LaTeX answer key, or let
  the LLM write them; add, delete and reorder items at any time.
- Optional LLM setup per question, done once and then fixed:
  1. the printed question is transcribed to LaTeX,
  2. rubric items (with the expected answer) are generated from that LaTeX and the max points.
  Both are editable, and every student is graded against the same text.
- Name field: the handwritten name (and student ID) is read locally with TrOCR
  (`microsoft/trocr-base-handwritten`, downloaded once, ~1.3 GB) and matched to the roster with
  fuzzy matching (rapidfuzz); the LLM is asked only when that match is unsure.
- Batch AI grading queue (multi-page PDFs, double-sided scans) with a local LLM via LM Studio;
  it never changes a question you graded by hand.
- Flags work continued on the back, poor alignment and unclear answers for human review.
- Review screen with score overrides and CSV export.

## Run
```
pip install -e .
python -m lmgrader
```
This installs PyTorch (CPU) for the local handwriting reader. On Linux, install the CPU build
first to avoid the large CUDA download: `pip install torch --index-url https://download.pytorch.org/whl/cpu`.

## Workflow
0. **Course** – pick it in the sidebar (or add it with *Manage courses…*). Everything below
   happens in that course.
1. **Exams & Templates** – *New exam…* with a blank page. Draw the answer boxes and the *Name*
   field. Set max points and the rubric items (or *Import answer key…* / *Import .tex…*). With
   an LLM: *Auto-setup with LLM* finds the boxes and reads the questions, and *Generate all missing
   rubrics* writes the rubrics; the *Question* mode marks where a question is printed, for the
   LLM. A wrong answer box: *Redraw box* (points, question and rubric stay).
   Drag questions in the Regions list (or ↑ / ↓ / *Sort by number*) to set their order.
   Add more versions with *Add version…* (regions can be copied over).
2. **Students** – *Import CSV…* with the course's roster: any CSV with a header row. Say which
   column has the names (first column by default; "First Last", "Last, First", or separate
   first and last name columns), the student ID and the email; other columns are ignored. Names
   on the scans are matched only against this course's students.
3. **Scan & Names** – pick the exam and add the scanned pages (a progress bar shows the PDF
   loading). Press **Read names** when you are ready, then confirm the detected student for rows
   that need you.
4. Grade, in either tab or both:
   - **Hand Grade** – pick a question and say what kind it is: *Blank answers only*, *Short
     answer* or *Multiple choice* (check the choice boxes LMGrader found on the blank exam once).
     Check the suggested groups (⚑: take out answers that don't belong, then *Looks right*) and
     group any others yourself (click answers to select them, click again to unselect; every
     question has a *Blank* group you can add answers to; with answers selected, a click on a group
     adds them to it; dissolving a group deletes its answers' hand grades). *Confirm groups* then
     shows every group with all its answers: click the ones that don't belong and *Move selected to
     ungrouped* (they are graded one by one), then *Groups are right*. Then check rubric items
     (keys 1-9) for each group and each remaining answer, Ctrl+Enter to save and go on; when the
     question is done a message says so, then you go to the next one or stay on this one.
     *Suggest groups…* sorts scans added later.
     *Regrade from scratch…* starts a question over with the same type, *Change question type…*
     with another one (either way its groups and hand grades are deleted, after asking).
   - **LLM Grade** – *Grade all ready*: the AI grades every question not graded by hand.
5. **Review** – fix flagged answers, adjust scores, export CSV. The chips above the questions
   jump to any question; Alt+↑/↓ moves between questions, Alt+←/→ between tests, Ctrl+Enter
   accepts the AI suggestion and goes to the next flagged question.
6. **Return** – upload each feedback PDF to Canvas, *Write grades into a CSV…* (your gradebook
   export: pick the grade column and how names are written; a new file is saved), or export the
   feedback PDFs to a folder. Email is turned off in this version.

**Help**: the *? Help* button at the bottom of the sidebar (or F1) explains the tab you are on,
with its keyboard shortcuts.

## Privacy and FERPA

LMGrader is built so that student education records stay under the instructor's and the
institution's control. FERPA compliance depends on how a tool is used, not only on the tool.
These are the properties that make LMGrader suitable for FERPA-covered work:

### 1. The AI runs on your own computer
- All AI work (reading questions, writing rubrics, reading names, grading answers) is done by a
  **local model** in LM Studio. The default address is `http://localhost:1234/v1`, which is
  this machine.
- **No scan, answer, name, ID or grade is sent to OpenAI, Anthropic, Google or any other cloud AI
  service.** No third party receives student records, so no vendor agreement or data-sharing
  contract is needed for the AI part.
- LMGrader works offline. Only the optional return step (email or Canvas, see below) connects to
  the internet, plus a one-time download of the handwriting model (TrOCR) from Hugging Face the
  first time names are read; that download sends no student data, and names are then read on
  this computer.

> **Keep it local.** LM Studio's address in Settings can be changed to any OpenAI-compatible
> server. Pointing it at a cloud provider would send scan images off your machine, and these
> guarantees would no longer hold. Leave it on `localhost` (or a server your institution runs).

### 2. Grading never sees who the student is
Every LLM request is limited to the smallest crop of the page it needs:

| Step | What the model sees | What it never sees |
|---|---|---|
| Template setup | The **blank** exam (no student data) | — |
| Grading a question | Only the crop of that question's **answer box**, plus the question, rubric and your grading instructions | Student name, student ID, email, roster, other students' work |
| Work on the back of a page | That back page, plus the question and rubric | Student name, student ID, email, roster |
| Reading the name / ID field | Only the crop of the **name field** (or ID field), to transcribe the handwriting | Answers, scores, feedback |

- **Grading is anonymous.** An answer is graded without the name or ID of the student who wrote
  it, so a student's identity can't affect their score.
- **Names are used only for transcription.** The name field crop is sent only to turn handwriting
  into text, and that request contains no answers or grades. Matching that text to your roster is
  done **locally by LMGrader** (fuzzy string matching), not by the model.
  - If the handwriting is ambiguous, the model is shown the same name crop with up to 5 roster
    names and asked which one it is. It gets names only: no IDs, emails or grades.
- Student IDs and emails from your roster are never put in a prompt. A handwritten ID is
  transcribed from its crop and compared with the roster on your machine.

### 3. Data stays on your machine
- Scans, the roster, grades and feedback are stored in a local SQLite database and folders under
  `~/.lmgrader` (or `LMGRADER_DATA`). Nothing is uploaded to an LMGrader server; there isn't one.
- The SMTP password and the Canvas access token are never written to disk in plain text: they
  live in the OS keyring (if `keyring` is installed) or only in memory until LMGrader closes.
- Secure the computer as you would any device that holds grades: an account password,
  full-disk encryption (BitLocker / FileVault), and backups kept in institution-approved storage.

### 4. Returning tests uses your institution's own systems
- **Canvas:** feedback PDFs go straight to your course in your institution's Canvas, with your
  own access token. Only students enrolled in the course can see them, and each student sees
  only their own.
- **Email** (turned off in this version): sent from your institutional account (e.g. Google Workspace or Microsoft 365) to
  each student's address on the roster. Each message goes to one student and contains only
  that student's test.
- Every return is logged, and LMGrader refuses to send the same test twice by accident, which
  prevents mix-ups.

## LM Studio
Start LM Studio, load a vision-capable model (e.g. Qwen3-VL, Gemma 3), and enable the local server
(default http://localhost:1234/v1). In Settings, tick *Use an LLM* (off by default) and
configure the server.

## Scanning
On Windows uses WIA/TWAIN if available; otherwise use "Add files…" to load scanned images (PNG/JPG/PDF).
