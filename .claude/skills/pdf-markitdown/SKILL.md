---
name: pdf-markitdown
description: >-
  Convert and work with PDF files using Microsoft MarkItDown
  (https://github.com/microsoft/markitdown). Use this skill ANY time a PDF
  is involved — when the user uploads, mentions, or asks to read, extract,
  summarize, analyze, or process a .pdf file. Also triggers for: "PDF",
  ".pdf", "читай PDF", "разбери PDF", "конвертируй PDF", "извлеки текст из
  PDF", "markitdown". The skill converts the PDF to clean Markdown first,
  then works from that Markdown.
---

# Working with PDFs via MarkItDown

## Core rule

Whenever a PDF needs to be read, parsed, summarized, analyzed, or have its
content extracted, **always convert it to Markdown with Microsoft MarkItDown
first**, then work from the resulting Markdown. Do not try to eyeball the raw
PDF or guess its contents — convert, then read the Markdown.

MarkItDown preserves document structure (headings, lists, tables, links) and
produces token-efficient Markdown that is easy to reason over.

## Step 1 — Ensure MarkItDown is installed

Run in the shell. It is idempotent, so it is safe to run every time:

```bash
pip install 'markitdown[all]' --break-system-packages -q || \
  pip install 'markitdown[pdf]' --break-system-packages -q
```

Notes:
- `[all]` pulls in every optional format; `[pdf]` is the minimal install for
  PDFs and is the fallback if `[all]` fails.
- MarkItDown requires Python 3.10+.
- If `pip` complains about the managed environment, the
  `--break-system-packages` flag (already included above) resolves it.

## Step 2 — Convert the PDF to Markdown

Command-line (simplest):

```bash
markitdown "/path/to/file.pdf" -o "/path/to/file.md"
```

Or via the Python API (use when you need the text inline or want more control):

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False)
result = md.convert("/path/to/file.pdf")
print(result.text_content)   # the Markdown
```

For **scanned / image-only PDFs** (no selectable text), the plain converter
returns little or nothing. In that case use OCR — either the `markitdown-ocr`
plugin with an LLM client, or Azure Document Intelligence:

```bash
# Azure Document Intelligence (higher quality, needs an endpoint)
markitdown "/path/to/file.pdf" -o "/path/to/file.md" -d -e "<doc_intelligence_endpoint>"
```

```bash
# markitdown-ocr plugin (uses an OpenAI-compatible vision model)
pip install markitdown-ocr openai --break-system-packages -q
markitdown --use-plugins "/path/to/file.pdf" -o "/path/to/file.md"
```

## Step 3 — Read and use the Markdown

1. `Read` the generated `.md` file (or the captured `text_content`).
2. Answer the user's question, summarize, extract tables, etc., from that
   Markdown.
3. Keep the `.md` file in the outputs folder so the user can open it.

## Handling rules

- **Always convert first.** Never skip MarkItDown and try to interpret a PDF
  by other means when this skill applies.
- **Empty or garbled output → suspect a scanned PDF.** Fall back to OCR
  (Step 2) before concluding the PDF has no content.
- **Large PDFs:** convert the whole file, then read the `.md` in sections
  rather than loading everything at once.
- **Tables:** MarkItDown renders tables as Markdown tables — preserve them when
  quoting or reformatting.
- **Multiple PDFs:** convert each separately into its own `.md`, then work
  across them.
- **Security:** MarkItDown reads with the current process's privileges. Only
  convert files the user provided or trusts; do not pass untrusted remote URLs.
- **Other formats too:** MarkItDown also converts DOCX, PPTX, XLSX, HTML,
  images, audio, EPUB, ZIP, and YouTube URLs to Markdown using the same
  `markitdown <file>` pattern — reach for it whenever a non-trivial document
  needs to become clean Markdown.

## Reference

- Repo: https://github.com/microsoft/markitdown
- Install: `pip install 'markitdown[all]'`
- Convert: `markitdown file.pdf -o file.md`
