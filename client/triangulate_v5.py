import os
import re
import json
import base64
import csv
import time
import threading
import requests
import paramiko
import xml.etree.ElementTree as ET
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytesseract
from PIL import Image, ImageEnhance
import cv2
from paddleocr import PaddleOCR
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel, Field

# Tesseract Config
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
tess_config = r'--oem 3 --psm 3 -l ben+eng'

CATALOG_FIELDS = [
    "item_barcode",
    "title",
    "subtitle",
    "author",
    "responsibility",
    "accession",
    "publisher",
    "year",
    "edition",
    "isbn",
    "issn",
    "pages",
    "place",
    "notes",
    "series_title",
    "abstract",
    "volume",
    "illus",
    "size",
    "url",
    "price",
    "copy",
    "subjects",
    "authormarc",
    "cutter_strict",
    "call_number",
]

DDC_SUBJECT_MAP = {
    'physics': '530', 'chemistry': '540', 'biology': '570', 'math': '510',
    'algebra': '512', 'calculus': '515', 'geometry': '516',
    'engineering': '620', 'marine': '623.8', 'nautical': '623.8',
    'ship': '623.8', 'navigation': '623.89', 'cargo': '387.5',
    'seamanship': '623.88', 'thermodynamics': '536.7', 'mechanics': '531',
    'english': '420', 'literature': '820', 'accounting': '657',
    'management': '658', 'computer': '004', 'programming': '005.1',
    'medical': '610', 'health': '613', 'law': '340', 'ocean': '551.46',
    'fish': '639.2', 'fisheries': '639.2', 'dictionary': '423', 'grammar': '425'
}

SUBJECT_HEADING_MAP = {
    'physics': 'Physics',
    'chemistry': 'Chemistry',
    'biology': 'Biology',
    'math': 'Mathematics',
    'algebra': 'Algebra',
    'calculus': 'Calculus',
    'geometry': 'Geometry',
    'engineering': 'Engineering',
    'marine': 'Marine technology',
    'nautical': 'Nautical science',
    'ship': 'Ships',
    'navigation': 'Navigation',
    'cargo': 'Cargo handling',
    'seamanship': 'Seamanship',
    'thermodynamics': 'Thermodynamics',
    'mechanics': 'Mechanics',
    'english': 'English language',
    'literature': 'Literature',
    'accounting': 'Accounting',
    'management': 'Management',
    'computer': 'Computer science',
    'programming': 'Programming',
    'medical': 'Medicine',
    'health': 'Health',
    'law': 'Law',
    'ocean': 'Oceanography',
    'fish': 'Fishes',
    'fisheries': 'Fisheries',
    'dictionary': 'Dictionaries',
    'grammar': 'Grammar',
}

STRICT_CUTTER_SECOND_MAP = {
    'A': '2', 'B': '3', 'C': '4', 'D': '5', 'E': '6', 'F': '7', 'G': '8', 'H': '9',
    'I': '12', 'J': '13', 'K': '14', 'L': '15', 'M': '16', 'N': '17', 'O': '18',
    'P': '19', 'Q': '22', 'R': '23', 'S': '24', 'T': '25', 'U': '26', 'V': '27',
    'W': '28', 'X': '29', 'Y': '32', 'Z': '33'
}

STRICT_CUTTER_THIRD_MAP = {
    'A': '1', 'B': '2', 'C': '3', 'D': '4', 'E': '5', 'F': '6', 'G': '7', 'H': '8', 'I': '9',
    'J': '1', 'K': '2', 'L': '3', 'M': '4', 'N': '5', 'O': '6', 'P': '7', 'Q': '8', 'R': '9',
    'S': '1', 'T': '2', 'U': '3', 'V': '4', 'W': '5', 'X': '6', 'Y': '7', 'Z': '8'
}

DEFAULT_BRANCH = os.environ.get("TRIANGULATE_V5_BRANCH", "MFA").strip() or "MFA"
DEFAULT_SHELVING_LOCATION = os.environ.get("TRIANGULATE_V5_SHELVING_LOCATION", "GEN").strip() or "GEN"
DEFAULT_ITEMTYPE = os.environ.get("TRIANGULATE_V5_ITEMTYPE", "BOOK").strip() or "BOOK"
DEFAULT_CN_SOURCE = os.environ.get("TRIANGULATE_V5_CN_SOURCE", "ddc").strip() or "ddc"
SKIP_COVER_PHOTO_UPLOAD = os.environ.get("TRIANGULATE_V5_SKIP_COVER", "1").strip().lower() in {"1", "true", "yes", "on"}


def get_env_int(name, default_value, min_value=1, max_value=64):
    raw = os.environ.get(name, str(default_value)).strip()
    try:
        value = int(raw)
    except Exception:
        value = default_value
    if value < min_value:
        value = min_value
    if value > max_value:
        value = max_value
    return value


def make_item_key(barcode, folder_path):
    bc = normalize_barcode(barcode)
    norm_folder = os.path.normcase(os.path.normpath(folder_path or ""))
    return f"{bc}|{norm_folder}"


def get_resume_paths(base_dir):
    state_file = os.path.join(base_dir, "triangulation_v5_resume_state.json")
    progress_file = os.path.join(base_dir, "triangulation_v5_processed_keys.txt")
    return state_file, progress_file


