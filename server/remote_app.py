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
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, send_from_directory
import pymysql

app = Flask(__name__)
app.secret_key = 'super_secret_koha_editor_key' # Replace in prod

# The queue for AI tasks
AI_QUEUE = {}

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
                
                if not pages.strip(): row_issues.append("Missing_Pages")
                
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
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            query = """
                SELECT 
                    b.biblionumber, i.barcode, i.itemcallnumber, b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages
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
                if not pages.strip(): reasons.append("Missing_Pages")
                
                if filter_type == 'garbled' and not is_bad:
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

# --- LABEL TAGGING ---
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
            # Find next batch_id
            cursor.execute("SELECT MAX(batch_id) as max_b FROM koha_mfa.creator_batches")
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
            
            conn.commit()
            return jsonify({"success": True, "batch_id": new_batch_id})
    finally:
        conn.close()

# --- IMAGE UPLOADS ---
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
@app.route('/api/queue/poll', methods=['GET'])
def queue_poll():
    for task_id, task in AI_QUEUE.items():
        if task['status'] == 'pending':
            task['status'] = 'processing'
            return jsonify({
                "task_id": task_id,
                "images": [f"http://{request.host}/koha_editor/images/{img}" for img in task['images']],
                "barcode": task.get('barcode'),
                "item_count": task.get('item_count')
            })
    return jsonify({"task_id": None})

@app.route('/api/queue/result', methods=['POST'])
def queue_result():
    data = request.json
    task_id = data.get('task_id')
    if task_id in AI_QUEUE:
        AI_QUEUE[task_id]['status'] = 'completed'
        AI_QUEUE[task_id]['result'] = data.get('result')
        return jsonify({"success": True})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/process_ai', methods=['POST'])
@login_required
def trigger_ai():
    data = request.json
    images = data.get('images', [])
    barcode = data.get('barcode', '')
    item_count = data.get('item_count', '')
    
    task_id = str(uuid.uuid4())
    AI_QUEUE[task_id] = {
        "status": "pending",
        "images": images,
        "barcode": barcode,
        "item_count": item_count,
        "result": None
    }
    return jsonify({"task_id": task_id})

@app.route('/api/process_ai/<task_id>', methods=['GET'])
@login_required
def check_ai(task_id):
    if task_id in AI_QUEUE:
        return jsonify(AI_QUEUE[task_id])
    return jsonify({"error": "Not found"}), 404

@app.route('/api/queue/status', methods=['GET'])
@login_required
def queue_status():
    return jsonify({"queue": AI_QUEUE})



# --- BACKGROUND AI WORKER ---
def ai_worker_loop():
    while True:
        try:
            for task_id, task in list(AI_QUEUE.items()):
                if task['status'] == 'pending':
                    task['status'] = 'processing'
                    
                    try:
                        # 1. Cloud OCR
                        img_name = task['images'][0]
                        img_path = os.path.join(IMAGE_DIR, img_name)
                        
                        with open(img_path, 'rb') as f:
                            encoded_string = base64.b64encode(f.read()).decode('utf-8')
                            
                        ocr_payload = {
                            'apikey': 'helloworld',
                            'base64Image': 'data:image/jpeg;base64,' + encoded_string,
                            'language': 'eng'
                        }
                        
                        try:
                            ocr_res = requests.post('https://api.ocr.space/parse/image', data=ocr_payload, timeout=15)
                            ocr_data = ocr_res.json()
                            raw_text = ""
                            if not ocr_data.get('IsErroredOnProcessing'):
                                for parsed in ocr_data.get('ParsedResults', []):
                                    raw_text += parsed.get('ParsedText', '') + " "
                        except Exception as e:
                            raw_text = "FAILED TO EXTRACT TEXT: " + str(e)
                            
                        if not raw_text.strip():
                            raw_text = "UNKNOWN TEXT"
                            
                        # 2. Local vLLM/Ollama Extraction (DeepSeek-R1)
                        prompt = f"Extract metadata from the following OCR text of a book cover/title page. Output strictly as JSON with the following keys:\n- title: (string)\n- author: (string)\n- publishercode: (string)\n- publicationyear: (string)\n- isbn: (string, 10 or 13 digits if found, else empty)\n- ddc: (string, 3-digit Dewey Decimal notation based on the topic, e.g. 004)\n- subjects: (comma separated string of 2-3 main topics)\n\nDo not include any other text or explanation. Output only valid JSON.\nText:\n{raw_text}"
                        
                        ollama_payload = {
                            "model": "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.0
                        }
                        
                        try:
                            # Try vLLM first
                            res = requests.post('http://localhost:8000/v1/chat/completions', json=ollama_payload, timeout=30)
                            ai_text = res.json()['choices'][0]['message']['content']
                        except:
                            try:
                                # Fallback to Ollama if vLLM is down
                                ollama_payload_fallback = {
                                    "model": "qwen2.5",
                                    "prompt": prompt,
                                    "stream": False
                                }
                                res = requests.post('http://localhost:11434/api/generate', json=ollama_payload_fallback, timeout=30)
                                ai_text = res.json().get('response', '')
                            except Exception as e:
                                ai_text = '{"title": "AI Offline", "author": "Check Server"}'
                        
                        # Clean DeepSeek think tags
                        ai_text = re.sub(r'<think>.*?</think>', '', ai_text, flags=re.DOTALL).strip()
                        
                        # Extract JSON
                        json_match = re.search(r'\{.*?\}', ai_text, flags=re.DOTALL)
                        if json_match:
                            try:
                                ai_json = json.loads(json_match.group(0))
                            except:
                                ai_json = {"title": "JSON Parse Error", "author": "Error"}
                        else:
                            ai_json = {"title": "Extraction Failed", "author": "Failed"}
                            
                        
                        # 3. OpenLibrary Priority Check
                        final_ddc = ai_json.get("ddc", "")
                        final_subjects = ai_json.get("subjects", "")
                        
                        # Try regex for ISBN just in case AI missed it
                        isbn_match = re.search(r'(?:ISBN(?:-1[03])?:? )?(?=[0-9X]{10}$|(?=(?:[0-9]+[- ]){3})[- 0-9X]{13}$|97[89][0-9]{10}$|(?=(?:[0-9]+[- ]){4})[- 0-9]{17}$)(?:97[89][- ]?)?[0-9]{1,5}[- ]?[0-9]+[- ]?[0-9]+[- ]?[0-9X]', raw_text, re.IGNORECASE)
                        isbn_to_check = ai_json.get('isbn', '')
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
                            
                        task['result'] = {
                            "title": ai_json.get("title", ""),
                            "author": ai_json.get("author", ""),
                            "publishercode": ai_json.get("publishercode", ""),
                            "publicationyear": ai_json.get("publicationyear", ""),
                            "isbn": isbn_to_check,
                            "ddc": final_ddc,
                            "subjects": final_subjects,
                            "raw_ocr": raw_text[:500] + "..."
                        }
                        task['status'] = 'completed'
                        
                        # (Optional) Auto-insert into DB logic could go here
                        
                    except Exception as e:
                        task['status'] = 'error'
                        task['result'] = {"error": str(e)}
        except Exception:
            pass
            
        time.sleep(3)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)

