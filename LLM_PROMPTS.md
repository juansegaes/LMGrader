# LLM prompts used in LMGrader

Every prompt sent to the local LLM, grouped by the function that builds it.
System prompts and reusable fragments are listed first.

## Shared fragments (llm/client.py)

### `grader_system(strictness)` (system prompt)
```
You are a careful, fair teaching assistant grading handwritten exam answers.
<STRICTNESS_TEXT[level]> Never invent work that is not visible in the image. Work the student crossed out is not part of the answer. If the professor's grading instructions conflict with these guidelines or with the rubric, follow the professor's instructions.
```
`STRICTNESS_TEXT` (level 5..10):
- 10: Grade strictly according to the rubric you are given. Award partial credit only where the rubric allows it.
- 9: Follow the rubric closely. Award a criterion only when the student's work clearly shows it. Within a criterion, you may give part of its points when the work is clearly on the right track (for example, a correct method with one arithmetic slip).
- 8: Use the rubric as the standard. Take points off for real mathematical errors and missing key steps. Do not take points off for minor presentation issues (notation, an unsimplified but equivalent answer, small arithmetic slips) unless the rubric explicitly requires them. Mention those issues in the feedback.
- 7: Use the rubric as a guide to what matters. Give partial credit for a correct method even when the final answer is wrong. Accept any equivalent form of the answer. Take at most a small deduction for a minor slip that does not change the idea. Mention every mistake in the feedback, even when it costs no points.
- 6: Focus on whether the student understands the main idea. An answer that is almost correct (one small slip, notation, a missing final step) gets full or nearly full marks. Any reasonable attempt earns partial credit. Give zero only for a blank or unrelated answer. Still point out mistakes in the feedback so the student learns from them.
- 5: Be very generous. If the answer is almost correct, give full marks, and never take points off for technicalities (notation, simplification, small arithmetic slips, missing units). Give substantial partial credit for any attempt that makes sense; give zero only if the answer is blank, entirely crossed out, or unrelated to the question. When unsure between two scores, give the higher one. Still point out mistakes in the feedback so the student learns from them.

### `_instructions_block(instructions, notes)`
```
Professor's grading instructions (apply to every question):
<instructions>

Professor's notes for this question:
<notes>

```
(only non-empty parts)

### `CROSSED_OUT_NOTE`
```
Crossed-out work: students cross out what they do not want graded (struck through, scribbled over, X'd out, or boxed with a line through it). Ignore it completely: do not transcribe it, do not give or take points for it, and do not mention it in the feedback. Grade only the work that is not crossed out. If everything is crossed out, treat the answer as blank. If you cannot tell whether something is crossed out, set needs_human_review true and say so in review_reason.

```

### `PARTS_NOTE`
```
If the question lists a main question and earlier parts, they are context only: grade only the part named 'Part to grade'.

```

### `RUBRIC_PARTS_NOTE`
```
If the question lists a main question and earlier parts, use them to understand the setup (the given matrix, function, data...), but write the rubric ONLY for the part named 'Part to grade': no criteria for the earlier parts. If this part uses a result of an earlier part, the expected answer uses the correct value of that result.

```

### `_prior_notes_block(prior_notes)`
```
Notes from the graders of the earlier parts of this question (about THIS student's work):
<prior_notes>
If this part reuses a wrong result from an earlier part, that error was already penalized there: do not take points off again for it. Grade this part on whether the work is correct given the student's own earlier result (follow-through), unless the professor's instructions say otherwise, and say in the feedback that the earlier error was carried forward.

```

### `NEXT_PART_STEP`
```
6. 'next_part_notes': a short note for the grader of the LATER parts of this question, who cannot see this answer. State the student's final result for this part, whether it is correct, and if it is wrong the correct result, so later graders can tell when an error is carried forward. Empty string if there is nothing useful.

```

### `FEEDBACK_LIMIT`
```
(at most 3 sentences; say each thing once)
```

### `LOOP_RETRY_NOTE` (appended on retry)
```
IMPORTANT: a previous answer to this repeated the same sentences over and over until it was cut off. Write every field exactly once, keep 'feedback' to at most 3 sentences, never repeat a sentence, and close the JSON.
```

---

## Template setup

