"""In-app help: one topic per tab (what it is for, the steps, its keys, what comes next), plus
Getting started and Keyboard shortcuts. Shown by the sidebar's Help button / F1 in a window that
stays open and follows the tab you are on. Offline; button names exactly as on screen."""
from html import escape
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QListWidgetItem, QTextBrowser
from PyQt6.QtCore import Qt

from .. import config
from . import theme

# (key, title) in sidebar order; "llm" only while an LLM is used
TOPICS = [("start", "Getting started"), ("templates", "Exams & Templates"), ("students", "Students"),
          ("scan", "Scan & Names"), ("hand", "Hand Grade"), ("llm", "LLM Grade"), ("review", "Review"),
          ("return", "Return"), ("settings", "Settings"), ("keys", "Keyboard shortcuts")]

def topics(llm_on: bool) -> list:
    """(key, title) of the topics shown (LLM Grade only with an LLM)."""
    return [(k, t) for k, t in TOPICS if llm_on or k != "llm"]

_ARROWS = {"Up": "↑", "Down": "↓", "Left": "←", "Right": "→", "Return": "Enter"}

def pretty_keys(seqs) -> str:
    """("Ctrl+Return", "Ctrl+Enter") -> "Ctrl+Enter"; "Alt+Down" -> "Alt+↓"."""
    out = []
    for s in seqs:
        p = "+".join(_ARROWS.get(part, part) for part in s.split("+"))
        if p not in out:
            out.append(p)
    return " / ".join(out)

def _k(text: str) -> str:
    return f"<span class='k'>{escape(text)}</span>"

def _keys_table(rows) -> str:
    return "<table cellspacing='0' cellpadding='3'>" + "".join(
        f"<tr><td style='padding-right:14px'>{_k(k)}</td><td>{escape(what)}</td></tr>" for k, what in rows) + "</table>"

def _next(*tabs) -> str:
    return "<p class='next'>Next: " + " or ".join(f"<b>{escape(t)}</b>" for t in tabs) + ".</p>"

# ---------- keys of each tab (Review's come from its own constants) ----------
def review_keys():
    from . import review_view as rv
    return [(pretty_keys(rv.KEYS_NEXT_QUESTION), "next question of this test"),
            (pretty_keys(rv.KEYS_PREV_QUESTION), "previous question"),
            (pretty_keys(rv.KEYS_NEXT_TEST), "next test in the list"),
            (pretty_keys(rv.KEYS_PREV_TEST), "previous test"),
            (pretty_keys(rv.KEYS_ACCEPT_NEXT), "accept the AI suggestion (or keep the score you typed) "
                                               "and go to the next flagged question"),
            (pretty_keys(rv.KEYS_NEXT_FLAGGED), "next flagged question (same as the button)")]

HAND_KEYS = [("1 … 9", "tick / untick rubric item 1 … 9"),
             ("Ctrl+Enter", "save the grade and go to the next one"),
             ("Alt+← / Alt+→", "previous / next answer without saving"),
             ("Esc", "unselect every answer (Group answers, Confirm groups)")]

EDITOR_KEYS = [("Arrow keys", "nudge the selected box"),
               ("Shift+drag", "draw a new box on top of existing boxes"),
               ("Delete", "delete the selected question (in the Regions list)"),
               ("Esc", "keep the old box while redrawing one"),
               ("Ctrl+wheel", "zoom the page"),
               ("Ctrl+S", "save now (saving is automatic anyway)")]

GENERAL_KEYS = [("F1", "help for the tab you are on"),
                ("Mouse wheel", "only scrolls: it never changes a number box or a list")]

# ---------- topics ----------
def _start(llm):
    grade = ("<b>Hand Grade</b> and/or <b>LLM Grade</b> — grade one question at a time across every test, "
             "or let the AI grade" if llm else "<b>Hand Grade</b> — grade one question at a time across every test")
    return f"""
<p>LMGrader grades paper exams on this computer. Work down the sidebar:</p>
<ol>
<li><b>Course</b> — pick it at the top of the sidebar (<b>Manage courses…</b> to add one). Every tab
shows this course's students and exams.</li>
<li><b>Exams &amp; Templates</b> — add the blank exam; mark the answer boxes and the name field; set
the points and the rubrics.</li>
<li><b>Students</b> — import the class roster (CSV).</li>
<li><b>Scan &amp; Names</b> — add the scanned tests, read the names, confirm who wrote each test.</li>
<li>{grade}.</li>
<li><b>Review</b> — check flagged answers and the scores.</li>
<li><b>Return</b> — upload the feedback PDFs to Canvas, write the grades into your CSV, or save the PDFs.</li>
</ol>
<p>Everything is saved as you go: close the app at any time and continue later.</p>
<p>{"The AI runs in LM Studio; the <b>Settings</b> help shows how to connect it." if llm else
"An AI is optional: to use one, connect LM Studio in <b>Settings</b> (see its help)."}</p>
<p>Press {_k("F1")} on any tab for help with that tab.</p>"""

