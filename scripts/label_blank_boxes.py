"""Label graded answer boxes as blank / not blank with the LLM loaded in LM Studio.

Training data for a small "is this answer box empty?" model. Every answer crop of the exam's
graded tests (grades.extracted_image_path) is shown to the LLM; the crop is copied into
<out>/blank or <out>/not_blank, and every decision is appended to <out>/labels.csv.
Multiple-choice answer boxes are not classified: they are only logged (label "multiple_choice").
Resumable: crops already listed in labels.csv are skipped.

    python scripts/label_blank_boxes.py --exam T2 --out dataset/blank_boxes
    python scripts/label_blank_boxes.py --exam T2 --limit 20        # quick trial
"""
import argparse
import csv
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lmgrader.config import db_path                                       # noqa: E402
from lmgrader.llm.client import LLMClient, _extract_json, _image_part, image_to_b64, prepare_crop  # noqa: E402
from lmgrader.template.alignment import read_image                        # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"multiple_choice": {"type": "boolean"}, "has_writing": {"type": "boolean"},
                   "reason": {"type": "string"}},
    "required": ["multiple_choice", "has_writing", "reason"],
}
PROMPT = (
    "This image is the answer space of one question on a scanned exam. First decide whether it is a "
    "multiple-choice answer (lettered/numbered options, bubbles or boxes to mark, true/false). If it "
    "is, set multiple_choice to true and has_writing to false: it will not be classified. Otherwise decide whether the student "
    "wrote or drew ANYTHING in it: text, numbers, math, a sketch, a graph, a checkmark or a circled "
    "choice. Crossed-out work still counts as writing. Printed text, the box border, lines, scanner "
    "noise, dust, shadows and smudges do NOT count. If the space is empty, has_writing is false. "
    "Respond ONLY as JSON: {\"multiple_choice\": boolean, \"has_writing\": boolean, \"reason\": string (max 10 words)}")


def crops_for_exam(exam: str):
    con = sqlite3.connect(db_path())
    rows = con.execute(
        "SELECT g.submission_id, g.region_id, g.extracted_image_path FROM grades g "
        "JOIN submissions s ON s.id = g.submission_id "
        "WHERE s.exam_name = ? AND g.extracted_image_path IS NOT NULL "
        "ORDER BY g.submission_id, g.region_id", (exam,)).fetchall()
    con.close()
    return [(sid, rid, Path(p)) for sid, rid, p in rows if p]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exam", default="T2")
    ap.add_argument("--out", default="dataset/blank_boxes")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new crops (0 = all)")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "blank").mkdir(parents=True, exist_ok=True)
    (out / "not_blank").mkdir(parents=True, exist_ok=True)
    log_path = out / "labels.csv"
    new_log = not log_path.exists()
    seen = set()
    if not new_log:
        with open(log_path, newline="", encoding="utf-8") as f:
            seen = {row[0] for row in csv.reader(f)}

    llm = LLMClient.from_config()
    items = crops_for_exam(args.exam)
    print(f"{len(items)} crops for exam {args.exam!r}; model {llm.model}")

    done = n_blank = n_not = n_err = n_mc = 0
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        log = csv.writer(f)
        if new_log:
            log.writerow(["file", "submission_id", "region_id", "label", "reason"])
        for sid, rid, src in items:
            name = src.name
            if name in seen:
                continue
            img = read_image(str(src)) if src.exists() else None
            if img is None:
                print(f"  missing: {src}")
                continue
            try:
                text = llm._chat([{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    _image_part(image_to_b64(prepare_crop(img)))]}],
                    SCHEMA, "blank_check", max_tokens=2000, temperature=0.0)
                d = _extract_json(text)
                if not isinstance(d, dict) or not isinstance(d.get("has_writing"), bool):
                    raise ValueError(f"unparseable reply: {text[:80]!r}")
            except Exception as e:
                n_err += 1
                print(f"  error on {name}: {e}")
                continue
            if d.get("multiple_choice"):
                log.writerow([name, sid, rid, "multiple_choice", d.get("reason", "")])
                f.flush()
                n_mc += 1
                print(f"  {name}: multiple choice, skipped")
                continue
            label = "not_blank" if d["has_writing"] else "blank"
            shutil.copy2(src, out / label / name)
            log.writerow([name, sid, rid, label, d.get("reason", "")])
            f.flush()
            done += 1
            n_blank += label == "blank"
            n_not += label == "not_blank"
            print(f"  {name}: {label} ({d.get('reason', '')})")
            if args.limit and done >= args.limit:
                break
    print(f"done: {done} labelled ({n_blank} blank, {n_not} not blank), {n_mc} multiple choice skipped, {n_err} errors -> {out}")


if __name__ == "__main__":
    main()
