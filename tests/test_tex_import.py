"""Questions (and solutions) read straight from an exam's .tex, without the LLM."""
import pytest
from lmgrader.template.tex_import import parse_tex, load_tex, clean_text
from lmgrader.template.answer_key import apply_answer_key
from test_answer_key import _template

EXAM_CLASS = r"""
\documentclass[addpoints]{exam}
\usepackage{amsmath}
\newcommand{\R}{\mathbb{R}}   % reals
\newcommand{\vect}[1]{\mathbf{#1}}
\DeclareMathOperator{\rank}{rank}
\title{Linear Algebra Midterm 2}
\begin{document}
\maketitle
Write your name on every page. % instructions, not a question
\begin{questions}
\question
Let $A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}$ and $\vect{b} = (1, 0)^T \in \R^2$.
\begin{parts}
\part[4] Compute $A^{-1}$.
\vspace{2in}
\begin{solution}
$A^{-1} = \frac{1}{-2}\begin{pmatrix} 4 & -2 \\ -3 & 1 \end{pmatrix}$
\end{solution}
\part[3] Use part (a) to solve $A\vect{x} = \vect{b}$. Is $\partial f$ relevant? No.
\begin{solutionorbox}[2in]
$\vect{x} = (-2, 3/2)^T$
\end{solutionorbox}
\end{parts}
\newpage
\question[2] True or false: every invertible matrix is diagonalizable.
\begin{solution}
False. $\begin{pmatrix} 1 & 1 \\ 0 & 1 \end{pmatrix}$ is invertible but not diagonalizable.
\end{solution}
\question[2] What is $\rank A$?
\begin{choices}
\choice 0
\choice 1
\CorrectChoice 2
\end{choices}
\question[5] Prove that $\det(AB) = \det A \det B$.
\makeemptybox{3in}
\end{questions}
\end{document}
"""

ENUMERATE = r"""
\documentclass{article}
\newcommand{\N}{\mathbb{N}}
\begin{document}
\begin{enumerate}
\item (6 pts) Let $f(x) = x^2 - 4$.
  \begin{enumerate}
  \item[(a)] Find $f(0)$. \hfill [2 points]
  \item Find the roots of $f$. (4 pts)
  \begin{answer} $x = \pm 2$ \end{answer}
  \end{enumerate}
\item Verdadero o falso: $0 \in \N$. (1 pt)
\vspace{1cm}
\item Sketch $f$ on $[-3, 3]$.
  \begin{center}
  \begin{tikzpicture} \draw (0,0) -- (1,1); \end{tikzpicture}
  \end{center}
\end{enumerate}
\end{document}
"""


def test_exam_class():
    key = parse_tex(EXAM_CLASS)
    assert key.exam == "Linear Algebra Midterm 2"
    assert [it.label for it in key.items] == ["1a", "1b", "2", "3", "4"]
    a, b, tf, mc, proof = key.items
    assert a.stem == r"Let $A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}$ and $\mathbf{b} = (1, 0)^T \in \mathbb{R}^2$."
    assert a.text == "Compute $A^{-1}$." and a.points == 4 and a.answer.startswith(r"$A^{-1} = \frac{1}{-2}")
    assert b.points == 3 and b.answer == r"$\mathbf{x} = (-2, 3/2)^T$" and r"\partial f" in b.text   # \part != \partial
    assert "solution" not in b.text and "2in" not in b.text
    assert tf.type == "true_false" and tf.answer.startswith("False.") and tf.points == 2
    assert mc.type == "multiple_choice" and mc.answer == "(C)"
    assert mc.text.splitlines() == [r"What is $\operatorname{rank} A$?", "(A) 0", "(B) 1", "(C) 2"]
    assert proof.points == 5 and proof.answer == "" and proof.text == r"Prove that $\det(AB) = \det A \det B$."
    assert "Write your name" not in "".join(it.text + it.stem for it in key.items)


