"""Renders README.md to docs/report.html (print it to PDF for SeismoPulse_Report.pdf).

    pip install markdown && python docs/build_report.py
"""
import markdown, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
md = (root / "README.md").read_text(encoding="utf-8")  # it has ₹, − and →, which cp1252 lacks
html = markdown.markdown(md, extensions=["tables", "fenced_code"])
css = """@page{size:A4;margin:14mm}body{font-family:'DejaVu Sans',Arial,sans-serif;font-size:9.8pt;line-height:1.45;color:#1d1d1f}
h1{font-size:22pt;color:#1f6f8b;margin:0 0 4px}h2{font-size:12.5pt;color:#1f6f8b;border-bottom:1px solid #d6e4ea;margin:14px 0 5px;page-break-after:avoid}
h3{font-size:10.5pt;page-break-after:avoid}table{border-collapse:collapse;width:100%;margin:6px 0;font-size:9pt;page-break-inside:avoid}
th,td{border:1px solid #c9d3d8;padding:3px 6px;text-align:left}th{background:#eef4f7}img{max-width:100%;display:block;margin:6px auto;page-break-inside:avoid}
pre{background:#f6f6f6;border:1px solid #ddd;padding:6px;font-size:8pt;white-space:pre-wrap;page-break-inside:avoid}code{font-size:8.5pt}
blockquote{background:#f3f8fa;border-left:4px solid #1f6f8b;margin:6px 0;padding:4px 10px}"""
(root / "docs" / "report.html").write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>SeismoPulse report</title><style>{css}</style></head><body>{html}</body></html>", encoding="utf-8")
