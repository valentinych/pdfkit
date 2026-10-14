#!/usr/bin/env python3
"""Local archival PDF -> cached OCR -> reviewed records -> template CSV/JSON.

No language-model API, network client, or autonomous agents are used.
OCR is a draft, never a source of silently inferred names or withdrawal status.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import html
import io
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

VERSION = "1.1"
HEADERS = ["Номер дела", "Наименование", "Дата с", "Дата по", "Количество", "Язык", "Описание", "Ц", "Статус", "defekt", "Примечание"]
MONTH_NAMES = {word: number for number, group in enumerate([
    "янв январь января", "фев февр февраль февраля", "мар март марта",
    "апр апрель апреля", "май мая", "июн июнь июня", "июл июль июля",
    "авг август августа", "сен сент сентябрь сентября", "окт октябрь октября",
    "ноя нояб ноябрь ноября", "дек декабрь декабря"], 1) for word in group.split()}

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def parse_date(text, endpoint):
    """Parse only explicit dates. Missing years and malformed dates are errors."""
    t = re.sub(r"\s+", " ", str(text or "").strip().lower())
    t = re.sub(r"\s*(?:года|год|г\.)\s*$", "", t).strip()
    if re.fullmatch(r"\d{4}", t):
        y = int(t)
        return date(y, 1, 1) if endpoint == "start" else date(y, 12, 31)
    m = re.fullmatch(r"(\d{1,2})[.](\d{1,2})[.](\d{4})", t)
    if m:
        d, mo, y = map(int, m.groups())
        return date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})\s*([а-яё]+)[.]?\s*(\d{4})", t)
    if m:
        d, month, y = m.groups()
        if month in MONTH_NAMES:
            return date(int(y), MONTH_NAMES[month], int(d))
    raise ValueError(f"Дата не распознана без догадки: {text!r}")

def normalize_interval(start, end):
    # A single complete date is duplicated; a single year spans that year.
    if not str(start or "").strip() and not str(end or "").strip():
        raise ValueError("Отсутствуют обе даты")
    left = start if str(start or "").strip() else end
    right = end if str(end or "").strip() else start
    return parse_date(left, "start"), parse_date(right, "end")

def modern_spelling(text):
    # Mechanical changes only: morphology, names and abbreviations need review.
    t = str(text).replace("ѣ", "е").replace("Ѣ", "Е").replace("і", "и").replace("І", "И").replace("ѳ", "ф").replace("Ѳ", "Ф")
    t = re.sub(r"(?<=[а-яёА-ЯЁ])[ъЪ](?=\b)", "", t)
    return re.sub(r"\s+", " ", t).strip()

def case_id(value):
    """Keep archival suffixes, including 107а; never renumber added files."""
    text = str(value).strip().lower()
    if not re.fullmatch(r"[1-9]\d*(?:[а-яё])?", text):
        raise ValueError(f"Некорректный номер дела {value!r}")
    return text

def case_key(value):
    match = re.fullmatch(r"(\d+)(.*)", str(value))
    return (int(match[1]), match[2]) if match else (0, str(value))

def expected_case_ids(doc):
    if "expected_case_ids" in doc:
        values = doc["expected_case_ids"]
        if not isinstance(values, list) or not values:
            raise ValueError("expected_case_ids должен содержать полный список номеров")
        ids = [case_id(v) for v in values]
        if len(set(ids)) != len(ids):
            raise ValueError("Повторный номер в expected_case_ids")
        return set(ids)
    expected = int(doc.get("expected_last_case", 0))
    if expected < 1:
        raise ValueError("Укажите expected_last_case или expected_case_ids по заверительной записи")
    return {str(i) for i in range(1, expected + 1)}

def compile_records(doc):
    if doc.get("reviewed") is not True:
        raise ValueError("Черновик не проверен: reviewed должен быть true после визуальной сверки")
    if not re.fullmatch(r"\d+", str(doc.get("fund", ""))):
        raise ValueError("Нужен цифровой номер фонда")
    required = expected_case_ids(doc)
    excluded = doc.get("excluded", [])
    ids, included, errors = set(), [], []
    for item in excluded:
        if not isinstance(item, dict) or item.get("withdrawal") != "withdrawn" or item.get("reviewed") is not True:
            raise ValueError("Исключение требует проверенной незачёркнутой пометки «выбыло»")
        case = case_id(item.get("case", ""))
        if case in ids:
            raise ValueError(f"Некорректный/повторный исключённый номер {case}")
        if type(item.get("page")) is not int or item["page"] < 1 or not item.get("note"):
            raise ValueError(f"Исключённое дело {case}: нужны страница и основание")
        ids.add(case)
    previous = None
    for source in doc.get("rows", []):
        row = dict(source)
        try:
            case = case_id(row.get("case", ""))
        except ValueError as e:
            errors.append(str(e))
            continue
        prefix = f"Дело {case}"
        if case in ids:
            errors.append(f"{prefix}: неверный или повторный номер")
            continue
        ids.add(case)
        if row.get("reviewed") is not True:
            errors.append(f"{prefix}: строка не проверена")
        state = row.get("withdrawal", "unknown")
        if state not in ("active", "cancelled"):
            errors.append(f"{prefix}: состояние выбывания не подтверждено")
        if not isinstance(row.get("page"), int) or row["page"] < 1:
            errors.append(f"{prefix}: нужна страница PDF")
        if "pages" in row and (not isinstance(row["pages"], list) or not row["pages"] or any(type(p) is not int or p < 1 for p in row["pages"]) or row.get("page") not in row["pages"]):
            errors.append(f"{prefix}: страницы должны быть положительными числами и включать исходную страницу")
        title = modern_spelling(row.get("title", ""))
        if re.match(r"^то\s+же\b", title, re.I):
            # Exact repetition is safe only with a reviewer-selected base.
            base = row.get("same_as")
            if title.rstrip(". ").lower() == "то же" and base and previous and str(base) == previous["case"]:
                title = previous["title"]
            else:
                errors.append(f"{prefix}: раскройте «То же» вручную, сохранив текущие имена и дополнения")
        if not title:
            errors.append(f"{prefix}: пустое название")
        note = str(row.get("note", "")).strip()
        if row.get("unresolved") and not note:
            errors.append(f"{prefix}: сомнительное чтение требует примечания")
        try:
            left, right = normalize_interval(row.get("start"), row.get("end"))
            if left > right and ("reversed_dates" not in row.get("accept", []) or not note):
                errors.append(f"{prefix}: обратный диапазон; нужна сверка и accept=[reversed_dates] с примечанием")
            row.update(start=left.strftime("%d.%m.%Y"), end=right.strftime("%d.%m.%Y"))
        except (ValueError, OverflowError) as e:
            errors.append(f"{prefix}: {e}")
        quantity = row.get("quantity")
        if quantity is not None and (type(quantity) is not int or quantity <= 0):
            errors.append(f"{prefix}: количество листов должно быть положительным целым или null")
        row.update(case=case, title=title, quantity=quantity)
        included.append(row)
        previous = row  # source order; no numeric sorting before resolving references
    missing, extra = required - ids, ids - required
    if missing:
        errors.append("Пропущены номера: " + ", ".join(sorted(missing, key=case_key)))
    if extra:
        errors.append("Неожиданные номера: " + ", ".join(sorted(extra, key=case_key)))
    if doc.get("declared_count") is not None and len(included) != int(doc["declared_count"]):
        errors.append(f"Количество {len(included)} не совпадает с заверительной записью {doc['declared_count']}")
    if errors:
        raise ValueError("\n".join(errors))
    return sorted(included, key=lambda r: case_key(r["case"]))

def csv_literal(value):
    s = str(value or "")
    # Prevent source text from becoming a formula during CSV import.
    return "'" + s if s.lstrip().startswith(("=", "+", "-", "@")) else s

def export_records(doc, rows, output):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "records.json", {**doc, "rows": rows})
    with (out / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        for row in rows:
            pages = row.get("pages", [row["page"]])
            note = f"Источник: NIAB_LF_{doc['fund']}_1.pdf, стр. {', '.join(map(str, pages))}."
            if row.get("note"):
                note += " " + row["note"]
            writer.writerow([row["case"], csv_literal(row["title"]), row["start"], row["end"], row["quantity"] or "", "", "", "", "", "", csv_literal(note)])
    return out / "inventory.csv"

def run(args):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, text=True).stdout

def words(tsv):
    result = []
    # Tesseract TSV contains literal quote characters, not CSV-quoted fields.
    for r in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if r.get("level") == "5" and r.get("text", "").strip():
            result.append({"text": r["text"], "x": int(r["left"]), "y": int(r["top"]), "w": int(r["width"]), "h": int(r["height"]), "conf": float(r["conf"])})
    return result

def region_text(ws, x0, x1, y0, y1, width, height):
    chosen = [w for w in ws if x0 * width <= w["x"] + w["w"] / 2 < x1 * width and y0 * height <= w["y"] + w["h"] / 2 < y1 * height]
    chosen.sort(key=lambda w: (round(w["y"] / 12), w["x"]))
    return " ".join(w["text"] for w in chosen), min((w["conf"] for w in chosen), default=0)

def native_scan_image(page, output):
    """Extract only a sole, full-page, unrotated raster with no PDF overlays."""
    from PIL import Image
    if page.rotation or list(page.cropbox) != list(page.mediabox) or len(page.images) != 1:
        return False
    operations = page.get_contents().operations
    if [op for _, op in operations] != [b"q", b"cm", b"Do", b"Q"]:
        return False
    matrix = list(map(float, operations[1][0]))
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    expected = [width, 0, 0, height, 0, 0]
    if any(abs(a - b) > .02 for a, b in zip(matrix, expected)) or list(map(float, page.mediabox.lower_left)) != [0, 0]:
        return False
    with Image.open(io.BytesIO(page.images[0].data)) as image:
        image.save(output)
    return True

def validate_profile(profile, page_count):
    expected = expected_case_ids(profile)
    seen_pages, seen_cases = set(), set()
    def bounds(value, label):
        if not isinstance(value, list) or len(value) != 2 or not all(type(x) in (int, float) for x in value) or not 0 <= value[0] < value[1] <= 1:
            raise ValueError(f"Некорректные границы {label}: {value}")
    if not profile.get("pages"):
        raise ValueError("Профиль не содержит страниц")
    for pg in profile["pages"]:
        number = pg.get("page")
        if type(number) is not int or not 1 <= number <= page_count or number in seen_pages:
            raise ValueError(f"Некорректная/повторная страница {number}")
        seen_pages.add(number)
        body = pg.get("body", [.15, .94])
        bounds(body, f"body страницы {number}")
        bounds(pg.get("content", [.06, .94]), f"content страницы {number}")
        for field, value in pg.get("columns", profile.get("columns", {})).items():
            bounds(value, f"{field} страницы {number}")
        anchors = pg.get("anchors", [])
        previous_y = -1
        page_cases = []
        for anchor in anchors:
            identifier, y = case_id(anchor.get("case", "")), anchor.get("y")
            if identifier not in expected or identifier in seen_cases:
                raise ValueError(f"Неожиданный/повторный якорь дела {identifier}")
            if type(y) not in (int, float) or not body[0] <= y < body[1] or y <= previous_y:
                raise ValueError(f"Неверный порядок/граница якоря дела {identifier} на странице {number}")
            pages = anchor.get("pages", [number])
            if not isinstance(pages, list) or number not in pages or any(type(p) is not int or not 1 <= p <= page_count for p in pages) or len(pages) != len(set(pages)):
                raise ValueError(f"Неверные страницы продолжения дела {identifier}")
            seen_cases.add(identifier); page_cases.append(identifier); previous_y = y
        if anchors and pg.get("cases") and page_cases != [case_id(c) for c in pg["cases"]]:
            raise ValueError(f"Якоря страницы {number} не совпадают с cases")

def review_html(doc, output):
    cards = []
    labels = {"case": "Номер дела", "title": "Наименование", "start": "Начальная дата", "end": "Конечная дата", "quantity": "Количество листов", "note": "Примечание", "same_as": "Предыдущее дело для точного «То же.»", "pages": "Страницы PDF (через запятую)"}
    for row in doc["rows"]:
        view = {**row, "pages": ",".join(map(str, row.get("pages", [row["page"]])))}
        fields = "".join(f'<label>{labels[k]}' + (f'<textarea rows="3" data-field="{k}">{html.escape(str(view.get(k, "") or ""))}</textarea>' if k in ("title", "note") else f'<input data-field="{k}" value="{html.escape(str(view.get(k, "") or ""), quote=True)}">') + '</label>' for k in ("case", "title", "start", "end", "quantity", "note", "same_as", "pages"))
        image = html.escape(row.get("image", ""), quote=True)
        contrast = html.escape(row.get("contrast_image", ""), quote=True)
        full_page = html.escape(row.get("page_image", ""), quote=True)
        links = row.get("page_images", [{"page":row["page"],"image":full_page}] if full_page else [])
        views = (f'<details><summary>Контрастный фрагмент</summary><a href="{contrast}" target="_blank"><img src="{contrast}" alt="Фрагмент с усиленным контрастом"></a></details>' if contrast else "") + " ".join(f'<a href="{html.escape(link["image"],quote=True)}" target="_blank">Полная страница {link["page"]}</a>' for link in links)
        cards.append(f'<article data-index="{len(cards)}"><h2>Страница {row["page"]}, {html.escape(row["id"])}</h2><a href="{image}" target="_blank"><img src="{image}" alt="Фрагмент исходного скана"></a>{views}<p>{html.escape("; ".join(row.get("issues", [])))}</p>{fields}<label>Выбывание<select data-field="withdrawal"><option value="unknown">Проверить</option><option value="active">Пометки нет</option><option value="cancelled">Пометка зачёркнута</option><option value="withdrawn">Незачёркнутая пометка «выбыло»</option></select></label><label><input type="checkbox" data-field="unresolved">Есть сомнительное чтение (поясните в note)</label><label><input type="checkbox" data-field="reversed">Обратные даты сверены (поясните в note)</label><label><input type="checkbox" data-field="checked">Сверено со сканом</label><button class="add">Ещё одно дело с этого фрагмента</button> <button class="remove">Убрать карточку</button></article>')
    payload = json.dumps(doc, ensure_ascii=False).replace("<", "\\u003c")
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Проверка описи</title><style>body{font:16px Arial;max-width:1100px;margin:auto;padding:24px}article{border-top:1px solid #aaa;padding:20px 0}img{max-width:100%;height:auto}textarea{width:98%;font:inherit;padding:7px}label{display:block;margin:10px 0}input:not([type=checkbox]){width:98%;font:inherit;padding:7px}button{padding:12px;font:inherit}p{color:#9b4d00}</style><h1>Сверка описи</h1><p>OCR — черновик. Проверьте имена, даты и правое поле. Для длинного заголовка укажите продолжение в примечании и исправьте запись. Рукописную страницу без распознанных номеров нужно разбить на отдельные записи в JSON. Непроверенные строки не попадут в готовый результат.</p><button id="save">Скачать исправления JSON</button>''' + "".join(cards) + '<script id="data" type="application/json">' + payload + '</script><script>const data=JSON.parse(document.querySelector("#data").textContent);document.querySelector("#save").onclick=()=>{const rows=[],excluded=[];let all=true;document.querySelectorAll("article").forEach((el,i)=>{const row={...data.rows[i]};el.querySelectorAll("[data-field]").forEach(x=>row[x.dataset.field]=x.type==="checkbox"?x.checked:x.value);if(!row.checked)all=false;row.reviewed=row.checked;if(row.withdrawal==="withdrawn")excluded.push(row);else rows.push(row)});const result={...data,rows,excluded,reviewed:all};const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:"application/json"}));a.download="reviewed.json";a.click();URL.revokeObjectURL(a.href)};</script></html>'
    # Keep source cards editable without an LLM, including several cases on a handwritten page.
    page = page.replace('const row={...data.rows[i]};', 'const row={...data.rows[Number(el.dataset.index)]};')
    page = page.replace('row.reviewed=row.checked;', 'if(row.quantity && !/^[1-9]\\d*$/.test(row.quantity)){alert("Неверное количество листов в деле "+row.case);throw Error("Invalid quantity")};row.quantity=row.quantity ? Number(row.quantity) : null;row.pages=row.pages ? row.pages.split(",").map(p=>Number(p.trim())) : [row.page];row.accept=row.reversed ? ["reversed_dates"] : [];delete row.reversed;row.reviewed=row.checked;')
    page = page.replace('Рукописную страницу без распознанных номеров нужно разбить на отдельные записи в JSON.', 'Для нескольких дел на одной рукописной странице используйте кнопку «Ещё одно дело с этого фрагмента».')
    page = page.replace('</script></html>', '''document.addEventListener("click",e=>{if(e.target.className==="remove")e.target.closest("article").remove();if(e.target.className==="add"){const old=e.target.closest("article"),copy=old.cloneNode(true);const index=data.rows.length;data.rows.push({...data.rows[Number(old.dataset.index)],id:data.rows[Number(old.dataset.index)].id+":manual:"+index});copy.dataset.index=index;copy.querySelectorAll("input,textarea").forEach(x=>{if(x.type==="checkbox")x.checked=false;else x.value=""});copy.querySelector("select").value="unknown";old.after(copy)}});</script></html>''')
    Path(output).write_text(page, encoding="utf-8")

def scan(pdf, profile_path, output):
    from PIL import Image, ImageOps
    from pypdf import PdfReader
    profile = read_json(profile_path)
    reader = PdfReader(pdf)
    validate_profile(profile, len(reader.pages))
    for binary in ("pdftoppm", "tesseract"):
        if not shutil.which(binary):
            raise ValueError(f"Установите {binary}")
    sha = digest(pdf)
    if profile.get("source_sha256") and profile["source_sha256"] != sha:
        raise ValueError("Профиль относится к другому PDF; исправьте профиль, сверив новый источник")
    engine = run(["tesseract", "--version"]).splitlines()[0]
    params = {"version": VERSION, "source_sha256": sha, "engine": engine, "dpi": profile.get("dpi", 220), "language": "rus", "psm": profile.get("psm", 6), "thresholding_method": 2, "preprocess": "white-margins-v1", "prefer_native_scan": profile.get("prefer_native_scan", False), "page_regions": [[p["page"], p.get("body", [.15, .94]), p.get("content", [.06, .94])] for p in profile["pages"]]}
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    root = Path(output)
    cache = root / "cache" / key
    cache.mkdir(parents=True, exist_ok=True)
    doc = {"fund": profile["fund"], "reviewed": False, "source_sha256": sha, "source_pdf": str(Path(pdf).resolve()), "ocr_parameters": params, "rows": [], "excluded": []}
    for field in ("expected_last_case", "expected_case_ids", "declared_count"):
        if field in profile: doc[field] = profile[field]
    for pg in profile["pages"]:
        page = int(pg["page"])
        image_path, tsv_path = cache / f"page-{page}.png", cache / f"page-{page}.tsv"
        if not image_path.exists():
            extracted = params["prefer_native_scan"] and native_scan_image(reader.pages[page - 1], image_path)
            if not extracted:
                run(["pdftoppm", "-f", page, "-l", page, "-singlefile", "-r", params["dpi"], "-png", str(Path(pdf).resolve()), str(image_path.with_suffix(""))])
        if not pg.get("handwritten") and not tsv_path.exists():
            original = Image.open(image_path).convert("L")
            width, height = original.size
            body = pg.get("body", [.15, .94])
            content = pg.get("content", [.06, .94])
            box = (int(width * content[0]), int(height * body[0]), int(width * content[1]), int(height * body[1]))
            processed = Image.new("L", original.size, 255)
            processed.paste(ImageOps.autocontrast(original.crop(box), cutoff=1), box[:2])
            ocr_path = cache / f"ocr-page-{page}.png"
            processed.save(ocr_path)
            tsv = run(["tesseract", ocr_path, "stdout", "-l", "rus", "--psm", params["psm"], "-c", "thresholding_method=2", "tsv"])
            tsv_path.write_text(tsv, encoding="utf-8")
        ws = [] if pg.get("handwritten") else words(tsv_path.read_text(encoding="utf-8"))
        im = Image.open(image_path)
        width, height = im.size
        columns = pg.get("columns", profile.get("columns", {}))
        body = pg.get("body", [0.15, 0.94])
        nx0, nx1 = columns.get("case", [0.07, 0.16])
        anchors = []
        if not pg.get("handwritten", False):
            for w in ws:
                token = w["text"].strip(".():;")
                if token.isdigit() and nx0 <= (w["x"] + w["w"] / 2) / width < nx1 and body[0] <= w["y"] / height < body[1]:
                    anchors.append((token, max(body[0], w["y"] / height - 0.008)))
            anchors.sort(key=lambda a: a[1])
        if pg.get("anchors"):
            # Explicit positions are reviewed source geometry, never fuzzy OCR guesses.
            anchors = [(str(a["case"]), float(a["y"])) for a in pg["anchors"]]
        expected_ids = set(map(str, pg.get("cases", [])))
        if expected_ids:
            anchors = [a for a in anchors if a[0] in expected_ids]
        if not anchors:
            # Never silently discard a page whose row segmentation failed.
            anchors = [("", body[0])]
        found = [a[0] for a in anchors if a[0]]
        for i, (case, y0) in enumerate(anchors):
            y1 = anchors[i + 1][1] if i + 1 < len(anchors) else body[1]
            issues = ["Сверить весь текст и правую колонку со сканом"]
            if pg.get("anchors"):
                issues.append("Номер и границы взяты из профиля; сверить со сканом")
            row = {"id": f"{sha[:12]}:{page}:{i}", "case": case, "page": page, "withdrawal": "unknown", "quantity": None, "note": "", "issues": issues, "checked": False}
            if pg.get("anchors"):
                row["pages"] = pg["anchors"][i].get("pages", [page])
            for field in ("title", "start", "end"):
                x0, x1 = columns.get(field, [0.25, 0.65])
                text, confidence = region_text(ws, x0, x1, y0, y1, width, height)
                row[field] = text
                row[f"ocr_{field}"] = text
                if confidence < 75 and not pg.get("handwritten"):
                    issues.append(f"Низкая уверенность OCR: {field}")
            if pg.get("handwritten"):
                issues.append("Рукопись: автоматическое распознавание не принято; разбить страницу на дела вручную")
            if set(found) != expected_ids and expected_ids:
                issues.append("Номера страницы не совпадают с профилем: " + ",".join(sorted(expected_ids - set(found), key=case_key)))
            if len(found) != len(set(found)):
                issues.append("Повторный номер при сегментации")
            if re.match(r"^то\s+же", row["title"], re.I):
                issues.append("Раскрыть «То же» по предыдущей исходной записи")
            crop = cache / f"row-{page}-{i}.png"
            im.crop((0, int(y0 * height), width, int(y1 * height))).save(crop)
            row["image"] = str(crop.relative_to(root))
            enhanced = cache / f"row-{page}-{i}-contrast.png"
            ImageOps.autocontrast(Image.open(crop).convert("L"), cutoff=.5).save(enhanced)
            row["contrast_image"] = str(enhanced.relative_to(root))
            row["page_image"] = str(image_path.relative_to(root))
            doc["rows"].append(row)
        print(f"Страница {page}: {len(anchors)} фрагментов (кэш {key})", flush=True)
    for row in doc["rows"]:
        row["page_images"] = [{"page":p,"image":str((cache / f"page-{p}.png").relative_to(root))} for p in row.get("pages", [row["page"]]) if (cache / f"page-{p}.png").exists()]
    write_json(root / "draft.json", doc)
    review_html(doc, root / "review.html")
    return doc

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sc = sub.add_parser("scan", help="PDF -> локальный OCR и очередь проверки")
    sc.add_argument("pdf"); sc.add_argument("--profile", required=True); sc.add_argument("--output", required=True)
    co = sub.add_parser("compile", help="Проверенный JSON -> CSV/JSON по шаблону")
    co.add_argument("reviewed"); co.add_argument("--output", required=True); co.add_argument("--source-pdf")
    args = ap.parse_args()
    try:
        if args.command == "scan":
            scan(args.pdf, args.profile, args.output)
        else:
            doc = read_json(args.reviewed)
            source_pdf = args.source_pdf or doc.get("source_pdf")
            if doc.get("source_sha256"):
                if not source_pdf:
                    raise ValueError("Укажите --source-pdf для проверки SHA-256")
                if digest(source_pdf) != doc["source_sha256"]:
                    raise ValueError("PDF изменился; старые исправления нельзя применять")
            rows = compile_records(doc)
            print(f"{len(rows)} дел -> {export_records(doc, rows, args.output)}")
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
