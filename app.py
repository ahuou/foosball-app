#!/usr/bin/env python3
"""
app.py — local LAN server for the Foosball Tracker (unchanged UX).

    python app.py   ->   serves http://0.0.0.0:5000/

This is now a THIN transport: a ThreadingHTTPServer whose handler adapts each
request into webapp.handle(...). All logic lives in the shared modules:
    core.py    — pure ELO/stats/matrices + HTML rendering
    store.py   — storage backends (LocalStore here; GitHubStore on Vercel)
    webapp.py  — routing / validation / cookies / trial mode

Local mode uses LocalStore (CSV under ./data/ + best-effort git commit) and a
local secret.key — zero-config, exactly as before. The serverless entry point
is api/index.py.

Python 3 standard library only.
"""

import os
import socket
import subprocess
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core
import webapp
from store import LocalStore

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
SAMPLE_CSV = os.path.join(DATA_DIR, "sample_data.csv")
SAMPLE_XLSX = os.path.join(DATA_DIR, "sample_data.xlsx")

HOST = "0.0.0.0"
PORT = 5000

STORE = LocalStore(data_dir=DATA_DIR, app_dir=APP_DIR)


# === Sample-data fixtures (local nicety; deterministic, git-safe) ===========

def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _col_letter(idx):
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_xml(header, rows):
    numeric_cols = {"score_a", "score_b"}
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]

    def cell(col_idx, row_idx, value, numeric):
        ref = f"{_col_letter(col_idx)}{row_idx}"
        if numeric:
            return f'<c r="{ref}"><v>{_xml_escape(value)}</v></c>'
        return (f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                f"{_xml_escape(value)}</t></is></c>")

    header_cells = "".join(cell(c, 1, h, False) for c, h in enumerate(header))
    out.append(f'<row r="1">{header_cells}</row>')
    for r, row in enumerate(rows, start=2):
        cells = "".join(
            cell(c, r, row[c], header[c] in numeric_cols)
            for c in range(len(header))
        )
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_sample_files():
    """Write deterministic data/sample_data.{csv,xlsx} from core.sample_matches()."""
    import csv
    os.makedirs(DATA_DIR, exist_ok=True)
    header = list(core.MATCHES_HEADER)
    rows = [[m[h] for h in header] for m in core.sample_matches()]

    with open(SAMPLE_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Matches" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_rels,
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/worksheets/sheet1.xml": _sheet_xml(header, rows),
    }
    fixed_dt = (2026, 7, 1, 0, 0, 0)
    with zipfile.ZipFile(SAMPLE_XLSX, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, data in parts.items():
            info = zipfile.ZipInfo(arcname, date_time=fixed_dt)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)


# === HTTP handler (adapts to webapp.handle) =================================

class Handler(BaseHTTPRequestHandler):
    server_version = "FoosballTracker/2.0"

    def do_GET(self):
        webapp.serve_via_bhrh(self, "GET", STORE)

    def do_POST(self):
        webapp.serve_via_bhrh(self, "POST", STORE)

    def log_message(self, fmt, *args):
        try:
            super().log_message(fmt, *args)
        except Exception:
            pass


# === main() =================================================================

def lan_ip_best_effort():
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=3
        ).stdout.split()
        if out:
            return out[0]
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def main():
    STORE.ensure_storage()
    try:
        write_sample_files()  # refresh the deterministic sample fixtures
    except Exception:
        pass
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving on http://{HOST}:{PORT}")
    ip = lan_ip_best_effort()
    if ip:
        print(f"LAN URL:    http://{ip}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
