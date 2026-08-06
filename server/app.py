import os
import json
import re
import xml.etree.ElementTree as ET
import paramiko
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

SSH_HOST = '103.103.89.142'
SSH_PORT = 3022
SSH_USER = 'root'
SSH_PASS = 'Ru3YOTEVE1MHWS8x4b/VwWPPCTs7fuNGyLf1wh4/fCHtIzeI'

def run_remote_sql(sql, fetch=True):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS)
    cmd = "mysql -u root -pkoha_mfa --default-character-set=utf8mb4 -B -r"
    stdin, stdout, stderr = ssh.exec_command(cmd)
    stdin.write(sql)
    stdin.flush()
    stdin.channel.shutdown_write()
    
    if fetch:
        out = stdout.read().decode('utf-8')
        ssh.close()
        return out
    else:
        err = stderr.read().decode('utf-8')
        ssh.close()
        return err

def is_garbled(text):
    if not text: return False
    # Check for replacement character or common mojibake
    if '\ufffd' in text or 'Ã' in text or 'Â' in text or 'â' in text:
        return True
    return False

def is_invalid_year(year):
    if not year: return False
    # Remove whitespace
    y = year.strip()
    if not y.isdigit() or len(y) < 3 or len(y) > 4:
        return True
    return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stats')
def stats():
    sql = """
    SELECT 
        i.barcode, b.biblionumber, b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages
    FROM koha_mfa.items i
    LEFT JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber
    LEFT JOIN koha_mfa.biblioitems bi ON i.biblioitemnumber = bi.biblioitemnumber
    """
    out = run_remote_sql(sql)
    lines = out.splitlines()
    if not lines:
        return jsonify({"error": "No records"})
    
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
    
    for line in lines[1:]:
        row = line.split('\t')
        row += [''] * (7 - len(row))
        total += 1
        title, author, pub, year, pages = row[2], row[3], row[4], row[5], row[6]
        
        row_issues = []
        if not title.strip(): row_issues.append("Missing Title")
        elif is_garbled(title): row_issues.append("Garbled Title")
        
        if not author.strip(): row_issues.append("Missing Author")
        elif is_garbled(author): row_issues.append("Garbled Author")
        
        if not pub.strip(): row_issues.append("Missing Publisher")
        elif is_garbled(pub): row_issues.append("Garbled Publisher")
        
        if not year.strip(): row_issues.append("Missing Year")
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

@app.route('/api/records')
def get_records():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    filter_type = request.args.get('filter', 'all')
    
    sql = """
    SELECT 
        b.biblionumber, i.barcode, b.title, b.author, bi.publishercode, bi.publicationyear, bi.pages
    FROM koha_mfa.items i
    LEFT JOIN koha_mfa.biblio b ON i.biblionumber = b.biblionumber
    LEFT JOIN koha_mfa.biblioitems bi ON i.biblioitemnumber = bi.biblioitemnumber
    """
    out = run_remote_sql(sql)
    lines = out.splitlines()
    
    records = []
    for line in lines[1:]:
        row = line.split('\t')
        row += [''] * (7 - len(row))
        bib, barcode, title, author, pub, year, pages = row
        
        is_bad = False
        reasons = []
        if is_garbled(title): reasons.append("Garbled Title"); is_bad = True
        if is_garbled(author): reasons.append("Garbled Author"); is_bad = True
        if is_invalid_year(year): reasons.append("Invalid Year"); is_bad = True
        if not title.strip() or not author.strip() or not pub.strip() or not year.strip():
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
        
    # Paginate
    start = (page - 1) * limit
    end = start + limit
    return jsonify({
        "total": len(records),
        "page": page,
        "data": records[start:end]
    })

def update_marc_xml(xml_str, field, new_value):
    try:
        root = ET.fromstring(xml_str)
        # Find or create datafield
        tag_map = {
            "title": ("245", "a"),
            "author": ("100", "a"),
            "publishercode": ("260", "b"),
            "publicationyear": ("260", "c"),
            "pages": ("300", "a")
        }
        tag, code = tag_map[field]
        
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

@app.route('/api/records/<biblionumber>', methods=['POST'])
def update_record(biblionumber):
    data = request.json
    
    # 1. Update SQL tables
    sql_updates = []
    if 'title' in data: sql_updates.append(f"UPDATE koha_mfa.biblio SET title = '{data['title'].replace(chr(39), chr(92)+chr(39))}' WHERE biblionumber = {biblionumber};")
    if 'author' in data: sql_updates.append(f"UPDATE koha_mfa.biblio SET author = '{data['author'].replace(chr(39), chr(92)+chr(39))}' WHERE biblionumber = {biblionumber};")
    if 'publishercode' in data: sql_updates.append(f"UPDATE koha_mfa.biblioitems SET publishercode = '{data['publishercode'].replace(chr(39), chr(92)+chr(39))}' WHERE biblionumber = {biblionumber};")
    if 'publicationyear' in data: sql_updates.append(f"UPDATE koha_mfa.biblioitems SET publicationyear = '{data['publicationyear'].replace(chr(39), chr(92)+chr(39))}' WHERE biblionumber = {biblionumber};")
    if 'pages' in data: sql_updates.append(f"UPDATE koha_mfa.biblioitems SET pages = '{data['pages'].replace(chr(39), chr(92)+chr(39))}' WHERE biblionumber = {biblionumber};")
    
    if sql_updates:
        run_remote_sql("\n".join(sql_updates), fetch=False)
        
    # 2. Update MARC XML
    xml_out = run_remote_sql(f"SELECT metadata FROM koha_mfa.biblio_metadata WHERE biblionumber = {biblionumber};")
    lines = xml_out.splitlines()
    if len(lines) > 1:
        current_xml = lines[1]
        for field, value in data.items():
            if field in ['title', 'author', 'publishercode', 'publicationyear', 'pages']:
                current_xml = update_marc_xml(current_xml, field, value)
        
        # Save back
        escaped_xml = current_xml.replace("'", "\\'")
        update_xml_sql = f"UPDATE koha_mfa.biblio_metadata SET metadata = '{escaped_xml}' WHERE biblionumber = {biblionumber};"
        run_remote_sql(update_xml_sql, fetch=False)
        
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5050)
