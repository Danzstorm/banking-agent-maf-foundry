"""Render knowledge/docs/*.md to PDF and upload them to the knowledge Blob container.

    export STORAGE_ACCOUNT_NAME=$(terraform -chdir=infra output -raw storage_account_name)
    uv run --group knowledge python knowledge/publish.py            # render + upload
    uv run --group knowledge python knowledge/publish.py --no-upload

Auth is Entra ID only (the storage account disables shared keys): your az login needs
Storage Blob Data Contributor, which Terraform assigns.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
import markdown
from azure.identity import DefaultAzureCredential
from fpdf import FPDF

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DIST = ROOT / "dist"
CONTAINER = "productos"
BLOB_API_VERSION = "2025-01-05"


def title_of(md_text: str) -> str:
    """The document title is its first H1."""
    for line in md_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise ValueError("document without an H1 title")


class TitledPDF(FPDF):
    """Repeats the document title at the top of every page.

    The knowledge source chunks PDFs per page, so each chunk then starts with its document
    title and the agent can cite it.
    """

    def header(self) -> None:
        self.set_font("Helvetica", size=9)
        self.cell(0, 6, self.title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.set_font("Helvetica", size=11)


def render_pdf(md_path: Path, out_dir: Path) -> Path:
    text = md_path.read_text(encoding="utf-8")
    html = markdown.markdown(text, extensions=["tables"])
    pdf = TitledPDF()
    pdf.title = title_of(text)
    pdf.set_title(pdf.title)
    pdf.set_author("Banco Andino Demo (ficticio)")
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)  # core font: Latin-1 covers Spanish accents
    pdf.write_html(html)
    out = out_dir / f"{md_path.stem}.pdf"
    pdf.output(str(out))
    return out


def upload(paths: list[Path], account: str) -> None:
    token = DefaultAzureCredential().get_token("https://storage.azure.com/.default").token
    base = f"https://{account}.blob.core.windows.net/{CONTAINER}"
    with httpx.Client(timeout=60) as client:
        for path in paths:
            resp = client.put(
                f"{base}/{path.name}",
                content=path.read_bytes(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-ms-version": BLOB_API_VERSION,
                    "x-ms-blob-type": "BlockBlob",
                    "Content-Type": "application/pdf",
                },
            )
            resp.raise_for_status()
            print(f"subido {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Renderiza los documentos a PDF y los sube.")
    parser.add_argument("--no-upload", action="store_true", help="Solo renderiza.")
    args = parser.parse_args()
    DIST.mkdir(exist_ok=True)
    pdfs = [render_pdf(md, DIST) for md in sorted(DOCS.glob("*.md"))]
    print(f"{len(pdfs)} PDF en {DIST}")
    if args.no_upload:
        return 0
    account = os.environ.get("STORAGE_ACCOUNT_NAME", "").strip()
    if not account:
        print("Falta STORAGE_ACCOUNT_NAME.", file=sys.stderr)
        return 2
    upload(pdfs, account)
    return 0


if __name__ == "__main__":
    sys.exit(main())
