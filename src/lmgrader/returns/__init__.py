"""Returning graded tests to students: email (SMTP) and LMS bulk-upload exports."""
from .items import ReturnItem, collect_items, ensure_pdf, split_name
from .mailer import build_message, send_batch, send_test, render, get_password, set_password, keyring_available
from .lms import export_lms, export_pdfs, PRESETS
from .canvas import CanvasClient, CanvasError, match_students, upload_batch

__all__ = ["ReturnItem", "collect_items", "ensure_pdf", "split_name", "build_message", "send_batch",
           "send_test", "render", "get_password", "set_password", "keyring_available", "export_lms", "export_pdfs", "PRESETS",
           "CanvasClient", "CanvasError", "match_students", "upload_batch"]