def _templates(llm):
    if llm:
        steps = """
<li><b>Auto-setup with LLM</b> finds every question, its answer box and the name field, then reads
each question as LaTeX. (Or draw the boxes yourself, below.)</li>
<li>Check the boxes and set each question's <b>Max points</b>.</li>
<li><b>Generate all missing rubrics</b>, then read and edit each rubric.</li>"""
        draw = ("<b>Answer</b> adds a question's answer box; <b>Question</b> marks where the selected "
                "question is printed (so the LLM can read it); <b>Name</b>, <b>ID</b> and <b>Version</b> "
                "mark where students write their name and ID and where the exam states its version.")
        kept = "Its points, question and rubric stay."
        exam = """
<li><b>Grading instructions</b> and <b>Strictness</b> are given to the AI for every question of
the exam; <b>Notes for the grader</b> only for one question.</li>"""
    else:
        steps = """
<li>Draw the answer boxes: with <b>Answer</b> selected, drag a box around each answer space. Draw the
<b>Name</b> field too (and <b>ID</b> if the exam has one).</li>
<li>Select each question in the Regions list; set its number in <b>Problem</b> and its <b>Max points</b>.</li>
<li>Add the rubric items, or bring questions and rubrics in with <b>Import answer key…</b> or
<b>Import .tex…</b>.</li>"""
        draw = ("<b>Answer</b> adds a question's answer box; <b>Name</b>, <b>ID</b> and <b>Version</b> mark "
                "where students write their name and ID and where the exam states its version.")
        kept = "Its points and rubric stay."
        exam = ""
    return f"""
<p>Set up each exam once: where students answer, what each question is worth and how it is graded.</p>
<ol>
<li><b>New exam…</b> — name it and choose the blank exam (PDF or images, every page).
<b>Add version…</b> for versions A, B…</li>{steps}
</ol>
<h3>Boxes</h3>
<ul>
<li>Draw buttons above the page: {draw}</li>
<li>Click a box to select it, then drag it or its handles to move or resize it.</li>
<li>Wrong answer box? Select the question and press <b>Redraw box</b>, then drag the new box (on any
page). {kept}</li>
<li><b>Delete</b> removes the whole question.</li>
<li>Drag questions in the Regions list (or ↑ / ↓ / <b>Sort by number</b>) to set their order.</li>
<li>Another version with the same layout? <b>Copy boxes from another version</b>.</li>
</ul>
<h3>Rubrics</h3>
<p><b>Add up (from 0)</b>: each item you tick while grading adds its points. <b>Deduct (from max)</b>:
the question starts at full marks and each ticked item takes points off.</p>
<h3>For the whole exam (left column)</h3>
<ul>
<li><b>Double-sided scans</b> and <b>Edit…</b>: how the scanned pages of one test are ordered.</li>{exam}
<li><b>Copy / move to course…</b>: the exam in another course (copy: without scans).</li>
</ul>
<p>Everything is saved automatically (“Saved ✓” at the top right).</p>
{_next("Students", "Scan & Names")}"""

def _students(llm):
    return f"""
<p>The roster of the course picked in the sidebar. The handwritten names (and student IDs) on the
scans are matched against it.</p>
<ul>
<li><b>Import CSV…</b>: any CSV with a header row, e.g. your Canvas or Banner export. Pick the column
with the names (“First Last”, “Last, First”, or first and last name in two columns) and, if you
have them, the ID and email columns. Students already in the course are skipped.</li>
<li><b>Add student</b>: one more student by hand.</li>
<li><b>Move to course…</b>: the selected students go to another course; their tests stay theirs.</li>
<li><b>Delete selected</b>: remove students.</li>
</ul>
{_next("Scan & Names")}"""

def _scan(llm):
    reader = ("Names and IDs are read on this computer; with <b>Use the LLM when unsure</b> the LLM helps "
              "with hard ones." if llm else "Names and IDs are read on this computer.")
    return f"""
<p>Add the scanned tests and confirm who wrote each one.</p>
<ol>
<li>Pick the <b>Exam</b> at the top. <b>Version: Auto-detect</b> finds each test's version.</li>
<li><b>Add files…</b>: one long PDF with every test, or several files. LMGrader cuts them into tests
of the exam's page count. Pages left over wait for the next upload (or <b>Discard pages</b>).</li>
<li>Press <b>Read names</b> in the banner. {reader}</li>
<li>In <b>2 Names</b>, fix the rows marked <b>Needs you</b> (tick <b>Only tests that need me</b> to see
just those): pick the student in the list. <b>Confirm all suggested</b> accepts the rest.</li>
</ol>
<ul>
<li>A test read as the wrong version: change it in the Version column.</li>
<li>Scans added by mistake: select them in <b>1 Upload</b> and press <b>Delete</b>.</li>
</ul>
{_next("Hand Grade", "LLM Grade") if llm else _next("Hand Grade")}"""