### `LLMClient.extract_question(image, part_of=None)` (llm/client.py)
Base prompt:
```
This image is a cropped region of a printed exam page containing one question. <part?>Transcribe the question text exactly, using LaTeX for all mathematics (inline math in $...$, display math in \[...\]). Include every given value and instruction. If there is a figure, describe it briefly in [Figure: ...]. Do NOT solve the question. Output only the LaTeX transcription, once: no preamble, no notes or comments, no code fences.
```
When `part_of=('1','b')`:
```
This crop shows part (b) of question 1. Transcribe only the text of part (b), without its label; if the main question's shared text or other parts are visible, leave them out. 
```

### `LLMClient.extract_part(image, number, letter)` (llm/client.py)
```
This image is a cropped region of a printed exam page. It shows the beginning of question <number>: the main question's shared text (the setup that applies to all of its parts, if there is one) followed by part (<letter>).

Transcribe exactly, using LaTeX for all mathematics (inline math in $...$, display math in \[...\]). Include every given value and instruction. Describe any figure briefly in [Figure: ...]. Do NOT solve anything. Write each text once: no notes or comments.
Reply in exactly this format and nothing else:
MAIN QUESTION:
<the shared text before part (<letter>), or NONE if the question starts directly with part (<letter>)>
PART (<letter>):
<the text of part (<letter>) only, without its label>
```

### `LLMClient.generate_rubric(question_latex, max_points, problem, instructions, notes, answer_key)` (llm/client.py)
```
Write a grading rubric for exam problem <problem>.

Question (LaTeX):
<question_latex>

Total points: <max_points>.

<instructions/notes block><RUBRIC_PARTS_NOTE><answer key block>

Use the answer key above as the expected final answer.   # only if answer_key given
# OR
First solve the problem and state the expected final answer concisely.   # otherwise

Then list 2-6 gradable criteria (setup, key steps, final answer, ...) whose points add up to exactly <max_points>. Each criterion must be checkable from a student's handwritten work.
True/false questions get NO partial credit: write exactly one criterion, worth all <max_points> points, for the correct answer (with its justification, if the question asks for one); the student gets all of it or nothing.
Respond ONLY as JSON: {"expected_answer": string, "criteria": [{"description": string, "points": number}]}
```
Answer-key block (if provided):
```
The professor's answer key for this question:
<answer_key>
This answer is authoritative: use it as expected_answer (you may restate it concisely) and do not contradict it.

```

### `LLMClient.detect_layout(image, width, height, sent_w, sent_h, columns)` (llm/client.py)
```
This is a blank exam page. Locate every question and every sub-part on it.
<column hint>
Make ONE entry per gradable part: if question 1 has parts (a) and (b), give two entries labelled "1a" and "1b" (parent number + part letter); a question without parts is labelled just with its number, e.g. "5". True/false or multiple-choice items with parts (a)-(f) also get one entry per part ("9a" ... "9f").
For each entry give:
- label: as described above
- question_box: box around the printed text of that part
- answer_box: box around where the student answers: the drawn rectangle under that part, or for true/false the bubbles of that part, or else the blank space below it
- points: the points printed for that part, or null if none are printed
Also give name_box: the rectangle/line where the student writes their NAME, and id_box: the rectangle/line where they write their STUDENT ID number (null if absent). Do not list the name or ID fields as questions.
Boxes are [x1, y1, x2, y2] with coordinates normalized to 0-1000 relative to the image width and height (0,0 = top-left, 1000,1000 = bottom-right).
Respond ONLY as JSON: {"questions": [{"label": string, "question_box": [x1,y1,x2,y2], "answer_box": [x1,y1,x2,y2], "points": number|null}], "name_box": [x1,y1,x2,y2]|null, "id_box": [x1,y1,x2,y2]|null}
```
Column hint:
- if columns>1: `The questions are laid out in <N> columns; read each column top to bottom before moving to the next column.\n`
- else: `The page may use more than one column of questions.\n`

---

## Grading

