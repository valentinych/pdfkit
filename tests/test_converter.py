import copy
import sys
import unittest
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import compile_records, csv_literal, modern_spelling, normalize_interval, parse_date, words, validate_profile, native_scan_image

def fixture():
    return {"fund": "2674", "reviewed": True, "expected_last_case": 2,
            "rows": [{"case": "1", "title": "Дело по заявлению Иванова.", "start": "1903", "end": "", "page": 3, "reviewed": True, "withdrawal": "active"},
                     {"case": "2", "title": "То же.", "same_as": "1", "start": "1 августа 1903 года", "end": "", "page": 3, "reviewed": True, "withdrawal": "active"}], "excluded": []}

class ConversionRules(unittest.TestCase):
    def test_full_single_date(self):
        self.assertEqual(normalize_interval("1 августа 1903 года", ""), (date(1903, 8, 1), date(1903, 8, 1)))
    def test_year_bounds(self):
        self.assertEqual(normalize_interval("1904", ""), (date(1904, 1, 1), date(1904, 12, 31)))
    def test_leap_day(self):
        self.assertEqual(parse_date("29.02.1904", "start"), date(1904, 2, 29))
        with self.assertRaises(ValueError): parse_date("29.02.1903", "start")
    def test_missing_and_invalid(self):
        for value in ("", "190г.", "1 августа", "32.01.1903", "01.13.1903", "1 маргарита 1903"):
            with self.assertRaises(ValueError): parse_date(value, "start")
    def test_same_as_explicit(self):
        rows = compile_records(fixture())
        self.assertEqual(rows[0]["title"], rows[1]["title"])
    def test_same_as_supplement_requires_review(self):
        d = fixture(); d["rows"][1]["title"] = "То же по фабрике Петрова."
        with self.assertRaisesRegex(ValueError, "раскройте"): compile_records(d)
    def test_missing_number(self):
        d = fixture(); d["rows"] = d["rows"][:1]
        with self.assertRaisesRegex(ValueError, "Пропущены"): compile_records(d)
    def test_duplicate(self):
        d = fixture(); d["rows"][1]["case"] = "1"
        with self.assertRaisesRegex(ValueError, "повторный"): compile_records(d)
    def test_unreviewed(self):
        d = fixture(); d["reviewed"] = False
        with self.assertRaisesRegex(ValueError, "не проверен"): compile_records(d)
        d["reviewed"] = True; d["rows"][1]["reviewed"] = False
        with self.assertRaisesRegex(ValueError, "строка не проверена"): compile_records(d)
    def test_cancelled_withdrawal_kept(self):
        d = fixture(); d["rows"][0]["withdrawal"] = "cancelled"
        self.assertEqual(len(compile_records(d)), 2)
    def test_uncertain_withdrawal_blocked(self):
        d = fixture(); d["rows"][0]["withdrawal"] = "unknown"
        with self.assertRaisesRegex(ValueError, "выбывания"): compile_records(d)
    def test_confirmed_withdrawal_accounted(self):
        d = fixture(); d["rows"] = d["rows"][:1]; d["excluded"] = [{"case": "2", "withdrawal": "withdrawn", "reviewed": True, "page": 3, "note": "Незачёркнутый штамп «выбыло»"}]
        self.assertEqual(len(compile_records(d)), 1)
    def test_cancelled_cannot_be_excluded(self):
        d = fixture(); d["excluded"] = [{"case": "3", "withdrawal": "cancelled", "reviewed": True}]
        with self.assertRaisesRegex(ValueError, "незачёркнутой"): compile_records(d)
    def test_reversed_preserved_only_with_note(self):
        d = fixture(); r = d["rows"][0]; r.update(start="11.06.1914", end="21.05.1914")
        with self.assertRaisesRegex(ValueError, "обратный"): compile_records(d)
        r.update(note="Обратный диапазон в источнике", accept=["reversed_dates"])
        self.assertEqual(compile_records(d)[0]["end"], "21.05.1914")
    def test_quantity_and_count(self):
        d = fixture(); d["declared_count"] = 3
        with self.assertRaisesRegex(ValueError, "заверительной"): compile_records(d)
        d.pop("declared_count"); d["rows"][0]["quantity"] = -1
        with self.assertRaisesRegex(ValueError, "количество"): compile_records(d)
        d["rows"][0].pop("quantity"); d["rows"][0]["pages"] = [0]
        with self.assertRaisesRegex(ValueError, "страницы"): compile_records(d)
    def test_old_spelling_without_name_guessing(self):
        self.assertEqual(modern_spelling("Дѣло объ имѣніи Ѳедора"), "Дело об имении Федора")
        self.assertEqual(modern_spelling("ОТЧЁТЪ"), "ОТЧЁТ")
    def test_formula_injection(self):
        for s in ("=1+1", "+cmd", " @SUM(A1)", "-2"):
            self.assertTrue(csv_literal(s).startswith("'"))
        self.assertEqual(csv_literal("Иванов"), "Иванов")
    def test_tsv_quotes_are_literal_not_multiline_csv(self):
        header="level\tleft\ttop\twidth\theight\tconf\ttext\n"
        data=header+'5\t1\t2\t3\t4\t88\t"То\n5\t5\t6\t7\t8\t90\tже\n'
        self.assertEqual([w["text"] for w in words(data)], ['"То', 'же'])
    def test_added_letter_case_preserved_and_sorted(self):
        d = fixture(); d['rows'][1]['title'] = 'Второе дело'
        d['expected_case_ids'] = ['1', '1а', '2']
        d['rows'].append({**d['rows'][0], 'case':'1а', 'title':'Дополнительное дело'})
        self.assertEqual([r['case'] for r in compile_records(d)], ['1','1а','2'])
        d['rows'].pop()
        with self.assertRaisesRegex(ValueError, 'Пропущены номера: 1а'): compile_records(d)
    def test_letters_cannot_silently_replace_numeric_cases(self):
        d = fixture(); d['rows'][1]['case'] = '2а'
        with self.assertRaisesRegex(ValueError, 'Неожиданные номера: 2а'): compile_records(d)
        d['expected_case_ids'] = ['1','1']
        with self.assertRaisesRegex(ValueError, 'Повторный номер'): compile_records(d)
    def test_geometry_blocks_wrong_order_outside_and_duplicate_anchors(self):
        p = {'expected_case_ids':['1','1а'], 'pages':[{'page':1,'body':[.2,.9],'anchors':[{'case':'1','y':.3},{'case':'1а','y':.5}]}]}
        validate_profile(p,1)
        for y in (.1,.3,1):
            bad=copy.deepcopy(p);bad['pages'][0]['anchors'][1]['y']=y
            with self.assertRaisesRegex(ValueError,'якоря'):validate_profile(bad,1)
        bad=copy.deepcopy(p);bad['pages'][0]['anchors'][1]['case']='1'
        with self.assertRaisesRegex(ValueError,'повторный'):validate_profile(bad,1)
    def test_native_extraction_rejects_rotation_and_pdf_overlays(self):
        from pypdf import PdfWriter
        page=PdfWriter().add_blank_page(width=100,height=100)
        self.assertFalse(native_scan_image(page,'unused.png'))
        # A stamp/vector overlay must never disappear in a native-raster shortcut.
        class Content:
            operations=[([],b'q'),([100,0,0,100,0,0],b'cm'),(['/Im0'],b'Do'),([],b'Q'),([],b'Tj')]
        class Page:
            rotation=0; cropbox=[0,0,100,100]; mediabox=cropbox; images=[object()]
            def get_contents(self):return Content()
        self.assertFalse(native_scan_image(Page(),'unused.png'))
    def test_native_extraction_keeps_source_resolution(self):
        import tempfile
        from PIL import Image
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'scan.pdf'; output=Path(folder)/'scan.png'
            Image.new('RGB',(80,120),'white').save(source,'PDF',resolution=150)
            self.assertTrue(native_scan_image(PdfReader(source).pages[0],output))
            with Image.open(output) as image: self.assertEqual(image.size,(80,120))

if __name__ == "__main__": unittest.main()
