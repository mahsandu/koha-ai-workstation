import os
import json
import uuid
import threading
import time
import bcrypt
import xml.etree.ElementTree as ET
import requests
import base64
import re
import logging
import shutil
import zipfile
import subprocess
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, send_from_directory, Blueprint, send_file

# Setup server logging
logging.basicConfig(filename='/var/www/koha_editor/app.log', level=logging.INFO, 
                    format='%(asctime)s %(levelname)s: %(message)s')

import pymysql
import sqlite3

try:
    import config as app_config
except Exception:
    app_config = None

try:
    from google import genai as google_genai
except Exception:
    google_genai = None

app = Flask(__name__)

# Blueprint for Browse Sources APIs
sources_bp = Blueprint('sources', __name__, url_prefix='/api/sources')
app.secret_key = 'super_secret_koha_editor_key' # Replace in prod
app.register_blueprint(sources_bp)

# The queue for AI tasks

SQLITE_DB = '/var/www/koha_editor/workstation.db'

def init_sqlite():
    conn = sqlite3.connect(SQLITE_DB)
    c = conn.cursor()
    # WAL mode improves concurrency when multiple worker threads access the queue.
    try:
        c.execute('PRAGMA journal_mode=WAL')
    except Exception:
        pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS ai_task_queue (
            task_id TEXT PRIMARY KEY,
            display_id INTEGER,
            type TEXT,
            status TEXT,
            biblionumber INTEGER,
            title TEXT,
            author TEXT,
            images TEXT,
            result_data TEXT,
            processing_log TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_sqlite()

# Ensure display_id exists for older schemas and assign sequential IDs based on created_at.
def migrate_queue_display_ids():
    try:
        conn = sqlite3.connect(SQLITE_DB)
        c = conn.cursor()
        for col in ['display_id', 'completed_at', 'processing_log', 'started_at']:
            try:
                if col == 'display_id':
                    c.execute(f'ALTER TABLE ai_task_queue ADD COLUMN {col} INTEGER')
                else:
                    c.execute(f'ALTER TABLE ai_task_queue ADD COLUMN {col} TIMESTAMP')
                conn.commit()
            except Exception:
                pass
        c.execute('''
            UPDATE ai_task_queue
            SET display_id = (
                SELECT new_id FROM (
                    SELECT task_id, ROW_NUMBER() OVER (ORDER BY created_at ASC) AS new_id
                    FROM ai_task_queue
                ) AS mapping
                WHERE mapping.task_id = ai_task_queue.task_id
            )
            WHERE display_id IS NULL
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f'display_id migration skipped: {e}')

migrate_queue_display_ids()

def _append_task_log(task_id, message, level='info'):
    """Append a timestamped log entry to a queue task's processing_log JSON array."""
    import json, datetime
    try:
        conn = sqlite3.connect(SQLITE_DB)
        c = conn.cursor()
        c.execute("SELECT processing_log FROM ai_task_queue WHERE task_id = ?", (task_id,))
        row = c.fetchone()
        logs = []
        if row and row[0]:
            try:
                logs = json.loads(row[0])
                if not isinstance(logs, list):
                    logs = []
            except Exception:
                logs = []
        logs.append({
            'ts': datetime.datetime.now().isoformat(),
            'level': level,
            'message': message
        })
        c.execute("UPDATE ai_task_queue SET processing_log = ? WHERE task_id = ?", (json.dumps(logs), task_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to append task log for {task_id}: {e}")

# Initialize source-related tables if not exist
conn = sqlite3.connect(SQLITE_DB)
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS source_folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        parent_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_id) REFERENCES source_folders(id)
    )
''')
cur.execute('''
    CREATE TABLE IF NOT EXISTS source_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        folder_id INTEGER,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        filetype TEXT,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        version TEXT,
        ocr_text TEXT,
        mrc_json TEXT,
        processing_log TEXT,
        FOREIGN KEY(folder_id) REFERENCES source_folders(id)
    )
''')
cur.execute('''
    CREATE TABLE IF NOT EXISTS source_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER,
        version_number TEXT,
        filepath TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(file_id) REFERENCES source_files(id)
    )
''')
conn.commit()
conn.close()

# Ensure source file storage directory exists (dedicated folder under /home)
SOURCE_ROOT = '/home/koha_sources'
SOURCE_DIR = SOURCE_ROOT
import os
os.makedirs(SOURCE_ROOT, exist_ok=True)

# Helper to resolve a relative path safely within SOURCE_ROOT
def resolve_source_path(rel_path):
    # Prevent path traversal
    safe_path = os.path.normpath(os.path.join(SOURCE_ROOT, rel_path))
    if not safe_path.startswith(os.path.abspath(SOURCE_ROOT)):
        raise ValueError('Invalid path')
    return safe_path




# Folder where uploaded images are saved on the server
IMAGE_DIR = '/var/www/koha_editor/images'
if not os.path.exists(IMAGE_DIR):
    os.makedirs(IMAGE_DIR, exist_ok=True)


CONFIG_PATH = '/var/www/koha_editor/config.json'
DEFAULT_CONFIG = {
    "db_host": "127.0.0.1",
    "db_user": "koha_mfa",
    "db_pass": "HdOd?^`UVa`c3^W~",
    "db_name": "koha_mfa",
    "items_per_page": 50
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w') as f:
            json.dump(DEFAULT_CONFIG, f)
        return DEFAULT_CONFIG
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def get_db_connection():
    config = load_config()
    return pymysql.connect(
        host=config.get('db_host', '127.0.0.1'),
        user=config.get('db_user', 'koha_mfa'),
        password=config.get('db_pass', 'HdOd?^`UVa`c3^W~'),
        database=config.get('db_name', 'koha_mfa'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )


def get_gemini_api_key():
    key = ""
    if app_config and hasattr(app_config, 'GEMINI_API_KEY'):
        key = str(getattr(app_config, 'GEMINI_API_KEY') or '').strip()
    if not key:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
    return key


def call_gemini(prompt, model='gemini-2.5-flash'):
    if google_genai is None:
        raise RuntimeError("google-genai package is not installed")
    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    client = google_genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt
    )
    text = (getattr(response, 'text', None) or '').strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return text


def call_local_ai(prompt, model_hint='gemma3:4b'):
    # Server-side local AI endpoints are tried first; tunneled remote is last resort.
    local_bases = [
        'http://127.0.0.1:8000',     # server-local Ollama/vLLM (primary)
        'http://127.0.0.1:11434',    # server-local Ollama native port
        'http://127.0.0.1:5000',     # server-local alternative API port
    ]
    configured_base = os.environ.get('LOCAL_AI_BASE_URL', '').strip()
    if configured_base and configured_base.rstrip('/') not in local_bases:
        local_bases.append(configured_base)  # workstation tunnel or custom endpoint

    openai_model = os.environ.get('LOCAL_AI_MODEL', model_hint).strip() or model_hint
    ollama_model = os.environ.get('OLLAMA_MODEL', model_hint).strip() or model_hint

    errors = []
    seen = set()
    for base in local_bases:
        base = base.rstrip('/')
        if not base or base in seen:
            continue
        seen.add(base)

        try:
            payload = {
                "model": openai_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0
            }
            res = requests.post(f'{base}/v1/chat/completions', json=payload, timeout=45)
            res.raise_for_status()
            data = res.json()
            text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            if text and text.strip():
                return text.strip()
            raise RuntimeError("empty chat completion")
        except Exception as e:
            errors.append(f"{base}/v1/chat/completions: {e}")

        if base.endswith(':11434'):
            try:
                payload = {
                    "model": ollama_model,
                    "prompt": prompt,
                    "stream": False
                }
                res = requests.post(f'{base}/api/generate', json=payload, timeout=60)
                res.raise_for_status()
                data = res.json()
                text = data.get('response', '')
                if text and text.strip():
                    return text.strip()
                raise RuntimeError("empty ollama response")
            except Exception as e:
                errors.append(f"{base}/api/generate: {e}")

    raise RuntimeError("Local AI unavailable: " + " | ".join(errors))


def run_ai_with_fallback(prompt, ai_model):
    mode = (ai_model or 'deepseek').lower().strip()
    errors = []

    def try_local():
        return call_local_ai(prompt)

    def try_gemini():
        return call_gemini(prompt)

    # For explicit "gemini"/"api" mode, try cloud API first, then server-local, then any configured remote.
    # For all other modes (deepseek, local, fallback), prefer server-local AI first,
    # then cloud API, then configured remote tunnel as final fallback.
    if mode in ('gemini', 'api'):
        engines = [('gemini', try_gemini, 'gemini'), ('local', try_local, 'local')]
    else:
        engines = [('local', try_local, 'local'), ('gemini', try_gemini, 'gemini')]

    for label, fn, engine_id in engines:
        try:
            return fn(), engine_id
        except Exception as e:
            errors.append(f'{label}: {e}')

    raise RuntimeError("All AI engines failed: " + " | ".join(errors))



def init_db():
    import sqlite3
    sq_conn = sqlite3.connect(SQLITE_DB)
    try:
        sq_conn.execute("ALTER TABLE ai_task_queue ADD COLUMN task_config TEXT")
        sq_conn.commit()
    except:
        pass
    finally:
        sq_conn.close()
        
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS koha_mfa.biblio_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    biblionumber INT NOT NULL,
                    old_metadata LONGTEXT,
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
    except Exception as e:
        logging.error(f"DB Init error: {e}")
    finally:
        conn.close()

init_db()

def is_garbled(text):

    if not text: return False
    if '\ufffd' in text or 'Ã' in text or 'Â' in text or 'â' in text:
        return True
    return False

def is_invalid_year(year):
    if not year: return False
    y = str(year).strip()
    if not y.isdigit() or len(y) < 3 or len(y) > 4:
        return True
    return False

def parse_marc_xml_to_json(xml_str):
    try:
        root = ET.fromstring(xml_str)
        # Handle namespaced MARCXML (default namespace http://www.loc.gov/MARC21/slim)
        ns = {'m': 'http://www.loc.gov/MARC21/slim'}
        fields = []
        for child in root:
            tag_name = child.tag
            if tag_name.startswith('{'):
                tag_name = tag_name.split('}', 1)[1]
            if tag_name == 'leader':
                fields.append({'tag': 'LDR', 'value': child.text or ''})
            elif tag_name == 'controlfield':
                fields.append({'tag': child.attrib.get('tag', ''), 'value': child.text or ''})
            elif tag_name == 'datafield':
                tag = child.attrib.get('tag', '')
                ind1 = child.attrib.get('ind1', ' ') or ' '
                ind2 = child.attrib.get('ind2', ' ') or ' '
                subfields = []
                for sf in child.findall('m:subfield', ns) if '{' in child.tag else child.findall('subfield'):
                    sf_tag = sf.tag
                    if sf_tag.startswith('{'):
                        sf_tag = sf_tag.split('}', 1)[1]
                    if sf_tag == 'subfield':
                        subfields.append({'code': sf.attrib.get('code', ''), 'value': sf.text or ''})
                fields.append({'tag': tag, 'ind1': ind1, 'ind2': ind2, 'subfields': subfields})
        return fields
    except Exception as e:
        import traceback
        traceback.print_exc()
        return []

def build_marc_xml_from_json(fields):
    root = ET.Element("record")
    for f in fields:
        tag = f.get('tag')
        if not tag: continue
        
        if tag == 'LDR':
            elem = ET.SubElement(root, "leader")
            elem.text = f.get('value', '')
        elif tag.isdigit() and int(tag) < 10: # Control fields 001-009
            elem = ET.SubElement(root, "controlfield", tag=tag)
            elem.text = f.get('value', '')
        else:
            elem = ET.SubElement(root, "datafield", tag=tag, ind1=f.get('ind1', ' '), ind2=f.get('ind2', ' '))
            for sf in f.get('subfields', []):
                selem = ET.SubElement(elem, "subfield", code=sf.get('code', ''))
                selem.text = sf.get('value', '')
    return ET.tostring(root, encoding='utf-8', xml_declaration=False).decode('utf-8')

def extract_flat_data_from_json(fields):
    """Extract flat values for SQL tables from JSON fields array"""
    data = {"title": "", "author": "", "publishercode": "", "publicationyear": "", "pages": ""}
    for f in fields:
        if f.get('tag') == '245':
            for sf in f.get('subfields', []):
                if sf.get('code') == 'a': data["title"] = sf.get('value', '')
        elif f.get('tag') == '100':
            for sf in f.get('subfields', []):
                if sf.get('code') == 'a': data["author"] = sf.get('value', '')
        elif f.get('tag') == '260':
            for sf in f.get('subfields', []):
                if sf.get('code') == 'b': data["publishercode"] = sf.get('value', '')
                elif sf.get('code') == 'c': data["publicationyear"] = sf.get('value', '')
        elif f.get('tag') == '300':
            for sf in f.get('subfields', []):
                if sf.get('code') == 'a': data["pages"] = sf.get('value', '')
    return data

# --- AUTHENTICATION ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT b.borrowernumber, b.password, b.flags, b.userid 
                    FROM borrowers b 
                    WHERE b.userid = %s OR b.cardnumber = %s
                """, (username, username))
                user = cursor.fetchone()
                
                if user:
                    db_hash = user['password'].encode('utf-8')
                    if bcrypt.checkpw(password.encode('utf-8'), db_hash):
                        cursor.execute("""
                            SELECT module_bit, code FROM user_permissions 
                            WHERE borrowernumber = %s
                        """, (user['borrowernumber'],))
                        perms = cursor.fetchall()
                        
                        has_cat_perm = False
                        if user['flags'] and int(user['flags']) % 2 == 1:
                            has_cat_perm = True
                        else:
                            for p in perms:
                                if p['module_bit'] == 9:
                                    has_cat_perm = True
                        
                        if has_cat_perm:
                            session['user'] = username
                            return redirect('/koha_editor/')
                        else:
                            return render_template('login.html', error="Permission Denied.")
                    else:
                        return render_template('login.html', error="Invalid password.")
                else:
                    return render_template('login.html', error="User not found.")
        finally:
            conn.close()
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/koha_editor/login')

def login_required(f):
    def wrap(*args, **kwargs):
        if 'user' not in session:
            return redirect('/koha_editor/login')
        return f(*args, **kwargs)
    wrap.__name__ = f.__name__
    return wrap

# --- ROUTES ---
@app.route('/')
@login_required
def index():
    return render_template('index.html', user=session['user'])

# --- LIVE CATALOG DASHBOARD ---
@app.route('/api/stats')
@login_required
def stats():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    i.barcode, b.biblionumber, b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages
                FROM koha_mfa.items i
                LEFT JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber
                LEFT JOIN koha_mfa.biblioitems bi ON i.biblioitemnumber = bi.biblioitemnumber
            """)
            rows = cursor.fetchall()
            
            total = 0
            complete = 0
            incomplete = 0
            issues = {
                "Missing_Title": 0, "Garbled_Title": 0,
                "Missing_Author": 0, "Garbled_Author": 0,
                "Missing_Publisher": 0, "Garbled_Publisher": 0,
                "Missing_Year": 0, "Invalid_Year": 0,
                "Missing_Pages": 0, "Missing_Data": 0
            }
            
            for row in rows:
                total += 1
                title = row['title'] or ''
                author = row['author'] or ''
                pub = row['publishercode'] or ''
                year = row['publicationyear'] or ''
                pages = row['pages'] or ''
                
                row_issues = []
                if not title.strip(): row_issues.append("Missing_Title")
                elif is_garbled(title): row_issues.append("Garbled_Title")
                
                if not author.strip(): row_issues.append("Missing_Author")
                elif is_garbled(author): row_issues.append("Garbled_Author")
                
                if not pub.strip(): row_issues.append("Missing_Publisher")
                elif is_garbled(pub): row_issues.append("Garbled_Publisher")
                
                if not str(year).strip(): row_issues.append("Missing_Year")
                elif is_invalid_year(year): row_issues.append("Invalid_Year")
                
                # Make pages optional so records can be 'clean'
                # if not pages.strip(): row_issues.append("Missing_Pages")
                
                if not title.strip() or not author.strip() or not pub.strip() or not str(year).strip():
                    row_issues.append("Missing_Data")
                
                if row_issues:
                    incomplete += 1
                    for issue in row_issues:
                        issues[issue] += 1
                else:
                    complete += 1

            # Queue stats
            sq_conn = sqlite3.connect(SQLITE_DB)
            sq_conn.row_factory = sqlite3.Row
            sq_c = sq_conn.cursor()
            sq_c.execute("SELECT status, COUNT(*) as c FROM ai_task_queue GROUP BY status")
            q_stats = {'pending': 0, 'processing': 0, 'completed': 0, 'error': 0}
            for r in sq_c.fetchall():
                if r['status'] in q_stats:
                    q_stats[r['status']] = r['c']
            sq_conn.close()

            return jsonify({
                "total": total,
                "complete": complete,
                "incomplete": incomplete,
                "issues": issues,
                "q_stats": q_stats
            })
    finally:
        conn.close()

@app.route('/api/records')
@login_required
def get_records():
    config = load_config()
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', config.get('items_per_page', 50)))
    filter_type = request.args.get('filter', 'all')
    issue_type = request.args.get('issue', None)
    
    q_keyword = request.args.get('q_keyword', '').strip()
    q_subject = request.args.get('q_subject', '').strip()
    q_barcode = request.args.get('q_barcode', '').strip()
    q_callnumber = request.args.get('q_callnumber', '').strip()
    
    sort_by = request.args.get('sort', 'barcode')
    sort_desc = request.args.get('desc', 'false') == 'true'

    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            query = """
                SELECT 
                    b.biblionumber, i.barcode, i.itemcallnumber, b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages, b.datecreated
                FROM koha_mfa.items i
                LEFT JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber
                LEFT JOIN koha_mfa.biblioitems bi ON i.biblioitemnumber = bi.biblioitemnumber
                WHERE 1=1
            """
            params = []
            
            if q_keyword:
                query += " AND (b.title LIKE %s OR b.author LIKE %s)"
                params.extend([f"%{q_keyword}%", f"%{q_keyword}%"])
            if q_barcode:
                query += " AND i.barcode LIKE %s"
                params.append(f"%{q_barcode}%")
            if q_callnumber:
                query += " AND i.itemcallnumber LIKE %s"
                params.append(f"%{q_callnumber}%")
                
            order_dir = "DESC" if sort_desc else "ASC"
            if sort_by == 'title':
                query += f" ORDER BY b.title {order_dir}"
            elif sort_by == 'author':
                query += f" ORDER BY b.author {order_dir}"
            elif sort_by == 'year':
                query += f" ORDER BY CAST(bi.publicationyear AS UNSIGNED) {order_dir}"
            else:
                query += f" ORDER BY CAST(i.barcode AS UNSIGNED) {order_dir}"
                
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            records = []
            for row in rows:
                bib = row['biblionumber']
                barcode = row['barcode']
                title = row['title'] or ''
                author = row['author'] or ''
                pub = row['publishercode'] or ''
                year = row['publicationyear'] or ''
                pages = row['pages'] or ''
                
                # Basic issue checking
                is_bad = False
                reasons = []
                if not title.strip(): reasons.append("Missing_Title")
                if is_garbled(title): reasons.append("Garbled_Title"); is_bad = True
                if not author.strip(): reasons.append("Missing_Author")
                if is_garbled(author): reasons.append("Garbled_Author"); is_bad = True
                if not pub.strip(): reasons.append("Missing_Publisher")
                if is_garbled(pub): reasons.append("Garbled_Publisher"); is_bad = True
                if not str(year).strip(): reasons.append("Missing_Year")
                if is_invalid_year(year): reasons.append("Invalid_Year"); is_bad = True
                # if not pages.strip(): reasons.append("Missing_Pages")
                
                if filter_type == 'garbled' and not is_bad:
                    continue
                if filter_type == 'broken' and len(reasons) == 0:
                    continue
                if filter_type == 'clean' and len(reasons) > 0:
                    continue
                if filter_type == 'today':
                    import datetime
                    if not row['datecreated'] or row['datecreated'].date() != datetime.date.today():
                        continue
                if filter_type == 'gaps':
                    # Skip non-numeric barcodes; shown only for gap visualization
                    if not str(barcode).isdigit():
                        continue

                if issue_type and issue_type != 'all' and issue_type not in reasons:
                    continue
                    
                # NOTE: Subject search is complex (requires XML parsing or tags table), 
                # so we will do it here in Python for now if requested.
                
                records.append({
                    "biblionumber": bib,
                    "barcode": barcode,
                    "title": title,
                    "author": author,
                    "publishercode": pub,
                    "publicationyear": year,
                    "pages": pages,
                    "issues": ", ".join([r.replace('_', ' ') for r in reasons])
                })
                
            start = (page - 1) * limit
            end = start + limit
            return jsonify({
                "total": len(records),
                "page": page,
                "data": records[start:end]
            })
    finally:
        conn.close()

@app.route('/api/records/<biblionumber>/raw')
@login_required
def get_raw_record(biblionumber):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT metadata FROM koha_mfa.biblio_metadata WHERE biblionumber = %s", (biblionumber,))
            row = cursor.fetchone()
            if row and row['metadata']:
                fields = parse_marc_xml_to_json(row['metadata'])
                return jsonify({"biblionumber": biblionumber, "fields": fields})
            return jsonify({"error": "No metadata found"}), 404
    finally:
        conn.close()

@app.route('/api/records/<biblionumber>', methods=['POST'])
@login_required
def update_record(biblionumber):
    data = request.json
    fields = data.get('fields', [])
    
    if not fields:
        return jsonify({"error": "No fields provided"}), 400
        
    flat_data = extract_flat_data_from_json(fields)
    new_xml = build_marc_xml_from_json(fields)
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Update SQL tables for fast searching
            cursor.execute("UPDATE koha_mfa.biblio SET title = %s, author = %s WHERE biblionumber = %s", 
                           (flat_data['title'], flat_data['author'], biblionumber))
            
            cursor.execute("UPDATE koha_mfa.biblioitems SET publishercode = %s, publicationyear = %s, pages = %s WHERE biblionumber = %s", 
                           (flat_data['publishercode'], flat_data['publicationyear'], flat_data['pages'], biblionumber))
            
            # 2. Update MARC XML
            cursor.execute("UPDATE koha_mfa.biblio_metadata SET metadata = %s WHERE biblionumber = %s", (new_xml, biblionumber))
            
            conn.commit()
    finally:
        conn.close()
        
    return jsonify({"success": True})

# --- ITEMS MANAGEMENT ---
@app.route('/api/records/<biblionumber>/summary')
@login_required
def get_record_summary(biblionumber):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT b.biblionumber, b.title, b.author, bi.publishercode, bi.publicationyear,
                       i.barcode, i.itemcallnumber, i.homebranch, i.location
                FROM koha_mfa.biblio b
                LEFT JOIN koha_mfa.biblioitems bi ON b.biblionumber = bi.biblionumber
                LEFT JOIN koha_mfa.items i ON b.biblionumber = i.biblionumber
                WHERE b.biblionumber = %s
                LIMIT 1
            """, (biblionumber,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "error": "Not found"}), 404
            classification = ""
            # Pull DDC/classification from 082/092 or 942$c if available in MARC
            cursor.execute("SELECT metadata FROM koha_mfa.biblio_metadata WHERE biblionumber = %s", (biblionumber,))
            m_row = cursor.fetchone()
            if m_row and m_row['metadata']:
                fields = parse_marc_xml_to_json(m_row['metadata'])
                for f in fields:
                    if f.get('tag') in ('082', '092'):
                        for sf in f.get('subfields', []):
                            if sf.get('code') == 'a':
                                classification = sf.get('value', '')
                                break
                    elif f.get('tag') == '942':
                        for sf in f.get('subfields', []):
                            if sf.get('code') == '2':
                                classification = sf.get('value', '')
                                break
            # Try to find a matching source folder by barcode or title prefix
            source_folder = None
            try:
                barcode_norm = (row.get('barcode') or '').strip().lstrip('0') or '0'
                title_prefix = re.sub(r'[^\w\s]', '', (row.get('title') or '').lower()).split()[:3]
                title_prefix = ' '.join(title_prefix)
                if os.path.isdir(SOURCE_DIR):
                    candidates = []
                    for name in os.listdir(SOURCE_DIR):
                        full = os.path.join(SOURCE_DIR, name)
                        if not os.path.isdir(full):
                            continue
                        norm_name = re.sub(r'[^\w\s]', '', name.lower())
                        score = 0
                        if barcode_norm and barcode_norm in norm_name:
                            score += 3
                        if title_prefix and title_prefix in norm_name:
                            score += 2
                        # Check subfolder name hints
                        if barcode_norm:
                            bname = os.path.basename(full)
                            if barcode_norm in re.sub(r'[^\w\s]', '', bname.lower()):
                                score += 1
                        if score > 0:
                            candidates.append((score, name))
                    if candidates:
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        source_folder = candidates[0][1]
            except Exception as e:
                logging.warning(f"Source folder lookup failed for biblio {biblionumber}: {e}")

            return jsonify({
                "success": True,
                "biblionumber": biblionumber,
                "title": row.get('title', ''),
                "author": row.get('author', ''),
                "publishercode": row.get('publishercode', ''),
                "publicationyear": row.get('publicationyear', ''),
                "barcode": row.get('barcode', ''),
                "itemcallnumber": row.get('itemcallnumber', ''),
                "homebranch": row.get('homebranch', ''),
                "location": row.get('location', ''),
                "classification": classification,
                "source_folder": source_folder
            })
    finally:
        conn.close()

@app.route('/api/records/<biblionumber>/items')
@login_required
def get_items(biblionumber):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT itemnumber, barcode, itemcallnumber, homebranch, location
                FROM koha_mfa.items 
                WHERE biblionumber = %s
            """, (biblionumber,))
            items = cursor.fetchall()
            return jsonify({"items": items})
    finally:
        conn.close()

