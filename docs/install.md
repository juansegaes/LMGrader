# Installing LMGrader

- [Install with Python](#install-with-python) (Windows or Linux)
- [Connect an AI (optional)](#connect-an-ai-optional)
- [Scanning](#scanning)
- [Where your data is](#where-your-data-is)

## Install with Python
Install Python 3.10 or newer and Git, then clone the repository. Use a virtual environment to
keep LMGrader's packages separate from other Python projects.

### Windows (PowerShell)
```powershell
git clone https://github.com/juansegaes/LMGrader.git
cd LMGrader
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
python -m lmgrader
```

If PowerShell prevents environment activation, allow it for the current terminal with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate the environment again.

### Linux
```sh
git clone https://github.com/juansegaes/LMGrader.git
cd LMGrader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install .
python -m lmgrader
```

Installing the CPU-only PyTorch build first avoids downloading CUDA packages on Linux. On either
platform, run `python -m lmgrader` from the cloned project folder with its virtual environment
activated.

Optional packages:
- `pip install keyring`: lets LMGrader remember the Canvas token in the system keyring.
- `pip install pywin32`: scanning from WIA scanners on Windows.
- `python-sane`: scanning with SANE on Linux.

To update, pull the latest changes in the cloned repository and run `python -m pip install .` again
from its activated virtual environment. To uninstall, run `python -m pip uninstall lmgrader`
from that environment and remove the cloned repository and `.venv` folder. Your exams and grades
are stored separately ([below](#where-your-data-is)).

### The handwriting model
The first time names are read (**Read names** in Scan & Names) or short answers are grouped in
Hand Grade, LMGrader downloads its handwriting reader (TrOCR, about 1.3 GB) from Hugging Face.
That takes a few minutes, once. After that, everything is read offline. The download sends no
student data.

## Connect an AI (optional)
LMGrader is useful without an AI: names are read on your computer and you grade in Hand Grade.
To let an AI also grade, read the printed questions and draft rubrics, run one on your own
computer with [LM Studio](https://lmstudio.ai) (free):

1. In LM Studio, download a **vision** model, one that reads images (e.g. Qwen3-VL, Qwen2.5-VL or
   Gemma 3). Qwen3-VL 4B runs well on a graphics card with 8 GB.
2. Open its **Developer** tab, load the model and start the server (Status: Running).
3. In LMGrader's **Settings**, tick **Use an LLM**. Leave **Base URL** as
   `http://localhost:1234/v1` (LM Studio on this computer); the **API key** can be anything.
4. Press **Load models**, pick your model in **Vision model**, then **Save**.

"No models returned" means the LM Studio server isn't running, or the address is wrong. LM Studio
on another computer: turn on its "Serve on Local Network" and use that computer's address
instead of `localhost`.

Keep the address on your own computer or a server your institution runs: see
[Privacy and FERPA](privacy.md).

## Scanning
- **The easy way:** scan the whole stack on your department's copier into one PDF (scan to USB or
  to email), then press **Add files…** in Scan & Names. LMGrader cuts it into tests by the exam's
  page count. Images (PNG, JPG) work too.
- **Double-sided:** tick **Double-sided scans** for the exam in Exams & Templates (left column).
  **Edit…** next to it sets how the pages of one test are ordered.
- **A scanner on this computer:** **Scan page** scans a page with WIA or TWAIN on Windows, or SANE
  on Linux.

## Where your data is
Everything LMGrader saves is in one folder:

- Windows: `%USERPROFILE%\.lmgrader` (e.g. `C:\Users\you\.lmgrader`)
- Linux: `~/.lmgrader`

It holds the database (`lmgrader.db`: exams, rosters, grades), the scans and `settings.json`.
Back up this folder to back up everything; keep backups in storage your institution approves.
To use another folder, set the environment variable `LMGRADER_DATA` to its path.