### `LLMClient.grade_answer(image, problem, group, rubric, max_points, question, instructions, notes, prior_notes, has_next, strictness)` (llm/client.py)
```
Problem <problem> (section '<group>'). Maximum points: <max_points>.

Question:
<question or '(question text not provided; infer it from the image)'>

Rubric:
<rubric or 'Grade based on correctness and completeness.'>

<instructions/notes><PARTS_NOTE><prior notes><CROSSED_OUT_NOTE>
The image shows the student's answer box. Steps:
1. Transcribe exactly what the student wrote (LaTeX for math) into 'transcription', leaving out crossed-out work.
<scoring step>
3. Give the student short, specific 'feedback' naming which criteria were met or missed <FEEDBACK_LIMIT>.
4. 'confidence': a number from 0 to 1 for how sure you are that the score is right (below 0.7 if any part is hard to read or the grading call is a judgment call).
5. 'continues_elsewhere': true if the student indicates the work continues somewhere else (e.g. 'see back', 'continued on next page', an arrow off the box, 'ver atrás').
<NEXT_PART_STEP if has_next>
Set needs_human_review true (and explain in review_reason) if the writing is illegible, the answer is ambiguous, the rubric does not cover the student's approach, or the work continues elsewhere.
Respond ONLY as JSON with keys: transcription, score, feedback, needs_human_review, review_reason, confidence, continues_elsewhere<, next_part_notes>.
```
Scoring step (level 10): `2. Apply each rubric criterion (and the professor's instructions) to the transcription and sum the points into 'score'.\n`
Other levels use `scoring_step(...)` (see function).

### `grade_with_continuation(...)` (llm/back_pages.py)
Same as grade_answer but with two images and this extra block:
```
IMAGE 1 is the student's answer box on the front of the page. IMAGE 2 is work the student wrote on the BACK of the page for this problem (<problem>); treat it as the continuation of the same answer and grade both together. Ignore anything in image 2 that clearly belongs to another problem.

```
and transcription step:
```
1. Transcribe what the student wrote in both images (LaTeX for math) into 'transcription', marking the back part with '[back]' and leaving out crossed-out work.
```
continues_elsewhere: true only if the work continues somewhere beyond these two images.

### `route_back_page(llm, image, labels)` (llm/back_pages.py)
```
This image is the BACK side of an exam page. Students use it to continue answers that did not fit in the boxes on the front, and they are asked to label each piece of work with its problem and part (e.g. '2a)', '3b.', '1)').

The exam's problems are: <labels>.

List every separate piece of work on this page. For each give:
- problem: the problem it belongs to, written exactly as one of the labels above. Use the student's label if there is one; a bare number like '1)' may stand for a problem with parts — then use the bare number. If unlabeled, infer it from the content only if you are sure, otherwise use an empty string.
- box: [x1, y1, x2, y2] around that piece of work, coordinates 0-1000 relative to the image.
- summary: a few words on what the work is.
Ignore the student's name, stray marks and anything crossed out. Set unlabeled true if any piece of work has no problem label.
Respond ONLY as JSON: {"items": [{"problem": string, "box": [x1,y1,x2,y2], "summary": string}], "unlabeled": boolean}
```

---

## Student identification

### `LLMClient.read_name(image)` (llm/client.py)
```
This image is the name field of an exam. Transcribe the student's handwritten name exactly as written (ignore printed labels like 'Name:'). Respond ONLY as JSON: {"name": string, "legible": boolean}
```

### `LLMClient.pick_name(image, candidates)` (llm/client.py)
```
This image is the handwritten name field of an exam. Which of these students wrote it?
<0: name
1: name
...>
Answer with the number of the matching student, or -1 if none match. Respond ONLY as JSON: {"choice": integer}
```

### `read_student_id(...)` (students/matching.py)
```
This image is the 'Student ID' field of an exam. Transcribe the handwritten student ID number exactly, digits only, no spaces. Ignore printed labels and anything crossed out. If the field is empty or unreadable, return an empty string. Respond ONLY as JSON: {"student_id": string, "legible": boolean}
```
(Note: `LLMClient` has no `read_student_id` method; this runs via `_chat` directly.)

### `LLMClient.read_version(image, labels)` (llm/client.py)
```
This image is the part of an exam page that says which version (form) of the exam it is: printed text such as 'Version B' / 'Form 2', or a letter the student circled, checked or wrote. The possible versions are: <"A", "B", ...>. Which one does it show? Answer "unknown" if it is blank, unreadable, or several are marked. Respond ONLY as JSON: {"version": string}
```

---

## Notes
- Every `_chat` call may be retried up to 3 times within 60s on error.
- On degenerate (looping/cut-off) grading replies, `run_grading` retries once with `LOOP_RETRY_NOTE` appended.
- On looping transcription, `_transcribe` retries once with a "write exactly once" note, then cuts repetition.
- Images are downscaled to <= 1600px side / 1.4M pixels before sending (`prepare_crop`).