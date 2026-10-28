---
name: pdf
description: "Read and split PDF files (commands: text, find, split)"
run: uv run pdf.py
params:
  command: { type: string, required: true, enum: [text, find, split] }
  file:    { type: string, required: true, kind: read-path }
  query:   { type: string, description: "search text (find)" }
  pages:   { type: string, description: "page range like 1-3,5 (text, split)" }
  out:     { type: string, kind: write-path, description: "output file (split)" }
category: none
timeout: 120
output: auto
server: any
---
Params with `kind: read-path` / `write-path` are checked by the dispatcher as read()/write() before the tool runs.
Reads parameters as JSON on stdin and prints the result to stdout.
`text` prints the text of the pages, `find` lists page numbers and lines that contain the query,
`split` writes the selected pages to a new PDF. Dependencies (PyMuPDF) are declared inside pdf.py
and fetched by `uv run` on first use.