@app.route('/api/items', methods=['POST'])
@login_required
def create_item():
    data = request.json
    biblionumber = data.get('biblionumber')
    barcode = data.get('barcode', '').strip()
    callnumber = data.get('itemcallnumber', '')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT itemnumber FROM koha_mfa.items WHERE barcode = %s", (barcode,))
            if cursor.fetchone():
                return jsonify({"error": "Barcode already exists!"}), 400
                
            cursor.execute("SELECT biblioitemnumber FROM koha_mfa.biblioitems WHERE biblionumber = %s", (biblionumber,))
            bitem = cursor.fetchone()
            if not bitem:
                return jsonify({"error": "Biblio not found"}), 404
            biblioitemnumber = bitem['biblioitemnumber']
            
            cursor.execute("""
                INSERT INTO koha_mfa.items (biblionumber, biblioitemnumber, barcode, itemcallnumber)
                VALUES (%s, %s, %s, %s)
            """, (biblionumber, biblioitemnumber, barcode, callnumber))
            conn.commit()
            return jsonify({"success": True, "itemnumber": cursor.lastrowid})
    finally:
        conn.close()

@app.route('/api/items/<itemnumber>', methods=['PUT', 'DELETE'])
@login_required
def modify_item(itemnumber):
    conn = get_db_connection()
    try:
        if request.method == 'DELETE':
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM koha_mfa.items WHERE itemnumber = %s", (itemnumber,))
                conn.commit()
            return jsonify({"success": True})
        
        elif request.method == 'PUT':
            data = request.json
            barcode = data.get('barcode', '').strip()
            callnumber = data.get('itemcallnumber', '')
            homebranch = data.get('homebranch', '')
            location = data.get('location', '')
            
            with conn.cursor() as cursor:
                if barcode:
                    cursor.execute("SELECT itemnumber FROM koha_mfa.items WHERE barcode = %s AND itemnumber != %s", (barcode, itemnumber))
                    if cursor.fetchone():
                        return jsonify({"error": "Barcode already exists!"}), 400
                
                cursor.execute("""
                    UPDATE koha_mfa.items 
                    SET barcode = %s, itemcallnumber = %s, homebranch = %s, location = %s
                    WHERE itemnumber = %s
                """, (barcode, callnumber, homebranch, location, itemnumber))
                conn.commit()
            return jsonify({"success": True})
    finally:
        conn.close()

