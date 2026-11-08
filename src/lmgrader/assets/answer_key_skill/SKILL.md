---
name: lmgrader-answer-key
description: Write an LMGrader answer key (JSON) from an exam's LaTeX source — exact question LaTeX, points, expected answers and rubrics — so LMGrader imports them instead of reading the printed exam with a vision model. Use when the user asks for an LMGrader answer key / LMGrader JSON for a .tex exam, or when writing an exam they will grade with LMGrader.
---

# LMGrader answer key from a .tex exam

LMGrader grades scanned paper exams. Its template editor finds the answer boxes on the printed
page; this JSON tells it, for each box, the exact question, points, expected answer and rubric.
Write one file per exam version, next to the .tex: `<texname>.lmgrader.json` (versions:
`<texname>.A.lmgrader.json`, `<texname>.B.lmgrader.json`, ...).

## Steps

1. Read the whole .tex (and every file it `\input`s / `\include`s). Find each question and part
   in print order, its points, and its solution/answer if the source has one.
2. Solve every question yourself and check it against the solution in the source, if any.
3. **Check every computable answer with Python** when Python is available (`python --version`).
   Write one script that recomputes each answer from the question's data and compares it with the
   answer you will put in the key: SymPy for exact math (inverses, determinants, solving systems,
   eigenvalues, row reduction, derivatives, integrals, limits, series, simplification), NumPy/SciPy
   for numerics and statistics, plain Python for counting/probability/arithmetic. Compare exactly
   (`sympy.simplify(a - b) == 0`, `Matrix.equals`) or with a tolerance for decimals. For true/false
   and multiple choice, check the claim on the data when it can be checked (e.g. a counterexample,
   or test every choice). If SymPy/NumPy is missing, say so and ask the user before installing it
   (`pip install sympy numpy`); meanwhile check by hand. Conceptual questions that can't be computed
   (proofs, explanations) are checked by reasoning; say which ones those were.
   If your answer, the source's solution and the Python check don't all agree, keep the source's
   answer in the key and tell the user exactly which question disagrees and what Python got.
   The check script is scratch work: don't leave it next to the exam unless the user asks.
