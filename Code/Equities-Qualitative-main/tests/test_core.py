import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
from qualitative_ir_downloader.filesystem import sanitize_filename, company_folder, document_path
from qualitative_ir_downloader.google_sheets import parse_rows
from qualitative_ir_downloader.urls import canonicalize_url, Scope
from qualitative_ir_downloader.scoring import score_ir, section_score
from qualitative_ir_downloader.models import Stock, Link, CollectionError
from qualitative_ir_downloader.extraction import extract_date, release_link
from qualitative_ir_downloader.pdf_utils import validate_pdf
from qualitative_ir_downloader.main import select_stocks, parser
from qualitative_ir_downloader.config import Config, DEFAULT_SERVICE_ACCOUNT_PATH
from qualitative_ir_downloader.google_sheets import read_stocks

class CoreTests(unittest.TestCase):
    def test_atomic_json_retries_transient_windows_lock(self):
        import json
        from unittest.mock import patch
        from qualitative_ir_downloader.filesystem import atomic_json
        original = Path.replace
        attempts = []
        def locked_once(source, target):
            attempts.append(source)
            if len(attempts) == 1:
                raise PermissionError("temporary sharing lock")
            return original(source, target)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "manifest.json"
            with patch.object(Path, "replace", locked_once), patch("qualitative_ir_downloader.filesystem.time.sleep"):
                atomic_json(target, {"status":"DOWNLOADED"})
            self.assertEqual(json.loads(target.read_text())["status"], "DOWNLOADED")
            self.assertEqual(len(attempts), 2)

    def test_atomic_json_permanent_lock_is_not_hidden(self):
        from unittest.mock import patch
        from qualitative_ir_downloader.filesystem import atomic_json
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "manifest.json"
            with patch.object(Path, "replace", side_effect=PermissionError("locked")) as replace, patch("qualitative_ir_downloader.filesystem.time.sleep"):
                with self.assertRaises(PermissionError):
                    atomic_json(target, {})
                self.assertEqual(replace.call_count, 5)

    def test_windows_names(self):
        self.assertEqual(sanitize_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename(" .. "), "untitled")
        self.assertLessEqual(len(sanitize_filename("x"*1000)), 120)

    def test_folders_and_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = company_folder(root, "BRK.B", "Acme / Company", "2026-09-06_10-45-32")
            self.assertTrue((folder / "News Releases").is_dir())
            self.assertTrue((folder / "Reports").is_dir())
            self.assertTrue((folder / "logs").is_dir())
            self.assertTrue(folder.name.startswith("BRK.B_Acme _ Company_qualitative analysis_"))
            other = company_folder(root, "BRK.B", "Acme / Company", "2026-09-06_10-45-32")
            self.assertNotEqual(folder, other)

    def test_document_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            first = document_path(Path(temp), "EXEL", "News: an update", None)
            self.assertEqual(first.name, "UNKNOWN-DATE_EXEL_News_ an update.pdf")
            first.touch()
            second = document_path(Path(temp), "EXEL", "News: an update", None)
            self.assertTrue(second.name.endswith("_2.pdf"))

    def test_url_identity_preserves_meaningful_query(self):
        self.assertEqual(canonicalize_url("https://EXAMPLE.com:443/news/?utm_source=x&year=2024#top"), "https://example.com/news?year=2024")
        self.assertNotEqual(canonicalize_url("https://example.com?page=1"), canonicalize_url("https://example.com?page=2"))
        self.assertEqual(canonicalize_url("mailto:test@example.com"), "")
        self.assertEqual(canonicalize_url("https://example.com:bad"), "")

    def test_scope(self):
        scope = Scope(["https://ir.acme.com/"])
        self.assertTrue(scope.allows("https://ir.acme.com/news"))
        self.assertFalse(scope.allows("https://evil.com/news"))
        self.assertFalse(scope.allows("https://ir.acme.com.evil.com/news"))

    def test_sheet_rows(self):
        rows = [["EXEL", "Exelixis"], [], ["BAD"], ["", "Missing ticker"], ["BRK.B", "Berkshire"]]
        with self.assertLogs("qualitative_ir_downloader.google_sheets", level="ERROR") as logs:
            result = parse_rows(rows, "Safe")
        self.assertEqual([r.spreadsheet_row for r in result], [4, 8])
        self.assertEqual(len(logs.output), 2)

    def test_short_worksheet_is_in_the_universe(self):
        from qualitative_ir_downloader.config import DOWNLOAD_ROOT, WORKSHEETS
        from pipeline_runner.runner import DEFAULT_DATA_ROOT, PipelineRunner

        self.assertIn("Short Secondary Summary", WORKSHEETS)
        self.assertEqual(DOWNLOAD_ROOT, DEFAULT_DATA_ROOT / "_Acquisition_Cache")
        self.assertEqual(PipelineRunner("ABC", "2026-09-26", mode="fresh").download_root, DOWNLOAD_ROOT)
        self.assertEqual(parse_rows([["ABC", "Acme"]], "Short Secondary Summary")[0].worksheet,
                         "Short Secondary Summary")
        self.assertEqual(parse_rows([["ABC", "Acme"]], "Short Secondary Summary")[0].selection_side,
                         "short")

    def test_sheet_refresh_recreates_deleted_cache_root(self):
        with tempfile.TemporaryDirectory() as temp, patch("gspread.service_account") as connect:
            book = connect.return_value.open_by_key.return_value
            book.worksheet.return_value.get.return_value = [["ABC", "Acme"]]
            cache_root = Path(temp) / "missing" / "_Acquisition_Cache"
            stocks = read_stocks(Config(download_root=cache_root))
            self.assertEqual(len(stocks), 4)
            self.assertTrue((cache_root / "stock_metadata.json").exists())

    def test_ir_scoring_rejects_information_sites(self):
        stock = Stock("ACME", "Acme", "Safe", 4)
        body = "Acme ACME Investor Relations News Press Releases Annual Reports Stock Information"
        official = score_ir("https://ir.acme.com", "Acme Investor Relations", body, stock)
        other = score_ir("https://finance.yahoo.com/acme", "Acme Investor Relations", body, stock)
        unverified = score_ir("https://random.com/acme", "Acme Investor Relations", body, stock)
        hosted = score_ir("https://provider.com/acme", "Acme Investor Relations", body, stock, True)
        self.assertGreaterEqual(official.score, 75)
        self.assertLess(other.score, 0)
        self.assertLess(unverified.score, 75)
        self.assertGreaterEqual(hosted.score, 75)

    def test_section_scoring(self):
        self.assertGreater(section_score(Link("https://ir.acme.com/news", "News Releases"), "news").score, 12)
        self.assertLess(section_score(Link("https://ir.acme.com/privacy", "Privacy"), "reports").score, 0)
        self.assertLess(section_score(Link("https://ir.acme.com/sec-filings", "SEC Filings"), "reports").score, 0)

    def test_release_dates(self):
        self.assertEqual(extract_date("Published August 14, 2026"), "2026-08-14")
        self.assertEqual(extract_date("2026-09-06T12:00:00Z"), "2026-09-06")
        self.assertIsNone(extract_date("latest news"))
        self.assertTrue(release_link(Link("https://ir.acme.com/news-release-details/acme-update", "Acme update")))
        self.assertFalse(release_link(Link("https://ir.acme.com/news", "News Releases")))

    def test_pdf_validation_and_hash(self):
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        buffer = io.BytesIO()
        writer.write(buffer)
        self.assertEqual(validate_pdf(buffer.getvalue()), validate_pdf(buffer.getvalue()))
        with self.assertRaises(CollectionError):
            validate_pdf(b"<html>Not a PDF</html>")
        with self.assertRaises(CollectionError):
            validate_pdf(b"%PDF-1.7 broken")

    def test_pdf_validation_accepts_passwordless_owner_encryption(self):
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        writer.encrypt("")
        buffer = io.BytesIO()
        writer.write(buffer)
        self.assertEqual(len(validate_pdf(buffer.getvalue())), 64)

    def test_selection(self):
        stocks = [Stock("A", "A", "Safe", 4), Stock("B", "B", "Safe", 5), Stock("C", "C", "Turnaround Story", 4)]
        self.assertEqual(select_stocks(stocks, start_ticker="b", limit=1), [stocks[1]])
        self.assertEqual(select_stocks(stocks, ticker="c"), [stocks[2]])
        with self.assertRaises(ValueError):
            select_stocks(stocks, ticker="missing")
        self.assertEqual(parser().parse_args([]).service_account, DEFAULT_SERVICE_ACCOUNT_PATH)