def test_enumerate():
    key = parse_tex(ENUMERATE)
    assert [it.label for it in key.items] == ["1a", "1b", "2", "3"]
    a, b, tf, sketch = key.items
    assert a.stem == "Let $f(x) = x^2 - 4$." and a.points == 2 and a.text == "Find $f(0)$."
    assert b.points == 4 and b.answer == r"$x = \pm 2$" and b.text == "Find the roots of $f$."
    assert tf.type == "true_false" and tf.points == 1 and tf.text == r"Verdadero o falso: $0 \in \mathbb{N}$."
    assert sketch.text == "Sketch $f$ on $[-3, 3]$.\n[Figure]" and sketch.points is None


def test_inputs_and_errors(tmp_path):
    (tmp_path / "q").mkdir()
    (tmp_path / "q" / "one.tex").write_text(r"\question[3] Solve $x^2 = 9$. % comment" "\n", encoding="utf-8")
    main = tmp_path / "exam.tex"
    main.write_text("\\documentclass{exam}\n\\begin{document}\n\\begin{questions}\n\\input{q/one}\n"
                    "\\end{questions}\n\\end{document}\n", encoding="utf-8")
    key = load_tex(main)
    assert [(it.label, it.text, it.points) for it in key.items] == [("1", "Solve $x^2 = 9$.", 3)]
    assert any("No solutions" in w for w in key.warnings)
    with pytest.raises(ValueError, match="No questions found"):
        parse_tex(r"\documentclass{article}\begin{document}Hello\end{document}")


def test_whole_question_points_warning():
    key = parse_tex(r"\begin{questions}\question[10] Let $g$. \begin{parts}\part Find $g'$. \part Find $g''$."
                    r"\end{parts}\end{questions}")
    assert [it.label for it in key.items] == ["1a", "1b"] and key.items[0].stem == "Let $g$."
    assert any("10 points for the whole question" in w for w in key.warnings)


def test_clean_text():
    assert clean_text("Find $x$.\\\\[2mm]\n\\vspace*{1cm}\n\\fillin[3][2in]\n\\vfill") == "Find $x$."


def test_editor_import_tex(qapp, tmp_path, monkeypatch):
    from lmgrader import db
    from lmgrader.template.template import persist
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    from conftest import SCREENSHOT_DIR
    asked = []
    monkeypatch.setattr(te.TemplateEditorView, "_confirm_key_import",
                        lambda self, msg, n: (asked.append(msg), True)[1])
    f = tmp_path / "midterm.tex"
    f.write_text(ENUMERATE, encoding="utf-8")
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    tv = mw.template_view
    try:
        t = _template(tmp_path, "Tex editor")
        t.id = persist(t)
        tv.refresh_list(t.id); tv.load(t.id)
        assert tv.btn_import_tex.isEnabled()
        tv.import_answer_key(str(f))
        assert "Matched 4 question(s): Q1 (a), Q1 (b), Q2, Q3" in asked[-1]
        tv.flush()
        saved = db.load_template(t.id)
        assert saved.stems["1"] == "Let $f(x) = x^2 - 4$." and saved.regions[1].answer_key == r"$x = \pm 2$"
        tv.region_list.setCurrentRow(1); qapp.processEvents()
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        mw.grab().save(str(SCREENSHOT_DIR / "template_editor_import_tex.png"))
    finally:
        tv.flush(); mw.close()


def test_tex_import_fills_template(tmp_path):
    t = _template(tmp_path, "Tex apply")
    key = parse_tex(EXAM_CLASS)
    rep = apply_answer_key(t, key)
    a, b, tf, mc, _ = t.regions
    assert rep.matched == ["Q1 (a)", "Q1 (b)", "Q2", "Q3"] and rep.no_box == ["4"]
    assert t.stems["1"].startswith("Let $A") and a.points == 4 and b.answer_key
    assert tf.rubric.as_text().endswith("(all or nothing: no partial credit)") and "(C)" in mc.rubric.as_text()
    assert not a.rubric and rep.rubrics_from_key == ["Q2", "Q3"]     # open questions: rubric by LLM