@app.route('/api/next_barcode')
@login_required
def get_next_barcode():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT barcode FROM koha_mfa.items WHERE barcode REGEXP '^[0-9]+$'")
            rows = cursor.fetchall()
            if not rows:
                return jsonify({"next_barcode": "10001"})
            
            barcodes = sorted([int(r['barcode']) for r in rows])
            if not barcodes: return jsonify({"next_barcode": "10001"})
            
            for i in range(len(barcodes) - 1):
                if barcodes[i+1] - barcodes[i] > 1:
                    return jsonify({"next_barcode": str(barcodes[i] + 1)})
                    
            return jsonify({"next_barcode": str(barcodes[-1] + 1)})
    finally:
        conn.close()

@app.route('/api/check_duplicate_biblio', methods=['POST'])
@login_required
def check_duplicate_biblio():
    data = request.json
    title = data.get('title', '').strip()
    author = data.get('author', '').strip()
    biblionumber = data.get('current_biblionumber', None)
    
    if not title:
        return jsonify({"is_duplicate": False})
        
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if biblionumber:
                cursor.execute("""
                    SELECT biblionumber FROM koha_mfa.biblio 
                    WHERE title = %s AND author = %s AND biblionumber != %s
                    LIMIT 1
                """, (title, author, biblionumber))
            else:
                cursor.execute("""
                    SELECT biblionumber FROM koha_mfa.biblio 
                    WHERE title = %s AND author = %s
                    LIMIT 1
                """, (title, author))
            row = cursor.fetchone()
            if row:
                return jsonify({"is_duplicate": True, "duplicate_biblio": row['biblionumber']})
            return jsonify({"is_duplicate": False})
    finally:
        conn.close()