def load_processed_keys_from_csv(csv_path):
    keys = set()
    if not os.path.exists(csv_path):
        return keys
    latest_state_by_key = {}

    def _is_deferred_row(row):
        method = (row.get('Method', '') or '').strip().lower()
        action = (row.get('Action', '') or '').strip().upper()
        notes = (row.get('Result_Notes', '') or '').strip().lower()
        if method == 'ollama (skipped/timeout)':
            return True
        if action == 'FAILED' and 'deferred' in notes and 'ollama' in notes:
            return True
        return False

    try:
        with open(csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                bc = row.get('Barcode', '')
                folder = row.get('Folder', '')
                if bc and folder:
                    key = make_item_key(bc, folder)
                    if _is_deferred_row(row):
                        latest_state_by_key[key] = False
                    else:
                        action = (row.get('Action', '') or '').strip().upper()
                        notes = (row.get('Result_Notes', '') or '').strip().lower()
                        # Strict policy: mark complete only after explicit live verification.
                        if action == 'FIXED' and 'verify: ok' in notes:
                            latest_state_by_key[key] = True
                        # Terminal policy: no-image rows can be closed to avoid endless retries.
                        elif action == 'SKIPPED' and 'no-image terminal' in notes:
                            latest_state_by_key[key] = True
                        elif action == 'FAILED':
                            latest_state_by_key[key] = False
    except Exception:
        pass
    for k, is_done in latest_state_by_key.items():
        if is_done:
            keys.add(k)
    return keys


def load_latest_status_by_key_from_csv(csv_path):
    status_by_key = {}
    if not os.path.exists(csv_path):
        return status_by_key
    try:
        with open(csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                bc = row.get('Barcode', '')
                folder = row.get('Folder', '')
                if not bc or not folder:
                    continue
                key = make_item_key(bc, folder)
                status_by_key[key] = {
                    'action': (row.get('Action', '') or '').strip().upper(),
                    'notes': (row.get('Result_Notes', '') or '').strip().lower(),
                    'method': (row.get('Method', '') or '').strip().lower(),
                }
    except Exception:
        pass
    return status_by_key


def load_resume_state(base_dir):
    state_file, progress_file = get_resume_paths(base_dir)
    force_new = os.environ.get("TRIANGULATE_V5_FORCE_NEW", "0").strip() == "1"
    if force_new:
        return {
            "resume": False,
            "state_file": state_file,
            "progress_file": progress_file,
            "csv_file": "",
            "processed_keys": set(),
            "run_stamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "state": {},
        }

    state = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as sf:
                state = json.load(sf)
        except Exception:
            state = {}

    csv_file = state.get("csv_file", "")
    completed = bool(state.get("completed", False))
    processed_keys = set()

    if (not completed) and csv_file and os.path.exists(csv_file):
        # Derive processed keys from CSV rows that passed strict verification.
        processed_keys = load_processed_keys_from_csv(csv_file)
        return {
            "resume": True,
            "state_file": state_file,
            "progress_file": progress_file,
            "csv_file": csv_file,
            "processed_keys": processed_keys,
            "run_stamp": state.get("run_stamp", ""),
            "state": state,
        }

    return {
        "resume": False,
        "state_file": state_file,
        "progress_file": progress_file,
        "csv_file": "",
        "processed_keys": set(),
        "run_stamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "state": state,
    }


def save_resume_state(state_file, payload):
    with open(state_file, 'w', encoding='utf-8') as sf:
        json.dump(payload, sf, ensure_ascii=True, indent=2)


def load_reconcile_targets_from_csv(csv_file):
    targets = {}
    if not os.path.exists(csv_file):
        return targets
    try:
        with open(csv_file, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                action = (row.get('Action', '') or '').strip().upper()
                if action != 'FIXED':
                    continue
                bib = (row.get('DB_Biblionumber', '') or '').strip()
                if not bib.isdigit():
                    continue
                targets[bib] = {
                    'subjects': normalize_subjects(row.get('subjects', '') or ''),
                    'title': _clean_ws(row.get('title', '') or ''),
                    'year': normalize_year(row.get('year', '') or ''),
                }
    except Exception as e:
        print(f"Failed to read reconciliation targets from CSV: {e}")
    return targets


def reconcile_record_fields_before_finish(csv_file):
    targets = load_reconcile_targets_from_csv(csv_file)
    if not targets:
        print("Reconciliation skipped: no FIXED biblionumbers found in current run log.")
        return

    print(f"Reconciliation pass: checking {len(targets)} biblionumbers from run log...")
    ssh_conn = None
    try:
        ssh_conn = get_db_connection()
        fixed_count = 0
        for bib, info in targets.items():
            # Enforce shelving and item field defaults consistently.
            br = DEFAULT_BRANCH.replace("'", "\\'")
            sh = DEFAULT_SHELVING_LOCATION.replace("'", "\\'")
            it = DEFAULT_ITEMTYPE.replace("'", "\\'")
            cn = DEFAULT_CN_SOURCE.replace("'", "\\'")
            ssh_conn.exec_command(
                f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"UPDATE koha_mfa.items SET homebranch='{br}', holdingbranch='{br}', location='{sh}', permanent_location='{sh}', ccode='{sh}', itype='{it}', cn_source='{cn}' WHERE biblionumber={bib};\""
            )

            # Keep year in enumchron and strip trailing years from call number.
            select_items = (
                "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e "
                f"\"SELECT i.itemnumber,IFNULL(i.itemcallnumber,''),IFNULL(i.enumchron,''),IFNULL(bi.publicationyear,'') FROM koha_mfa.items i LEFT JOIN koha_mfa.biblioitems bi ON bi.biblionumber=i.biblionumber WHERE i.biblionumber={bib};\""
            )
            stdin, stdout, stderr = ssh_conn.exec_command(select_items)
            item_rows = [r for r in stdout.read().decode('utf-8', 'replace').splitlines() if r.strip()]
            for row in item_rows:
                parts = row.split('\t')
                if not parts or not parts[0].strip().isdigit():
                    continue
                itemnumber = parts[0].strip()
                callnum = (parts[1] if len(parts) > 1 else '').strip()
                enumchron = (parts[2] if len(parts) > 2 else '').strip()
                pubyear = (parts[3] if len(parts) > 3 else '').strip()
                m = re.search(r'(1[5-9]\d{2}|20\d{2}|21\d{2})\s*$', callnum)
                trailing_year = m.group(1) if m else ''
                base_call = re.sub(r'\s*(1[5-9]\d{2}|20\d{2}|21\d{2})\s*$', '', callnum).strip()
                base_call = re.sub(r'\b([A-Z]{2,6})(1[5-9]\d{2}|20\d{2}|21\d{2})\b', r'\\1', base_call)
                target_year = normalize_year(enumchron) or trailing_year or normalize_year(pubyear) or info.get('year', '')

                base_sql = base_call.replace("'", "\\'")
                year_sql = (target_year or '').replace("'", "\\'")
                if year_sql:
                    upd = (
                        "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e "
                        f"\"UPDATE koha_mfa.items SET itemcallnumber='{base_sql}', enumchron='{year_sql}' WHERE itemnumber={itemnumber};\""
                    )
                else:
                    upd = (
                        "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e "
                        f"\"UPDATE koha_mfa.items SET itemcallnumber='{base_sql}' WHERE itemnumber={itemnumber};\""
                    )
                ssh_conn.exec_command(upd)

            # Ensure MARC 650 exists; if missing, generate from run log/title.
            q_meta = (
                "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e "
                f"\"SELECT b.title, HEX(bm.metadata) FROM koha_mfa.biblio b JOIN koha_mfa.biblio_metadata bm ON bm.biblionumber=b.biblionumber WHERE b.biblionumber={bib};\""
            )
            stdin, stdout, stderr = ssh_conn.exec_command(q_meta)
            out = stdout.read().decode('utf-8', 'replace').strip()
            if out and '\t' in out:
                first_tab = out.find('\t')
                db_title = out[:first_tab]
                hex_xml = out[first_tab + 1:].strip()
                if hex_xml:
                    try:
                        xml_text = bytes.fromhex(hex_xml).decode('utf-8', 'replace').replace('\\n', '\n')
                        root = ET.fromstring(xml_text)
                        has_650 = bool(root.findall("datafield[@tag='650']"))
                        if not has_650:
                            subj = info.get('subjects', '')
                            if not subj:
                                subj = generate_subjects_by_skill({'title': info.get('title', '') or db_title, 'subtitle': '', 'notes': '', 'subjects': ''})
                            subj = normalize_subjects(subj)
                            if subj:
                                for heading in [h.strip() for h in subj.split(';') if h.strip()]:
                                    f650 = ET.Element('datafield', {'ind1': ' ', 'ind2': '0', 'tag': '650'})
                                    sf = ET.Element('subfield', {'code': 'a'})
                                    sf.text = heading
                                    f650.append(sf)
                                    root.append(f650)
                                new_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')
                                new_hex = new_xml.encode('utf-8').hex().upper()
                                upd_meta = (
                                    "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e "
                                    f"\"UPDATE koha_mfa.biblio_metadata SET metadata=UNHEX('{new_hex}') WHERE biblionumber={bib};\""
                                )
                                ssh_conn.exec_command(upd_meta)
                    except Exception:
                        pass

            fixed_count += 1
            if fixed_count % 100 == 0:
                print(f"Reconciliation progress: {fixed_count}/{len(targets)}")

        print(f"Reconciliation complete: {fixed_count} biblionumbers checked.")
    except Exception as e:
        print(f"Reconciliation failed: {e}")
    finally:
        if ssh_conn:
            ssh_conn.close()


def empty_catalog_payload():
    return {k: "" for k in CATALOG_FIELDS}


def _clean_ws(text):
    return re.sub(r'\s+', ' ', (text or '')).strip()


def normalize_barcode(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return ""
    if len(digits) <= 6:
        return digits.zfill(6)
    return digits


def _capitalize_token(token):
    if not token:
        return ""
    if token.isupper() or re.search(r'\d', token):
        return token
    if len(token) == 1 and token.isalpha():
        return token.upper()
    return token[:1].upper() + token[1:].lower()


def _normalize_phrase_capitalization(text):
    raw = _clean_ws(text)
    if not raw:
        return ""
    stop_words = {
        'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'into', 'nor',
        'of', 'on', 'or', 'over', 'the', 'to', 'up', 'via', 'with'
    }
    words = raw.split(' ')
    normalized = []
    for i, word in enumerate(words):
        core = word.strip()
        low = core.lower()
        if i > 0 and low in stop_words:
            normalized.append(low)
            continue
        normalized.append(_capitalize_token(core))
    return ' '.join(normalized)


def _strip_common_noise(text):
    out = _clean_ws(text)
    out = re.sub(r'\[photo_[^\]]+\]', '', out, flags=re.IGNORECASE)
    out = re.sub(r'\b(library|accession\s*no\.?|marine fisheries academy)\b.*$', '', out, flags=re.IGNORECASE)
    return _clean_ws(out)


def normalize_title_and_subtitle(title, subtitle):
    t = _strip_common_noise(title)
    s = _strip_common_noise(subtitle)

    if not s and ':' in t:
        parts = t.split(':', 1)
        t = _clean_ws(parts[0])
        s = _clean_ws(parts[1])

    t = t.rstrip(' /:;,.')
    s = s.rstrip(' /:;,.')
    t = _normalize_phrase_capitalization(t)
    s = _normalize_phrase_capitalization(s)
    return t, s


def _looks_corporate_name(name):
    low = name.lower()
    markers = [
        'university', 'academy', 'organization', 'ministry', 'department', 'company',
        'ltd', 'limited', 'association', 'society', 'institute', 'committee', 'board'
    ]
    return any(m in low for m in markers)


def is_corporate_author(author):
    return _looks_corporate_name(_clean_ws(author))


def normalize_personal_name(name):
    n = _clean_ws(name)
    if not n:
        return ""
    n = n.strip(' .;:')

    suffixes = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'phd', 'md'}
    particles = {'da', 'de', 'del', 'della', 'der', 'di', 'la', 'le', 'van', 'von', 'bin', 'al'}

    def normalize_forenames(text):
        parts = []
        for tok in text.split(' '):
            tok = tok.strip()
            if not tok:
                continue
            if re.fullmatch(r'[A-Za-z]\.?', tok):
                parts.append(tok[0].upper() + '.')
            else:
                parts.append(_capitalize_token(tok))
        return ' '.join(parts)

    if ',' in n:
        parts = [p.strip() for p in n.split(',', 1)]
        if parts[0] and parts[1]:
            return f"{_normalize_phrase_capitalization(parts[0])}, {normalize_forenames(parts[1])}".strip()
        return n

    if _looks_corporate_name(n):
        return n

    tokens = [t for t in n.split(' ') if t]
    if len(tokens) == 1:
        return _capitalize_token(n)

    suffix = ""
    if tokens and tokens[-1].lower().strip('.') in {s.strip('.') for s in suffixes}:
        suffix = tokens.pop(-1)

    surname_end = len(tokens) - 1
    surname_start = surname_end
    while surname_start - 1 >= 0 and tokens[surname_start - 1].lower() in particles:
        surname_start -= 1

    surname = ' '.join(tokens[surname_start:]).strip(',')
    forenames = ' '.join(tokens[:surname_start]).strip(',')
    forenames = normalize_forenames(forenames)
    surname = _normalize_phrase_capitalization(surname)
    if suffix:
        forenames = (forenames + ", " + suffix.upper().rstrip('.')).strip(', ')
    if surname and forenames:
        return f"{surname}, {forenames}"
    return _normalize_phrase_capitalization(n)


def normalize_author_field(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""

    # Split common multi-author separators and normalize each name.
    parts = re.split(r'\s*(?:;|\band\b|&)\s*', raw, flags=re.IGNORECASE)
    parts = [p for p in (_clean_ws(p) for p in parts) if p]
    if not parts:
        return ""

    normalized = [normalize_personal_name(p) for p in parts]
    return '; '.join([n for n in normalized if n])


def normalize_subjects(value):
    items = []
    if isinstance(value, list):
        items = [str(v) for v in value if str(v).strip()]
    else:
        raw = _clean_ws(value)
        if raw:
            items = re.split(r'\s*[;|]\s*', raw)

    out = []
    for item in items:
        text = _strip_common_noise(item)
        if not text:
            continue
        text = text.replace('—', '--')
        parts = [p.strip(' .') for p in re.split(r'\s*--\s*', text) if p.strip(' .')]
        parts = [_normalize_phrase_capitalization(p) for p in parts]
        heading = ' -- '.join(parts).strip()
        if heading and heading not in out:
            out.append(heading)
    return '; '.join(out)


def normalize_edition(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    low = raw.lower()
    # Edition should not contain volume/issue statements.
    if re.search(r'\b(vol\.?|volume|v\.|issue|no\.|number)\b', low):
        return ""

    # Pick first proper edition token: 2nd ed., 3rd rev. ed., etc.
    m = re.search(r'(\d{1,2})(st|nd|rd|th)\s*(rev\.)?\s*ed\.?', low)
    if m:
        ord_num = m.group(1)
        ord_sfx = m.group(2)
        rev = " rev." if m.group(3) else ""
        return f"{ord_num}{ord_sfx}{rev} ed."

    # Fallback for patterns like "first edition" (rare).
    m2 = re.search(r'\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+edition\b', low)
    if m2:
        words = {
            'first': '1st', 'second': '2nd', 'third': '3rd', 'fourth': '4th', 'fifth': '5th',
            'sixth': '6th', 'seventh': '7th', 'eighth': '8th', 'ninth': '9th', 'tenth': '10th'
        }
        return f"{words[m2.group(1)]} ed."

    return ""


def normalize_year(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    m = re.search(r'(1[5-9]\d{2}|20\d{2}|21\d{2})', raw)
    return m.group(1) if m else ""


def normalize_isbn(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    raw = re.sub(r'(?i)^isbn\s*:?\s*', '', raw)
    m = re.search(r'([0-9Xx\-]{10,20})', raw)
    return m.group(1).upper() if m else ""


def normalize_issn(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    raw = re.sub(r'(?i)^issn\s*:?\s*', '', raw)
    m = re.search(r'(\d{4})[-\s]?(\d{3}[0-9Xx])', raw)
    return f"{m.group(1)}-{m.group(2).upper()}" if m else ""


def normalize_pages(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    m = re.search(r'(\d{1,5})\s*(?:p\.?|pages?)', raw, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)} p."
    if re.fullmatch(r'\d{1,5}', raw):
        return f"{raw} p."
    return raw


def normalize_size(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    m = re.search(r'(\d{1,3}(?:\.\d+)?)\s*(?:cm|centimeters?)\.?', raw, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)} cm."
    return raw


def normalize_call_number(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    raw = raw.replace('\t', ' ')
    raw = re.sub(r'\s+', ' ', raw).strip()
    return raw.upper()


def has_non_numeric_authormarc(call_number):
    cn = normalize_call_number(call_number)
    if not cn:
        return False
    parts = cn.split(' ')
    if len(parts) < 2:
        return False
    return bool(re.fullmatch(r'[A-Z]{2,6}', parts[1]))


def is_valid_call_number(value):
    cn = normalize_call_number(value)
    if not cn:
        return False
    if cn in {'N/A', '1', '000', '000.0'}:
        return False
    return bool(re.search(r'\d', cn) and re.search(r'[A-Z]', cn))


def derive_ddc_from_catalog(catalog):
    existing = normalize_call_number(catalog.get('call_number', ''))
    m = re.match(r'^(\d{3}(?:\.\d+)?)\b', existing)
    if m:
        return m.group(1)

    text = " ".join([
        str(catalog.get('subjects', '')),
        str(catalog.get('title', '')),
        str(catalog.get('subtitle', '')),
    ]).lower()
    for keyword, ddc in sorted(DDC_SUBJECT_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if keyword in text:
            return ddc
    return '000'


def generate_subjects_by_skill(catalog):
    existing = normalize_subjects(catalog.get('subjects', ''))
    if existing:
        return existing

    text = " ".join([
        str(catalog.get('title', '')),
        str(catalog.get('subtitle', '')),
        str(catalog.get('notes', '')),
    ]).lower()
    headings = []
    for keyword, heading in sorted(SUBJECT_HEADING_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if keyword in text and heading not in headings:
            headings.append(heading)

    if not headings:
        ddc = derive_ddc_from_catalog(catalog)
        if ddc.startswith('639.2'):
            headings.append('Fisheries')
        elif ddc.startswith('623'):
            headings.append('Marine technology')
        elif ddc.startswith('510'):
            headings.append('Mathematics')
        elif ddc.startswith('530'):
            headings.append('Physics')
        elif ddc.startswith('540'):
            headings.append('Chemistry')
        elif ddc.startswith('570'):
            headings.append('Biology')

    return '; '.join(headings)


def _author_stem_for_marks(author, title):
    author_norm = normalize_author_field(author)
    source = author_norm.split(';')[0] if author_norm else ''
    if ',' in source:
        source = source.split(',', 1)[0]
    if not source:
        source = normalize_title_and_subtitle(title, "")[0]
    stem = re.sub(r'[^A-Za-z]', '', source or '').upper()
    if stem:
        return stem
    title_fallback = re.sub(r'[^A-Za-z]', '', title or '').upper()
    return title_fallback


def generate_authormarc_non_numeric(author, title, allow_title_fallback=False):
    source = normalize_author_field(author)
    if is_corporate_author(source):
        return ""
    if not source and not allow_title_fallback:
        return ""
    stem = _author_stem_for_marks(source if source else "", title if allow_title_fallback else "")
    if not stem:
        return ""
    mark = stem[:3]
    return mark.ljust(3, 'X')


def generate_strict_cutter_from_table(author, title, allow_title_fallback=False):
    source = normalize_author_field(author)
    if is_corporate_author(source):
        return ""
    if not source and not allow_title_fallback:
        return ""
    stem = _author_stem_for_marks(source if source else "", title if allow_title_fallback else "")
    if not stem:
        return ""
    initial = stem[0]
    second = stem[1] if len(stem) > 1 else 'A'
    third = stem[2] if len(stem) > 2 else 'A'
    second_num = STRICT_CUTTER_SECOND_MAP.get(second, '29')
    third_num = STRICT_CUTTER_THIRD_MAP.get(third, '1')
    title_clean = re.sub(r'[^A-Za-z]', '', title or '')
    workmark = title_clean[0].lower() if title_clean else 'a'
    return f"{initial}{second_num}{third_num}{workmark}"


def generate_call_components_by_skill(catalog, db_call_number=""):
    ddc = derive_ddc_from_catalog(catalog)
    author_val = catalog.get('author', '')
    allow_title_fallback = not _clean_ws(author_val)
    authormarc = generate_authormarc_non_numeric(author_val, catalog.get('title', ''), allow_title_fallback=allow_title_fallback)
    cutter_strict = generate_strict_cutter_from_table(author_val, catalog.get('title', ''), allow_title_fallback=allow_title_fallback)
    existing = normalize_call_number(catalog.get('call_number', ''))
    db_existing = normalize_call_number(db_call_number)
    use_existing = is_valid_call_number(existing) and has_non_numeric_authormarc(existing)
    if use_existing:
        call_number = existing
    else:
        use_db = is_valid_call_number(db_existing) and has_non_numeric_authormarc(db_existing)
        if use_db:
            call_number = db_existing
        else:
            call_number = f"{ddc} {authormarc}" if authormarc else "N/A"
    return call_number, authormarc, cutter_strict


def enforce_callnumber_chronology_policy(call_number, year):
    cn = normalize_call_number(call_number)
    yr = normalize_year(year)
    if not cn:
        return "", yr

    # Remove accidental year concatenation into author mark blocks (e.g., SHA1603 -> SHA).
    cn = re.sub(r'\b([A-Z]{2,6})(1[5-9]\d{2}|20\d{2}|21\d{2})\b', r'\1', cn)

    if yr:
        cn = re.sub(rf'(?:\s+{re.escape(yr)})+$', '', cn).strip()
    else:
        # If no explicit year exists, infer from repeated trailing year tokens.
        m = re.search(r'\b(1[5-9]\d{2}|20\d{2}|21\d{2})(?:\s+\1)+\s*$', cn)
        if m:
            yr = m.group(1)
            cn = re.sub(rf'(?:\s+{re.escape(yr)})+$', '', cn).strip()

    return cn, yr


def retry_personal_author_from_sources(catalog):
    title = _clean_ws(catalog.get('title', ''))
    subtitle = _clean_ws(catalog.get('subtitle', ''))
    query = _clean_ws(f"{title} {subtitle}")
    if not query:
        return ""

    loc = query_loc_sru_full(query)
    if loc:
        loc_author = normalize_author_field(loc.get('author', ''))
        if loc_author and not is_corporate_author(loc_author):
            return loc_author

    ol = query_openlibrary(query)
    if ol:
        ol_author = normalize_author_field(ol.get('author', ''))
        if ol_author and not is_corporate_author(ol_author):
            return ol_author
    return ""


def normalize_url(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    if re.match(r'(?i)^https?://', raw):
        return raw
    return ""


def normalize_isbn_digits(value):
    raw = _clean_ws(value).upper()
    if not raw:
        return ""
    out = re.sub(r'[^0-9X]', '', raw)
    if len(out) == 13:
        return out
    if len(out) == 10:
        return out
    return ""


def derive_cover_url(catalog):
    existing = normalize_url(catalog.get('url', ''))
    if existing:
        return existing

    isbn_digits = normalize_isbn_digits(catalog.get('isbn', ''))
    if isbn_digits:
        # Prefer OpenLibrary cover endpoint when ISBN is available.
        return f"https://covers.openlibrary.org/b/isbn/{isbn_digits}-L.jpg"

    return ""


def derive_amazon_cover_url(catalog):
    isbn_digits = normalize_isbn_digits(catalog.get('isbn', ''))
    if not isbn_digits:
        return ""
    if len(isbn_digits) == 13 and isbn_digits.startswith('978'):
        isbn10 = isbn_digits[3:12]
    elif len(isbn_digits) == 10:
        isbn10 = isbn_digits
    else:
        isbn10 = ""
    if not isbn10:
        return ""
    return f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.L.jpg"


def normalize_accession(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    m = re.search(r'([A-Za-z0-9\-/]{3,30})', raw)
    return m.group(1).upper() if m else ""


def normalize_publisher(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""
    raw = raw.rstrip(' ,;:')
    return raw


def normalize_publication_place(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""
    raw = raw.rstrip(' ,;:')
    if raw.lower() == 'unknown':
        return ""
    return raw


def normalize_place(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""
    raw = raw.rstrip(' ,;:')
    # Keep abbreviations uppercase, title-case regular words.
    words = []
    for w in raw.split(' '):
        words.append(w if w.isupper() else w.capitalize())
    return ' '.join(words)


def normalize_responsibility(value):
    raw = _strip_common_noise(value)
    return raw.rstrip(' /:;,.') if raw else ""


def normalize_notes_field(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""
    return raw if raw.endswith('.') else raw + '.'


def normalize_series_title(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""
    raw = re.sub(r'\s*;\s*$', '', raw)
    return raw.rstrip(' ,:')


def normalize_volume(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    m = re.search(r'(?i)\b(?:vol\.?|volume|v\.)\s*([0-9A-Za-z]+)\b', raw)
    if m:
        return m.group(1)
    if re.fullmatch(r'[0-9A-Za-z]{1,10}', raw):
        return raw
    return ""


def normalize_illus(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    if re.search(r'(?i)\bill(us|ustrations?)?\b', raw):
        return 'ill.'
    return raw


def normalize_price(value):
    raw = _clean_ws(value)
    if not raw:
        return ""
    m = re.search(r'([A-Za-z]{0,3}\s*[0-9]+(?:\.[0-9]{1,2})?)', raw)
    return m.group(1).strip() if m else ""


def normalize_copy(value):
    raw = _clean_ws(value)
    if not raw:
        return "1"
    m = re.search(r'(\d+)', raw)
    if not m:
        return "1"
    n = int(m.group(1))
    return str(n if n > 0 else 1)


def normalize_abstract(value):
    raw = _strip_common_noise(value)
    if not raw:
        return ""
    return raw


def split_volume_from_edition_or_series(edition_value, series_value):
    vol = ""
    ed = edition_value
    m_ed = re.search(r'(?i)\b(?:vol\.?|volume|v\.)\s*([0-9A-Za-z]+)\b', edition_value or "")
    if m_ed:
        vol = m_ed.group(1)
        ed = re.sub(r'(?i)\b(?:vol\.?|volume|v\.)\s*[0-9A-Za-z]+\b', '', edition_value).strip(' ,;')
    if not vol:
        m_ser = re.search(r'(?i)\b(?:vol\.?|volume|v\.)\s*([0-9A-Za-z]+)\b', series_value or "")
        if m_ser:
            vol = m_ser.group(1)
    return ed, vol


def normalize_catalog_payload(data):
    payload = empty_catalog_payload()
    if not data:
        return payload
    for key in CATALOG_FIELDS:
        val = data.get(key, "")
        if key == 'subjects':
            payload[key] = normalize_subjects(val)
        else:
            payload[key] = _clean_ws("" if val is None else str(val))

    payload['title'], payload['subtitle'] = normalize_title_and_subtitle(payload['title'], payload['subtitle'])
    payload['author'] = normalize_author_field(payload['author'])
    payload['responsibility'] = normalize_responsibility(payload['responsibility'])
    payload['accession'] = normalize_accession(payload['accession'])
    payload['publisher'] = normalize_publisher(payload['publisher'])
    payload['place'] = normalize_publication_place(payload['place'])
    payload['notes'] = normalize_notes_field(payload['notes'])
    payload['series_title'] = normalize_series_title(payload['series_title'])
    payload['abstract'] = normalize_abstract(payload['abstract'])

    if not payload['responsibility'] and payload['author']:
        payload['responsibility'] = payload['author']

    payload['year'] = normalize_year(payload['year'])
    payload['isbn'] = normalize_isbn(payload['isbn'])
    payload['issn'] = normalize_issn(payload['issn'])
    payload['pages'] = normalize_pages(payload['pages'])
    payload['size'] = normalize_size(payload['size'])
    payload['url'] = normalize_url(payload['url'])
    payload['call_number'] = normalize_call_number(payload['call_number'])
    payload['illus'] = normalize_illus(payload['illus'])
    payload['price'] = normalize_price(payload['price'])
    payload['copy'] = normalize_copy(payload['copy'])
    payload['subjects'] = normalize_subjects(payload['subjects'])

    ed, vol = split_volume_from_edition_or_series(payload['edition'], payload['series_title'])
    payload['edition'] = normalize_edition(ed)
    payload['volume'] = normalize_volume(payload['volume'])
    if not payload['volume'] and vol:
        payload['volume'] = normalize_volume(vol)

    if not payload['title'] and payload['subtitle']:
        payload['title'] = payload['subtitle']
        payload['subtitle'] = ""

    if not payload['edition']:
        payload['edition'] = "N/A"
    return payload


def apply_standard_defaults(payload, db_call_number="", barcode=""):
    out = dict(payload)
    out['item_barcode'] = normalize_barcode(out.get('item_barcode', '')) or normalize_barcode(barcode)
    out['url'] = normalize_url(out.get('url', ''))
    if SKIP_COVER_PHOTO_UPLOAD:
        out['url'] = ""
    else:
        if not out['url']:
            out['url'] = derive_cover_url(out)
        if not out['url']:
            out['url'] = derive_amazon_cover_url(out)

    # Policy: do not generate author-based marks from corporate authors.
    if is_corporate_author(out.get('author', '')):
        preserved = normalize_call_number(out.get('call_number', ''))
        if not is_valid_call_number(preserved):
            preserved = normalize_call_number(db_call_number)
        year_norm = normalize_year(out.get('year', ''))
        preserved, extracted_year = enforce_callnumber_chronology_policy(preserved, year_norm)
        out['call_number'] = preserved if preserved else 'N/A'
        out['authormarc'] = 'N/A'
        out['cutter_strict'] = 'N/A'
        out['edition'] = normalize_edition(out.get('edition', '')) or 'N/A'
        out['copy'] = normalize_copy(out.get('copy', ''))
        out['year'] = year_norm or extracted_year
        out['subjects'] = normalize_subjects(out.get('subjects', ''))
        if not out['subjects']:
            out['subjects'] = normalize_subjects(generate_subjects_by_skill(out))
        return out

    call_number, authormarc, cutter_strict = generate_call_components_by_skill(out, db_call_number)
    year_norm = normalize_year(out.get('year', ''))
    call_number, extracted_year = enforce_callnumber_chronology_policy(call_number, year_norm)
    out['call_number'] = normalize_call_number(call_number)
    out['authormarc'] = _clean_ws(authormarc).upper() if authormarc else 'N/A'
    out['cutter_strict'] = _clean_ws(cutter_strict) if cutter_strict else 'N/A'
    out['edition'] = normalize_edition(out.get('edition', '')) or 'N/A'
    out['copy'] = normalize_copy(out.get('copy', ''))
    out['year'] = year_norm or extracted_year
    out['subjects'] = normalize_subjects(out.get('subjects', ''))
    if not out['subjects']:
        out['subjects'] = normalize_subjects(generate_subjects_by_skill(out))
    return out


def patch_marcxml_with_catalog(xml_text, catalog, biblionumber):
    if not xml_text:
        return None
    try:
        if isinstance(xml_text, bytes):
            xml_text = xml_text.decode('utf-8', 'replace')
        xml_text = xml_text.replace('\\n', '\n')
        root = ET.fromstring(xml_text)

        def get_or_create_datafield(tag, ind1=' ', ind2=' '):
            for field in root.findall(f"datafield[@tag='{tag}']"):
                return field
            field = ET.Element('datafield', {'ind1': ind1, 'ind2': ind2, 'tag': tag})
            root.append(field)
            return field

        def set_subfield(tag, code, value, ind1=' ', ind2=' '):
            if not value:
                return
            field = get_or_create_datafield(tag, ind1, ind2)
            for sub in list(field):
                if sub.tag == 'subfield' and sub.get('code') == code:
                    field.remove(sub)
            sub = ET.Element('subfield', {'code': code})
            sub.text = value
            field.append(sub)

        title = _clean_ws(catalog.get('title', ''))
        subtitle = _clean_ws(catalog.get('subtitle', ''))
        author = _clean_ws(catalog.get('author', ''))
        publisher = _clean_ws(catalog.get('publisher', ''))
        place = _clean_ws(catalog.get('place', ''))
        year = _clean_ws(catalog.get('year', ''))
        series_title = _clean_ws(catalog.get('series_title', ''))
        cover_url = "" if SKIP_COVER_PHOTO_UPLOAD else normalize_url(catalog.get('url', ''))
        subjects = normalize_subjects(catalog.get('subjects', ''))

        set_subfield('100', 'a', author, ind1='1', ind2=' ')
        set_subfield('245', 'a', title, ind1='1', ind2='0')
        set_subfield('245', 'b', subtitle, ind1='1', ind2='0')
        set_subfield('245', 'c', author, ind1='1', ind2='0')
        if place or publisher or year:
            set_subfield('260', 'a', f'{place},' if place else '', ind1=' ', ind2=' ')
            set_subfield('260', 'b', f'{publisher},' if publisher else '', ind1=' ', ind2=' ')
            set_subfield('260', 'c', year, ind1=' ', ind2=' ')
            set_subfield('264', 'a', f'{place},' if place else '', ind1=' ', ind2='1')
            set_subfield('264', 'b', f'{publisher},' if publisher else '', ind1=' ', ind2='1')
            set_subfield('264', 'c', year, ind1=' ', ind2='1')
        if series_title:
            set_subfield('490', 'a', series_title, ind1=' ', ind2=' ')
        if subjects:
            for field in list(root.findall("datafield[@tag='650']")):
                root.remove(field)
            for heading in [h.strip() for h in subjects.split(';') if h.strip()]:
                field = ET.Element('datafield', {'ind1': ' ', 'ind2': '0', 'tag': '650'})
                sub = ET.Element('subfield', {'code': 'a'})
                sub.text = heading
                field.append(sub)
                root.append(field)
        for field in list(root.findall("datafield[@tag='856']")):
            root.remove(field)
        if cover_url:
            set_subfield('856', 'u', cover_url, ind1='4', ind2='0')
            set_subfield('856', 'z', 'Cover image', ind1='4', ind2='0')

        for cf in root.findall("controlfield[@tag='005']"):
            cf.text = datetime.now().strftime('%Y%m%d%H%M%S.0')

        new_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')
        return new_xml
    except Exception:
        return None


def update_live_marc_record(ssh_conn, biblionumber, catalog):
    stdin, stdout, stderr = ssh_conn.exec_command(
        f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e \"SELECT HEX(metadata) FROM koha_mfa.biblio_metadata WHERE biblionumber = {biblionumber};\""
    )
    hex_xml = stdout.read().decode('utf-8', 'replace').strip()
    if not hex_xml:
        return False

    try:
        xml_text = bytes.fromhex(hex_xml).decode('utf-8', 'replace')
    except Exception:
        return False

    new_xml = patch_marcxml_with_catalog(xml_text, catalog, biblionumber)
    if not new_xml:
        return False

    new_xml_hex = new_xml.encode('utf-8').hex().upper()

    title = _clean_ws(catalog.get('title', '')).replace("'", "\\'")
    author = _clean_ws(catalog.get('author', '')).replace("'", "\\'")
    publisher = _clean_ws(catalog.get('publisher', '')).replace("'", "\\'")
    place = _clean_ws(catalog.get('place', '')).replace("'", "\\'")
    year = _clean_ws(catalog.get('year', '')).replace("'", "\\'")
    cover_url = normalize_url(catalog.get('url', '')).replace("'", "\\'")
    sql = f"""
    UPDATE koha_mfa.biblio SET title='{title}', author='{author}' WHERE biblionumber={biblionumber};
    UPDATE koha_mfa.biblioitems SET publishercode='{publisher}', publicationyear='{year}' WHERE biblionumber={biblionumber};
    UPDATE koha_mfa.biblio_metadata SET metadata=UNHEX('{new_xml_hex}') WHERE biblionumber={biblionumber};
    """
    ssh_conn.exec_command(f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"{sql}\"")
    return True


def verify_live_record_completion(ssh_conn, biblionumber, catalog):
    try:
        biblio_id = int(str(biblionumber).strip())
    except Exception:
        return False, "verify: invalid biblionumber"

    year = normalize_year(catalog.get('year', ''))
    subjects = normalize_subjects(catalog.get('subjects', ''))

    # Verify items have required shelving/location fields and chronology policy.
    q_counts = (
        "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e "
        f"\"SELECT COUNT(*),SUM(CASE WHEN IFNULL(homebranch,'')<>'' AND IFNULL(holdingbranch,'')<>'' AND IFNULL(location,'')<>'' AND IFNULL(permanent_location,'')<>'' AND IFNULL(ccode,'')<>'' AND IFNULL(itype,'')<>'' AND IFNULL(cn_source,'')<>'' THEN 1 ELSE 0 END) FROM koha_mfa.items WHERE biblionumber={biblio_id};\""
    )
    stdin, stdout, stderr = ssh_conn.exec_command(q_counts)
    row = stdout.read().decode('utf-8', 'replace').strip()
    if not row:
        return False, "verify: no items row"

    parts = row.split('\t')
    if len(parts) < 2:
        return False, "verify: items query parse failed"

    try:
        total_items = int((parts[0] or '0').strip() or '0')
        good_items = int((parts[1] or '0').strip() or '0')
    except Exception:
        return False, "verify: items query parse failed"

    if total_items <= 0:
        return False, "verify: no items row"

    if good_items < total_items:
        return False, "verify: shelving/location fields missing"

    if year:
        q_year_mismatch = (
            "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e "
            f"\"SELECT COUNT(*) FROM koha_mfa.items WHERE biblionumber={biblio_id} AND IFNULL(enumchron,'') <> '{year}';\""
        )
        stdin, stdout, stderr = ssh_conn.exec_command(q_year_mismatch)
        mismatch = (stdout.read().decode('utf-8', 'replace').strip() or '0').strip()
        try:
            if int(mismatch) > 0:
                return False, "verify: enumchron mismatch"
        except Exception:
            return False, "verify: enumchron check failed"

        q_year_in_call = (
            "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e "
            f"\"SELECT COUNT(*) FROM koha_mfa.items WHERE biblionumber={biblio_id} AND IFNULL(itemcallnumber,'') REGEXP '(^|[[:space:]]){year}[[:space:]]*$';\""
        )
        stdin, stdout, stderr = ssh_conn.exec_command(q_year_in_call)
        in_call = (stdout.read().decode('utf-8', 'replace').strip() or '0').strip()
        try:
            if int(in_call) > 0:
                return False, "verify: year still present in call number"
        except Exception:
            return False, "verify: call number chronology check failed"

    # Verify MARC metadata when fields exist in catalog.
    q_meta = (
        "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e "
        f"\"SELECT HEX(metadata) FROM koha_mfa.biblio_metadata WHERE biblionumber={biblio_id};\""
    )
    stdin, stdout, stderr = ssh_conn.exec_command(q_meta)
    hex_xml = stdout.read().decode('utf-8', 'replace').strip()
    if not hex_xml:
        return False, "verify: missing biblio metadata"
    try:
        xml_text = bytes.fromhex(hex_xml).decode('utf-8', 'replace')
        root = ET.fromstring(xml_text)
        
        field_checks = {
            'title': ("245", "a"),
            'author': ("100", "a"),
            'publisher': ("260", "b"),
            'place': ("260", "a"),
            'year': ("260", "c"),
            'series_title': ("490", "a"),
        }
        
        for key, (tag, code) in field_checks.items():
            val = catalog.get(key, "").strip()
            if val:
                found = False
                for field in root.findall(f"datafield[@tag='{tag}']"):
                    for sub in field.findall(f"subfield[@code='{code}']"):
                        if sub.text and val[:10].lower() in sub.text.lower():
                            found = True
                            break
                    if found: break
                
                if not found and key in ['publisher', 'place', 'year']:
                    for field in root.findall("datafield[@tag='264']"):
                        for sub in field.findall(f"subfield[@code='{code}']"):
                            if sub.text and val[:10].lower() in sub.text.lower():
                                found = True
                                break
                        if found: break
                
                if not found:
                    return False, f"verify: missing {key} in MARC metadata"
        
        if subjects:
            has_650 = bool(root.findall("datafield[@tag='650']"))
            if not has_650:
                return False, "verify: missing 650 subject field"
    except Exception as e:
        return False, f"verify: metadata parse failed ({e})"

    return True, "verify: ok"


def extract_barcode_from_folder(folder_name):
    name = folder_name.strip()
    match = re.match(r'^(\d{1,6})(?:\s*p\s*\d+)?$', name, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).zfill(6)

def get_db_connection():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('103.103.89.142', port=3022, username='root', password='Ru3YOTEVE1MHWS8x4b/VwWPPCTs7fuNGyLf1wh4/fCHtIzeI')
    return ssh

def is_bad_record(title, author):
    if not title or title.strip() == '' or len(title.strip()) < 3: return True
    if title == 'Remediated Unknown Title': return True
    if title == 'Unknown' or title == '. ATTA': return True
    clean_t = "".join(c for c in title if c.isalnum() or c.isspace())
    if len(clean_t) < 2: return True
    return False

# ----------------- OCR ENGINES -----------------

paddle_init_lock = threading.Lock()
PADDLE_OCR_ENGINE = None
def get_paddle_ocr():
    global PADDLE_OCR_ENGINE
    with paddle_init_lock:
        if PADDLE_OCR_ENGINE is None:
            PADDLE_OCR_ENGINE = PaddleOCR(use_angle_cls=True, lang='en')
    return PADDLE_OCR_ENGINE

def preprocess_and_extract_paddleocr(img_path):
    print("      [PaddleOCR] Extracting text from image...")
    try:
        img = cv2.imread(img_path)
        if img is None:
            return ""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ocr = get_paddle_ocr()
        result = ocr.ocr(gray)
        if not result or not result[0]:
            return ""
        lines = []
        for line in result[0]:
            text = line[1][0]
            if len(text.strip()) > 3:
                lines.append(text.strip())
        return " | ".join(lines)
    except Exception as e:
        print(f"      [PaddleOCR Failed] {e}")
        return ""

def ocr_via_tesseract(img_path):
    print("      [Tesseract] Fast Keyword OCR...")
    try:
        img = Image.open(img_path).convert('L')
        img = ImageEnhance.Contrast(img).enhance(2.0)
        text = pytesseract.image_to_string(img, config=tess_config)
        lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 3]
        if not lines: return ""
        # Return first few lines as a keyword search string
        return " ".join(lines[:3])[:100]
    except Exception as e:
        return ""

def ocr_via_ocrspace(img_path, api_key="K85433388788957"):
    print("      [OCR.Space] Secondary Cloud OCR...")
    try:
        url = 'https://api.ocr.space/parse/image'
        with open(img_path, 'rb') as f:
            payload = {'apikey': api_key, 'OCREngine': 2, 'language': 'ben'}
            r = requests.post(url, files={'file': f}, data=payload, timeout=20)
            result = r.json()
            if result.get('IsErroredOnProcessing') == False:
                text = result.get('ParsedResults', [{}])[0].get('ParsedText', '')
                lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 3]
                if not lines: return ""
                return " ".join(lines[:3])[:100]
    except Exception as e:
        print(f"      [OCR.Space Failed] {e}")
    return ""

DB_CRITICAL_PARALLEL = get_env_int("TRIANGULATE_V5_DB_CRITICAL_PARALLEL", 6, min_value=1, max_value=32)
db_critical_semaphore = threading.Semaphore(DB_CRITICAL_PARALLEL)
VERIFY_RETRIES = get_env_int("TRIANGULATE_V5_VERIFY_RETRIES", 2, min_value=0, max_value=10)
DEEPSEEK_API_KEY = os.environ.get("TRIANGULATE_V5_DEEPSEEK_API_KEY", "").strip()
USE_DEEPSEEK_API = os.environ.get("TRIANGULATE_V5_USE_DEEPSEEK_API", "0").strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_BASE_URL = os.environ.get("TRIANGULATE_V5_DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
DEEPSEEK_MODEL = os.environ.get("TRIANGULATE_V5_DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
DEEPSEEK_TIMEOUT = get_env_int("TRIANGULATE_V5_DEEPSEEK_TIMEOUT", 90, min_value=15, max_value=300)
OCR_TARGET_MAX_ATTEMPTS = get_env_int("TRIANGULATE_V5_OCR_TARGET_MAX_ATTEMPTS", 3, min_value=1, max_value=10)

class CatalogRecord(BaseModel):
    title: str = Field(default="")
    subtitle: str = Field(default="")
    author: str = Field(default="")
    responsibility: str = Field(default="")
    accession: str = Field(default="")
    publisher: str = Field(default="")
    year: str = Field(default="")
    edition: str = Field(default="")
    isbn: str = Field(default="")
    issn: str = Field(default="")
    pages: str = Field(default="")
    place: str = Field(default="")
    notes: str = Field(default="")
    series_title: str = Field(default="")
    abstract: str = Field(default="")
    volume: str = Field(default="")
    illus: str = Field(default="")
    size: str = Field(default="")
    url: str = Field(default="")
    price: str = Field(default="")
    copy: str = Field(default="")
    subjects: list[str] = Field(default_factory=list)
    call_number: str = Field(default="")

parser = JsonOutputParser(pydantic_object=CatalogRecord)
langchain_prompt = PromptTemplate(
    template="Extract bibliographic metadata from OCR/context hints. Output strictly as JSON.\n{format_instructions}\nHints: {hints}\n",
    input_variables=["hints"],
    partial_variables={"format_instructions": parser.get_format_instructions()},
)
vllm_llm = ChatOpenAI(
    base_url="http://localhost:8000/v1",
    api_key="EMPTY",
    model="deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    temperature=0.0,
    max_retries=1,
    timeout=60
)
langchain_chain = langchain_prompt | vllm_llm | parser


def verify_with_retries(ssh_conn, biblionumber, catalog):
    last_ok = False
    last_note = "verify: not-run"
    attempts = VERIFY_RETRIES + 1
    for attempt in range(attempts):
        ok, note = verify_live_record_completion(ssh_conn, biblionumber, catalog)
        if ok:
            return True, note
        last_ok, last_note = ok, note
        if attempt < attempts - 1:
            # Re-apply MARC patch before retry when validation fails to reduce transient misses.
            update_live_marc_record(ssh_conn, biblionumber, catalog)
    return last_ok, last_note

def ocr_via_langchain(img_path, hints_text=""):
    folder_name = os.path.basename(os.path.dirname(img_path)) if img_path else "unknown"
    print(f"      [vLLM WSL] Concurrent LangChain extraction for {folder_name}...")
    try:
        hint_block = hints_text[:3000] if hints_text else "No OCR hints available."
        result = langchain_chain.invoke({"hints": hint_block})
        return result
    except Exception as e:
        print(f"      [vLLM Failed] {e}")
        return {}


def ocr_via_deepseek(hints_text=""):
    if not USE_DEEPSEEK_API or not DEEPSEEK_API_KEY:
        return {}

    print("      [DeepSeek Responses API] FINAL FALLBACK - Cloud AI Extraction...")
    try:
        guidance = (
            "Extract bibliographic metadata from OCR/context hints. "
            "Return ONLY strict JSON with keys: "
            "title, subtitle, author, responsibility, accession, publisher, year, edition, isbn, issn, pages, place, notes, series_title, abstract, volume, illus, size, url, price, copy, subjects, call_number. "
            "Use empty string when unknown. subjects must be either an array of strings or a semicolon-separated string."
        )
        if hints_text:
            guidance += f" OCR hints: {hints_text[:1200]}"

        user_input = hints_text[:4000] if hints_text else "No OCR hints available. Infer cautiously from given context only."
        payload = {
            "model": DEEPSEEK_MODEL,
            "instructions": guidance,
            "input": user_input,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }
        r = requests.post(f"{DEEPSEEK_BASE_URL}/responses", headers=headers, json=payload, timeout=DEEPSEEK_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        content = (data.get("output_text") or "").strip()
        if not content:
            try:
                output_items = data.get("output") or []
                text_parts = []
                for item in output_items:
                    for c in item.get("content") or []:
                        t = (c.get("text") or "").strip()
                        if t:
                            text_parts.append(t)
                content = "\n".join(text_parts).strip()
            except Exception:
                content = ""
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"      [DeepSeek Failed] {e}")
    return {}


def is_complete_extraction_payload(raw_data):
    if not raw_data:
        return False
    try:
        payload = normalize_catalog_payload(raw_data)
        title = payload.get('title', '')
        author = payload.get('author', '')
        return not is_bad_record(title, author)
    except Exception:
        return False

# ----------------- GLOBAL CATALOG APIs -----------------
def query_openlibrary(query_text):
    print(f"      [OpenLibrary] Searching for: {query_text}")
    try:
        r = requests.get(f"https://openlibrary.org/search.json?q={requests.utils.quote(query_text)}&limit=1", timeout=15)
        data = r.json()
        if data.get("numFound", 0) > 0:
            doc = data["docs"][0]
            cover_url = ""
            cover_id = doc.get("cover_i")
            if cover_id:
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            return {
                "title": doc.get("title", ""),
                "author": ", ".join(doc.get("author_name", [])),
                "publisher": ", ".join(doc.get("publisher", [])),
                "place": ", ".join(doc.get("publish_place", [])),
                "year": str(doc.get("first_publish_year", "")),
                "isbn": doc.get("isbn", [""])[0] if doc.get("isbn") else "",
                "url": cover_url
            }
    except:
        pass
    return None

def query_loc_sru_full(query_text):
    print(f"      [LOC SRU] Searching for: {query_text}")
    try:
        url = f"http://lx2.loc.gov:210/lcdb?version=1.1&operation=searchRetrieve&query=bath.title=\"{requests.utils.quote(query_text)}\"&maximumRecords=1&recordSchema=marcxml"
        r = requests.get(url, timeout=15)
        root = ET.fromstring(r.content)
        ns = {'zs': 'http://www.loc.gov/zing/srw/', 'marc': 'http://www.loc.gov/MARC21/slim'}
        records = root.findall('.//zs:record', ns)
        if not records: return None
        
        # Parse rich MARC data
        res = {"title": "", "author": "", "publisher": "", "place": "", "year": "", "isbn": "", "call_number": ""}
        marc = records[0].find('.//marc:record', ns)
        for datafield in marc.findall('marc:datafield', ns):
            tag = datafield.get('tag')
            if tag == '245':
                a = datafield.find("marc:subfield[@code='a']", ns)
                if a is not None: res["title"] = a.text
            if tag == '100':
                a = datafield.find("marc:subfield[@code='a']", ns)
                if a is not None: res["author"] = a.text
            if tag == '260' or tag == '264':
                a = datafield.find("marc:subfield[@code='a']", ns)
                b = datafield.find("marc:subfield[@code='b']", ns)
                c = datafield.find("marc:subfield[@code='c']", ns)
                if a is not None: res["place"] = a.text
                if b is not None: res["publisher"] = b.text
                if c is not None: res["year"] = c.text
            if tag == '020':
                a = datafield.find("marc:subfield[@code='a']", ns)
                if a is not None: res["isbn"] = a.text
            if tag == '082' or tag == '050':
                a = datafield.find("marc:subfield[@code='a']", ns)
                if a is not None: res["call_number"] = a.text
        if res["title"]: return res
    except:
        pass
    return None

# ----------------- MAIN PIPELINE -----------------
def process_book(barcode, folder_path, img_path, is_gap, db_title, db_author, biblionumber, db_call_number):
    notes = []
    ssh_conn = None

    if not img_path:
        cat = apply_standard_defaults(empty_catalog_payload(), db_call_number, barcode)
        # Verification-only path: for existing records, allow completion when live fields already pass.
        if biblionumber and str(biblionumber).strip().isdigit():
            try:
                with db_critical_semaphore:
                    ssh_conn = get_db_connection()
                    ok, verify_note = verify_with_retries(ssh_conn, biblionumber, cat)
                if ssh_conn:
                    ssh_conn.close()
                if ok:
                    return [
                        barcode,
                        folder_path,
                        is_gap,
                        'NO_IMAGE',
                        'FIXED',
                        f'No image file found in folder. | verification-only pass. | {verify_note}',
                        str(biblionumber or ''),
                        db_title,
                        db_author,
                    ] + [cat[k] for k in CATALOG_FIELDS]
                return [
                    barcode,
                    folder_path,
                    is_gap,
                    'NO_IMAGE',
                    'SKIPPED',
                    f'No image file found in folder. | no-image terminal: live verification failed ({verify_note}). | manual source image required.',
                    str(biblionumber or ''),
                    db_title,
                    db_author,
                ] + [cat[k] for k in CATALOG_FIELDS]
            except Exception as e:
                if ssh_conn:
                    ssh_conn.close()
                return [
                    barcode,
                    folder_path,
                    is_gap,
                    'NO_IMAGE',
                    'SKIPPED',
                    f'No image file found in folder. | no-image terminal: verify exception ({e}). | manual source image required.',
                    str(biblionumber or ''),
                    db_title,
                    db_author,
                ] + [cat[k] for k in CATALOG_FIELDS]

        return [
            barcode,
            folder_path,
            is_gap,
            'NO_IMAGE',
            'SKIPPED',
            'No image file found in folder. | no-image terminal: no live biblionumber for verification. | manual source image required.',
            str(biblionumber or ''),
            db_title,
            db_author,
        ] + [cat[k] for k in CATALOG_FIELDS]
    
    # We only connect to DB if we actually need to do OCR and inject
    
    try:
        engine_used = "None"
        api_data = None
        ai_data = {}
        hints_seen = []
        attempted = {
            'tesseract_api': 0,
            'ocrspace_api': 0,
            'ollama': 0,
            'deepseek': 0,
        }

        # Round-robin: each OCR target runs once per cycle, up to N cycles.
        for _round in range(OCR_TARGET_MAX_ATTEMPTS):
            # Target 1: Tesseract -> API
            if attempted['tesseract_api'] < OCR_TARGET_MAX_ATTEMPTS and not api_data and not ai_data:
                attempted['tesseract_api'] += 1
                fast_text = ocr_via_tesseract(img_path)
                if fast_text and len(fast_text) > 4:
                    hints_seen.append(fast_text)
                    candidate = query_loc_sru_full(fast_text)
                    if not candidate:
                        candidate = query_openlibrary(fast_text)
                    if candidate and is_complete_extraction_payload(candidate):
                        api_data = candidate
                        engine_used = "Tesseract -> API"

            if api_data or ai_data:
                break

            # Target 2: OCR.Space -> API
            if attempted['ocrspace_api'] < OCR_TARGET_MAX_ATTEMPTS and not api_data and not ai_data:
                attempted['ocrspace_api'] += 1
                cloud_text = ocr_via_ocrspace(img_path)
                if cloud_text and len(cloud_text) > 4:
                    hints_seen.append(cloud_text)
                    candidate = query_loc_sru_full(cloud_text)
                    if not candidate:
                        candidate = query_openlibrary(cloud_text)
                    if candidate and is_complete_extraction_payload(candidate):
                        api_data = candidate
                        engine_used = "OCR.Space -> API"

            if api_data or ai_data:
                break

            # Target 3: vLLM + LangChain
            if attempted['ollama'] < OCR_TARGET_MAX_ATTEMPTS and not api_data and not ai_data:
                attempted['ollama'] += 1
                
                # OpenCV -> PaddleOCR step
                paddle_text = preprocess_and_extract_paddleocr(img_path)
                
                hints = " | ".join([x for x in hints_seen + [paddle_text, db_title, db_author] if x])
                candidate = ocr_via_langchain(img_path, hints_text=hints)
                if candidate and is_complete_extraction_payload(candidate):
                    ai_data = candidate
                    engine_used = "vLLM / LangChain (JSON)"

            if api_data or ai_data:
                break

            # Target 4: DeepSeek Responses API
            if attempted['deepseek'] < OCR_TARGET_MAX_ATTEMPTS and not api_data and not ai_data:
                attempted['deepseek'] += 1
                hints = " | ".join([x for x in hints_seen + [db_title, db_author] if x])
                candidate = ocr_via_deepseek(hints_text=hints)
                if candidate and is_complete_extraction_payload(candidate):
                    ai_data = candidate
                    engine_used = "DeepSeek Responses API (Final Fallback)"
                    notes.append("DeepSeek Responses API used as final fallback after Ollama.")

            if api_data or ai_data:
                break

        # 3. DB INJECTION
        if api_data:
            catalog = normalize_catalog_payload(api_data)
            catalog['item_barcode'] = normalize_barcode(barcode)
            if is_corporate_author(catalog.get('author', '')):
                notes.append("Corporate author detected; retrying personal author extraction.")
                retried_author = retry_personal_author_from_sources(catalog)
                if retried_author:
                    catalog['author'] = retried_author
                    notes.append("Personal author recovered from authority sources.")
                else:
                    notes.append("Corporate author retained; author-mark generation blocked.")
            catalog = apply_standard_defaults(catalog, db_call_number, barcode)
            # We got rich data from an API!
            title = (catalog.get('title') or 'Unknown').replace("'", "\\'")
            author = (catalog.get('author') or 'Unknown').replace("'", "\\'")
            pub = catalog.get('publisher', '').replace("'", "\\'")
            year = catalog.get('year', '').replace("'", "\\'")
            isbn = catalog.get('isbn', '').replace("'", "\\'")
            callnum = catalog.get('call_number', '').replace("'", "\\'")
            enumchron_sql = f"'{year}'" if year else "NULL"
            branch_sql = DEFAULT_BRANCH.replace("'", "\\'")
            shelf_sql = DEFAULT_SHELVING_LOCATION.replace("'", "\\'")
            itemtype_sql = DEFAULT_ITEMTYPE.replace("'", "\\'")
            cn_source_sql = DEFAULT_CN_SOURCE.replace("'", "\\'")
            bibnum = ""
            verify_bib = str(biblionumber or '').strip()

            with db_critical_semaphore:
                ssh_conn = get_db_connection()
                if is_gap or not biblionumber:
                    ssh_conn.exec_command(f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"INSERT INTO koha_mfa.biblio (frameworkcode, author, title, datecreated) VALUES ('', '{author}', '{title}', NOW());\"")
                    stdin, stdout, stderr = ssh_conn.exec_command("mysql -u root -pkoha_mfa -N -e 'SELECT LAST_INSERT_ID();'")
                    bibnum = stdout.read().decode().strip()
                    if bibnum and bibnum.isdigit():
                        ssh_conn.exec_command(f"mysql -u root -pkoha_mfa -e \"INSERT INTO koha_mfa.biblioitems (biblionumber, itemtype, publishercode, publicationyear, isbn) VALUES ({bibnum}, 'BK', '{pub}', '{year}', '{isbn}');\"")
                        ssh_conn.exec_command(f"mysql -u root -pkoha_mfa -e \"INSERT INTO koha_mfa.items (biblionumber, biblioitemnumber, barcode, homebranch, holdingbranch, location, permanent_location, ccode, itype, cn_source, itemcallnumber, enumchron) VALUES ({bibnum}, {bibnum}, '{barcode}', '{branch_sql}', '{branch_sql}', '{shelf_sql}', '{shelf_sql}', '{shelf_sql}', '{itemtype_sql}', '{cn_source_sql}', '{callnum}', {enumchron_sql});\"")
                        marc_ok = update_live_marc_record(ssh_conn, bibnum, catalog)
                        if not marc_ok:
                            notes.append("MARC patch warning: update failed on first attempt.")
                    biblionumber = bibnum
                    verify_bib = bibnum
                    notes.append("Gap created from API.")
                else:
                    ssh_conn.exec_command(f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"UPDATE koha_mfa.biblio SET title = '{title}', author = '{author}' WHERE biblionumber = {biblionumber};\"")
                    ssh_conn.exec_command(f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"UPDATE koha_mfa.biblioitems SET publishercode = '{pub}', publicationyear = '{year}', isbn = '{isbn}' WHERE biblionumber = {biblionumber};\"")
                    callnum_update = f"itemcallnumber = '{callnum}'" if callnum else "itemcallnumber = itemcallnumber"
                    enumchron_update = f"enumchron = '{year}'" if year else "enumchron = enumchron"
                    ssh_conn.exec_command(
                        f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"UPDATE koha_mfa.items SET {callnum_update}, {enumchron_update}, homebranch = '{branch_sql}', holdingbranch = '{branch_sql}', location = '{shelf_sql}', permanent_location = '{shelf_sql}', ccode = '{shelf_sql}', itype = '{itemtype_sql}', cn_source = '{cn_source_sql}' WHERE biblionumber = {biblionumber};\""
                    )
                    marc_ok = update_live_marc_record(ssh_conn, biblionumber, catalog)
                    if not marc_ok:
                        notes.append("MARC patch warning: update failed on first attempt.")
                    verify_bib = str(biblionumber).strip()
                    notes.append("Fixed via API Triangulation.")

                ok, verify_note = verify_with_retries(ssh_conn, verify_bib, catalog)

            notes.append(verify_note)
            if not ok:
                ssh_conn.close()
                final_bib = verify_bib
                return [
                    barcode,
                    folder_path,
                    is_gap,
                    engine_used,
                    'FAILED',
                    ' | '.join(notes),
                    str(final_bib or ''),
                    db_title,
                    db_author,
                ] + [catalog[k] for k in CATALOG_FIELDS]
            
            ssh_conn.close()
            final_bib = bibnum if (is_gap or not biblionumber) else biblionumber
            return [
                barcode,
                folder_path,
                is_gap,
                engine_used,
                'FIXED',
                ' | '.join(notes),
                str(final_bib or ''),
                db_title,
                db_author,
            ] + [catalog[k] for k in CATALOG_FIELDS]

        # 4. AI path (populated by round-robin targets above)
        if ai_data:
            pass
        else:
            cat = apply_standard_defaults(empty_catalog_payload(), db_call_number, barcode)
            return [
                barcode,
                folder_path,
                is_gap,
                "MANUAL_REVIEW",
                "FAILED",
                f"Needs manual review: extraction incomplete after {OCR_TARGET_MAX_ATTEMPTS} attempts per target (Tesseract->API, OCR.Space->API, Ollama, DeepSeek).",
                str(biblionumber or ""),
                db_title,
                db_author,
            ] + [cat[k] for k in CATALOG_FIELDS]

        catalog = normalize_catalog_payload(ai_data)
        catalog['item_barcode'] = normalize_barcode(barcode)
        if is_corporate_author(catalog.get('author', '')):
            notes.append("Corporate author detected; retrying personal author extraction.")
            retried_author = retry_personal_author_from_sources(catalog)
            if retried_author:
                catalog['author'] = retried_author
                notes.append("Personal author recovered from authority sources.")
            else:
                notes.append("Corporate author retained; author-mark generation blocked.")
        catalog = apply_standard_defaults(catalog, db_call_number, barcode)
        title = (catalog.get('title') or 'Unknown').replace("'", "\\'")
        author = (catalog.get('author') or 'Unknown').replace("'", "\\'")
        acc = catalog.get('accession', '').replace("'", "\\'")
        callnum = catalog.get('call_number', '').replace("'", "\\'")
        year = catalog.get('year', '').replace("'", "\\'")
        enumchron_sql = f"'{year}'" if year else "NULL"
        branch_sql = DEFAULT_BRANCH.replace("'", "\\'")
        shelf_sql = DEFAULT_SHELVING_LOCATION.replace("'", "\\'")
        itemtype_sql = DEFAULT_ITEMTYPE.replace("'", "\\'")
        cn_source_sql = DEFAULT_CN_SOURCE.replace("'", "\\'")
        
        bibnum = ""
        verify_bib = str(biblionumber or '').strip()

        with db_critical_semaphore:
            ssh_conn = get_db_connection()
            if is_gap or not biblionumber:
                ssh_conn.exec_command(f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"INSERT INTO koha_mfa.biblio (frameworkcode, author, title, datecreated) VALUES ('', '{author}', '{title}', NOW());\"")
                stdin, stdout, stderr = ssh_conn.exec_command("mysql -u root -pkoha_mfa -N -e 'SELECT LAST_INSERT_ID();'")
                bibnum = stdout.read().decode().strip()
                if bibnum and bibnum.isdigit():
                    ssh_conn.exec_command(f"mysql -u root -pkoha_mfa -e \"INSERT INTO koha_mfa.biblioitems (biblionumber, itemtype) VALUES ({bibnum}, 'BK');\"")
                    acc_sql = f"'{acc}'" if acc else "NULL"
                    ssh_conn.exec_command(f"mysql -u root -pkoha_mfa -e \"INSERT INTO koha_mfa.items (biblionumber, biblioitemnumber, barcode, homebranch, holdingbranch, location, permanent_location, ccode, itype, cn_source, stocknumber, itemcallnumber, enumchron) VALUES ({bibnum}, {bibnum}, '{barcode}', '{branch_sql}', '{branch_sql}', '{shelf_sql}', '{shelf_sql}', '{shelf_sql}', '{itemtype_sql}', '{cn_source_sql}', {acc_sql}, '{callnum}', {enumchron_sql});\"")
                    marc_ok = update_live_marc_record(ssh_conn, bibnum, catalog)
                    if not marc_ok:
                        notes.append("MARC patch warning: update failed on first attempt.")
                verify_bib = bibnum
                notes.append("Gap created from AI OCR.")
            else:
                ssh_conn.exec_command(f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"UPDATE koha_mfa.biblio SET title = '{title}', author = '{author}' WHERE biblionumber = {biblionumber};\"")
                stock_update = f"stocknumber = '{acc}'" if acc else "stocknumber = stocknumber"
                callnum_update = f"itemcallnumber = '{callnum}'" if callnum else "itemcallnumber = itemcallnumber"
                enumchron_update = f"enumchron = '{year}'" if year else "enumchron = enumchron"
                ssh_conn.exec_command(
                    f"mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -e \"UPDATE koha_mfa.items SET {stock_update}, {callnum_update}, {enumchron_update}, homebranch = '{branch_sql}', holdingbranch = '{branch_sql}', location = '{shelf_sql}', permanent_location = '{shelf_sql}', ccode = '{shelf_sql}', itype = '{itemtype_sql}', cn_source = '{cn_source_sql}' WHERE biblionumber = {biblionumber};\""
                )
                marc_ok = update_live_marc_record(ssh_conn, biblionumber, catalog)
                if not marc_ok:
                    notes.append("MARC patch warning: update failed on first attempt.")
                verify_bib = str(biblionumber).strip()
                notes.append("Fixed via AI OCR Fallback.")

            ok, verify_note = verify_with_retries(ssh_conn, verify_bib, catalog)

        notes.append(verify_note)
        if not ok:
            ssh_conn.close()
            final_bib = verify_bib
            return [
                barcode,
                folder_path,
                is_gap,
                engine_used,
                'FAILED',
                ' | '.join(notes),
                str(final_bib or ''),
                db_title,
                db_author,
            ] + [catalog[k] for k in CATALOG_FIELDS]

        ssh_conn.close()
        final_bib = bibnum if (is_gap or not biblionumber) else biblionumber
        return [
            barcode,
            folder_path,
            is_gap,
            engine_used,
            'FIXED',
            ' | '.join(notes),
            str(final_bib or ''),
            db_title,
            db_author,
        ] + [catalog[k] for k in CATALOG_FIELDS]

    except Exception as e:
        if ssh_conn: ssh_conn.close()
        cat = apply_standard_defaults(empty_catalog_payload(), db_call_number, barcode)
        return [
            barcode,
            folder_path,
            is_gap,
            'ERROR',
            'FAILED',
            str(e),
            str(biblionumber or ''),
            db_title,
            db_author,
        ] + [cat[k] for k in CATALOG_FIELDS]

if __name__ == "__main__":
    base_dir = r"D:\Clients\MarineFisheries2"
    images_root = os.path.join(
        base_dir,
        "external",
        "book-card-to-marc",
        "workflows",
        "mfa-google-drive-images-to-koha",
        "data",
        "images",
    )
    scan_dir = images_root if os.path.isdir(images_root) else base_dir
    gap_file = os.path.join(base_dir, "barcode_gaps.md")
    
    gaps = set()
    try:
        with open(gap_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith("- `") and len(line) > 10:
                    bc = line.split("`")[1]
                    gaps.add(bc)
                elif '-' in line and line.count('-') == 1:
                    try:
                        parts = line.split('-')
                        for i in range(int(parts[0]), int(parts[1]) + 1): gaps.add(f"{i:06d}")
                    except: pass
                elif len(line) == 6 and line.isdigit(): gaps.add(line)
    except: pass
    
    # Pre-fetch entire Koha catalog to avoid SSH bombing!
    print("Fetching Koha database catalog in one query...")
    koha_catalog = {}
    try:
        main_ssh = get_db_connection()
        stdin, stdout, stderr = main_ssh.exec_command("mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -N -e \"SELECT i.barcode, b.title, b.author, i.biblionumber, IFNULL(i.itemcallnumber,'') FROM koha_mfa.items i JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber;\"")
        raw_db = stdout.read().decode().strip().split('\n')
        for row in raw_db:
            if not row.strip(): continue
            parts = row.split('\t')
            bc = parts[0].strip().zfill(6)
            koha_catalog[bc] = {
                "title": parts[1] if len(parts) > 1 else "",
                "author": parts[2] if len(parts) > 2 else "",
                "bibnum": parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else ""),
                "call_number": parts[4] if len(parts) > 4 else "",
            }
        main_ssh.close()
        print(f"Loaded {len(koha_catalog)} healthy catalog items from Koha.")
    except Exception as e:
        print("Failed to fetch catalog:", e)

    print(f"Exhaustive scan for book folders under: {scan_dir}")
    book_folders = []
    for root, dirs, files in os.walk(scan_dir):
        folder_name = os.path.basename(root)
        barcode = extract_barcode_from_folder(folder_name)
        if barcode:
            imgs = [f for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'))]
            img_path = os.path.join(root, imgs[0]) if imgs else None

            is_gap = barcode in gaps
            db_record = koha_catalog.get(barcode, None)

            db_t = db_record["title"] if db_record else ""
            db_a = db_record["author"] if db_record else ""
            db_b = db_record["bibnum"] if db_record else ""
            db_c = db_record["call_number"] if db_record else ""
            book_folders.append((barcode, root, img_path, is_gap, db_t, db_a, db_b, db_c))
    
    total_folders = len(book_folders)
    total_gaps = len([x for x in book_folders if x[3]])

    resume_data = load_resume_state(base_dir)
    state_file = resume_data["state_file"]
    progress_file = resume_data["progress_file"]
    processed_keys = set(resume_data["processed_keys"])
    run_stamp = resume_data["run_stamp"] or datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = resume_data["csv_file"] or os.path.join(base_dir, f"triangulation_v5_log_{run_stamp}.csv")

    latest_status_by_key = {}
    if os.path.exists(csv_file):
        latest_status_by_key = load_latest_status_by_key_from_csv(csv_file)

    pending_book_folders = []
    for item in book_folders:
        item_key = make_item_key(item[0], item[1])
        if item_key not in processed_keys:
            pending_book_folders.append(item)

    def pending_priority(item):
        bc, folder, img_path, is_gap, db_t, db_a, db_b, db_c = item
        key = make_item_key(bc, folder)
        st = latest_status_by_key.get(key, {})
        action = st.get('action', '')
        notes = st.get('notes', '')

        # Priority bands (lower first):
        # 0 = unseen/not failed yet and has image
        # 1 = previously failed but has image (likely recoverable)
        # 2 = no-image rows with existing biblionumber (verification-only possible)
        # 3 = no-image rows without biblionumber / persistently deferred
        has_image = bool(img_path)
        has_live_bib = bool(str(db_b or '').strip().isdigit())
        is_failed = (action == 'FAILED')
        is_no_image_fail = ('no image file found in folder' in notes)
        is_deferred = ('deferred' in notes and 'ollama' in notes)

        if has_image and not is_failed:
            band = 0
        elif has_image and is_failed and not is_deferred:
            band = 1
        elif (not has_image) and has_live_bib:
            band = 2
        else:
            band = 3

        # Stable secondary sort by barcode then folder name.
        return (band, bc, os.path.basename(folder).lower())

    pending_book_folders.sort(key=pending_priority)

    print(f"Books queued for Triangulation Pipeline: {total_folders} (Gaps: {total_gaps}).")
    if resume_data["resume"]:
        print(f"Resume mode: continuing previous run log {csv_file}")
        print(f"Already processed: {len(processed_keys)} | Remaining: {len(pending_book_folders)}")
        print("Queue policy: prioritize records likely to verify complete first; keep hard failures for later in the same run.")
    else:
        print(f"Starting new run log: {csv_file}")

    save_resume_state(state_file, {
        "run_stamp": run_stamp,
        "csv_file": csv_file,
        "progress_file": progress_file,
        "completed": False,
        "updated_at": datetime.now().isoformat(),
        "total_folders": total_folders,
    })

    csv_mode = 'a' if resume_data["resume"] else 'w'
    max_workers = get_env_int("TRIANGULATE_V5_WORKERS", 20, min_value=1, max_value=48)
    retry_passes = get_env_int("TRIANGULATE_V5_RETRY_PASSES", 3, min_value=0, max_value=20)
    print(f"Parallel workers: {max_workers} | vLLM WSL High-Concurrency Active")

    with open(progress_file, 'a', encoding='utf-8') as pf, open(csv_file, csv_mode, newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if csv_mode == 'w':
            writer.writerow([
                'Timestamp',
                'Barcode',
                'Folder',
                'Is_Gap',
                'Method',
                'Action',
                'Result_Notes',
                'DB_Biblionumber',
                'DB_Title',
                'DB_Author',
            ] + CATALOG_FIELDS)
            f.flush()
        
        processed_count = len(processed_keys)
        log_lock = threading.Lock()
        pending_batch = list(pending_book_folders)
        pass_no = 0
        while pending_batch and pass_no <= retry_passes:
            if pass_no > 0:
                print(f"Retry pass {pass_no}/{retry_passes}: attempting deferred folders: {len(pending_batch)}")
            deferred_next = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_item = {
                    executor.submit(process_book, b, folder, p, g, dt, da, db, dc): (b, folder, p, g, dt, da, db, dc)
                    for b, folder, p, g, dt, da, db, dc in pending_batch
                }
                for future in as_completed(future_to_item):
                    b, folder, p, g, dt, da, db, dc = future_to_item[future]
                    try:
                        res = future.result()
                        item_key = make_item_key(b, folder)
                        method = (res[3] or '').strip().lower() if len(res) > 3 else ''
                        action = (res[4] or '').strip().upper() if len(res) > 4 else ''
                        notes = (res[5] or '').strip().lower() if len(res) > 5 else ''
                        is_deferred = (method == 'ollama (skipped/timeout)') or (action == 'FAILED' and 'deferred' in notes and 'ollama' in notes)
                        is_verified_complete = (action == 'FIXED' and 'verify: ok' in notes)
                        is_terminal_no_image = (action == 'SKIPPED' and 'no-image terminal' in notes)

                        with log_lock:
                            writer.writerow([datetime.now().isoformat()] + res)
                            f.flush()

                        if is_deferred:
                            deferred_next.append((b, folder, p, g, dt, da, db, dc))
                            print(f"[{processed_count}/{total_folders}] {b} @ {os.path.basename(folder)} -> deferred")
                            continue

                        if is_verified_complete and item_key not in processed_keys:
                            with log_lock:
                                pf.write(item_key + "\n")
                                pf.flush()
                            processed_keys.add(item_key)
                            processed_count += 1
                            print(f"[{processed_count}/{total_folders}] {b} @ {os.path.basename(folder)} -> {res[9]}")
                        elif is_terminal_no_image and item_key not in processed_keys:
                            with log_lock:
                                pf.write(item_key + "\n")
                                pf.flush()
                            processed_keys.add(item_key)
                            processed_count += 1
                            print(f"[{processed_count}/{total_folders}] {b} @ {os.path.basename(folder)} -> closed (no-image terminal)")
                        elif action == 'FAILED':
                            print(f"[{processed_count}/{total_folders}] {b} @ {os.path.basename(folder)} -> failed (not complete; requires verified fix)")
                        else:
                            print(f"[{processed_count}/{total_folders}] {b} @ {os.path.basename(folder)} -> pending verification")
                    except Exception as e:
                        print(f"[{processed_count}/{total_folders}] {b} ERROR: {e}")

            if not deferred_next:
                pending_batch = []
                break
            pending_batch = deferred_next
            pass_no += 1

        if pending_batch:
            print(f"Unfinished deferred folders after retry passes: {len(pending_batch)}. They will resume automatically on next restart.")

    reconcile_record_fields_before_finish(csv_file)

    all_verified_complete = (len(processed_keys) >= total_folders)
    if not all_verified_complete:
        print(f"Run not marked complete: verified records {len(processed_keys)}/{total_folders}. Resume will continue remaining records.")

    save_resume_state(state_file, {
        "run_stamp": run_stamp,
        "csv_file": csv_file,
        "progress_file": progress_file,
        "completed": all_verified_complete,
        "updated_at": datetime.now().isoformat(),
        "total_folders": total_folders,
        "processed_total": len(processed_keys),
    })
    print(f"Run log saved: {csv_file}")
