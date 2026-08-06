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
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, send_from_directory, Blueprint

# Setup server logging
logging.basicConfig(filename='/var/www/koha_editor/app.log', level=logging.INFO, 
                    format='%(asctime)s %(levelname)s: %(message)s')

import pymysql
import sqlite3

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
    c.execute('''
        CREATE TABLE IF NOT EXISTS ai_task_queue (
            task_id TEXT PRIMARY KEY,
            type TEXT,
            status TEXT,
            biblionumber INTEGER,
            title TEXT,
            author TEXT,
            images TEXT,
            result_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_sqlite()

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
        fields = []
        for child in root:
            if child.tag == 'leader':
                fields.append({'tag': 'LDR', 'value': child.text})
            elif child.tag == 'controlfield':
                fields.append({'tag': child.attrib.get('tag', ''), 'value': child.text})
            elif child.tag == 'datafield':
                tag = child.attrib.get('tag', '')
                ind1 = child.attrib.get('ind1', ' ')
                ind2 = child.attrib.get('ind2', ' ')
                subfields = []
                for sf in child.findall('subfield'):
                    subfields.append({'code': sf.attrib.get('code', ''), 'value': sf.text})
                fields.append({'tag': tag, 'ind1': ind1, 'ind2': ind2, 'subfields': subfields})
        return fields
    except Exception as e:
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

            return jsonify({
                "total": total,
                "complete": complete,
                "incomplete": incomplete,
                "issues": issues
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
            
            with conn.cursor() as cursor:
                if barcode:
                    cursor.execute("SELECT itemnumber FROM koha_mfa.items WHERE barcode = %s AND itemnumber != %s", (barcode, itemnumber))
                    if cursor.fetchone():
                        return jsonify({"error": "Barcode already exists!"}), 400
                
                cursor.execute("""
                    UPDATE koha_mfa.items 
                    SET barcode = %s, itemcallnumber = %s 
                    WHERE itemnumber = %s
                """, (barcode, callnumber, itemnumber))
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
                sq_c.execute("INSERT INTO ai_task_queue (task_id, type, status, biblionumber, title, author, images) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                             (task_id, 'db_fix', 'pending', row['biblionumber'], row['title'], row['author'], '[]'))
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
    
    return jsonify({
        "total_biblios": total_biblios,
        "broken_biblios": broken_biblios,
        "total_items": total_items,
        "queue_count": queue_count,
        "catalogued_today": catalogued_today,
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
    
    for bib in biblionumbers:
        sq_c.execute("SELECT task_id FROM ai_task_queue WHERE type='db_fix' AND biblionumber=? AND status IN ('pending', 'processing')", (bib,))
        if not sq_c.fetchone():
            task_id = str(uuid.uuid4())
            sq_c.execute("INSERT INTO ai_task_queue (task_id, type, status, biblionumber, images, task_config) VALUES (?, ?, ?, ?, ?, ?)",
                         (task_id, 'db_fix', 'pending', bib, '', task_config))
            count += 1
            
    sq_conn.commit()
    sq_conn.close()
    
    return jsonify({"success": True, "queued": count})

# --- IMAGE UPLOADS ---

# --- SOURCE EXPLORER ---

SOURCE_DIR = '/var/www/koha_editor/sources'
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
            
            items.append({
                "name": f,
                "path": rel_path,
                "is_dir": is_dir,
                "size": size,
                "mtime": mtime
            })
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
    target_dir = resolve_source_path(path)
    os.makedirs(target_dir, exist_ok=True)
    
    # Enforce 10MB per file limit
    max_size = 10 * 1024 * 1024
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    files = request.files.getlist('file')
    saved = []
    try:
        for f in files:
            if f.filename:
                # Check file size
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(0)
                if size > max_size:
                    return jsonify({"error": f"File {f.filename} exceeds 10MB limit"}), 400
                # Basic sanitization
                filename = "".join(c for c in f.filename if c.isalnum() or c in " ._-")
                save_path = os.path.join(target_dir, filename)
                f.save(save_path)
                saved.append(filename)
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
            
        ext = target_file.lower().split('.')[-1]
        
        # Stream image types
        if ext in ['jpg', 'jpeg', 'png', 'gif']:
            return send_from_directory(os.path.dirname(target_file), os.path.basename(target_file))
        
        # Read text types
        if ext in ['txt', 'json', 'md', 'csv']:
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

# --- BACKGROUND AI WORKER ---

def ai_worker_loop():
    import json
    while True:
        try:
            sq_conn = sqlite3.connect(SQLITE_DB)
            sq_conn.row_factory = sqlite3.Row
            sq_c = sq_conn.cursor()
            sq_c.execute("SELECT * FROM ai_task_queue WHERE status = 'pending' LIMIT 1")
            task = sq_c.fetchone()
            
            if task:
                task_id = task['task_id']
                sq_c.execute("UPDATE ai_task_queue SET status = 'processing' WHERE task_id = ?", (task_id,))
                sq_conn.commit()
                sq_conn.close()
                
                try:
                    raw_text = ""
                    ai_json = {}
                    is_db_fix = (task['type'] == 'db_fix')
                    
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
                                raw_text = f"Title: {b_row['title']}\nAuthor: {b_row['author']}\nPublisher: {b_row['publishercode']}"
                        finally:
                            conn.close()
                            
                    else:
                        images = json.loads(task['images']) if task['images'] else []
                        if images:
                            img_name = images[0]
                            img_path = os.path.join(IMAGE_DIR, img_name)
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
                    from google import genai
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
                    
                    if is_db_fix:
                        if fix_type == 'metadata':
                            prompt = f"Fix missing basic metadata. Output JSON with keys: title, author, publishercode, publicationyear, isbn.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
                        elif fix_type == 'classification':
                            prompt = f"Classify this book. Output JSON with keys: ddc, subjects.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
                        else:
                            prompt = f"Fix all missing catalog metadata. Output JSON with keys: title, author, publishercode, publicationyear, isbn, ddc, subjects.\nDo not output anything else.\n\nBook Info:\n{raw_text}"
                    else:
                        prompt = f"Extract metadata from OCR text. Output JSON with keys: title, author, publishercode, publicationyear, isbn, ddc, subjects.\nDo not output anything else.\n\nText:\n{raw_text}"
                        
                    ai_text = ""
                    # 1. Try Gemini if specified OR fallback to it
                    if ai_model == 'gemini' or ai_model == 'fallback':
                        try:
                            api_key = config.GEMINI_API_KEY if config else os.environ.get("GEMINI_API_KEY")
                            client = genai.Client(api_key=api_key)
                            response = client.models.generate_content(
                                model='gemini-2.5-flash',
                                contents=prompt
                            )
                            ai_text = response.text
                        except Exception as e:
                            print(f"Gemini API Error: {e}")
                            ai_text = '{"title": "Gemini API Error", "author": "Check logs"}'
                            
                    # 2. Local DeepSeek via Tunnel
                    else:
                        try:
                            payload = {"model": "deepseek-ai/DeepSeek-R1-Distill-Llama-8B", "messages": [{"role": "user", "content": prompt}], "temperature": 0.0}
                            res = requests.post('http://localhost:8000/v1/chat/completions', json=payload, timeout=30)
                            ai_text = res.json()['choices'][0]['message']['content']
                        except Exception as e:
                            print(f"DeepSeek Tunnel Error: {e}")
                            # Auto Fallback to Gemini if vLLM tunnel is frozen
                            print("Falling back to Gemini...")
                            try:
                                api_key = config.GEMINI_API_KEY if config else os.environ.get("GEMINI_API_KEY")
                                client = genai.Client(api_key=api_key)
                                response = client.models.generate_content(
                                    model='gemini-2.5-flash',
                                    contents=prompt
                                )
                                ai_text = response.text
                            except:
                                ai_text = '{"title": "DeepSeek & Gemini Offline", "author": "Check Server"}'
                    
                    ai_text = re.sub(r'<think>.*?</think>', '', ai_text, flags=re.DOTALL).strip()
                    json_match = re.search(r'\{.*?\}', ai_text, flags=re.DOTALL)
                    if json_match:
                        try:
                            ai_json = json.loads(json_match.group(0))
                        except:
                            ai_json = {"title": "JSON Parse Error", "author": "Error"}
                    else:
                        ai_json = {"title": "Extraction Failed", "author": "Failed"}
                        
                    final_ddc = ai_json.get("ddc", "")
                    final_subjects = ai_json.get("subjects", "")
                    isbn_to_check = ai_json.get('isbn', '')
                    
                    if not is_db_fix:
                        isbn_match = re.search(r'(?:ISBN(?:-1[03])?:? )?(?=[0-9X]{10}$|(?=(?:[0-9]+[- ]){3})[- 0-9X]{13}$|97[89][0-9]{10}$|(?=(?:[0-9]+[- ]){4})[- 0-9]{17}$)(?:97[89][- ]?)?[0-9]{1,5}[- ]?[0-9]+[- ]?[0-9]+[- ]?[0-9X]', raw_text, re.IGNORECASE)
                        if not isbn_to_check and isbn_match:
                            isbn_to_check = re.sub(r'[^0-9X]', '', isbn_match.group(0).upper())
                            
                    if isbn_to_check:
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
                        except:
                            pass
                            
                    result_data = {
                        "title": ai_json.get("title", ""),
                        "author": ai_json.get("author", ""),
                        "publishercode": ai_json.get("publishercode", ""),
                        "publicationyear": ai_json.get("publicationyear", ""),
                        "isbn": isbn_to_check,
                        "ddc": final_ddc,
                        "subjects": final_subjects,
                        "raw_ocr": raw_text[:500] + "..."
                    }
                        
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
                        except Exception as db_err:
                            logging.error(f"Failed DB auto-fix: {db_err}")
                        finally:
                            conn.close()
                            
                    # Update SQLite
                    sq_conn2 = sqlite3.connect(SQLITE_DB)
                    sq_c2 = sq_conn2.cursor()
                    sq_c2.execute("UPDATE ai_task_queue SET status = 'completed', result_data = ? WHERE task_id = ?", (json.dumps(result_data), task_id))
                    sq_conn2.commit()
                    sq_conn2.close()
                    logging.info(f"Task {task_id} completed.")
                    
                except Exception as e:
                    sq_conn3 = sqlite3.connect(SQLITE_DB)
                    sq_c3 = sq_conn3.cursor()
                    sq_c3.execute("UPDATE ai_task_queue SET status = 'error', result_data = ? WHERE task_id = ?", (json.dumps({"error": str(e)}), task_id))
                    sq_conn3.commit()
                    sq_conn3.close()
                    logging.error(f"Task {task_id} failed: {e}")
            else:
                sq_conn.close()
                
        except Exception as ex:
            logging.error(f"Daemon Loop Error: {ex}")
            
        time.sleep(3)

# Start the worker daemon
import threading

def source_worker_loop():
    import json, base64, requests, os, uuid, time
    from google import genai
    
    while True:
        try:
            sq_conn = sqlite3.connect(SQLITE_DB)
            sq_conn.row_factory = sqlite3.Row
            sq_c = sq_conn.cursor()
            sq_c.execute("SELECT * FROM ai_task_queue WHERE type IN ('source_ocr', 'source_meta', 'source_mrc', 'source_stage') AND status = 'pending' LIMIT 1")
            task = sq_c.fetchone()
            
            if task:
                task_id = task['task_id']
                t_type = task['type']
                target_file = task['images']
                
                sq_c.execute("UPDATE ai_task_queue SET status = 'processing' WHERE task_id = ?", (task_id,))
                sq_conn.commit()
                sq_conn.close()
                
                try:
                    result_data = {"status": "success", "file": target_file}
                    
                    if t_type == 'source_ocr':
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
                            api_key = config.GEMINI_API_KEY if 'config' in globals() else os.environ.get("GEMINI_API_KEY")
                            client = genai.Client(api_key=api_key)
                            response = client.models.generate_content(
                                model='gemini-2.5-flash',
                                contents=prompt
                            )
                            ai_text = response.text
                            # clean markdown
                            ai_text = ai_text.replace("```json", "").replace("```", "").strip()
                            json.loads(ai_text) # validate
                        except Exception as e:
                            ai_text = json.dumps({"title": "AI Error", "error": str(e)})
                            
                        meta_file = target_file.replace('.ocr.txt', '') + ".meta.json"
                        with open(meta_file, 'w', encoding='utf-8') as f:
                            f.write(ai_text)
                        result_data['meta_file'] = meta_file
                        
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
logging.info("Source Explorer Worker Thread started.")


worker_thread = threading.Thread(target=ai_worker_loop, daemon=True)
worker_thread.start()
logging.info("Background AI Worker Thread started.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)