4. Write a `rubric` for every open question (true/false and multiple choice don't need one:
   LMGrader makes them all-or-nothing). Use the source's grading scheme if it has one; otherwise
   split the points over the key steps of the solution you verified, plus the final answer. Write
   it as the structured object shown in Format. In LMGrader a human grades by hand by ticking rubric
   items, applying them to whole groups of identical answers, and the AI grader
   reads them too. So make every item atomic and independently checkable from the handwritten work,
   and reusable across students: separate items for "correct setup", "correct final answer" and each
   common mistake (as a negative item), not one big paragraph. Use `"scoring": "positive"` (the
   default: the items a student earns add up to the question's points) for partial credit, or
   `"scoring": "negative"` (every item is a deduction from full marks, with negative points) when
   the grading scheme is "start full, take off for errors". The user chooses in LMGrader whether to
   import these rubrics or let LMGrader write its own.
5. Write the JSON with a script (e.g. Python `json.dump(..., ensure_ascii=False, indent=2)`), not by
   hand: LaTeX backslashes must be escaped in JSON (`\\frac`), which a script gets right.
6. Validate: `python -m lmgrader.template.answer_key <file>.lmgrader.json`. It prints the questions
   it read or the error to fix, and flags positive rubrics whose items don't add up to the points
   and positive items in a negative rubric. (If `lmgrader` is not importable, at least check that
   the file loads with `json.load`.)
7. Tell the user the file path, which answers were verified with Python (and any disagreement),
   and, in LMGrader: open the exam version → detect/draw the boxes → **Import answer key…**
   (tick "Import the rubrics from the key" to use your rubrics).

## Format

```json
{
  "format": "lmgrader-answer-key",
  "version": 2,
  "exam": "MATH 221 Midterm 2",
  "exam_version": "A",
  "instructions": "Optional exam-wide grading instructions (e.g. no credit without supporting work).",
  "questions": [
    {
      "label": "1",
      "stem": "Let $A = \\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}$ and $b = (1, 0)^T$.",
      "parts": [
        {"label": "a", "text": "Compute $A^{-1}$.", "points": 4,
         "answer": "$A^{-1} = \\frac{1}{-2}\\begin{pmatrix} 4 & -2 \\\\ -3 & 1 \\end{pmatrix}$",
         "rubric": {"scoring": "positive", "items": [
           {"points": 1, "description": "Computes $\\det A = -2$"},
           {"points": 2, "description": "Correct adjugate / row reduction"},
           {"points": 1, "description": "Correct final $A^{-1}$"}
         ]}},
        {"label": "b", "text": "Use (a) to solve $Ax = b$.", "points": 3,
         "answer": "$x = A^{-1}b = (-2, \\tfrac{3}{2})^T$",
         "rubric": {"scoring": "positive", "items": [
           {"points": 2, "description": "Computes $x = A^{-1}b$ with their $A^{-1}$ from (a)"},
           {"points": 1, "description": "Correct $x$ (or correct for their $A^{-1}$)"}
         ]},
         "notes": "Full credit if they correctly use a wrong $A^{-1}$ from (a)."}
      ]
    },
    {"label": "2", "type": "true_false", "points": 2,
     "text": "True or false: every invertible matrix is diagonalizable.", "answer": "False"},
    {"label": "3", "type": "multiple_choice", "points": 2,
     "text": "Which is a basis of $\\mathbb{R}^2$? (A) $\\{(1,0)\\}$ (B) $\\{(1,1),(1,-1)\\}$ (C) $\\{(1,2),(2,4)\\}$",
     "answer": "(B)"},
    {"label": "4", "type": "open", "points": 3,
     "text": "Find $\\det B$ for $B = \\begin{pmatrix} 2 & 0 \\\\ 1 & 3 \\end{pmatrix}$.",
     "answer": "$\\det B = 6$",
     "rubric": {"scoring": "negative", "items": [
       {"points": -1, "description": "Arithmetic slip in the product or the sum"},
       {"points": -2, "description": "Wrong formula for a $2 \\times 2$ determinant (e.g. $ad + bc$)"}
     ]}}
  ]
}
```

Fields (only `label` is required; leave out what the source doesn't say):

| field | meaning |
|---|---|
| `label` | Question number as printed (`"1"`, `"2"`). For parts: the part letter (`"a"`, `"b"`; roman `"i"`, `"ii"` also work) — LMGrader joins it to the question number (`1a`) and matches it to the box labelled `1a` / `Q1 (a)`. |
| `stem` | (questions with `parts`) The shared setup printed before the parts. Not repeated in each part. |
| `text` | The question (or only this part) as LaTeX: inline math in `$...$`, display math in `$$...$$` or environments like `pmatrix`. Copy it from the source, without layout. |
| `points` | Max points for this question/part. Defaults to the rubric's `max_points`, or else the sum of a positive rubric's items; if both are missing, the points set in LMGrader are kept. |
| `answer` | The expected final answer (LaTeX). Make it complete enough that a grader can check a student's result against it. |
| `type` | `"open"` (default), `"true_false"`, `"multiple_choice"`. True/false and multiple choice get an all-or-nothing rubric automatically (no partial credit) from `answer`. |
| `rubric` | Optional object `{"scoring", "items", "max_points"?}`. `scoring`: `"positive"` (default) = the score starts at 0 and each item the student earns adds its points; the positive items must add up to `points`. `"negative"` = the score starts at `points` and every item is a deduction (negative `points`; the score never goes below 0). Each item is `{"points", "description"}`: `points` may be negative in a positive rubric (e.g. a `-1` penalty for an arithmetic slip); `description` is what the grader checks in the handwritten work (LaTeX allowed). `max_points` is used only if the question has no `points`. Write one for every open question (step 4); the user decides at import whether to use it. Without one, LMGrader's LLM writes the rubric from `text` + `answer`. |
| `notes` | Optional per-question instructions for the grader (e.g. accepted alternative forms, follow-through rules). |

Keys written in the old list form (`[{"points", "criterion"}]`, version 1) are still read.

## Turning the LaTeX into `text`

- Keep the math and wording exactly; remove layout and answer space: `\vspace`, `\vfill`,
  `\newpage`, `\fbox`/`\framebox` answer boxes, `\fillin`, `\answerline`, `\makeemptybox`,
  `\droppoints`, tikz answer grids, and the points marker itself (`(4 pts)`, `[4 points]`).
- Expand the author's own macros (`\newcommand`) into standard LaTeX (e.g. `\R` → `\mathbb{R}`),
  so the text makes sense without the preamble.
- Keep figures as a short description in brackets if they matter for the question
  (`[Figure: graph of $f$ with a local max at $x=1$]`).
- `exam` document class: `\question[4]` → points 4; `\begin{parts}` `\part[2]` → `parts` with
  points 2; `\subpart` (a third level) is not supported: ask the user whether to merge the
  subparts into their part or to label them as parts of their own. `\begin{solution}...\end{solution}`
  → `answer` (a short final answer, not the full worked solution; put grading-relevant steps in
  `rubric`). `\begin{choices}`/`\begin{oneparchoice}` with `\CorrectChoice` → `multiple_choice`, the
  choices written into `text` as `(A) ... (B) ...` and the correct letter as `answer`.
  `\begin{checkboxes}` with several `\CorrectChoice` → `multiple_choice` with all correct letters.
- A true/false question asking for a justification: still `true_false`, with the justification
  requirement in `text` and `answer` holding both (e.g. `"False. Counterexample: ..."`).
- Other classes / hand-made numbering (`enumerate`, `\section*{Problem 1}`): use the printed
  numbers and letters as labels.
- Different exam versions (`\ifversionA`, random numbers, separate files): one JSON per version,
  each with that version's numbers and answers.