def _hand(llm):
    ai = "<li>The AI never changes a grade you give here.</li>" if llm else ""
    return f"""
<p>Grade one question at a time across every test. Answers that deserve the same grade are grouped,
and you grade each group once.</p>
<ol>
<li>Pick the <b>Exam</b>, <b>Version</b> and <b>Question</b>. The first time, say what kind of question
it is: blank answers only, short answer or multiple choice (for multiple choice, check the choice
boxes LMGrader found on the blank exam).</li>
<li><b>1 Blanks</b>: untick any answer that isn't really blank, then <b>Confirm blanks →</b>.</li>
<li><b>2 Group answers</b>: suggested groups are marked ⚑ — take out answers that don't belong, then
<b>Looks right ✓</b>. Group others yourself: click answers to select them (click again to unselect,
or drag a box around several), then <b>New group from selection</b> or <b>Add selection to group</b>
(or click a group in the list). <b>Dissolve group</b> also deletes the hand grades of its answers.</li>
<li><b>3 Confirm groups</b>: every group with all its answers. Click the answers that don't belong, then
<b>Move selected to ungrouped</b> (they are graded one by one). <b>Groups are right → grade</b> goes on.</li>
<li><b>4 Grade</b>: tick rubric items (keys {_k("1")}–{_k("9")}), add a comment if you like, then
<b>Save &amp; next</b> ({_k("Ctrl+Enter")}). Groups come first, then single answers. When the
question is done, the next one opens.</li>
</ol>
<ul>
<li><b>Edit rubric…</b>: add or change rubric items while grading.</li>
<li><b>Suggest groups…</b>: sort tests scanned later.</li>
<li><b>Regrade from scratch…</b>: start the question over with the same type; <b>Change question
type…</b>: start it over as another type. Either way its groups and hand grades are deleted, after
asking.</li>{ai}
</ul>
<h3>Keys</h3>{_keys_table(HAND_KEYS)}
{_next("Review")}"""

def _llm(llm):
    return f"""
<p>The AI grades every question nobody graded by hand, using the question, its rubric and your grading
instructions. Progress is saved after every question.</p>
<ul>
<li><b>Grade all ready</b> grades the tests whose student is confirmed (tick <b>Also grade tests
without a confirmed student</b> for the rest). <b>Stop</b> at any time; it continues where it stopped.</li>
<li>Answers the AI is unsure about, work that continues on the back and poor scans are marked
<b>Needs human grade</b> for Review.</li>
<li><b>Regrade…</b> grades the selected (or all) tests again, e.g. after changing a rubric. You choose
whether hand-graded questions are kept.</li>
</ul>
{_next("Review")}"""

def _review(llm):
    retry = "; <b>Retry AI grade</b> asks the AI again" if llm else ""
    return f"""
<p>Check the grades before returning them. Flagged questions need you: the AI's score is only a
suggestion until you accept it or type your own.</p>
<ul>
<li>Pick a test on the left (<b>Show: Needs human grade</b> lists only those).</li>
<li>The chips above the questions show every question: ⚠ needs you, ✓ graded by a person, no mark =
AI grade. Click one to jump to it.</li>
<li>Type a score in the box to set your grade, or press <b>Accept suggestion</b>. Edit the feedback
the student will read.</li>
<li><b>Full page</b> and <b>Back of page</b> show the whole scan; <b>Show rubric</b> shows the
rubric{retry}.</li>
<li><b>Next flagged</b> goes to the next question that needs you, then to the next test.</li>
<li><b>Preview feedback</b> shows the first page the student gets.</li>
</ul>
<h3>Keys</h3>{_keys_table(review_keys())}
{_next("Return")}"""

def _return(llm):
    email = ("<li><b>Email</b>: send each student their PDF (<b>Send test to me</b> first to check it).</li>"
             if config.EMAIL_ENABLED else "")
    return f"""
<p>Give the graded tests back. Each student gets a feedback PDF: a first page with every question's
score and feedback, then their scans with red boxes and scores.</p>
<ul>
<li><b>Canvas feedback</b>: enter your Canvas address, <b>Connect</b> with a personal access token,
pick the course and the assignment, then <b>Upload to all not yet uploaded</b>. Each PDF is attached as
a comment; Canvas grades are not changed.</li>
<li><b>Grades into your CSV</b>: <b>Write grades into a CSV…</b> fills a column of a gradebook CSV you
already have (e.g. a Canvas export) and saves it as a new file.</li>
<li><b>Feedback PDFs</b>: <b>Export PDFs to folder…</b> saves one PDF per student, named by the
pattern.</li>{email}
<li>Tests that still need review are left out unless you tick <b>Include tests that still need
review</b>. The <b>Returned</b> column shows what already went out, so nothing is sent twice.</li>
</ul>"""

