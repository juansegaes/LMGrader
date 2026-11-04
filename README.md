# LMGrader

FOSS local alternative to Gradescope for grading physical test templates.

## Features
- Desktop GUI (PyQt6) for scanning tests and defining answer boxes on a blank template.
- Exams with multiple versions (A, B, C…); scans are matched to the right version automatically.
- Two-stage LLM setup per question, done once and then fixed:
  1. the printed question is transcribed to LaTeX,
  2. a rubric (expected answer + point criteria) is generated from that LaTeX and the max points.
  Both are editable, and every student is graded against the same text.
- Name field: the handwritten name is read by the LLM and matched to the roster with fuzzy
  matching (rapidfuzz), with an LLM tiebreak for ambiguous cases.
- Batch grading queue (multi-page PDFs, double-sided scans) with a local LLM via LM Studio.
- Flags work continued on the back, poor alignment and unclear answers for human review.
- Review screen with score overrides and CSV export.

## Run
```
pip install -e .
python -m lmgrader
```

## Workflow
1. **Exams & Templates** – *New exam…* with a blank page. Draw answer boxes, then select each
   region and draw its *Question area*. Draw the *Name field*. Set max points, then
   *Extract all missing questions* and *Generate all missing rubrics*. Review the text.
   Add more versions with *Add version…* (regions can be copied over).
2. **Students** – import a CSV (`name, student_id, email`).
3. **Scan & Grade** – pick the exam, add scanned pages, confirm the detected student for rows
   marked *Check*, then *Grade ready pages*.
4. **Review** – fix flagged answers, adjust scores, export CSV.

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
  the internet.

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
- **Email:** sent from your institutional account (e.g. Google Workspace or Microsoft 365) to
  each student's address on the roster. Each message goes to one student and contains only
  that student's test.
- Every return is logged, and LMGrader refuses to send the same test twice by accident, which
  prevents mix-ups.

## LM Studio
Start LM Studio, load a vision-capable model (e.g. Qwen3-VL, Gemma 3), and enable the local server
(default http://localhost:1234/v1). Configure it in Settings.

## Scanning
On Windows uses WIA/TWAIN if available; otherwise use "Add files…" to load scanned images (PNG/JPG/PDF).
