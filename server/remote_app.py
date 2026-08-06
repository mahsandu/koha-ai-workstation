import os
import json
import uuid
import threading
import time
import bcrypt
import xml.etree.ElementTree as ET
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

def get_db_connection():
    return pymysql.connect(
        host='127.0.0.1',
        user='koha_mfa',
        password='HdOd?^`UVa`c3^W~',
        database='koha_mfa',
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

def update_marc_xml(xml_str, field, new_value):
    try:
        root = ET.fromstring(xml_str)
        tag_map = {
            "title": ("245", "a"),
            "author": ("100", "a"),
            "publishercode": ("260", "b"),
            "publicationyear": ("260", "c"),
            "pages": ("300", "a")
        }
        tag, code = tag_map.get(field, (None, None))
        if not tag: return xml_str
        
        datafield = None
        for df in root.findall(f".//datafield[@tag='{tag}']"):
            datafield = df
            break
            
        if datafield is None:
            datafield = ET.SubElement(root, "datafield", tag=tag, ind1=" ", ind2=" ")
            
        subfield = None
        for sf in datafield.findall(f"./subfield[@code='{code}']"):
            subfield = sf
            break
            
        if subfield is None:
            subfield = ET.SubElement(datafield, "subfield", code=code)
            
        subfield.text = new_value
        return ET.tostring(root, encoding='utf-8', xml_declaration=False).decode('utf-8')
    except Exception as e:
        return xml_str

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
                "Missing Title": 0, "Garbled Title": 0,
                "Missing Author": 0, "Garbled Author": 0,
                "Missing Publisher": 0, "Garbled Publisher": 0,
                "Missing Year": 0, "Invalid Year": 0,
                "Missing Pages": 0
            }
            
            for row in rows:
                total += 1
                title = row['title'] or ''
                author = row['author'] or ''
                pub = row['publishercode'] or ''
                year = row['publicationyear'] or ''
                pages = row['pages'] or ''
                
                row_issues = []
                if not title.strip(): row_issues.append("Missing Title")
                elif is_garbled(title): row_issues.append("Garbled Title")
                
                if not author.strip(): row_issues.append("Missing Author")
                elif is_garbled(author): row_issues.append("Garbled Author")
                
                if not pub.strip(): row_issues.append("Missing Publisher")
                elif is_garbled(pub): row_issues.append("Garbled Publisher")
                
                if not str(year).strip(): row_issues.append("Missing Year")
                elif is_invalid_year(year): row_issues.append("Invalid Year")
                
                if not pages.strip(): row_issues.append("Missing Pages")
                
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
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    filter_type = request.args.get('filter', 'all')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    b.biblionumber, i.barcode, b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages
                FROM koha_mfa.items i
                LEFT JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber
                LEFT JOIN koha_mfa.biblioitems bi ON i.biblioitemnumber = bi.biblioitemnumber
            """)
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
                
                is_bad = False
                reasons = []
                if is_garbled(title): reasons.append("Garbled Title"); is_bad = True
                if is_garbled(author): reasons.append("Garbled Author"); is_bad = True
                if is_invalid_year(year): reasons.append("Invalid Year"); is_bad = True
                if not title.strip() or not author.strip() or not pub.strip() or not str(year).strip():
                    reasons.append("Missing Data")
                    is_bad = True
                    
                if filter_type == 'garbled' and not is_bad:
                    continue
                    
                records.append({
                    "biblionumber": bib,
                    "barcode": barcode,
                    "title": title,
                    "author": author,
                    "publishercode": pub,
                    "publicationyear": year,
                    "pages": pages,
                    "issues": ", ".join(reasons)
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

@app.route('/api/records/<biblionumber>', methods=['POST'])
@login_required
def update_record(biblionumber):
    data = request.json
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Update SQL tables
            if 'title' in data: cursor.execute("UPDATE koha_mfa.biblio SET title = %s WHERE biblionumber = %s", (data['title'], biblionumber))
            if 'author' in data: cursor.execute("UPDATE koha_mfa.biblio SET author = %s WHERE biblionumber = %s", (data['author'], biblionumber))
            if 'publishercode' in data: cursor.execute("UPDATE koha_mfa.biblioitems SET publishercode = %s WHERE biblionumber = %s", (data['publishercode'], biblionumber))
            if 'publicationyear' in data: cursor.execute("UPDATE koha_mfa.biblioitems SET publicationyear = %s WHERE biblionumber = %s", (data['publicationyear'], biblionumber))
            if 'pages' in data: cursor.execute("UPDATE koha_mfa.biblioitems SET pages = %s WHERE biblionumber = %s", (data['pages'], biblionumber))
            
            # 2. Update MARC XML
            cursor.execute("SELECT metadata FROM koha_mfa.biblio_metadata WHERE biblionumber = %s", (biblionumber,))
            row = cursor.fetchone()
            if row and row['metadata']:
                current_xml = row['metadata']
                for field, value in data.items():
                    if field in ['title', 'author', 'publishercode', 'publicationyear', 'pages']:
                        current_xml = update_marc_xml(current_xml, field, value)
                
                cursor.execute("UPDATE koha_mfa.biblio_metadata SET metadata = %s WHERE biblionumber = %s", (current_xml, biblionumber))
                
            conn.commit()
    finally:
        conn.close()
        
    return jsonify({"success": True})


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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