def _settings(llm):
    return f"""
<ul>
<li><b>Appearance</b>: light or dark colors (also the button at the bottom of the sidebar).</li>
<li><b>Use an LLM</b>: off, LMGrader never contacts an AI — names are read on this computer, you grade
in Hand Grade, and the AI buttons and the LLM Grade tab are hidden. On, it uses an AI server such as
LM Studio on this computer (<b>Connection</b>: its address and a vision model; <b>Load models</b>
lists them).</li>
<li><b>Human grading</b>: below which AI confidence a question goes to you in Review, and the
full-marks exceptions.</li>
</ul>
<p>Press <b>Save</b> to keep your changes.</p>
<h3>Connect LM Studio</h3>
<p>LM Studio (free, lmstudio.ai) runs the AI on your own computer; nothing is sent online.</p>
<ol>
<li>In LM Studio, download a <b>vision</b> model, one that reads images (e.g. Qwen2.5-VL, Qwen3-VL or
Gemma 3).</li>
<li>Open its <b>Developer</b> tab, load the model and start the server (Status: Running).</li>
<li>Here, tick <b>Use an LLM</b>. Leave <b>Base URL</b> as {_k(config.DEFAULT_LLM_BASE_URL)} (LM Studio's
address on this computer); the <b>API key</b> can be anything.</li>
<li>Press <b>Load models</b>, pick your model in <b>Vision model</b>, then <b>Save</b>.</li>
</ol>
<p>“No models returned” means the LM Studio server isn't running (or the address is wrong). LM Studio
on another computer: turn on its “Serve on Local Network” and use that computer's address instead of
localhost.</p>"""

def _keys(llm):
    return (f"<h3>Review</h3>{_keys_table(review_keys())}"
            f"<h3>Hand Grade</h3>{_keys_table(HAND_KEYS)}"
            f"<h3>Exams &amp; Templates</h3>{_keys_table(EDITOR_KEYS)}"
            f"<h3>Everywhere</h3>{_keys_table(GENERAL_KEYS)}")

_BUILDERS = {"start": _start, "templates": _templates, "students": _students, "scan": _scan, "hand": _hand,
             "llm": _llm, "review": _review, "return": _return, "settings": _settings, "keys": _keys}

def help_html(key: str, llm_on: bool) -> str:
    """The help text of topic `key` (HTML body, title included)."""
    title = dict(TOPICS)[key]
    return f"<h2>{escape(title)}</h2>" + _BUILDERS[key](llm_on)


class HelpDialog(QDialog):
    """Topics on the left, the text on the right. Non-modal: it stays open while you work."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LMGrader help")
        self.resize(780, 640)
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.setFixedWidth(190)
        lay.addWidget(self.list)
        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        lay.addWidget(self.text, 1)
        self.list.currentRowChanged.connect(self._render)
        self.llm_on = config.llm_enabled()
        self._fill()

    def _fill(self):
        cur = self.current_topic()
        self.list.blockSignals(True)
        self.list.clear()
        for key, title in topics(self.llm_on):
            it = QListWidgetItem(title)
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.show_topic(cur or "start")

    def set_llm(self, on: bool):
        """Settings → "Use an LLM" changed: topics and texts follow."""
        if on != self.llm_on:
            self.llm_on = on
            self._fill()

    def current_topic(self):
        it = self.list.currentItem() if self.list.count() else None
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def topic_keys(self) -> list:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def show_topic(self, key: str):
        """Show topic `key` (Getting started if it isn't listed, e.g. LLM Grade with the LLM off)."""
        keys = self.topic_keys()
        row = keys.index(key) if key in keys else 0
        if self.list.currentRow() == row:
            self._render(row)
        else:
            self.list.setCurrentRow(row)

    def _render(self, row: int):
        if row < 0:
            return
        key = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        doc = self.text.document()
        doc.setDefaultStyleSheet(
            f"h2 {{ font-size: 15pt; margin-bottom: 6px; }} h3 {{ font-size: 11pt; margin-top: 12px; }}"
            f"li {{ margin-bottom: 4px; }} p.next {{ color: {theme.ACCENT}; margin-top: 12px; }}"
            f".k {{ background-color: {theme.SURFACE_ALT}; font-family: Consolas, monospace; }}")
        self.text.setHtml(help_html(key, self.llm_on))
        self.text.verticalScrollBar().setValue(0)
