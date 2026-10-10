"""Parses ONE document page by page in a throwaway process.

Executed by app/assistant/document_cache.py with the interpreter of the
isolated parser venv (see requirements-parser.txt): Docling and
faster-whisper pull in torch/transformers/tokenizers whose pins conflict with
chromadb's, and their models are too heavy to keep loaded in the web process.
So this module must only import the standard library, docling and
faster_whisper - never the rest of the app.

Usage: python -m app.assistant.parse_worker --mode docling|audio --filename NAME
       [--max-pages N] [--whisper-model M] [--audio-page-seconds S]
       python -m app.assistant.parse_worker --mode warmup [--whisper-model M]

The file's bytes arrive on stdin. stdout carries exactly one JSON document:
    {"pages": [{"page_number", "content", "tables", "start_seconds", "end_seconds"}]}
everything else goes to stderr. Exit code 3 = the format can't be parsed
(cached as UNSUPPORTED by the caller); any other non-zero exit is a failure
that may be retried.

Docling: PDFs and images go through the layout + TableFormer (ACCURATE) + OCR
pipeline; paginated output is exported per page with
DoclingDocument.export_to_markdown(page_no=...). Formats without pages
(DOCX, XLSX, HTML, Markdown, CSV) come back as a single page 1.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys

EXIT_UNSUPPORTED = 3


class Unsupported(Exception):
    pass


def _table_entry(index: int, table, doc) -> dict:
    # The cell grid, header row included (export_to_dataframe would invent
    # numeric column labels when TableFormer marks no header).
    rows = [[cell.text for cell in row] for row in table.data.grid]
    return {"index": index, "markdown": table.export_to_markdown(doc=doc), "rows": rows}


def parse_docling(filename: str, data: bytes, max_pages: int) -> list[dict]:
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
    from docling.exceptions import ConversionError

    options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    options.table_structure_options.do_cell_matching = True
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        }
    )
    try:
        result = converter.convert(
            DocumentStream(name=filename, stream=io.BytesIO(data)),
            max_num_pages=max_pages,
            raises_on_error=True,
        )
    except ConversionError as exc:
        # Raised for formats Docling doesn't recognise ("File format not allowed").
        raise Unsupported(str(exc)) from exc
    doc = result.document

    tables_by_page: dict[int, list[dict]] = {}
    for index, table in enumerate(doc.tables):
        page_no = table.prov[0].page_no if table.prov else 1
        tables_by_page.setdefault(page_no, []).append(_table_entry(index, table, doc))

    if not doc.pages:
        tables = [t for page_tables in tables_by_page.values() for t in page_tables]
        return [{"page_number": 1, "content": doc.export_to_markdown(), "tables": tables}]

    return [
        {
            "page_number": page_no,
            "content": doc.export_to_markdown(page_no=page_no),
            "tables": tables_by_page.get(page_no, []),
        }
        for page_no in sorted(doc.pages)
    ]


def _timestamp(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_audio(data: bytes, model_name: str, page_seconds: int) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    try:
        segments, _info = model.transcribe(io.BytesIO(data), vad_filter=True)
        # transcribe() is lazy: decoding errors surface while iterating.
        segments = list(segments)
    except Exception as exc:
        # PyAV couldn't decode it - not an audio stream we can read.
        if type(exc).__module__.startswith("av"):
            raise Unsupported(f"cannot decode audio: {exc}") from exc
        raise

    windows: dict[int, list] = {}
    for segment in segments:
        windows.setdefault(int(segment.start // max(page_seconds, 1)), []).append(segment)

    pages = []
    for page_number, window in enumerate((windows[k] for k in sorted(windows)), start=1):
        pages.append(
            {
                "page_number": page_number,
                "content": "\n".join(f"[{_timestamp(s.start)}] {s.text.strip()}" for s in window),
                "tables": [],
                "start_seconds": round(window[0].start, 2),
                "end_seconds": round(window[-1].end, 2),
            }
        )
    return pages


def warmup(model_name: str) -> None:
    """Build-time model download (Dockerfile): one real conversion of a blank
    PDF makes Docling fetch every model its pipeline loads (layout,
    TableFormer, OCR), and constructing WhisperModel fetches Whisper's, so
    nothing is downloaded at request time."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument.new()
    pdf.new_page(200, 200)
    buf = io.BytesIO()
    pdf.save(buf)
    parse_docling("warmup.pdf", buf.getvalue(), max_pages=1)

    from faster_whisper import WhisperModel

    WhisperModel(model_name, device="cpu", compute_type="int8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["docling", "audio", "warmup"], required=True)
    parser.add_argument("--filename", default="document")
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--audio-page-seconds", type=int, default=300)
    args = parser.parse_args()

    if args.mode == "warmup":
        warmup(args.whisper_model)
        return 0

    data = sys.stdin.buffer.read()
    out = sys.stdout
    try:
        # Libraries that print would corrupt the JSON on stdout.
        with contextlib.redirect_stdout(sys.stderr):
            if args.mode == "docling":
                pages = parse_docling(args.filename, data, args.max_pages)
            else:
                pages = parse_audio(data, args.whisper_model, args.audio_page_seconds)
    except Unsupported as exc:
        print(f"unsupported: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED

    json.dump({"pages": pages}, out, ensure_ascii=False)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
