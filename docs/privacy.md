# Privacy and FERPA

LMGrader is built so that student education records stay under the instructor's and the
institution's control. FERPA compliance depends on how a tool is used, not only on the tool.
These are the properties that make LMGrader suitable for FERPA-covered work.

## 1. The AI runs on your own computer
- LMGrader works without any AI. *Use an LLM* in Settings is off until you turn it on, and while
  it is off LMGrader never contacts an AI.
- When it is on, all AI work (reading questions, writing rubrics, reading hard names, grading
  answers) is done by a **local model** in LM Studio. The default address is
  `http://localhost:1234/v1`, which is this computer.
- **No scan, answer, name, ID or grade is sent to OpenAI, Anthropic, Google or any other cloud AI
  service.** No third party receives student records, so no vendor agreement or data-sharing
  contract is needed for the AI part.
- Handwritten names and IDs are read **on this computer** by a handwriting model (TrOCR). It is
  downloaded once from Hugging Face the first time names are read. That download sends no student
  data.
- LMGrader works offline. Only that one-time download and the optional return step (Canvas, or
  email, see below) connect to the internet.

> **Keep it local.** LM Studio's address in Settings can be changed to any OpenAI-compatible
> server. Pointing it at a cloud provider would send scan images off your machine, and these
> guarantees would no longer hold. Leave it on `localhost` (or a server your institution runs).

## 2. Grading never sees who the student is
Every LLM request is limited to the smallest crop of the page it needs:

| Step | What the model sees | What it never sees |
|---|---|---|
| Template setup | The **blank** exam (no student data) | — |
| Grading a question | Only the crop of that question's **answer box**, plus the question, rubric and your grading instructions | Student name, student ID, email, roster, other students' work |
| Work on the back of a page | That back page, plus the question and rubric | Student name, student ID, email, roster |
| Reading the name / ID field (only when the local reader is unsure) | Only the crop of the **name field** (or ID field), to transcribe the handwriting | Answers, scores, feedback |

- **Grading is anonymous.** An answer is graded without the name or ID of the student who wrote
  it, so a student's identity can't affect their score.
- **Names are used only for transcription.** The name field crop is sent only to turn handwriting
  into text, and that request contains no answers or grades. Matching that text to your roster is
  done **locally by LMGrader** (fuzzy string matching), not by the model.
  - If the handwriting is ambiguous, the model is shown the same name crop with up to 5 roster
    names and asked which one it is. It gets names only: no IDs, emails or grades.
- Student IDs and emails from your roster are never put in a prompt. A handwritten ID is
  transcribed from its crop and compared with the roster on your machine.

## 3. Data stays on your machine
- Scans, the roster, grades and feedback are stored in a local SQLite database and folders under
  `%USERPROFILE%\.lmgrader` on Windows, `~/.lmgrader` elsewhere (or the folder in the
  `LMGRADER_DATA` environment variable). Nothing is uploaded to an LMGrader server; there isn't
  one.
- The Canvas access token (and the SMTP password) are never written to disk in plain text. They
  live in the OS keyring (Windows Credential Manager in the Windows build; from source, if the
  `keyring` package is installed) or only in memory until LMGrader closes.
- Secure the computer as you would any device that holds grades: an account password,
  full-disk encryption (BitLocker / FileVault), and backups kept in institution-approved storage.

## 4. Returning tests uses your institution's own systems
- **Canvas:** feedback PDFs go straight to your course in your institution's Canvas, with your
  own access token. Only students enrolled in the course can see them, and each student sees
  only their own. Canvas grades are not changed.
- **Email** (turned off in this version): sent from your institutional account (e.g. Google
  Workspace or Microsoft 365) to each student's address on the roster. Each message goes to one
  student and contains only that student's test.
- Every return is logged, and LMGrader refuses to send the same test twice by accident, which
  prevents mix-ups.