@app.route('/api/fixer/start', methods=['POST'])
@login_required
def start_fixer_batch():
    data = request.json
    biblionumber = data.get('biblionumber') # Optional: if single
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if biblionumber:
                cursor.execute('''
                    SELECT b.biblionumber, b.title, b.author, bi.publishercode, bi.publicationyear 
                    FROM koha_mfa.biblio b 
                    LEFT JOIN koha_mfa.biblioitems bi ON b.biblionumber = bi.biblionumber 
                    WHERE b.biblionumber = %s
                ''', (biblionumber,))
                rows = cursor.fetchall()
            else:
                # Find all with missing/garbled author, pub, year
                cursor.execute('''
                    SELECT b.biblionumber, b.title, b.author, bi.publishercode, bi.publicationyear 
                    FROM koha_mfa.biblio b 
                    LEFT JOIN koha_mfa.biblioitems bi ON b.biblionumber = bi.biblionumber 
                    WHERE b.author IS NULL OR b.author = '' 
                       OR bi.publishercode IS NULL OR bi.publishercode = ''
                       OR bi.publicationyear IS NULL OR bi.publicationyear = ''
                ''')
                rows = cursor.fetchall()
            
            count = 0
            sq_conn = sqlite3.connect(SQLITE_DB)
            sq_c = sq_conn.cursor()
            for row in rows:
                task_id = "FIX_" + str(uuid.uuid4())
                sq_c.execute('SELECT COALESCE(MAX(display_id), 0) + 1 FROM ai_task_queue')
                next_display_id = sq_c.fetchone()[0]
                sq_c.execute("INSERT INTO ai_task_queue (task_id, display_id, type, status, biblionumber, title, author, images) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (task_id, next_display_id, 'db_fix', 'pending', row['biblionumber'], row['title'], row['author'], '[]'))
                count += 1
            sq_conn.commit()
            sq_conn.close()
            return jsonify({"success": True, "tasks_added": count})
    finally:
        conn.close()

@app.route('/api/fixer/revert/<biblionumber>', methods=['POST'])
@login_required
def revert_biblio(biblionumber):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT old_metadata FROM koha_mfa.biblio_history WHERE biblionumber = %s ORDER BY id DESC LIMIT 1", (biblionumber,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "No history found for this record."}), 404
                
            old_xml = row['old_metadata']
            fields = parse_marc_xml_to_json(old_xml)
            flat_data = extract_flat_data_from_json(fields)
            
            cursor.execute("UPDATE koha_mfa.biblio SET title = %s, author = %s WHERE biblionumber = %s", 
                           (flat_data['title'], flat_data['author'], biblionumber))
            cursor.execute("UPDATE koha_mfa.biblioitems SET publishercode = %s, publicationyear = %s, pages = %s WHERE biblionumber = %s", 
                           (flat_data['publishercode'], flat_data['publicationyear'], flat_data['pages'], biblionumber))
            cursor.execute("UPDATE koha_mfa.biblio_metadata SET metadata = %s WHERE biblionumber = %s", (old_xml, biblionumber))
            
            # Delete the history entry so we can revert further back if needed
            cursor.execute("DELETE FROM koha_mfa.biblio_history WHERE biblionumber = %s ORDER BY id DESC LIMIT 1", (biblionumber,))
            conn.commit()
            return jsonify({"success": True})
    finally:
        conn.close()

# --- LOGS VIEWER ---

@app.route('/api/logs')
@login_required
def view_logs():
    try:
        with open('/var/www/koha_editor/app.log', 'r') as f:
            lines = f.readlines()
            return jsonify({"logs": "".join(lines[-200:])}) # Return last 200 lines
    except Exception as e:
        return jsonify({"error": str(e)})

# --- ADMIN SETTINGS ---
@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def manage_settings():
    username = session['user']
    conn = get_db_connection()
    is_super = False
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT flags FROM koha_mfa.borrowers WHERE userid = %s", (username,))
            u = cursor.fetchone()
            if u and u['flags'] and int(u['flags']) % 2 == 1:
                is_super = True
    finally:
        conn.close()
        
    if not is_super:
        return jsonify({"error": "Superuser permission required"}), 403
        
    if request.method == 'GET':
        return jsonify(load_config())
        
    if request.method == 'POST':
        data = request.json
        with open(CONFIG_PATH, 'w') as f:
            json.dump(data, f)
        return jsonify({"success": True})

# ------- LABELS ENHANCEMENT: add pagination & indexing -------
# Ensure indexes exist for faster label queries
conn = get_db_connection()
try:
    with conn.cursor() as cursor:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_barcode ON koha_mfa.items (barcode)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_callnumber ON koha_mfa.items (itemcallnumber)')
    conn.commit()
finally:
    conn.close()

# --- LABEL TAGGING ---

@app.route('/api/labels/preview', methods=['GET'])
@login_required
def preview_labels():
    start_bc = request.args.get('start', '')
    end_bc = request.args.get('end', '')
    if not start_bc:
        return jsonify({"error": "Missing start barcode"}), 400

    # Build barcode list
    prefix = ""
    for char in start_bc:
        if not char.isdigit():
            prefix += char
        else:
            break
    try:
        s_num = int(start_bc.replace(prefix, ''))
        e_num = int((end_bc or start_bc).replace(prefix, ''))
    except:
        return jsonify({"error": "Invalid barcode format"}), 400

    target_barcodes = [f"{prefix}{i}" for i in range(s_num, e_num + 1)]

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            format_strings = ','.join(['%s'] * len(target_barcodes))
            # Apply pagination parameters if provided
            limit = int(request.args.get('limit', 100))
            offset = int(request.args.get('offset', 0))
            cursor.execute(f"""
                SELECT i.barcode, i.itemcallnumber, i.itemnumber,
                       b.title, b.author
                FROM koha_mfa.items i
                JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber
                WHERE i.barcode IN ({format_strings})
                ORDER BY i.barcode
                LIMIT %s OFFSET %s
            """, tuple(target_barcodes) + (limit, offset))
            items = cursor.fetchall()
        return jsonify({"success": True, "items": items, "limit": limit, "offset": offset})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/labels/batch', methods=['POST'])
@login_required
def create_label_batch():
    data = request.json
    start_bc = data.get('start')
    end_bc = data.get('end')
    
    if not start_bc or not end_bc:
        return jsonify({"error": "Missing barcode range"}), 400
        
    prefix = ""
    for char in start_bc:
        if not char.isdigit():
            prefix += char
        else:
            break
            
    try:
        s_num = int(start_bc.replace(prefix, ''))
        e_num = int(end_bc.replace(prefix, ''))
    except:
        return jsonify({"error": "Invalid barcode format"}), 400
        
    target_barcodes = [f"{prefix}{i}" for i in range(s_num, e_num + 1)]
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Get itemnumbers for these barcodes
            format_strings = ','.join(['%s'] * len(target_barcodes))
            cursor.execute(f"SELECT itemnumber FROM koha_mfa.items WHERE barcode IN ({format_strings})", tuple(target_barcodes))
            items = cursor.fetchall()
            
            if not items:
                return jsonify({"error": "No matching items found"}), 404
                
            # Create a new batch in creator_batches
            # Ensure creator_batches table exists
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS creator_batches (
                    batch_id INTEGER PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Find next batch_id
            cursor.execute("SELECT MAX(batch_id) as max_b FROM creator_batches")
            b_row = cursor.fetchone()
            new_batch_id = (b_row['max_b'] or 0) + 1
            
            for item in items:
                # Insert to creator_batches
                cursor.execute("""
                    INSERT INTO koha_mfa.creator_batches (batch_id, item_number, creator) 
                    VALUES (%s, %s, 'labels')
                """, (new_batch_id, item['itemnumber']))
                
                # Update itemnotes_nonpublic to mark as [TAGGED]
                cursor.execute("SELECT itemnotes_nonpublic FROM koha_mfa.items WHERE itemnumber = %s", (item['itemnumber'],))
                n_row = cursor.fetchone()
                notes = n_row['itemnotes_nonpublic'] or ""
                if "[TAGGED]" not in notes:
                    new_notes = (notes + " [TAGGED]").strip()
                    cursor.execute("UPDATE koha_mfa.items SET itemnotes_nonpublic = %s WHERE itemnumber = %s", (new_notes, item['itemnumber']))
            
            # Insert new batch record
            cursor.execute('INSERT INTO creator_batches (batch_id) VALUES (?)', (new_batch_id,))
            conn.commit()
            return jsonify({"success": True, "batch_id": new_batch_id})
    finally:
        conn.close()


@app.route('/api/dashboard/stats', methods=['GET'])
@login_required
def get_dashboard_stats():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Total Biblios
            cursor.execute("SELECT COUNT(*) as c FROM koha_mfa.biblio")
            total_biblios = cursor.fetchone()['c']
            
            # Broken Biblios (Missing Author or Pub Year)
            cursor.execute('''
                SELECT COUNT(*) as c FROM koha_mfa.biblio b
                LEFT JOIN koha_mfa.biblioitems bi ON b.biblionumber = bi.biblionumber
                WHERE b.author IS NULL OR b.author = '' OR bi.publicationyear IS NULL OR bi.publicationyear = ''
            ''')
            broken_biblios = cursor.fetchone()['c']
            
            # Total Items
            cursor.execute("SELECT COUNT(*) as c FROM koha_mfa.items")
            total_items = cursor.fetchone()['c']
            
            # Catalogued Today
            cursor.execute("SELECT COUNT(*) as c FROM koha_mfa.biblio WHERE DATE(datecreated) = CURDATE()")
            catalogued_today = cursor.fetchone()['c']
    finally:
        conn.close()
        
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_conn.row_factory = sqlite3.Row
    sq_c = sq_conn.cursor()
    
    sq_c.execute("SELECT status, COUNT(*) as c FROM ai_task_queue WHERE created_at >= datetime('now', '-1 day') GROUP BY status")
    rows = sq_c.fetchall()
    q_stats = {'pending': 0, 'processing': 0, 'completed': 0, 'error': 0}
    for r in rows:
        if r['status'] in q_stats:
            q_stats[r['status']] = r['c']
            
    sq_conn.close()
    
    queue_count = q_stats['pending'] + q_stats['processing']
    
    # Barcode gap count (numeric barcodes only)
    gap_count = 0
    try:
        conn2 = get_db_connection()
        try:
            with conn2.cursor() as cursor:
                cursor.execute("SELECT barcode FROM koha_mfa.items WHERE barcode REGEXP '^[0-9]+$'")
                rows = cursor.fetchall()
                if rows:
                    barcodes = sorted([int(r['barcode']) for r in rows])
                    for i in range(len(barcodes) - 1):
                        if barcodes[i+1] - barcodes[i] > 1:
                            gap_count += 1
        finally:
            conn2.close()
    except Exception as e:
        logging.error(f"Barcode gap count failed: {e}")

    return jsonify({
        "total_biblios": total_biblios,
        "broken_biblios": broken_biblios,
        "total_items": total_items,
        "queue_count": queue_count,
        "catalogued_today": catalogued_today,
        "gap_count": gap_count,
        "q_stats": q_stats
    })


@app.route('/api/queue/batch', methods=['POST'])
@login_required
def batch_queue():
    data = request.json
    biblionumbers = data.get('biblionumbers', [])
    fix_type = data.get('fix_type', 'full')
    ai_model = data.get('ai_model', 'deepseek')
    
    if not biblionumbers:
        return jsonify({"error": "No items selected"}), 400
        
    import uuid, sqlite3, json
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_c = sq_conn.cursor()
    
    count = 0
    task_config = json.dumps({"fix_type": fix_type, "ai_model": ai_model})
    
    task_ids = []
    for bib in biblionumbers:
        sq_c.execute("SELECT task_id FROM ai_task_queue WHERE type='db_fix' AND biblionumber=? AND status IN ('pending', 'processing')", (bib,))
        if not sq_c.fetchone():
            task_id = str(uuid.uuid4())
            sq_c.execute('SELECT COALESCE(MAX(display_id), 0) + 1 FROM ai_task_queue')
            next_display_id = sq_c.fetchone()[0]
            sq_c.execute("INSERT INTO ai_task_queue (task_id, display_id, type, status, biblionumber, images, task_config) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (task_id, next_display_id, 'db_fix', 'pending', bib, '', task_config))
            task_ids.append(task_id)
            count += 1

    sq_conn.commit()
    sq_conn.close()

    return jsonify({"success": True, "queued": count, "task_ids": task_ids})

@app.route('/api/queue/task/<task_id>', methods=['GET'])
@login_required
def get_queue_task(task_id):
    import sqlite3, json
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_conn.row_factory = sqlite3.Row
    sq_c = sq_conn.cursor()
    sq_c.execute("SELECT * FROM ai_task_queue WHERE task_id = ?", (task_id,))
    row = sq_c.fetchone()
    sq_conn.close()
    if not row:
        return jsonify({"success": False, "error": "Task not found"}), 404

    result_data = row['result_data']
    processing_log = row['processing_log']
    try:
        result_data = json.loads(result_data) if result_data else {}
    except Exception:
        result_data = {"raw": result_data}
    try:
        processing_log = json.loads(processing_log) if processing_log else []
        if not isinstance(processing_log, list):
            processing_log = []
    except Exception:
        processing_log = []

    return jsonify({
        "success": True,
        "task": {
            "task_id": row['task_id'],
            "display_id": row['display_id'],
            "type": row['type'],
            "status": row['status'],
            "biblionumber": row['biblionumber'],
            "title": row['title'],
            "author": row['author'],
            "images": row['images'],
            "task_config": row['task_config'],
            "created_at": row['created_at'],
            "completed_at": row['completed_at'],
            "result_data": result_data,
            "processing_log": processing_log
        }
    })

# --- IMAGE UPLOADS ---

# --- SOURCE EXPLORER ---

SOURCE_DIR = SOURCE_ROOT
if not os.path.exists(SOURCE_DIR):
    os.makedirs(SOURCE_DIR, exist_ok=True)

def resolve_source_path(subpath):
    # Prevent path traversal
    if not subpath: return SOURCE_DIR
    target = os.path.abspath(os.path.join(SOURCE_DIR, subpath))
    if not target.startswith(SOURCE_DIR):
        return SOURCE_DIR
    return target

@app.route('/api/zebra/status', methods=['GET'])
@login_required
def zebra_status():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Query the zebraqueue for un-indexed records
            cursor.execute("SELECT count(*) as count FROM koha_mfa.zebraqueue WHERE done = 0")
            row = cursor.fetchone()
            count = row['count'] if row else 0
        return jsonify({"success": True, "pending_count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/zebra/reindex', methods=['POST'])
@login_required
def zebra_reindex():
    import subprocess
    try:
        # Run Koha fast re-indexing script
        cmd = 'koha-shell -c "rebuild_zebra.pl -b -a -z" mfa'
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # We don't block fully if it takes long, but for -z it's usually fast.
        out, err = process.communicate(timeout=60)
        
        if process.returncode == 0:
            return jsonify({"success": True, "message": "Zebra re-indexed successfully."})
        else:
            return jsonify({"error": err.decode('utf-8')}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"success": True, "message": "Re-index started in background."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _folder_catalog_status(folder_path):
    """Check cataloguing readiness for a source folder (one biblio record)."""
    status = {
        "has_images": False,
        "has_originals": False,
        "has_ocr": False,
        "has_metadata": False,
        "has_mrc": False,
        "has_koha_import": False,
        "image_count": 0,
        "ready_for": []
    }
    if not os.path.isdir(folder_path):
        return status
    for root, dirs, files in os.walk(folder_path):
        for fname in files:
            lower = fname.lower()
            if lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                status["image_count"] += 1
                if '_original' in lower:
                    status["has_originals"] = True
                else:
                    status["has_images"] = True
            elif lower.endswith('.ocr.txt'):
                status["has_ocr"] = True
            elif lower.endswith('.json') and not lower.endswith('_meta.json'):
                # Could be extracted_metadata.json or similar
                status["has_metadata"] = True
            elif lower.endswith(('.mrc', '.mrk')):
                status["has_mrc"] = True
            elif 'koha_import' in lower and lower.endswith('.mrc'):
                status["has_koha_import"] = True
    # Determine next ready step
    if status["has_images"]:
        status["ready_for"].append("OCR")
    if status["has_ocr"]:
        status["ready_for"].append("Metadata")
    if status["has_metadata"]:
        status["ready_for"].append("MRC")
    if status["has_mrc"]:
        status["ready_for"].append("Stage to Koha")
    return status


@app.route('/api/source/list', methods=['GET'])
@login_required
def source_list():
    path = request.args.get('path', '')
    target_dir = resolve_source_path(path)
    
    if not os.path.exists(target_dir):
        return jsonify({"error": "Path does not exist"}), 404
        
    items = []
    try:
        for f in os.listdir(target_dir):
            full_path = os.path.join(target_dir, f)
            rel_path = os.path.relpath(full_path, SOURCE_DIR)
            # Use forward slashes for relative path in UI
            rel_path = rel_path.replace('\\', '/')
            is_dir = os.path.isdir(full_path)
            size = os.path.getsize(full_path) if not is_dir else 0
            mtime = os.path.getmtime(full_path)
            
            item = {
                "name": f,
                "path": rel_path,
                "is_dir": is_dir,
                "size": size,
                "mtime": mtime
            }
            if is_dir:
                item["catalog_status"] = _folder_catalog_status(full_path)
            items.append(item)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
    return jsonify({"path": path.replace('\\', '/'), "items": sorted(items, key=lambda x: (not x['is_dir'], x['name']))})

@app.route('/api/source/mkdir', methods=['POST'])
@login_required
def source_mkdir():
    data = request.json
    path = data.get('path', '')
    new_dir = data.get('name', '').strip()
    if not new_dir: return jsonify({"error": "Directory name required"}), 400
    
    target_dir = resolve_source_path(os.path.join(path, new_dir))
    try:
        os.makedirs(target_dir, exist_ok=True)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/source/upload', methods=['POST'])
@login_required
def source_upload():
    path = request.form.get('path', '')
    base_target_dir = resolve_source_path(path)
    os.makedirs(base_target_dir, exist_ok=True)

    # Enforce 20MB per file limit
    max_size = 20 * 1024 * 1024
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    files = request.files.getlist('file')
    saved = []
    try:
        for f in files:
            if not f.filename:
                continue
            # Check file size
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(0)
            if size > max_size:
                return jsonify({"error": f"File {f.filename} exceeds 20MB limit"}), 400

            # Browser folder uploads include relative paths like "folder/file.jpg"
            rel_path = f.filename.replace('\\', '/')
            filename = os.path.basename(rel_path)
            sub_dir = os.path.dirname(rel_path)

            # Sanitize path components
            filename = "".join(c for c in filename if c.isalnum() or c in " ._-")
            if sub_dir:
                # Remove leading/trailing dots/slashes and sanitize each part
                parts = [p.strip().strip('.') for p in sub_dir.split('/') if p.strip().strip('.')]
                parts = ["".join(c for c in p if c.isalnum() or c in " _-") for p in parts]
                target_dir = os.path.join(base_target_dir, *parts)
                os.makedirs(target_dir, exist_ok=True)
            else:
                target_dir = base_target_dir

            save_path = os.path.join(target_dir, filename)
            f.save(save_path)
            saved.append(rel_path)
        return jsonify({"success": True, "saved": saved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/source/file', methods=['GET', 'POST'])
@login_required
def source_file_ops():
    if request.method == 'GET':
        path = request.args.get('path', '')
        target_file = resolve_source_path(path)
        
        if not os.path.exists(target_file) or os.path.isdir(target_file):
            return jsonify({"error": "File not found"}), 404
        
        # Download request must be handled before preview branches
        if request.args.get('download') == '1':
            return send_file(target_file, as_attachment=True)
            
        ext = target_file.lower().split('.')[-1]
        
        # Stream image types
        if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
            return send_from_directory(os.path.dirname(target_file), os.path.basename(target_file))
        
        # Read text types
        if ext in ['txt', 'json', 'md', 'csv', 'mrc']:
            try:
                with open(target_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                return jsonify({"success": True, "content": content})
            except Exception as e:
                return jsonify({"error": "Could not read file: " + str(e)}), 500
                
        return jsonify({"error": "Unsupported file type for preview"}), 400

    if request.method == 'POST':
        data = request.json
        path = data.get('path', '')
        content = data.get('content', '')
        target_file = resolve_source_path(path)
        
        if not path:
            return jsonify({"error": "Path required"}), 400
            
        try:
            # Write content to file
            with open(target_file, 'w', encoding='utf-8') as f:
                f.write(content)
            # Record version timestamp in source_versions table
            version_number = str(int(time.time()))
            vconn = sqlite3.connect(SQLITE_DB)
            vcur = vconn.cursor()
            vcur.execute(
                'INSERT OR IGNORE INTO source_versions (file_id, version_number, filepath) VALUES ((SELECT id FROM source_files WHERE filepath = ? LIMIT 1), ?, ?)',
                (target_file, version_number, target_file)
            )
            vconn.commit()
            vconn.close()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


# --- SOURCE PROCESSING API ---
@app.route('/api/source/process_ocr', methods=['POST'])
@login_required
def process_ocr():
    data = request.json
    paths = data.get('paths', [])
    if not paths: return jsonify({"error": "No files selected"}), 400
    
    import sqlite3
    conn = sqlite3.connect(SQLITE_DB)
    try:
        task_ids = []
        cursor = conn.cursor()
        for p in paths:
            target_file = resolve_source_path(p)
            if not os.path.exists(target_file): continue
            if not target_file.lower().endswith(('.jpg', '.jpeg', '.png')): continue
            
            task_id = f"ocr_{uuid.uuid4().hex[:12]}"
            cursor.execute(
                "INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                (task_id, 'source_ocr', 'pending', target_file, '')
            )
            task_ids.append(task_id)
        conn.commit()
        return jsonify({"success": True, "task_ids": task_ids})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/source/process_ai', methods=['POST'])
@login_required
def process_ai_metadata():
    data = request.json
    paths = data.get('paths', [])
    if not paths: return jsonify({"error": "No files selected"}), 400
    
    import sqlite3
    conn = sqlite3.connect(SQLITE_DB)
    try:
        task_ids = []
        cursor = conn.cursor()
        for p in paths:
            target_file = resolve_source_path(p)
            if not os.path.exists(target_file): continue
            if not target_file.lower().endswith(('.txt', '.ocr.txt')): continue
            
            task_id = f"meta_{uuid.uuid4().hex[:12]}"
            cursor.execute(
                "INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                (task_id, 'source_meta', 'pending', target_file, '')
            )
            task_ids.append(task_id)
        conn.commit()
        return jsonify({"success": True, "task_ids": task_ids})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/source/generate_mrc', methods=['POST'])
@login_required
def generate_mrc():
    data = request.json
    paths = data.get('paths', [])
    if not paths: return jsonify({"error": "No files selected"}), 400
    import sqlite3
    conn = sqlite3.connect(SQLITE_DB)
    try:
        task_ids = []
        cursor = conn.cursor()
        for p in paths:
            target_file = resolve_source_path(p)
            if not os.path.exists(target_file) or not target_file.lower().endswith('.json'): continue
            task_id = f"mrc_{uuid.uuid4().hex[:12]}"
            cursor.execute("INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                           (task_id, 'source_mrc', 'pending', target_file, ''))
            task_ids.append(task_id)
        conn.commit()
        return jsonify({"success": True, "task_ids": task_ids})
    finally: conn.close()

@app.route('/api/source/stage_koha', methods=['POST'])
@login_required
def stage_koha():
    data = request.json
    paths = data.get('paths', [])
    if not paths: return jsonify({"error": "No files selected"}), 400
    import sqlite3
    conn = sqlite3.connect(SQLITE_DB)
    try:
        task_ids = []
        cursor = conn.cursor()
        for p in paths:
            target_file = resolve_source_path(p)
            if not os.path.exists(target_file) or not target_file.lower().endswith('.mrc'): continue
            task_id = f"stage_{uuid.uuid4().hex[:12]}"
            cursor.execute("INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                           (task_id, 'source_stage', 'pending', target_file, ''))
            task_ids.append(task_id)
        conn.commit()
        return jsonify({"success": True, "task_ids": task_ids})
    finally: conn.close()


# --- SOURCE FILE MANAGER ---

def _safe_inside_source(path):
    abs_path = os.path.abspath(path)
    return abs_path.startswith(os.path.abspath(SOURCE_DIR))

@app.route('/api/source/delete', methods=['POST'])
@login_required
def source_delete():
    data = request.json
    paths = data.get('paths', [])
    if not paths: return jsonify({"error": "No paths provided"}), 400
    deleted = 0
    errors = []
    for p in paths:
        target = resolve_source_path(p)
        if not _safe_inside_source(target):
            errors.append(f"Invalid path: {p}"); continue
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            deleted += 1
        except Exception as e:
            errors.append(f"{p}: {e}")
    return jsonify({"success": len(errors) == 0, "deleted": deleted, "errors": errors})

@app.route('/api/source/rename', methods=['POST'])
@login_required
def source_rename():
    data = request.json
    path = data.get('path', '')
    new_name = data.get('new_name', '').strip()
    if not path or not new_name: return jsonify({"error": "Path and new name required"}), 400
    target = resolve_source_path(path)
    if not _safe_inside_source(target): return jsonify({"error": "Invalid path"}), 400
    new_target = os.path.join(os.path.dirname(target), new_name)
    if not _safe_inside_source(new_target): return jsonify({"error": "Invalid new name"}), 400
    try:
        os.rename(target, new_target)
        return jsonify({"success": True, "new_path": os.path.relpath(new_target, SOURCE_DIR).replace('\\', '/')})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/source/copy_move', methods=['POST'])
@login_required
def source_copy_move():
    data = request.json
    paths = data.get('paths', [])
    destination = data.get('destination', '')
    mode = data.get('mode', 'copy')
    if not paths: return jsonify({"error": "No paths provided"}), 400
    dest_dir = resolve_source_path(destination)
    if not _safe_inside_source(dest_dir): return jsonify({"error": "Invalid destination"}), 400
    os.makedirs(dest_dir, exist_ok=True)
    errors = []
    for p in paths:
        target = resolve_source_path(p)
        if not _safe_inside_source(target): errors.append(f"Invalid path: {p}"); continue
        dest_path = os.path.join(dest_dir, os.path.basename(target))
        try:
            if mode == 'move':
                shutil.move(target, dest_path)
            else:
                if os.path.isdir(target):
                    shutil.copytree(target, dest_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(target, dest_path)
        except Exception as e:
            errors.append(f"{p}: {e}")
    return jsonify({"success": len(errors) == 0, "errors": errors})

@app.route('/api/source/zip', methods=['POST'])
@login_required
def source_zip():
    data = request.json
    paths = data.get('paths', [])
    name = data.get('name', 'archive').strip() or 'archive'
    base_path = data.get('base_path', '')
    if not paths: return jsonify({"error": "No paths provided"}), 400
    base_dir = resolve_source_path(base_path)
    if not _safe_inside_source(base_dir): return jsonify({"error": "Invalid base path"}), 400
    archive_name = name if name.endswith('.zip') else name + '.zip'
    archive_path = os.path.join(base_dir, archive_name)
    try:
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                target = resolve_source_path(p)
                if not _safe_inside_source(target): continue
                if os.path.isdir(target):
                    for root, dirs, files in os.walk(target):
                        for f in files:
                            fp = os.path.join(root, f)
                            arcname = os.path.relpath(fp, SOURCE_DIR).replace('\\', '/')
                            zf.write(fp, arcname)
                else:
                    arcname = os.path.relpath(target, SOURCE_DIR).replace('\\', '/')
                    zf.write(target, arcname)
        rel = os.path.relpath(archive_path, SOURCE_DIR).replace('\\', '/')
        return jsonify({"success": True, "archive": rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/source/unzip', methods=['POST'])
@login_required
def source_unzip():
    data = request.json
    path = data.get('path', '')
    destination = data.get('destination', '')
    if not path: return jsonify({"error": "No path provided"}), 400
    target = resolve_source_path(path)
    if not _safe_inside_source(target) or not target.lower().endswith('.zip'):
        return jsonify({"error": "Invalid zip file"}), 400
    dest_dir = resolve_source_path(destination)
    if not _safe_inside_source(dest_dir): return jsonify({"error": "Invalid destination"}), 400
    try:
        with zipfile.ZipFile(target, 'r') as zf:
            zf.extractall(dest_dir)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/source/download_zip', methods=['POST'])
@login_required
def source_download_zip():
    data = request.json
    paths = data.get('paths', [])
    if not paths: return jsonify({"error": "No paths provided"}), 400
    tmp_name = f"download_{uuid.uuid4().hex[:12]}.zip"
    tmp_path = os.path.join(SOURCE_DIR, tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                target = resolve_source_path(p)
                if not _safe_inside_source(target): continue
                if os.path.isdir(target):
                    for root, dirs, files in os.walk(target):
                        for f in files:
                            fp = os.path.join(root, f)
                            arcname = os.path.basename(target) + '/' + os.path.relpath(fp, target).replace('\\', '/')
                            zf.write(fp, arcname)
                else:
                    zf.write(target, os.path.basename(target))
        rel = os.path.relpath(tmp_path, SOURCE_DIR).replace('\\', '/')
        return jsonify({"success": True, "archive": rel})
    except Exception as e:
        if os.path.exists(tmp_path): os.remove(tmp_path)
        return jsonify({"error": str(e)}), 500


# --- IMAGE TOOLS ---

def _image_tool_queue(paths, tool, params=None):
    import sqlite3
    conn = sqlite3.connect(SQLITE_DB)
    try:
        task_ids = []
        cursor = conn.cursor()
        params_json = json.dumps(params or {})
        for p in paths:
            target = resolve_source_path(p)
            if not os.path.exists(target): continue
            task_id = f"img_{tool}_{uuid.uuid4().hex[:12]}"
            cursor.execute(
                "INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                (task_id, f'image_{tool}', 'pending', target, params_json)
            )
            task_ids.append(task_id)
        conn.commit()
        return task_ids
    finally:
        conn.close()

@app.route('/api/source/image_tool', methods=['POST'])
@login_required
def source_image_tool():
    data = request.json
    paths = data.get('paths', [])
    tool = data.get('tool', '')
    params = data.get('params', {})
    scope = data.get('scope', 'selected')  # selected | folder
    if tool not in ['crop', 'optimize', 'deskew', 'colorize', 'autofix', 'rotate', 'smart_crop', 'smart_rotate']:
        return jsonify({"error": "Invalid tool"}), 400

    try:
        # If no explicit selection, default to all images in current folder
        if scope == 'folder' or not paths:
            path = data.get('path', '')
            target_dir = resolve_source_path(path)
            if not os.path.isdir(target_dir):
                return jsonify({"error": "Folder not found"}), 400
            paths = []
            for root, dirs, files in os.walk(target_dir):
                for f in files:
                    if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
                        full = os.path.join(root, f)
                        paths.append(os.path.relpath(full, SOURCE_DIR).replace('\\', '/'))
            if not paths:
                return jsonify({"error": "No images in folder"}), 400
        else:
            paths = [p for p in paths if p.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'))]
            if not paths:
                return jsonify({"error": "Select image files"}), 400

        # Map smart tools to base tool; pass smart flag in params
        base_tool = tool
        if tool == 'smart_crop':
            base_tool = 'crop'
            params = params or {}
            params['mode'] = 'smart'
        elif tool == 'smart_rotate':
            base_tool = 'rotate'
            params = params or {}
            params['mode'] = 'smart'

        task_ids = _image_tool_queue(paths, base_tool, params)
        return jsonify({"success": True, "task_ids": task_ids, "image_count": len(paths)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _queue_pipeline_tasks(image_paths, run_ocr=True, run_meta=True, run_mrc=True):
    """Queue OCR, metadata, and MRC tasks for a list of image paths."""
    import sqlite3
    conn = sqlite3.connect(SQLITE_DB)
    try:
        cursor = conn.cursor()
        queued = {'ocr': [], 'meta': [], 'mrc': []}
        ocr_targets = []
        for p in image_paths:
            target = resolve_source_path(p)
            if not os.path.exists(target) or not target.lower().endswith(('.jpg', '.jpeg', '.png')):
                continue
            ocr_targets.append(target)

        if run_ocr and ocr_targets:
            for target in ocr_targets:
                task_id = f"ocr_{uuid.uuid4().hex[:12]}"
                cursor.execute(
                    "INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                    (task_id, 'source_ocr', 'pending', target, '')
                )
                queued['ocr'].append(task_id)

        if run_meta and ocr_targets:
            for target in ocr_targets:
                txt_path = target + '.ocr.txt'
                task_id = f"meta_{uuid.uuid4().hex[:12]}"
                cursor.execute(
                    "INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                    (task_id, 'source_meta', 'pending', txt_path if os.path.exists(txt_path) else target, '')
                )
                queued['meta'].append(task_id)

        if run_mrc:
            # Look for existing JSON files paired with these images/folders
            json_candidates = set()
            for p in image_paths:
                base = os.path.splitext(resolve_source_path(p))[0]
                for ext in ['.json']:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        json_candidates.add(candidate)
                folder = os.path.dirname(resolve_source_path(p))
                for f in os.listdir(folder) if os.path.isdir(folder) else []:
                    if f.lower().endswith('.json'):
                        json_candidates.add(os.path.join(folder, f))
            for jpath in json_candidates:
                task_id = f"mrc_{uuid.uuid4().hex[:12]}"
                cursor.execute(
                    "INSERT INTO ai_task_queue (task_id, type, status, images, result_data) VALUES (?, ?, ?, ?, ?)",
                    (task_id, 'source_mrc', 'pending', jpath, '')
                )
                queued['mrc'].append(task_id)

        conn.commit()
        return queued
    finally:
        conn.close()


@app.route('/api/source/organize', methods=['POST'])
@login_required
def source_organize():
    """
    Merge duplicate folders ending with _output into their base folder and move
    loose files into matching folders (e.g. '1234 p1' or '1234 p1_output').
    """
    import re, shutil
    merged = []
    moved = []
    errors = []

    try:
        for item in os.listdir(SOURCE_DIR):
            item_path = os.path.join(SOURCE_DIR, item)
            if not os.path.isdir(item_path):
                continue
            m = re.match(r'^(.+)\s*_output$', item, flags=re.IGNORECASE)
            if not m:
                continue
            base_name = m.group(1).strip()
            base_path = os.path.join(SOURCE_DIR, base_name)
            if not os.path.isdir(base_path):
                # No base folder; rename _output folder to base name
                try:
                    os.rename(item_path, base_path)
                    merged.append(f"{item} -> {base_name}")
                except Exception as e:
                    errors.append(f"rename {item}: {e}")
                continue
            # Merge contents from _output into base
            try:
                for root, dirs, files in os.walk(item_path):
                    rel = os.path.relpath(root, item_path)
                    dest_dir = base_path if rel == '.' else os.path.join(base_path, rel)
                    os.makedirs(dest_dir, exist_ok=True)
                    for f in files:
                        src = os.path.join(root, f)
                        dst = os.path.join(dest_dir, f)
                        # Avoid overwrite collisions by appending a suffix
                        if os.path.exists(dst):
                            stem, ext = os.path.splitext(f)
                            dst = os.path.join(dest_dir, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
                        shutil.move(src, dst)
                    for d in dirs:
                        os.makedirs(os.path.join(dest_dir, d), exist_ok=True)
                # Remove empty _output tree
                shutil.rmtree(item_path)
                merged.append(f"{item} into {base_name}")
            except Exception as e:
                errors.append(f"merge {item}: {e}")

        # Second pass: move loose files into matching folders
        folder_names = [n for n in os.listdir(SOURCE_DIR) if os.path.isdir(os.path.join(SOURCE_DIR, n))]
        for item in os.listdir(SOURCE_DIR):
            item_path = os.path.join(SOURCE_DIR, item)
            if os.path.isdir(item_path):
                continue
            # Find best matching folder by longest name prefix
            best = None
            for fname in folder_names:
                clean = re.sub(r'\s*_output$', '', fname, flags=re.IGNORECASE).strip()
                if item.startswith(clean) or item.lower().startswith(clean.lower()):
                    if best is None or len(clean) > len(best[0]):
                        best = (clean, fname)
            if best:
                dest_dir = os.path.join(SOURCE_DIR, best[1])
                try:
                    dst = os.path.join(dest_dir, item)
                    if os.path.exists(dst):
                        stem, ext = os.path.splitext(item)
                        dst = os.path.join(dest_dir, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
                    shutil.move(item_path, dst)
                    moved.append(f"{item} -> {best[1]}")
                except Exception as e:
                    errors.append(f"move {item}: {e}")

        # Third pass: recursively remove empty folders
        removed_empty = []
        for root, dirs, files in os.walk(SOURCE_DIR, topdown=False):
            if root == SOURCE_DIR:
                continue
            try:
                remaining = os.listdir(root)
            except Exception:
                continue
            if not remaining:
                try:
                    os.rmdir(root)
                    removed_empty.append(os.path.relpath(root, SOURCE_DIR).replace('\\', '/'))
                except Exception as e:
                    errors.append(f"remove empty {os.path.relpath(root, SOURCE_DIR)}: {e}")

        return jsonify({"success": True, "merged": merged, "moved": moved, "removed_empty": removed_empty, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/source/remove_empty_folders', methods=['POST'])
@login_required
def source_remove_empty_folders():
    """Recursively remove empty folders under SOURCE_DIR."""
    removed = []
    errors = []
    try:
        # Walk bottom-up so nested empty folders are removed first
        for root, dirs, files in os.walk(SOURCE_DIR, topdown=False):
            if root == SOURCE_DIR:
                continue
            # Only remove if directory itself is now empty (no files, no remaining subdirs)
            try:
                remaining = os.listdir(root)
            except Exception:
                continue
            if not remaining:
                try:
                    os.rmdir(root)
                    rel = os.path.relpath(root, SOURCE_DIR).replace('\\', '/')
                    removed.append(rel)
                except Exception as e:
                    rel = os.path.relpath(root, SOURCE_DIR).replace('\\', '/')
                    errors.append(f"{rel}: {e}")
        return jsonify({"success": True, "removed": removed, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/source/normalize', methods=['POST'])
@login_required
def source_normalize():
    """
    Normalize every source folder so it contains only images + canonical catalog files.
    - Flatten subdirectories
    - Merge .txt files into {folder}.ocr.txt
    - Consolidate .mrc files to koha_import.mrc
    - Consolidate .mrk files to {folder}.mrk
    - Remove empty subdirectories
    """
    import re
    flattened = []
    merged_txt = []
    consolidated_mrc = []
    consolidated_mrk = []
    removed_empty = []
    errors = []

    try:
        for item in os.listdir(SOURCE_DIR):
            folder_path = os.path.join(SOURCE_DIR, item)
            if not os.path.isdir(folder_path):
                continue
            folder_name = os.path.basename(folder_path)
            safe_name = re.sub(r'[^\w\s-]', '_', folder_name).strip()

            # 1) Flatten subdirectories
            for root, dirs, files in os.walk(folder_path, topdown=False):
                if root == folder_path:
                    continue
                for f in files:
                    src = os.path.join(root, f)
                    dst = os.path.join(folder_path, f)
                    if os.path.exists(dst):
                        stem, ext = os.path.splitext(f)
                        dst = os.path.join(folder_path, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
                    try:
                        shutil.move(src, dst)
                        flattened.append(f"{os.path.relpath(src, SOURCE_DIR)} -> {os.path.relpath(dst, SOURCE_DIR)}")
                    except Exception as e:
                        errors.append(f"move {src}: {e}")
                # Try to remove now-empty subdir
                try:
                    os.rmdir(root)
                except Exception:
                    pass

            # 2) Merge .txt files into {folder}.ocr.txt
            txt_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.txt') and os.path.isfile(os.path.join(folder_path, f))]
            if len(txt_files) > 1 or (txt_files and txt_files[0].lower() != f"{safe_name.lower()}.ocr.txt"):
                canonical_txt = f"{folder_name}.ocr.txt"
                canonical_path = os.path.join(folder_path, canonical_txt)
                merged_content = []
                for tf in sorted(txt_files):
                    try:
                        with open(os.path.join(folder_path, tf), 'r', encoding='utf-8', errors='ignore') as fh:
                            merged_content.append(f"--- {tf} ---\n")
                            merged_content.append(fh.read())
                            merged_content.append("\n")
                    except Exception as e:
                        errors.append(f"read txt {tf}: {e}")
                try:
                    with open(canonical_path, 'w', encoding='utf-8') as fh:
                        fh.write("".join(merged_content))
                    for tf in txt_files:
                        if tf != canonical_txt:
                            try:
                                os.remove(os.path.join(folder_path, tf))
                            except Exception as e:
                                errors.append(f"remove txt {tf}: {e}")
                    merged_txt.append(f"{folder_name}: {len(txt_files)} txt files -> {canonical_txt}")
                except Exception as e:
                    errors.append(f"merge txt in {folder_name}: {e}")

            # 3) Consolidate .mrc files -> keep koha_import.mrc, remove others
            mrc_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.mrc') and os.path.isfile(os.path.join(folder_path, f))]
            if len(mrc_files) > 1:
                keep = None
                for mf in mrc_files:
                    if mf.lower() == 'koha_import.mrc':
                        keep = mf
                        break
                if not keep:
                    keep = mrc_files[0]
                for mf in mrc_files:
                    if mf != keep:
                        try:
                            os.remove(os.path.join(folder_path, mf))
                            consolidated_mrc.append(f"{folder_name}: removed {mf}")
                        except Exception as e:
                            errors.append(f"remove mrc {mf}: {e}")

            # 4) Consolidate .mrk files -> keep {folder}.mrk, remove others
            mrk_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.mrk') and os.path.isfile(os.path.join(folder_path, f))]
            if len(mrk_files) > 1:
                canonical_mrk = f"{folder_name}.mrk"
                keep = canonical_mrk if canonical_mrk in mrk_files else mrk_files[0]
                for mf in mrk_files:
                    if mf != keep:
                        try:
                            os.remove(os.path.join(folder_path, mf))
                            consolidated_mrk.append(f"{folder_name}: removed {mf}")
                        except Exception as e:
                            errors.append(f"remove mrk {mf}: {e}")

            # 5) Remove any remaining empty subdirectories
            for root, dirs, files in os.walk(folder_path, topdown=False):
                if root == folder_path:
                    continue
                try:
                    os.rmdir(root)
                    removed_empty.append(os.path.relpath(root, SOURCE_DIR).replace('\\', '/'))
                except Exception:
                    pass

        return jsonify({
            "success": True,
            "flattened": flattened,
            "merged_txt": merged_txt,
            "consolidated_mrc": consolidated_mrc,
            "consolidated_mrk": consolidated_mrk,
            "removed_empty": removed_empty,
            "errors": errors
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/source/stats', methods=['GET'])
@login_required
def source_stats():
    """Return aggregate counts for source folders, images, and file types."""
    stats = {
        "folders": 0,
        "images": 0,
        "original_images": 0,
        "ocr_ready": 0,
        "metadata_ready": 0,
        "mrc_ready": 0,
        "koha_import_ready": 0,
        "file_types": {},
        "top_folders": []
    }
    try:
        folder_list = []
        for item in os.listdir(SOURCE_DIR):
            item_path = os.path.join(SOURCE_DIR, item)
            if not os.path.isdir(item_path):
                continue
            stats["folders"] += 1
            status = _folder_catalog_status(item_path)
            stats["images"] += status.get("image_count", 0)
            if status.get("has_originals"):
                stats["original_images"] += 1
            if status.get("has_ocr"):
                stats["ocr_ready"] += 1
            if status.get("has_metadata"):
                stats["metadata_ready"] += 1
            if status.get("has_mrc"):
                stats["mrc_ready"] += 1
            if status.get("has_koha_import"):
                stats["koha_import_ready"] += 1
            folder_list.append({"name": item, "status": status})

        # File type counts
        for root, dirs, files in os.walk(SOURCE_DIR):
            for f in files:
                ext = f.split('.')[-1].lower() if '.' in f else 'no_ext'
                stats["file_types"][ext] = stats["file_types"].get(ext, 0) + 1

        stats["top_folders"] = folder_list[:20]
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/source/batch_process', methods=['POST'])
@login_required
def source_batch_process():
    data = request.json
    scope = data.get('scope', 'folder')
    path = data.get('path', '')
    run_ocr = data.get('ocr', True)
    run_meta = data.get('meta', True)
    run_mrc = data.get('mrc', True)

    image_paths = []
    try:
        if scope == 'folder':
            target_dir = resolve_source_path(path)
            if not os.path.isdir(target_dir):
                return jsonify({"error": "Folder not found"}), 400
            for root, dirs, files in os.walk(target_dir):
                for f in files:
                    if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                        full = os.path.join(root, f)
                        image_paths.append(os.path.relpath(full, SOURCE_DIR).replace('\\', '/'))
        else:
            for item in os.listdir(SOURCE_DIR):
                item_path = os.path.join(SOURCE_DIR, item)
                if not os.path.isdir(item_path):
                    continue
                for root, dirs, files in os.walk(item_path):
                    for f in files:
                        if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                            full = os.path.join(root, f)
                            image_paths.append(os.path.relpath(full, SOURCE_DIR).replace('\\', '/'))

        if not image_paths:
            return jsonify({"error": "No images found"}), 400

        # Queue auto image fixes first: crop, deskew, rotate, autofix, optimize
        fix_tools = ['crop', 'deskew', 'rotate', 'autofix', 'optimize']
        for tool in fix_tools:
            _image_tool_queue(image_paths, tool)

        # Queue pipeline (OCR, meta, MRC)
        queued = _queue_pipeline_tasks(image_paths, run_ocr, run_meta, run_mrc)

        report = []
        if image_paths:
            report.append(f"{len(image_paths)} images queued for auto-fix")
        if queued.get('ocr'):
            report.append(f"{len(queued['ocr'])} OCR tasks")
        if queued.get('meta'):
            report.append(f"{len(queued['meta'])} meta tasks")
        if queued.get('mrc'):
            report.append(f"{len(queued['mrc'])} MRC tasks")
        return jsonify({"success": True, "report": report, "image_count": len(image_paths)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- END SOURCE EXPLORER ---


@app.route('/api/upload', methods=['POST'])
@login_required
def upload_image():
    if 'image' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    filename = str(uuid.uuid4()) + "_" + file.filename.replace('/', '_')
    filepath = os.path.join(IMAGE_DIR, filename)
    file.save(filepath)
    return jsonify({"success": True, "filename": filename})

@app.route('/images/<filename>')
@login_required
def get_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

# --- DISTRIBUTED AI QUEUE ---
@app.route('/api/process_ai', methods=['POST'])
@login_required
def trigger_ai():
    data = request.json
    images = data.get('images', [])
    barcode = data.get('barcode', '')
    item_count = data.get('item_count', '')
    import json
    
    task_id = "UP_" + str(uuid.uuid4())
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_c = sq_conn.cursor()
    sq_c.execute("INSERT INTO ai_task_queue (task_id, type, status, title, images) VALUES (?, ?, ?, ?, ?)", 
                 (task_id, 'upload', 'pending', barcode, json.dumps(images)))
    sq_conn.commit()
    sq_conn.close()
    
    return jsonify({"task_id": task_id})

@app.route('/api/queue/status', methods=['GET'])
@login_required
def queue_status():
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_conn.row_factory = sqlite3.Row
    sq_c = sq_conn.cursor()
    sq_c.execute("SELECT * FROM ai_task_queue ORDER BY created_at DESC LIMIT 100")
    rows = sq_c.fetchall()
    sq_conn.close()
    
    import json
    queue = {}
    for r in rows:
        result_data = json.loads(r['result_data']) if r['result_data'] else None
        imgs = json.loads(r['images']) if r['images'] else []
        queue[r['task_id']] = {
            "status": r['status'],
            "type": r['type'],
            "biblionumber": r['biblionumber'],
            "title": r['title'],
            "author": r['author'],
            "images": imgs,
            "barcode": r['title'], 
            "result": result_data
        }
    return jsonify({"queue": queue})

@app.route('/api/queue/requeue/<task_id>', methods=['POST'])
@login_required
def requeue_task(task_id):
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_c = sq_conn.cursor()
    sq_c.execute("UPDATE ai_task_queue SET status = 'pending', result_data = NULL WHERE task_id = ?", (task_id,))
    sq_conn.commit()
    sq_conn.close()
    return jsonify({"success": True})


@app.route('/api/queue/list', methods=['GET'])
@login_required
def get_queue_list():
    page = int(request.args.get('page', 1))
    limit = 50
    offset = (page - 1) * limit
    status = request.args.get('status', 'all')
    q_bib = request.args.get('q_bib', '').strip()
    
    import sqlite3, datetime
    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_conn.row_factory = sqlite3.Row
    sq_c = sq_conn.cursor()
    
    query = "SELECT * FROM ai_task_queue WHERE 1=1"
    params = []
    
    if status != 'all':
        query += " AND status = ?"
        params.append(status)
        
    if q_bib:
        query += " AND biblionumber LIKE ?"
        params.append(f"%{q_bib}%")
        
    # Count total
    sq_c.execute(f"SELECT COUNT(*) as c FROM ({query})", params)
    total = sq_c.fetchone()['c']
    
    # Get paginated
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    sq_c.execute(query, params)
    rows = sq_c.fetchall()
    sq_conn.close()
    
    data = []
    for r in rows:
        try:
            config = r['task_config']
        except:
            config = '{}'
        try:
            result = r['result_data']
        except:
            result = '{}'
            
        data.append({
            "task_id": r['task_id'],
            "type": r['type'],
            "status": r['status'],
            "biblionumber": r['biblionumber'],
            "created_at": r['created_at'],
            "task_config": config,
            "result_data": result
        })
        
    return jsonify({
        "success": True,
        "data": data,
        "total": total,
        "page": page,
        "limit": limit
    })



@app.route('/api/import/parse_excel', methods=['POST'])
@login_required
def parse_excel_import():
    import io
    file = request.files.get('file')
    if not file:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    try:
        name = file.filename.lower()
        content = file.read()
        if name.endswith('.csv'):
            text = content.decode('utf-8', errors='replace')
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) < 2:
                return jsonify({"success": True, "rows": []})
            headers = [h.strip().strip('"') for h in lines[0].split(',')]
            rows = []
            for line in lines[1:]:
                vals = [v.strip().strip('"') for v in line.split(',')]
                rows.append({headers[i]: vals[i] if i < len(vals) else '' for i in range(len(headers))})
            return jsonify({"success": True, "rows": rows})
        try:
            import pandas as pd
            df = pd.read_excel(io.BytesIO(content))
            rows = df.head(1000).fillna('').astype(str).to_dict(orient='records')
            return jsonify({"success": True, "rows": rows})
        except Exception as e:
            return jsonify({"success": False, "error": f"Excel parse failed: {e}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/import/process', methods=['POST'])
@login_required
def process_import_rows():
    import uuid, sqlite3, json
    data = request.get_json() or {}
    rows = data.get('rows', [])
    ai_model = data.get('ai_model', 'auto')
    strategy = data.get('strategy', 'enrich')
    if not rows:
        return jsonify({"success": False, "error": "No rows provided"}), 400

    sq_conn = sqlite3.connect(SQLITE_DB)
    sq_c = sq_conn.cursor()
    queued = 0
    for row in rows[:1000]:  # cap for safety
        task_id = str(uuid.uuid4())
        sq_c.execute('SELECT COALESCE(MAX(display_id), 0) + 1 FROM ai_task_queue')
        next_display_id = sq_c.fetchone()[0]
        sq_c.execute("""
            INSERT INTO ai_task_queue (task_id, display_id, type, status, biblionumber, title, author, images, task_config)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, next_display_id, 'source_meta', 'pending', None,
            str(row.get('title', '')), str(row.get('author', '')), '[]',
            json.dumps({"ai_model": ai_model, "strategy": strategy, "row_data": row})
        ))
        queued += 1
    sq_conn.commit()
    sq_conn.close()
    return jsonify({"success": True, "queued": queued})


# --- BACKGROUND AI WORKER ---

def _check_stalled_tasks(timeout_seconds=300):
    """Mark long-processing tasks as error so they can be retried or requeued.
    Uses started_at (set when a worker claims the task), falling back to created_at."""
    import datetime
    try:
        conn = sqlite3.connect(SQLITE_DB)
        c = conn.cursor()
        since = (datetime.datetime.now() - datetime.timedelta(seconds=timeout_seconds)).isoformat()
        c.execute("""
            UPDATE ai_task_queue SET status = 'error', result_data = ?
            WHERE status = 'processing'
              AND COALESCE(started_at, created_at) < ?
        """, (json.dumps({"error": "Task timed out while processing"}), since))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Stalled task check failed: {e}")


# In-memory queue for tasks already claimed from SQLite. A single claim thread
# feeds this queue, and multiple AI worker threads consume from it. This avoids
# concurrent SQLite UPDATE...RETURNING races under WAL mode while still allowing
# parallel AI/Ollama requests.
import queue
_task_queue = queue.Queue()


def task_claim_loop():
    """Single thread that atomically claims pending tasks and hands them to AI workers."""
    while True:
        try:
            _check_stalled_tasks(timeout_seconds=300)

            sq_conn = sqlite3.connect(SQLITE_DB, timeout=30, isolation_level='IMMEDIATE')
            sq_conn.row_factory = sqlite3.Row
            sq_c = sq_conn.cursor()
            try:
                sq_c.execute("""
                    UPDATE ai_task_queue SET status = 'processing', started_at = CURRENT_TIMESTAMP
                    WHERE task_id = (
                        SELECT task_id FROM ai_task_queue
                        WHERE status = 'pending'
                        ORDER BY
                            CASE type
                                WHEN 'db_fix' THEN 1
                                WHEN 'source_stage' THEN 2
                                WHEN 'source_mrc' THEN 3
                                WHEN 'source_meta' THEN 4
                                WHEN 'source_ocr' THEN 5
                                WHEN 'image_autofix' THEN 6
                                WHEN 'image_crop' THEN 7
                                WHEN 'image_deskew' THEN 8
                                WHEN 'image_optimize' THEN 9
                                WHEN 'image_rotate' THEN 10
                                ELSE 11
                            END,
                            created_at ASC
                        LIMIT 1
                    )
                    RETURNING *
                """)
                task = sq_c.fetchone()
                sq_conn.commit()
            except Exception as claim_err:
                logging.warning(f"Task claim failed: {claim_err}")
                task = None
            finally:
                sq_conn.close()

            if task:
                _append_task_log(task['task_id'], f"Claimed by dispatcher, type={task['type']}, biblionumber={task['biblionumber']}")
                _task_queue.put(dict(task))
            else:
                time.sleep(1)
        except Exception as ex:
            logging.error(f"Task claim loop error: {ex}")
            time.sleep(3)


def ai_worker_loop():
    import json
    while True:
        task = None
        try:
            task = _task_queue.get(timeout=5)
        except queue.Empty:
            time.sleep(0.5)
            continue
        except Exception as ex:
            logging.error(f"AI worker queue error: {ex}")
            time.sleep(1)
            continue

        if not task:
            continue

        task_id = task['task_id']
        try:
            def task_log(msg, level='info'):
                _append_task_log(task_id, msg, level)

            raw_text = ""
            ai_json = {}
            is_db_fix = (task['type'] == 'db_fix')
            task_log(f"Task claimed: type={task['type']}, biblionumber={task['biblionumber']}, requested_engine={task['task_config']}")

            if is_db_fix:
                biblionumber = task['biblionumber']
                conn = get_db_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT metadata FROM koha_mfa.biblio_metadata WHERE biblionumber = %s", (biblionumber,))
                        row = cursor.fetchone()
                        old_metadata = row['metadata'] if row else ""

                        cursor.execute('''SELECT b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages
                            FROM koha_mfa.biblio b
                            LEFT JOIN koha_mfa.biblioitems bi ON b.biblionumber = bi.biblionumber
                            WHERE b.biblionumber = %s''', (biblionumber,))
                        b_row = cursor.fetchone()
                        raw_text = f"Title: {b_row['title']}\\nAuthor: {b_row['author']}\\nPublisher: {b_row['publishercode']}"
                        task_log(f"Fetched biblio {biblionumber}: title={b_row['title']}, author={b_row['author']}, publisher={b_row['publishercode']}, year={b_row['publicationyear']}")
                finally:
                    conn.close()

            else:
                images = json.loads(task['images']) if task['images'] else []
                if images:
                    img_name = images[0]
                    img_path = os.path.join(IMAGE_DIR, img_name)
                    task_log(f"Processing image {img_name}")
                    with open(img_path, 'rb') as f:
                        encoded_string = base64.b64encode(f.read()).decode('utf-8')
                    ocr_payload = {'apikey': 'helloworld', 'base64Image': 'data:image/jpeg;base64,' + encoded_string, 'language': 'eng'}
                    try:
                        ocr_res = requests.post('https://api.ocr.space/parse/image', data=ocr_payload, timeout=15)
                        ocr_data = ocr_res.json()
                        if not ocr_data.get('IsErroredOnProcessing'):
                            for parsed in ocr_data.get('ParsedResults', []):
                                raw_text += parsed.get('ParsedText', '') + " "
                    except Exception as e:
                        raw_text = "FAILED TO EXTRACT TEXT: " + str(e)
                if not raw_text.strip():
                    raw_text = "UNKNOWN TEXT"

            import json
            try:
                task_config_str = task['task_config']
            except:
                task_config_str = '{}'
            try:
                task_config = json.loads(task_config_str) if task_config_str else {}
            except:
                task_config = {}

            fix_type = task_config.get('fix_type', 'full')
            ai_model = task_config.get('ai_model', 'deepseek')
            task_log(f"Prepared fix_type={fix_type}, ai_model={ai_model}, text_length={len(raw_text)}")

            if is_db_fix:
                if fix_type == 'metadata':
                    prompt = f"Fix missing basic metadata. Output JSON with keys: title, author, publishercode, publicationyear, isbn.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
                elif fix_type == 'classification':
                    prompt = f"Classify this book. Output JSON with keys: ddc, subjects.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
                elif fix_type == 'aacr2':
                    prompt = f"Apply AACR2 cataloging rules to this bibliographic record. Correct punctuation, capitalization, and field structure. Output JSON with keys: title, author, publishercode, publicationyear, isbn, ddc, subjects.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
                else:
                    prompt = f"Fix all missing catalog metadata. Output JSON with keys: title, author, publishercode, publicationyear, isbn, ddc, subjects.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
            else:
                prompt = f"Extract metadata from OCR text. Output JSON with keys: title, author, publishercode, publicationyear, isbn, ddc, subjects.\nDo not output anything else.\n\nText:\n{raw_text}"

            task_log(f"Built prompt ({len(prompt)} chars)")

            ai_warning = ""
            try:
                task_log(f"Calling AI engines (requested={ai_model})...")
                ai_text, engine_used = run_ai_with_fallback(prompt, ai_model)
                task_log(f"AI responded via engine={engine_used}")
                logging.info(f"Task {task_id} engine={engine_used} requested={ai_model}")

                ai_text = re.sub(r'<thinking>.*?<\/thinking>', '', ai_text, flags=re.DOTALL).strip()
                json_match = re.search(r'\{.*?\}', ai_text, flags=re.DOTALL)
                if json_match:
                    try:
                        ai_json = json.loads(json_match.group(0))
                        task_log(f"Parsed AI JSON: {list(ai_json.keys())}")
                    except Exception as parse_err:
                        ai_json = {"title": "JSON Parse Error", "author": "Error"}
                        task_log(f"JSON parse failed: {parse_err}", level='warning')
                else:
                    ai_json = {"title": "Extraction Failed", "author": "Failed"}
                    task_log("No JSON object found in AI response", level='warning')
            except Exception as engine_err:
                engine_used = 'heuristic'
                ai_warning = str(engine_err)
                task_log(f"AI engines failed: {engine_err}", level='error')
                logging.error(f"Task {task_id} AI engines unavailable, using heuristic fallback: {engine_err}")
                ai_json = {
                    "title": (b_row['title'] if is_db_fix and b_row else task['title']) or "",
                    "author": (b_row['author'] if is_db_fix and b_row else task['author']) or "",
                    "publishercode": (b_row['publishercode'] if is_db_fix and b_row else "") or "",
                    "publicationyear": (b_row['publicationyear'] if is_db_fix and b_row else "") or "",
                    "isbn": "",
                    "ddc": "",
                    "subjects": ""
                }
            final_ddc = ai_json.get("ddc", "")
            final_subjects = ai_json.get("subjects", "")
            isbn_to_check = ai_json.get('isbn', '')

            if not is_db_fix:
                isbn_match = re.search(r'(?:ISBN(?:-1[03])?:? )?(?=[0-9X]{10}$|(?=(?:[0-9]+[- ]){3})[- 0-9X]{13}$|97[89][0-9]{10}$|(?=(?:[0-9]+[- ]){4})[- 0-9]{17}$)(?:97[89][- ]?)?[0-9]{1,5}[- ]?[0-9]+[- ]?[0-9]+[- ]?[0-9X]', raw_text, re.IGNORECASE)
                if not isbn_to_check and isbn_match:
                    isbn_to_check = re.sub(r'[^0-9X]', '', isbn_match.group(0).upper())

            if isbn_to_check:
                task_log(f"Looking up ISBN {isbn_to_check} on OpenLibrary")
                try:
                    ol_res = requests.get(f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn_to_check}&jscmd=data&format=json", timeout=10)
                    ol_data = ol_res.json()
                    book_key = f"ISBN:{isbn_to_check}"
                    if book_key in ol_data:
                        book_info = ol_data[book_key]
                        if "classifications" in book_info and "dewey_decimal_class" in book_info["classifications"]:
                            final_ddc = book_info["classifications"]["dewey_decimal_class"][0]
                        if "subjects" in book_info:
                            final_subjects = ", ".join([s['name'] for s in book_info["subjects"]][:3])
                        task_log(f"OpenLibrary enrichment: ddc={final_ddc}, subjects={final_subjects[:80]}")
                    else:
                        task_log(f"OpenLibrary returned no data for ISBN {isbn_to_check}")
                except Exception as ol_err:
                    task_log(f"OpenLibrary lookup failed: {ol_err}", level='warning')

            # If every engine failed, don't treat the heuristic placeholder as a real fix.
            # Mark the task error so it can be retried once an AI engine is available.
            if engine_used == 'heuristic':
                raise RuntimeError(f"AI unavailable; heuristic fallback not saved. {ai_warning}")

            result_data = {
                "title": ai_json.get("title", ""),
                "author": ai_json.get("author", ""),
                "publishercode": ai_json.get("publishercode", ""),
                "publicationyear": ai_json.get("publicationyear", ""),
                "isbn": isbn_to_check,
                "ddc": final_ddc,
                "subjects": final_subjects,
                "engine": engine_used,
                "engine_warning": ai_warning,
                "raw_ocr": raw_text[:500] + "..."
            }
            task_log(f"AI result preview: title={result_data['title']}, author={result_data['author']}, year={result_data['publicationyear']}, ddc={result_data['ddc']}")

            if is_db_fix:
                conn = get_db_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("INSERT INTO koha_mfa.biblio_history (biblionumber, old_metadata) VALUES (%s, %s)", (biblionumber, old_metadata))
                        new_title = result_data['title'] or b_row['title']
                        new_author = result_data['author'] or b_row['author']
                        new_pub = result_data['publishercode'] or b_row['publishercode']
                        new_year = result_data['publicationyear'] or b_row['publicationyear']
                        cursor.execute("UPDATE koha_mfa.biblio SET title = %s, author = %s WHERE biblionumber = %s", (new_title, new_author, biblionumber))
                        cursor.execute("UPDATE koha_mfa.biblioitems SET publishercode = %s, publicationyear = %s WHERE biblionumber = %s", (new_pub, new_year, biblionumber))
                        conn.commit()
                        task_log(f"Updated Koha DB: title={new_title}, author={new_author}, publisher={new_pub}, year={new_year}")
                except Exception as db_err:
                    task_log(f"Koha DB update failed: {db_err}", level='error')
                    logging.error(f"Failed DB auto-fix: {db_err}")
                finally:
                    conn.close()

            # Update SQLite
            sq_conn2 = sqlite3.connect(SQLITE_DB)
            sq_c2 = sq_conn2.cursor()
            sq_c2.execute("UPDATE ai_task_queue SET status = 'completed', result_data = ?, completed_at = CURRENT_TIMESTAMP WHERE task_id = ?", (json.dumps(result_data), task_id))
            sq_conn2.commit()
            sq_conn2.close()
            task_log("Task completed successfully")
            logging.info(f"Task {task_id} completed.")

        except Exception as e:
            task_log(f"Task failed: {e}", level='error')
            sq_conn3 = sqlite3.connect(SQLITE_DB)
            sq_c3 = sq_conn3.cursor()
            sq_c3.execute("UPDATE ai_task_queue SET status = 'error', result_data = ? WHERE task_id = ?", (json.dumps({"error": str(e)}), task_id))
            sq_conn3.commit()
            sq_conn3.close()
            logging.error(f"Task {task_id} failed: {e}")

def _ensure_original(input_path):
    """Preserve original image as _original.<ext> before any destructive edit."""
    import os, shutil
    base, ext = os.path.splitext(input_path)
    original_path = f"{base}_original{ext}"
    if not os.path.exists(original_path):
        shutil.copy2(input_path, original_path)
    return original_path


def _smart_crop_bbox(img):
    """Detect content boundaries using adaptive threshold + contour logic."""
    import numpy as np
    from PIL import ImageOps
    try:
        gray = img.convert('L')
        # Increase contrast and invert so text/content is dark
        arr = np.array(ImageOps.autocontrast(gray))
        # Adaptive threshold: content darker than local mean
        from scipy.ndimage import uniform_filter
        mean = uniform_filter(arr.astype(float), size=15, mode='constant')
        binary = (arr < (mean - 10)).astype(np.uint8) * 255
        # Find bounding box of content
        rows = np.any(binary > 0, axis=1)
        cols = np.any(binary > 0, axis=0)
        if not rows.any() or not cols.any():
            return None
        top = np.argmax(rows)
        bottom = len(rows) - np.argmax(rows[::-1])
        left = np.argmax(cols)
        right = len(cols) - np.argmax(cols[::-1])
        # Add small padding
        w, h = img.size
        pad = 5
        return (max(0, left - pad), max(0, top - pad), min(w, right + pad), min(h, bottom + pad))
    except Exception:
        return None


def _detect_text_angle(img):
    """Detect skew angle of text using projection profile over fine range."""
    import numpy as np
    try:
        gray = img.convert('L')
        arr = np.array(gray)
        # Binarize
        from scipy.ndimage import uniform_filter
        mean = uniform_filter(arr.astype(float), size=15, mode='constant')
        binary = (arr < (mean - 5)).astype(np.uint8) * 255
        best_angle = 0
        best_score = 0
        for angle in np.arange(-10, 10.5, 0.5):
            rotated = np.array(gray.rotate(angle, expand=True, fillcolor=255))
            proj = rotated.sum(axis=1)
            score = np.var(proj)
            if score > best_score:
                best_score = score
                best_angle = angle
        return best_angle
    except Exception:
        return 0


def process_image_tool(input_path, tool, params=None):
    import os, shutil, json
    from PIL import Image, ImageEnhance, ImageFilter
    params = params or {}
    base, ext = os.path.splitext(input_path)
    output_path = f"{base}_{tool}{ext}"
    try:
        # Always preserve original before editing the file in-place
        _ensure_original(input_path)

        if tool == 'optimize':
            img = Image.open(input_path)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(input_path, optimize=True, quality=85)
        elif tool == 'deskew':
            # PIL has no built-in deskew; rotate by detected skew angle
            img = Image.open(input_path).convert('RGB')
            angle = _detect_skew(img)
            if abs(angle) > 0.1:
                img = img.rotate(-angle, expand=True, fillcolor=(255, 255, 255))
            img.save(input_path)
        elif tool == 'crop':
            img = Image.open(input_path)
            mode = params.get('mode', 'auto')
            if mode == 'smart':
                bbox = _smart_crop_bbox(img)
            else:
                bbox = img.convert('RGB').getbbox()
            if bbox:
                img.crop(bbox).save(input_path)
        elif tool == 'rotate':
            mode = params.get('mode', 'manual')
            if mode == 'smart':
                img = Image.open(input_path).convert('RGB')
                angle = _detect_text_angle(img)
            else:
                angle = float(params.get('angle', 90))
            img = Image.open(input_path).convert('RGB')
            if abs(angle) > 0.1:
                img = img.rotate(-angle, expand=True, fillcolor=(255, 255, 255))
            img.save(input_path)
        elif tool == 'colorize':
            img = Image.open(input_path).convert('RGB')
            enhancer = ImageEnhance.Color(img)
            img = enhancer.enhance(1.5)
            img.save(input_path)
        elif tool == 'autofix':
            img = Image.open(input_path).convert('RGB')
            img = ImageEnhance.Contrast(img).enhance(1.2)
            img = ImageEnhance.Sharpness(img).enhance(1.2)
            img.save(input_path)
        else:
            shutil.copy2(input_path, output_path)
            return {"success": True, "output": os.path.relpath(output_path, SOURCE_DIR).replace('\\', '/')}
        return {"success": True, "output": os.path.relpath(input_path, SOURCE_DIR).replace('\\', '/')}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _detect_skew(img):
    """Simple skew detection using projection profile. Returns angle in degrees."""
    import numpy as np
    try:
        gray = img.convert('L')
        arr = np.array(gray)
        best_angle = 0
        best_score = 0
        for angle in range(-15, 16):
            rotated = np.array(gray.rotate(angle, expand=True, fillcolor=255))
            proj = rotated.sum(axis=1)
            score = np.var(proj)
            if score > best_score:
                best_score = score
                best_angle = angle
        return best_angle
    except Exception:
        return 0


# Start the worker daemon
import threading

def _background_batch_retry():
    """Periodically retry pending tasks that have been waiting too long."""
    import datetime
    try:
        conn = sqlite3.connect(SQLITE_DB)
        c = conn.cursor()
        # Requeue tasks stuck in error for more than 5 minutes and originally pending for more than 10 minutes
        since_error = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat()
        c.execute("""
            UPDATE ai_task_queue SET status = 'pending', result_data = NULL
            WHERE status = 'error'
              AND created_at < ?
              AND (result_data IS NULL OR result_data NOT LIKE '%heuristic fallback not saved%')
        """, (since_error,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Batch retry check failed: {e}")


def source_worker_loop():
    import json, base64, requests, os, uuid, time

    while True:
        try:
            # Source/image tasks are now handled by ai_worker_loop with priority ordering.
            # This loop only runs periodic maintenance: stalled-task timeout and auto-retry.
            _check_stalled_tasks(timeout_seconds=180)
            _background_batch_retry()
            time.sleep(10)
            continue


            if task:
                task_id = task['task_id']
                t_type = task['type']
                target_file = task['images']

                sq_c.execute("UPDATE ai_task_queue SET status = 'processing' WHERE task_id = ?", (task_id,))
                sq_conn.commit()
                sq_conn.close()

                try:
                    result_data = {"status": "success", "file": target_file}

                    if t_type.startswith('image_'):
                        tool = t_type.replace('image_', '')
                        params = {}
                        try:
                            params = json.loads(task['result_data'] or '{}')
                        except Exception:
                            params = {}
                        result_data = process_image_tool(target_file, tool, params)
                    elif t_type == 'source_ocr':
                        with open(target_file, 'rb') as f:
                            encoded_string = base64.b64encode(f.read()).decode('utf-8')
                        ocr_payload = {'apikey': 'helloworld', 'base64Image': 'data:image/jpeg;base64,' + encoded_string, 'language': 'eng'}
                        ocr_res = requests.post('https://api.ocr.space/parse/image', data=ocr_payload, timeout=20)
                        ocr_data = ocr_res.json()
                        raw_text = ""
                        if not ocr_data.get('IsErroredOnProcessing'):
                            for parsed in ocr_data.get('ParsedResults', []):
                                raw_text += parsed.get('ParsedText', '') + "\n"
                        else:
                            raise Exception("OCR Space Error")
                            
                        ocr_file = target_file + ".ocr.txt"
                        with open(ocr_file, 'w', encoding='utf-8') as f:
                            f.write(raw_text)
                        result_data['ocr_file'] = ocr_file
                        result_data['text_preview'] = raw_text[:200]
                        
                    elif t_type == 'source_meta':
                        with open(target_file, 'r', encoding='utf-8') as f:
                            text_content = f.read()
                            
                        prompt = f"Extract metadata from OCR text. Output JSON with keys: title, author, publishercode, publicationyear, isbn, ddc, subjects, pages.\nDo not output anything else.\n\nText:\n{text_content}"

                        try:
                            ai_text, engine_used = run_ai_with_fallback(prompt, 'fallback')
                            ai_text = ai_text.replace("```json", "").replace("```", "").strip()
                            json.loads(ai_text)
                        except Exception as e:
                            ai_text = json.dumps({"title": "AI Error", "error": str(e)})
                            engine_used = 'none'
                            
                        meta_file = target_file.replace('.ocr.txt', '') + ".meta.json"
                        with open(meta_file, 'w', encoding='utf-8') as f:
                            f.write(ai_text)
                        result_data['meta_file'] = meta_file
                        result_data['engine'] = engine_used
                        
                    elif t_type == 'source_mrc':
                        import pymarc
                        with open(target_file, 'r', encoding='utf-8') as f:
                            meta = json.load(f)
                        
                        record = pymarc.Record()
                        record.add_field(pymarc.Field(tag='245', indicators=['0','0'], subfields=[pymarc.Subfield('a', meta.get('title', 'Unknown Title'))]))
                        if meta.get('author'):
                            record.add_field(pymarc.Field(tag='100', indicators=['1',' '], subfields=[pymarc.Subfield('a', meta.get('author'))]))
                        if meta.get('publishercode'):
                            record.add_field(pymarc.Field(tag='260', indicators=[' ',' '], subfields=[pymarc.Subfield('b', meta.get('publishercode'))]))
                        if meta.get('publicationyear'):
                            record.add_field(pymarc.Field(tag='260', indicators=[' ',' '], subfields=[pymarc.Subfield('c', str(meta.get('publicationyear')))]))
                        if meta.get('isbn'):
                            record.add_field(pymarc.Field(tag='020', indicators=[' ',' '], subfields=[pymarc.Subfield('a', meta.get('isbn'))]))
                            
                        mrc_file = target_file.replace('.meta.json', '').replace('.json', '') + ".mrc"
                        with open(mrc_file, 'wb') as f2:
                            f2.write(record.as_marc())
                        
                        result_data['mrc_file'] = mrc_file
                        
                    elif t_type == 'source_stage':
                        import subprocess
                        # Run Koha stage MARC command
                        cmd = f'koha-shell -c "perl /usr/share/koha/bin/stage_file.pl --file {target_file} --comment \'AI Workstation Import\'" mfa'
                        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        out, err = process.communicate()
                        
                        log_file = target_file + ".log.json"
                        with open(log_file, 'w', encoding='utf-8') as f2:
                            json.dump({
                                "command": cmd,
                                "stdout": out.decode('utf-8', errors='ignore'),
                                "stderr": err.decode('utf-8', errors='ignore'),
                                "returncode": process.returncode,
                                "timestamp": time.time()
                            }, f2)
                        
                        result_data['stage_log'] = log_file
                        result_data['returncode'] = process.returncode
                        if process.returncode != 0:
                            raise Exception(f"Stage failed. Check log: {log_file}")
                    
                    sq_conn2 = sqlite3.connect(SQLITE_DB)
                    sq_c2 = sq_conn2.cursor()
                    sq_c2.execute("UPDATE ai_task_queue SET status = 'completed', result_data = ? WHERE task_id = ?", (json.dumps(result_data), task_id))
                    sq_conn2.commit()
                    sq_conn2.close()
                    
                except Exception as e:
                    sq_conn3 = sqlite3.connect(SQLITE_DB)
                    sq_c3 = sq_conn3.cursor()
                    sq_c3.execute("UPDATE ai_task_queue SET status = 'error', result_data = ? WHERE task_id = ?", (json.dumps({"error": str(e)}), task_id))
                    sq_conn3.commit()
                    sq_conn3.close()
            else:
                sq_conn.close()
        except Exception as ex:
            logging.error(f"Source Worker Loop Error: {ex}")

        time.sleep(3)

source_thread = threading.Thread(target=source_worker_loop, daemon=True)
source_thread.start()
logging.info("Source Explorer maintenance thread started.")


# Start a single claim thread to serialize SQLite access and avoid duplicate claims.
claim_thread = threading.Thread(target=task_claim_loop, daemon=True)
claim_thread.start()
logging.info("Task claim thread started.")

# Start multiple AI worker threads to process tasks in parallel (Ollama can
# handle concurrent requests). SQLite queue access is serialized by the claim loop.
AI_WORKER_COUNT = 2
for i in range(AI_WORKER_COUNT):
    t = threading.Thread(target=ai_worker_loop, daemon=True)
    t.start()
    logging.info(f"Background AI Worker Thread {i+1}/{AI_WORKER_COUNT} started.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)

