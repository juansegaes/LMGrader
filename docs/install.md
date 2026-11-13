# Installing LMGrader

- [Windows: download and run](#windows-download-and-run) (no Python needed)
- [From source](#from-source) (Windows or Linux)
- [Connect an AI (optional)](#connect-an-ai-optional)
- [Scanning](#scanning)
- [Where your data is](#where-your-data-is)

## Windows: download and run
1. Download `LMGrader-<version>-windows-x64.zip` from
   [Releases](https://github.com/juansegaes/LMGrader/releases/latest).
2. Unzip it where you keep programs, e.g. `Documents\LMGrader`. Keep the folder together:
   `LMGrader.exe` needs the `_internal` folder next to it.
3. Run `LMGrader.exe`. You can pin it to Start or to the taskbar.

Windows may show *Windows protected your PC* the first time, because the app isn't code-signed.
Click *More info*, then *Run anyway*.

It needs 64-bit Windows 10 or 11. The download is about 310 MB (800 MB unzipped).

**Updating:** delete the old `LMGrader` folder and unzip the new one. Your exams, rosters and
grades are kept in your user folder ([below](#where-your-data-is)), not in the app folder, so they
stay.

**Uninstalling:** delete the `LMGrader` folder. To remove your data too, also delete
`%USERPROFILE%\.lmgrader` (export or back up the grades first) and the handwriting model in
`%USERPROFILE%\.cache\huggingface\hub\models--microsoft--trocr-base-handwritten`.

### The handwriting model
The first time names are read (**Read names** in Scan & Names) or short answers are grouped in
Hand Grade, LMGrader downloads its handwriting reader (TrOCR, about 1.3 GB) from Hugging Face.
That takes a few minutes, once. After that, everything is read offline. The download sends no
student data.

## From source
Python 3.10 or newer, on Windows or Linux.

```
git clone https://github.com/juansegaes/LMGrader.git
cd LMGrader
pip install -e .
python -m lmgrader
```

This installs PyTorch (CPU) for the handwriting reader. On Linux, install the CPU build first to
avoid the large CUDA download:
`pip install torch --index-url https://download.pytorch.org/whl/cpu`.

Optional packages (the Windows download includes the first two):
- `pip install keyring`: lets LMGrader remember the Canvas token in the system keyring.
- `pip install pywin32`: scanning from WIA scanners on Windows.
- `python-sane`: scanning with SANE on Linux.

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
