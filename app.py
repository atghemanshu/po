import os
import re
import secrets
import string
import random
from functools import wraps
import json
import uuid # Added for ATS criteria IDs

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument
import requests

# --- App Setup ---
TEMP_FOLDER = os.path.join(os.path.dirname(__file__), 'temp')
os.makedirs(TEMP_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))
app.config['SESSION_TYPE'] = 'filesystem' # Recommended for storing larger session data if needed

# --- Configuration ---
OCR_SPACE_API_URL = "https://api.ocr.space/parse/image"
OCR_SPACE_API_KEY = os.environ.get('OCR_SPACE_API_KEY', "K87955728688957")
if OCR_SPACE_API_KEY == "K87955728688957":
    print("Warning: Using default/placeholder OCR Space API key.")

# --- Master Field Definitions (For Admin Configuration Screens) ---
MASTER_FIELD_DEFINITIONS = {
    "po": [
        {"id": "po_doc_number", "label": "PO Number", "description": "Purchase Order Number (e.g., 81100)"},
        {"id": "po_doc_vendor_id", "label": "Vendor", "description": "Vendor ID (e.g., S101334)"},
        {"id": "po_doc_phone", "label": "Phone", "description": "Vendor Phone Number (e.g., 734-426-3655)"},
        {"id": "po_doc_total", "label": "Total", "description": "Grand Total Amount (e.g., $ 5,945.00)"},
        {"id": "po_doc_order_date", "label": "Order Date", "description": "PO Order Date (e.g., 8/8/2024)"},
    ],
    "ats": [
        {"id": "ats_sr_no", "label": "Sr no.", "description": "Serial or Reference Number (e.g., S009)"},
        {"id": "ats_name", "label": "Name", "description": "Candidate's Full Name (e.g., Olivia Miller)"},
        {"id": "ats_gender", "label": "Gender", "description": "Candidate's Gender (e.g., M, F, Other)"},
        {"id": "ats_phone", "label": "Phone", "description": "Candidate's Phone Number (e.g., 8788019869)"},
        {"id": "ats_city", "label": "City", "description": "Candidate's City (e.g., Sydney)"},
        {"id": "ats_age", "label": "Age", "description": "Candidate's Age (e.g., 28)"},
        {"id": "ats_country", "label": "Country", "description": "Candidate's Country (e.g., Australia)"},
        {"id": "ats_address", "label": "Address", "description": "Candidate's Full Address (e.g., 42 Bondi Beach Road)"},
        {"id": "ats_email", "label": "Email", "description": "Candidate's Email Address (e.g., olivia.m@example.net)"},
        {"id": "ats_skills", "label": "Skills", "description": "Comma-separated or list of skills (e.g., Shopify, Java, React)"},
        {"id": "ats_salary", "label": "Salary", "description": "Expected or Current Salary (numeric part)"},
        {"id": "ats_percentage", "label": "Percentage", "description": "Relevant Percentage/Score (e.g., academic)"},
        {"id": "ats_experience_years", "label": "Experience (Years)", "description": "Total years of professional experience"},
    ]
}

# --- Fields for User-Side Extraction (Fixed Sets) ---
PO_FIELDS_FOR_USER_EXTRACTION = ["PO Number", "Vendor", "Phone", "Total", "Order Date", "Vendor Name"]
ATS_FIELDS_FOR_USER_EXTRACTION = ["Sr no.", "Name", "Gender", "Phone", "City", "Age", "Country", "Address", "Email", "Skills","Salary", "Percentage"]

# --- Fields for PO Comparison (Against Admin-Entered DB) ---
PO_KEY_COMPARISON_FIELDS = ["PO Number", "Vendor", "Phone", "Total", "Order Date"] # Vendor Name could be added if desired

# --- Map Field IDs to Labels (used internally for consistency if needed) ---
FIELD_ID_TO_LABEL_MAP = {
    doc_type: {field['id']: field['label'] for field in fields}
    for doc_type, fields in MASTER_FIELD_DEFINITIONS.items()
}

# --- Define available tabs/modules in the system ---
AVAILABLE_TABS = {
    "po": {"id": "po", "name": "PO Verification", "icon": "fas fa-file-invoice"},
    "ats": {"id": "ats", "name": "ATS Verification", "icon": "fas fa-file-alt"},
}

# --- Data Storage ---
USERS_DB = {
    "admin@example.com": {
        "username": "admin_user", "hashed_password": generate_password_hash("admin@a123"), "role": "admin"
        # Admin role implies full access, no need for explicit permissions dict here if simplified
    },
    # Non-admin users will have roles like "po_verifier", "ats_verifier", "sub_admin"
    # Their tab access is derived from their role.
     "po_user@example.com": {"username": "po_user", "hashed_password": generate_password_hash("po@123"), "role": "po_verifier"},
     "ats_user@example.com": {"username": "ats_user", "hashed_password": generate_password_hash("ats@123"), "role": "ats_verifier"},
     "sub_admin@example.com": {"username": "sub_admin_user", "hashed_password": generate_password_hash("sub@123"), "role": "sub_admin"},
}

# Database for PO data entered by admin
# Structure: dummy_database["po"]["<PO_NUMBER>"] = {"Field Label": "Value", ...}
dummy_database = {
    "po": {
        "81100": { # Example entry, admin will add more
            "PO Number": "81100",
            "Vendor": "S101334",
            "Phone": "734-426-3655",
            "Total": "$ 5,945.00",
            "Order Date": "8/8/2024",
        }
    }
    # "ats" section removed from here as it's not used for direct comparison anymore
}

# Database for ATS criteria defined by admin
# Structure: ATS_VALIDATION_CRITERIA_DB["<Field_Label>"] = [ {criterion_dict1}, {criterion_dict2}, ... ]
ATS_VALIDATION_CRITERIA_DB = {}
# Example:
# ATS_VALIDATION_CRITERIA_DB = {
# "Age": [{"id": "uuid1", "condition_type": "min_numeric", "value1": 18, "is_active": True, "field_label": "Age"}],
# "Skills": [{"id": "uuid2", "condition_type": "contains_any", "keywords": ["java", "python"], "is_active": True, "field_label": "Skills"}]
# }

# Database for storing extracted data from user-uploaded resumes
# Structure: RESUMES_DATA_DB["<filename_or_id>"] = {structured_data_dict}
RESUMES_DATA_DB = {}


# --- Helper Functions ---
def generate_temporary_password(length=10):
    alphabet = string.ascii_letters + string.digits + string.punctuation
    while True:
        password = ''.join(secrets.choice(alphabet) for i in range(length))
        if (any(c.islower() for c in password) and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password) and any(c in string.punctuation for c in password)
                and len(password) >= length):
            break
    return password

# --- Authentication & Authorization Decorators ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or not session.get('logged_in'):
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login_page'))
        if session.get('role') == 'admin': # Admins go to admin dashboard
             flash('Admins should use the admin console.', 'info')
             return redirect(url_for('admin_dashboard'))
        if not session.get('accessible_tabs_info'): # Non-admins must have accessible tabs
            flash('You do not have access to any application modules. Please contact an administrator.', 'warning')
            return redirect(url_for('logout')) # Or login_page
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or not session.get('logged_in') or session.get('role') != 'admin':
            flash('You must be an admin to access this page.', 'danger')
            return redirect(url_for('admin_login_page'))
        return f(*args, **kwargs)
    return decorated_function

# --- OCR / File Processing (Largely unchanged, ensure it handles errors gracefully) ---
def ocr_image_via_api(image_path):
    if OCR_SPACE_API_KEY == "K87955728688957": print("Warning: Attempting OCR with placeholder API key.")
    try:
        with open(image_path, 'rb') as f: image_data = f.read()
        payload = {'apikey': OCR_SPACE_API_KEY, 'language': 'eng', 'isOverlayRequired': False}
        files = {'file': (os.path.basename(image_path), image_data)}
        response = requests.post(OCR_SPACE_API_URL, files=files, data=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        if result and not result.get('IsErroredOnProcessing'):
            parsed_results = result.get('ParsedResults')
            if parsed_results and len(parsed_results) > 0:
                return parsed_results[0].get('ParsedText', "").strip()
            return "No parsed results in OCR response."
        error_message = result.get('ErrorMessage', ["Unknown OCR Error"])[0]
        return f"OCR API Error: {error_message}"
    except requests.exceptions.RequestException as e: return f"OCR Connection Error: {e}"
    except Exception as e: return f"OCR Processing Error: {e}"

def extract_text_from_pdf(file_path):
    try:
        text = pdf_extract_text(file_path)
        text = text.strip() if text else ""
        if len(text) < 50: # Threshold for attempting OCR on potentially image-based PDF
            print(f"DEBUG: PDF '{os.path.basename(file_path)}' yielded little text. Attempting OCR.")
            ocr_text = ocr_image_via_api(file_path)
            if ocr_text and not ocr_text.lower().startswith("error"): return ocr_text
            if not text and (not ocr_text or ocr_text.lower().startswith("error")):
                return f"Error: No text extracted from PDF '{os.path.basename(file_path)}' by direct means or OCR."
        return text if text else "No text extracted from PDF."
    except Exception as e:
        print(f"DEBUG: pdfminer failed for '{os.path.basename(file_path)}': {e}. Attempting OCR fallback.")
        ocr_text = ocr_image_via_api(file_path)
        if ocr_text and not ocr_text.lower().startswith("error"): return ocr_text
        return f"Error extracting text from PDF (pdfminer/OCR failed): {e}"

def extract_text_from_docx(file_path):
    try:
        doc = DocxDocument(file_path)
        full_text = [p.text for p in doc.paragraphs if p.text]
        return '\n'.join(full_text).strip() if full_text else "No text extracted from DOCX."
    except Exception as e: return f"Error extracting text from DOCX: {e}"

def extract_text_from_file(file_path, filename):
    _, file_extension = os.path.splitext(filename)
    file_extension = file_extension.lower()
    if file_extension in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff']:
        return ocr_image_via_api(file_path)
    elif file_extension == '.pdf':
        return extract_text_from_pdf(file_path)
    elif file_extension == '.docx':
        return extract_text_from_docx(file_path)
    return f"Error: Unsupported file format '{file_extension}'."


# --- Structured Data Extraction ---
def extract_structured_data(text, fields_to_extract_labels, upload_type=None):
    if not text or not fields_to_extract_labels: return {}
    data = {label: None for label in fields_to_extract_labels}
    lines = text.strip().split('\n')
    text_lower = text.lower()

    # Generic Key-Value (improved slightly)
    for i, line_text in enumerate(lines):
        line_strip = line_text.strip()
        for field_label in fields_to_extract_labels:
            if data[field_label] is not None: continue # Already found by more specific logic or previous generic

            # Try exact label match first: "Label: Value" or "Label Value" (if value is on same line)
            # More robust: allows for variations in spacing and optional colon
            pattern_label = re.escape(field_label)
            match = re.match(r"^\s*" + pattern_label + r"\s*[:\-]?\s*(.+)", line_strip, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if value: data[field_label] = value; break # Found for this field_label, move to next field_label

            # Fallback: if label is found anywhere in line, and next line might be value
            if field_label.lower() in line_strip.lower() and i + 1 < len(lines):
                next_line_strip = lines[i+1].strip()
                # Avoid matching if next line also looks like a label for another field
                if next_line_strip and not any(other_label.lower() + ":" in next_line_strip.lower() for other_label in fields_to_extract_labels if other_label != field_label):
                    if not data[field_label]: # Only if not already found
                         data[field_label] = next_line_strip


    if upload_type == 'po':
        # PO Number (using sample doc: "PO Number: 81100")
        if "PO Number" in fields_to_extract_labels and data["PO Number"] is None:
            m = re.search(r"PO Number\s*:\s*([A-Z0-9\-]+)", text, re.IGNORECASE)
            if m: data["PO Number"] = m.group(1).strip()
        
        # Order Date (using sample doc: "Order Date: 8/8/2024")
        if "Order Date" in fields_to_extract_labels and data["Order Date"] is None:
            m = re.search(r"Order Date\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
            if m: data["Order Date"] = m.group(1).strip()

        # Vendor ID (using sample doc: "Vendor: S101334")
        if "Vendor" in fields_to_extract_labels and data["Vendor"] is None: # "Vendor" is the label for Vendor ID
            m = re.search(r"\bVendor\s*:\s*(S\d+)\b", text, re.IGNORECASE)
            if m: data["Vendor"] = m.group(1).strip()
        
        # Vendor Name (using sample doc: "PROTOMATIC, INC." under "Vendor:")
        if "Vendor Name" in fields_to_extract_labels and data["Vendor Name"] is None:
            # Look for a line starting with a known company suffix after "Vendor:"
            # This is tricky due to varying formats. Sample doc has it on next line.
            m_block = re.search(r"Vendor\s*:\s*\n\s*([A-Z][A-Za-z\s.,&'-]+(?:INC\.|LLC|LTD|CO\.?)\b)", text, re.IGNORECASE | re.MULTILINE)
            if m_block: data["Vendor Name"] = m_block.group(1).strip()
            else: # Fallback if it's not immediately after "Vendor:" line
                m_name = re.search(r"^(PROTOMATIC,\s*INC\.)$", text, re.MULTILINE | re.IGNORECASE) # Specific for sample
                if m_name: data["Vendor Name"] = m_name.group(1).strip()


        # Phone (using sample doc: "Phone: 734-426-3655" associated with vendor)
        if "Phone" in fields_to_extract_labels and data["Phone"] is None:
            # Look for phone after "Vendor:" block or near vendor details
            vendor_block_match = re.search(r"Vendor\s*:.*?Phone\s*:\s*(\(?\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}(?:\s*x\d+)?)", text, re.IGNORECASE | re.DOTALL)
            if vendor_block_match:
                data["Phone"] = vendor_block_match.group(1).strip()
            else: # More generic phone search if not tied to vendor block
                m_phone = re.search(r"Phone\s*:\s*(\(?\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}(?:\s*x\d+)?)", text, re.IGNORECASE)
                if m_phone: data["Phone"] = m_phone.group(1).strip()

        # Total (using sample doc: "Total: $ 5,945.00" at the end)
        if "Total" in fields_to_extract_labels and data["Total"] is None:
            m = re.search(r"\b(?:Total|Amount Due)\b\s*[:\s]*([\$€£]?\s*\d{1,3}(?:,\d{3})*\.\d{2})", text, re.IGNORECASE | re.MULTILINE)
            if m: data["Total"] = m.group(1).strip()
            # Specific for the sample doc where "Total:" is followed by the value on the same line
            m_sample_total = re.search(r"Total\s*:\s*(\$\s*\d{1,3}(?:,\d{3})*\.\d{2})", text, re.IGNORECASE)
            if m_sample_total and data["Total"] is None : data["Total"] = m_sample_total.group(1).strip()


    elif upload_type == 'ats':
        # Using the sample resume:
        # Sr no.: S009
        # Name: Olivia Miller
        # Gender: F
        # Phone: 8788019869
        # City: Sydney
        # Age: 28
        # Country: Australia
        # Address: 42 Bondi Beach Road
        # Email: olivia.m@example.net
        # Skills: Shopify, Java, React, Camunda

        if "Sr no." in fields_to_extract_labels and data["Sr no."] is None:
            m = re.search(r"Sr\s*no\.\s*:\s*(\S+)", text, re.IGNORECASE)
            if m: data["Sr no."] = m.group(1).strip()
        
        if "Name" in fields_to_extract_labels and data["Name"] is None:
            m = re.search(r"Name\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Name"] = m.group(1).strip()
        
        if "Gender" in fields_to_extract_labels and data["Gender"] is None:
            m = re.search(r"Gender\s*:\s*([A-Za-z]+)", text, re.IGNORECASE)
            if m: data["Gender"] = m.group(1).strip()

        if "Phone" in fields_to_extract_labels and data["Phone"] is None:
            m = re.search(r"Phone\s*:\s*([\d\s\-\(\)]+)", text, re.IGNORECASE)
            if m: data["Phone"] = re.sub(r"[^\d]", "", m.group(1).strip()) # Clean to just digits

        if "City" in fields_to_extract_labels and data["City"] is None:
            m = re.search(r"City\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["City"] = m.group(1).strip()

        if "Age" in fields_to_extract_labels and data["Age"] is None:
            m = re.search(r"Age\s*:\s*(\d+)", text, re.IGNORECASE)
            if m: data["Age"] = m.group(1).strip()

        if "Country" in fields_to_extract_labels and data["Country"] is None:
            m = re.search(r"Country\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Country"] = m.group(1).strip()

        if "Address" in fields_to_extract_labels and data["Address"] is None:
            m = re.search(r"Address\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Address"] = m.group(1).strip()
        
        if "Email" in fields_to_extract_labels and data["Email"] is None:
            m = re.search(r"Email\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text, re.IGNORECASE)
            if m: data["Email"] = m.group(1).strip()

        if "Skills" in fields_to_extract_labels and data["Skills"] is None:
            m = re.search(r"Skills\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Skills"] = m.group(1).strip()
        if "Salary" in fields_to_extract_labels and data["Salary"] is None:
            # Common patterns: "Salary: $50,000", "Expected Salary: 60k", "Current CTC: 12 LPA"
            # This regex tries to capture numbers, optional k/lpa, currency symbols
            m_salary = re.search(
                r"(?:salary|ctc|compensation|expected salary)\s*[:\-]?\s*([\$€£₹]?\s*\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?\s*(?:k|lpa)?)",
                text, re.IGNORECASE
            )
            if m_salary:
                salary_str = m_salary.group(1).strip()
                # Further cleaning/normalization can be done here if needed (e.g., convert 'k' to '000')
                data["Salary"] = salary_str 
            else: # Fallback: look for lines with just numbers and currency/k/lpa near salary-like words
                for i, line in enumerate(lines):
                    if any(keyword in line.lower() for keyword in ["salary", "ctc", "compensation"]):
                        # Check current line and next few lines for a salary-like value
                        for j in range(i, min(i + 3, len(lines))):
                            potential_salary_line = lines[j]
                            m_val = re.search(r"([\$€£₹]?\s*\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?\s*(?:k|lpa)?)", potential_salary_line)
                            if m_val and len(m_val.group(1).strip()) > 2 : # Avoid matching small random numbers
                                if not re.search(r"\d{4}", m_val.group(1)): # Avoid matching years if they look like salary
                                    data["Salary"] = m_val.group(1).strip()
                                    break
                        if data["Salary"]: break
        
        # ADDED: Percentage Extraction
        if "Percentage" in fields_to_extract_labels and data["Percentage"] is None:
            # Common patterns: "Percentage: 85%", "Score: 75.5 %", "CGPA: 8.2/10"
            # Looks for numbers followed by '%' or 'cgpa' patterns
            m_percent = re.search(
                r"(?:percentage|score|marks|grade)\s*[:\-]?\s*(\d{1,2}(?:\.\d{1,2})?\s*%)", 
                text, re.IGNORECASE
            )
            if m_percent:
                data["Percentage"] = m_percent.group(1).strip()
            else: # CGPA style
                m_cgpa = re.search(
                    r"(?:cgpa)\s*[:\-]?\s*(\d(?:\.\d{1,2})?(?:\s*/\s*\d{1,2})?)", 
                    text, re.IGNORECASE
                )
                if m_cgpa:
                    data["Percentage"] = m_cgpa.group(1).strip() + " CGPA" # Add context
            # Fallback: Look for standalone percentages if the above fail
            if data["Percentage"] is None:
                 m_standalone_percent = re.search(r"\b(\d{1,2}(?:\.\d{1,2})?\s*%)", text)
                 if m_standalone_percent:
                     # Check context if possible to avoid random percentages
                     line_containing_percent = ""
                     for line in lines:
                         if m_standalone_percent.group(1) in line:
                             line_containing_percent = line.lower()
                             break
                     if any(kw in line_containing_percent for kw in ["aggregate", "overall", "academic", "score"]):
                         data["Percentage"] = m_standalone_percent.group(1).strip()
    return data

# --- PO Database Comparison Logic ---
def get_po_db_record(po_number_value):
    return dummy_database["po"].get(po_number_value)

def compare_po_data(extracted_data, db_record, comparison_field_labels):
    if not db_record: return 0, {}, "PO Record not found in database."
    if not comparison_field_labels: return 0, {}, "No PO fields specified for comparison."
    matched_fields = 0; mismatched = {}
    # Ensure we only compare fields that are supposed to be in the DB record for comparison
    actual_comparable_db_fields = [label for label in comparison_field_labels if label in db_record]
    if not actual_comparable_db_fields: return 0, {}, "None of the comparison fields exist in the DB record."
    
    total_comparable = len(actual_comparable_db_fields)
    for label in actual_comparable_db_fields:
        db_val = str(db_record.get(label, "")).strip().lower().replace('$', '').replace(',', '')
        ext_val = str(extracted_data.get(label, "")).strip().lower().replace('$', '').replace(',', '')
        if ext_val == db_val and db_val != "": matched_fields += 1
        elif db_val != "": # Only count mismatch if DB expects a value
            mismatched[label] = {"db_value": db_record.get(label), "extracted_value": extracted_data.get(label)}
            
    accuracy = (matched_fields / total_comparable) * 100 if total_comparable > 0 else 0
    return accuracy, mismatched, None


# --- ATS Data Validation Against Admin Criteria ---
def validate_ats_data(extracted_data, criteria_db):
    if not criteria_db: return 100.0, {}, "No ATS criteria defined by admin." # No criteria means 100% pass
    
    total_active_criteria = 0
    passed_criteria_count = 0
    failed_criteria_details = {} # {"Field Label": "Reason for failure"}

    for field_label, criteria_list in criteria_db.items():
        extracted_value_str = str(extracted_data.get(field_label, "")).strip()
        extracted_value_lower = extracted_value_str.lower()

        for criterion in criteria_list:
            if not criterion.get("is_active", False): continue
            total_active_criteria += 1
            passed_this_criterion = False
            condition_type = criterion.get("condition_type")
            
            try:
                if condition_type == "range_numeric":
                    min_val = float(criterion.get("value1", 0))
                    max_val = float(criterion.get("value2", float('inf')))
                    num_ext_val = float(extracted_value_str) if extracted_value_str else None
                    if num_ext_val is not None and min_val <= num_ext_val <= max_val: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' not in range [{min_val}-{max_val}]"
                
                elif condition_type == "contains_any":
                    keywords = [kw.strip().lower() for kw in criterion.get("keywords", []) if kw.strip()]
                    if any(kw in extracted_value_lower for kw in keywords): passed_this_criterion = True
                    else: reason = f"Did not contain any of: {', '.join(criterion.get('keywords',[]))}"
                
                elif condition_type == "equals_string":
                    target_str = str(criterion.get("value1", "")).strip().lower()
                    if extracted_value_lower == target_str: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' not equal to '{criterion.get('value1', '')}'"
                
                elif condition_type == "min_numeric":
                    min_val = float(criterion.get("value1", 0))
                    num_ext_val = float(extracted_value_str) if extracted_value_str else None
                    if num_ext_val is not None and num_ext_val >= min_val: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' below minimum of {min_val}"

                elif condition_type == "max_numeric":
                    max_val = float(criterion.get("value1", float('inf')))
                    num_ext_val = float(extracted_value_str) if extracted_value_str else None
                    if num_ext_val is not None and num_ext_val <= max_val: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' above maximum of {max_val}"

                elif condition_type == "is_one_of":
                    options = [opt.strip().lower() for opt in criterion.get("options", []) if opt.strip()]
                    if extracted_value_lower in options: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' not one of: {', '.join(criterion.get('options',[]))}"
                
                else: # Unknown condition type
                    reason = f"Unknown condition type '{condition_type}'"
                    # total_active_criteria -= 1 # Or handle as a fail

            except ValueError: # If float conversion fails for numeric types
                reason = f"Extracted value '{extracted_value_str}' is not a valid number for numeric comparison."
            except Exception as e:
                reason = f"Error during validation: {str(e)}"

            if passed_this_criterion:
                passed_criteria_count += 1
            else:
                # MODIFICATION HERE: Include extracted_value in the details
                failed_criteria_details[f"{field_label} (Rule: {condition_type})"] = {
                    "reason": reason,
                    "extracted_value": extracted_value_str # The value that was checked
                }

    if total_active_criteria == 0:
        # Return empty dict for failed_details if no active criteria
        return 100.0, {}, "No active ATS criteria to validate against." 
        
    accuracy = (passed_criteria_count / total_active_criteria) * 100
    return accuracy, failed_criteria_details, None


# --- Routes ---
@app.route('/', methods=['GET'])
def landing_page():
    if 'logged_in' in session and session.get('logged_in'):
        role = session.get('role')
        if role == 'admin': return redirect(url_for('admin_dashboard'))
        if session.get('accessible_tabs_info'): return redirect(url_for('app_dashboard'))
    return render_template('Template1.html') # The new Protomatic landing page

def _load_user_session_data(user_email, user_data):
    session['logged_in'] = True
    session['user_email'] = user_email
    session['username'] = user_data.get("username", user_email)
    session['role'] = user_data.get('role', 'user') # Should always have a role from USERS_DB

    accessible_tabs_info = {}
    user_role = session['role']

    if user_role == "po_verifier" or user_role == "sub_admin":
        tab_config = AVAILABLE_TABS["po"]
        accessible_tabs_info["po"] = {
            "id": "po", "name": tab_config["name"], "icon": tab_config["icon"],
            # "allowed_field_labels": PO_FIELDS_FOR_USER_EXTRACTION # For display if needed by template
        }
    if user_role == "ats_verifier" or user_role == "sub_admin":
        tab_config = AVAILABLE_TABS["ats"]
        accessible_tabs_info["ats"] = {
            "id": "ats", "name": tab_config["name"], "icon": tab_config["icon"],
            # "allowed_field_labels": ATS_FIELDS_FOR_USER_EXTRACTION # For display
        }
    session['accessible_tabs_info'] = accessible_tabs_info

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'logged_in' in session and session['logged_in']:
        if session.get('role') == 'admin': return redirect(url_for('admin_dashboard'))
        if session.get('accessible_tabs_info'): return redirect(url_for('app_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user_data = USERS_DB.get(email)

        if user_data and user_data['role'] != 'admin' and check_password_hash(user_data['hashed_password'], password):
            _load_user_session_data(email, user_data) # Sets up accessible_tabs_info
            if not session.get('accessible_tabs_info'):
                flash('Login successful, but no modules assigned for your role.', 'warning')
                session.clear(); return redirect(url_for('login_page'))
            flash(f'Login successful! Welcome {session["username"]}.', 'success')
            return redirect(url_for('app_dashboard'))
        else:
            flash('Invalid credentials or not a user account.', 'danger')
    return render_template('login.html')

@app.route('/admin', methods=['GET', 'POST']) # Admin login
def admin_login_page():
    if 'logged_in' in session and session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user_data = USERS_DB.get(email)
        if user_data and user_data['role'] == 'admin' and check_password_hash(user_data['hashed_password'], password):
            session['logged_in'] = True
            session['user_email'] = email
            session['username'] = user_data.get("username", "Admin")
            session['role'] = "admin"
            flash('Admin login successful!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid admin credentials.', 'danger')
    return render_template('admin_login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('landing_page'))

# --- User Dashboard ---
@app.route('/app', methods=['GET', 'POST'])
@login_required
def app_dashboard():
    # Store results in session for potential use by download_report
    # This is a simple way, for larger data or production, consider alternatives
    if 'processed_results_for_report' not in session:
        session['processed_results_for_report'] = {}

    results = {} # For current request display
    accessible_tabs_info = session.get('accessible_tabs_info', {})
    
    # Determine active tab
    default_tab_id = next(iter(accessible_tabs_info)) if accessible_tabs_info else None
    active_tab_id = request.form.get('active_tab_id', request.args.get('active_tab_id', default_tab_id))
    if active_tab_id not in accessible_tabs_info and default_tab_id:
        active_tab_id = default_tab_id
    elif not active_tab_id and not default_tab_id: # Should not happen due to @login_required
        flash("Error: No accessible tabs and no default.", "danger")
        return redirect(url_for('logout'))


    if request.method == 'POST':
        upload_type = request.form.get('upload_type')
        active_tab_id = upload_type # Make the tab of upload active

        if upload_type not in accessible_tabs_info:
             flash(f"Access denied for {upload_type.upper()} processing.", "danger")
             return redirect(url_for('app_dashboard', active_tab_id=active_tab_id))

        if 'document' not in request.files:
            flash('No file part in request.', 'warning')
        else:
            doc_files = request.files.getlist('document')
            if not doc_files or all(f.filename == '' for f in doc_files):
                flash('No files selected.', 'warning')
            else:
                processed_count = 0
                for doc_file in doc_files:
                    filename = doc_file.filename
                    if not filename: continue
                    
                    # Use a more unique temp filename to avoid conflicts if multiple users upload same name
                    temp_filename_base = secrets.token_hex(8) + "_" + filename 
                    temp_file_path = os.path.join(TEMP_FOLDER, temp_filename_base)
                    file_results_for_template = {} # Data to pass to template for this file
                    
                    try:
                        doc_file.save(temp_file_path)
                        extracted_text = extract_text_from_file(temp_file_path, filename)
                        file_results_for_template["extracted_text"] = extracted_text

                        if extracted_text and not extracted_text.lower().startswith("error"):
                            if upload_type == 'po':
                                structured_data = extract_structured_data(extracted_text, PO_FIELDS_FOR_USER_EXTRACTION, upload_type='po')
                                file_results_for_template["structured_data"] = structured_data
                                
                                po_number_val = structured_data.get("PO Number")
                                db_record = None; comp_error = "PO Number not extracted."
                                if po_number_val:
                                    db_record = get_po_db_record(po_number_val)
                                    if db_record:
                                        accuracy, mismatched, comp_error = compare_po_data(structured_data, db_record, PO_KEY_COMPARISON_FIELDS)
                                        file_results_for_template["accuracy"] = accuracy
                                        file_results_for_template["mismatched_fields"] = mismatched
                                        file_results_for_template["db_record_for_display"] = {k: db_record.get(k) for k in PO_KEY_COMPARISON_FIELDS if k in db_record}
                                        file_results_for_template["compared_fields_list"] = PO_KEY_COMPARISON_FIELDS
                                    else: comp_error = f"PO Number '{po_number_val}' not found in database."
                                file_results_for_template["comparison_error"] = comp_error if not db_record or comp_error else None


                            elif upload_type == 'ats':
                                structured_data = extract_structured_data(extracted_text, ATS_FIELDS_FOR_USER_EXTRACTION, upload_type='ats')
                                file_results_for_template["structured_data"] = structured_data
                                RESUMES_DATA_DB[filename] = structured_data # Store extracted data

                                accuracy, failed_details, validation_error_msg = validate_ats_data(structured_data, ATS_VALIDATION_CRITERIA_DB)
                                file_results_for_template["accuracy"] = accuracy
                                file_results_for_template["mismatched_fields"] = failed_details # These are failed criteria
                                file_results_for_template["comparison_error"] = validation_error_msg # Overall message
                                file_results_for_template["compared_fields_list"] = [ # List active criteria fields
                                    f_label for f_label, crits in ATS_VALIDATION_CRITERIA_DB.items() if any(c.get("is_active") for c in crits)
                                ]
                            
                            processed_count += 1
                        else:
                            file_results_for_template["error"] = extracted_text or "Text extraction failed."
                        
                        results[filename] = file_results_for_template
                        session['processed_results_for_report'][filename] = file_results_for_template # Save for report
                        session.modified = True 

                    except Exception as e:
                        app.logger.error(f"Error processing {filename}: {e}", exc_info=True)
                        results[filename] = {"error": f"Server error during processing: {str(e)}"}
                    finally:
                        if os.path.exists(temp_file_path):
                            try: os.remove(temp_file_path)
                            except OSError as e_os: app.logger.error(f"Error removing temp file {temp_file_path}: {e_os}")
                
                if processed_count == 0 and doc_files: flash('Could not process any of the selected files.', 'warning')
                elif processed_count > 0: flash(f'Successfully processed {processed_count} file(s).', 'info')

    current_tab_display_name = accessible_tabs_info.get(active_tab_id, {}).get("name", "Dashboard")
    
    return render_template('app_dashboard.html',
                           results=results,
                           accessible_tabs_info=accessible_tabs_info,
                           active_tab_id=active_tab_id,
                           current_tab_display_name=current_tab_display_name,
                           PO_FIELDS_FOR_USER_EXTRACTION=PO_FIELDS_FOR_USER_EXTRACTION, # Pass to template
                           ATS_FIELDS_FOR_USER_EXTRACTION=ATS_FIELDS_FOR_USER_EXTRACTION # Pass to template
                           )

# --- Placeholder for PDF Report Download ---
@app.route('/download_report/<doc_type>/<filename_key>')
@login_required
def download_report(doc_type, filename_key):
    # Retrieve results from session (this is a simple approach)
    processed_results = session.get('processed_results_for_report', {}).get(filename_key)

    if not processed_results:
        flash(f"Report data for '{filename_key}' not found or expired.", "warning")
        return redirect(url_for('app_dashboard'))

    if 'error' in processed_results:
        return Response(f"Cannot generate report for '{filename_key}' due to processing error: {processed_results['error']}", mimetype='text/plain')

    # For now, return JSON data as a "report"
    # In a real app, you'd generate a PDF here using ReportLab, WeasyPrint, etc.
    report_content = f"--- Report for {filename_key} ({doc_type.upper()}) ---\n\n"
    report_content += "Extracted Text:\n" + processed_results.get('extracted_text', 'N/A')[:500] + "...\n\n"
    report_content += "Structured Data:\n" + json.dumps(processed_results.get('structured_data', {}), indent=2) + "\n\n"
    
    if doc_type == 'po':
        report_content += "PO Comparison:\n"
        report_content += f"  Accuracy: {processed_results.get('accuracy', 0):.2f}%\n"
        if processed_results.get('db_record_for_display'):
            report_content += "  DB Record Compared Against:\n" + json.dumps(processed_results.get('db_record_for_display'), indent=2) + "\n"
        if processed_results.get('mismatched_fields'):
            report_content += "  Mismatches:\n" + json.dumps(processed_results.get('mismatched_fields'), indent=2) + "\n"
    elif doc_type == 'ats':
        report_content += "ATS Validation:\n"
        report_content += f"  Accuracy (Criteria Met): {processed_results.get('accuracy', 0):.2f}%\n"
        if processed_results.get('mismatched_fields'): # Failed criteria details
            report_content += "  Failed Criteria:\n" + json.dumps(processed_results.get('mismatched_fields'), indent=2) + "\n"

    response = Response(report_content, mimetype='text/plain')
    response.headers['Content-Disposition'] = f'attachment; filename="report_{doc_type}_{filename_key.replace(" ", "_")}.txt"'
    return response


# --- Admin Routes & APIs ---
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    admin_configurable_fields_for_template = {
        "po": MASTER_FIELD_DEFINITIONS.get("po", []),
        "ats": MASTER_FIELD_DEFINITIONS.get("ats", [])
    }
    return render_template(
        'admin_dashboard.html',
        admin_configurable_fields_data = admin_configurable_fields_for_template
    )

# --- Admin User Management APIs ---
@app.route('/api/admin/manage_users', methods=['GET'])
@admin_required
def api_manage_get_users():
    user_list = [{"email": email, "username": data.get("username"), "role": data.get("role")}
                 for email, data in USERS_DB.items() if data.get("role") != 'admin']
    return jsonify(user_list)

@app.route('/api/admin/manage_users', methods=['POST'])
@admin_required
def api_manage_add_user():
    data = request.json
    email = data.get('email')
    username = data.get('username')
    password = data.get('password')
    role = data.get('role') 

    if not all([email, username, password, role]): return jsonify({"error": "All fields required"}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email): return jsonify({"error": "Invalid email"}), 400
    if email in USERS_DB: return jsonify({"error": "Email already exists"}), 409
    valid_roles = ["sub_admin", "po_verifier", "ats_verifier"]
    if role not in valid_roles: return jsonify({"error": f"Invalid role. Must be one of: {', '.join(valid_roles)}"}), 400

    USERS_DB[email] = {"username": username, "hashed_password": generate_password_hash(password), "role": role}
    print(f"Admin added user {username} ({email}) with role {role}")
    return jsonify({"message": "User created successfully", "user": {"email": email, "username": username, "role": role}}), 201

@app.route('/api/admin/manage_users/<string:user_email>', methods=['PUT'])
@admin_required
def api_manage_update_user(user_email):
    if user_email not in USERS_DB or USERS_DB[user_email].get("role") == 'admin':
        return jsonify({"error": "User not found or cannot modify admin"}), 404
    data = request.json; updated = False
    if 'username' in data and data['username'].strip():
        USERS_DB[user_email]['username'] = data['username'].strip(); updated = True
    if 'role' in data and data['role'] in ["sub_admin", "po_verifier", "ats_verifier"]:
        USERS_DB[user_email]['role'] = data['role']; updated = True
    elif 'role' in data: return jsonify({"error": "Invalid role for update"}), 400
    if 'password' in data and data['password']: # Admin can reset/change password
        USERS_DB[user_email]['hashed_password'] = generate_password_hash(data['password'])
        flash(f"Password for {user_email} has been changed by admin.", "info") # Flash for admin maybe?
        updated = True
        print(f"Admin changed password for {user_email}")

    if not updated: return jsonify({"message": "No changes provided."}), 200
    return jsonify({"message": "User updated.", "user": {"email": user_email, **USERS_DB[user_email]}}), 200

@app.route('/api/admin/manage_users/<string:user_email>', methods=['DELETE'])
@admin_required
def api_manage_delete_user(user_email):
    if user_email not in USERS_DB or USERS_DB[user_email].get("role") == 'admin':
        return jsonify({"error": "User not found or cannot delete admin"}), 404
    del USERS_DB[user_email]
    return jsonify({"message": "User deleted successfully"}), 200

# --- Admin API for PO Data Entry & Count ---
@app.route('/api/admin/po_database_entry/<string:po_number>', methods=['DELETE'])
@admin_required
def api_delete_po_database_entry(po_number):
    if "po" in dummy_database and po_number in dummy_database["po"]:
        del dummy_database["po"][po_number]
        # Optional: Re-index or clean up if po_number was a numeric key that needs reordering
        # For string keys, direct deletion is fine.
        return jsonify({"message": f"PO entry '{po_number}' deleted successfully."}), 200
    return jsonify({"error": f"PO entry '{po_number}' not found."}), 404

@app.route('/api/admin/po_database_entries', methods=['GET'])
@admin_required
def api_get_all_po_database_entries():
    # Return a list of PO entries. Each entry is a dict.
    # For easier processing in JS, convert the dict of dicts to a list of dicts,
    # including the PO number as a key in each dict.
    po_entries_list = []
    for po_number, data in dummy_database.get("po", {}).items():
        entry = {"PO Number": po_number} # Ensure PO Number is part of the entry
        entry.update(data)
        po_entries_list.append(entry)
    return jsonify(po_entries_list)

@app.route('/api/admin/po_database_entry', methods=['POST'])
@admin_required
def api_add_po_data_entry():
    data = request.json 
    po_number = data.get("PO Number") 
    if not po_number: return jsonify({"error": "PO Number is required."}), 400
    
    valid_po_labels = {f["label"] for f in MASTER_FIELD_DEFINITIONS.get("po", [])}
    entry_data = {key: value for key, value in data.items() if key in valid_po_labels and value.strip()}
    if not entry_data.get("PO Number"): return jsonify({"error": "PO Number field data missing or not configured."}), 400

    dummy_database["po"][po_number] = entry_data 
    return jsonify({"message": f"PO data for '{po_number}' saved."}), 200

@app.route('/api/admin/po_database_count', methods=['GET'])
@admin_required
def api_get_po_database_count():
    return jsonify({"count": len(dummy_database.get("po", {}))})


# --- Admin APIs for ATS Criteria Management & Count ---
@app.route('/api/admin/ats_criteria', methods=['GET'])
@admin_required
def api_get_ats_criteria():
    return jsonify(ATS_VALIDATION_CRITERIA_DB)

@app.route('/api/admin/ats_criteria', methods=['POST'])
@admin_required
def api_add_ats_criterion():
    data = request.json
    field_label = data.get("field_label")
    condition_type = data.get("condition_type")
    if not field_label or not condition_type: return jsonify({"error": "Field and condition type required."}), 400
    if not any(f["label"] == field_label for f in MASTER_FIELD_DEFINITIONS.get("ats",[])): return jsonify({"error": f"Invalid ATS field: {field_label}"}), 400
    
    # Basic validation for values based on condition_type
    # This can be expanded significantly
    if condition_type in ["range_numeric", "min_numeric", "max_numeric"]:
        if data.get("value1") is None: return jsonify({"error": "Value1 required for numeric conditions."}), 400
        try: float(data.get("value1"))
        except (ValueError, TypeError): return jsonify({"error": "Value1 must be a number for numeric conditions."}), 400
        if condition_type == "range_numeric":
            if data.get("value2") is None: return jsonify({"error": "Value2 required for numeric range."}), 400
            try: float(data.get("value2"))
            except (ValueError, TypeError): return jsonify({"error": "Value2 must be a number for numeric range."}), 400
    elif condition_type == "contains_any":
        if not data.get("keywords") or not isinstance(data.get("keywords"), list): return jsonify({"error": "Keywords (list) required for 'contains any'."}), 400
    elif condition_type == "is_one_of":
        if not data.get("options") or not isinstance(data.get("options"), list): return jsonify({"error": "Options (list) required for 'is one of'."}), 400


    new_criterion_id = str(uuid.uuid4())
    new_criterion = {"id": new_criterion_id, "is_active": data.get("is_active", True), **data}
    
    if field_label not in ATS_VALIDATION_CRITERIA_DB: ATS_VALIDATION_CRITERIA_DB[field_label] = []
    ATS_VALIDATION_CRITERIA_DB[field_label].append(new_criterion)
    return jsonify({"message": "ATS criterion added.", "criterion": new_criterion}), 201

@app.route('/api/admin/ats_criteria/<string:field_label_url>/<string:criterion_id>', methods=['PUT'])
@admin_required
def api_update_ats_criterion(field_label_url, criterion_id): # field_label_url from path
    data = request.json
    field_label_payload = data.get("field_label") # field_label from payload

    # Ensure field_label consistency or handle if it can be changed
    if field_label_payload and field_label_payload != field_label_url:
        # Logic to move criterion if field_label changes - complex, for now disallow or handle carefully
        return jsonify({"error": "Changing field_label via update is not directly supported. Delete and re-add."}), 400
    
    target_field_label = field_label_url # Use the one from URL as primary key

    if target_field_label not in ATS_VALIDATION_CRITERIA_DB: return jsonify({"error": f"No criteria for field: {target_field_label}"}), 404
    
    criterion_found = False
    for i, crit in enumerate(ATS_VALIDATION_CRITERIA_DB[target_field_label]):
        if crit["id"] == criterion_id:
            # Update all fields except 'id' and potentially 'field_label' if it's immutable
            update_data = {k:v for k,v in data.items() if k not in ['id', 'field_label']}
            ATS_VALIDATION_CRITERIA_DB[target_field_label][i].update(update_data)
            criterion_found = True
            updated_criterion = ATS_VALIDATION_CRITERIA_DB[target_field_label][i]
            break
            
    if not criterion_found: return jsonify({"error": "Criterion ID not found."}), 404
    return jsonify({"message": "ATS criterion updated.", "criterion": updated_criterion}), 200


@app.route('/api/admin/ats_criteria/<string:field_label>/<string:criterion_id>', methods=['DELETE'])
@admin_required
def api_delete_ats_criterion(field_label, criterion_id):
    if field_label not in ATS_VALIDATION_CRITERIA_DB: return jsonify({"error": f"No criteria for field: {field_label}"}), 404
    
    initial_len = len(ATS_VALIDATION_CRITERIA_DB[field_label])
    ATS_VALIDATION_CRITERIA_DB[field_label] = [c for c in ATS_VALIDATION_CRITERIA_DB[field_label] if c["id"] != criterion_id]
    
    if len(ATS_VALIDATION_CRITERIA_DB[field_label]) == initial_len: return jsonify({"error": "Criterion ID not found."}), 404
    if not ATS_VALIDATION_CRITERIA_DB[field_label]: del ATS_VALIDATION_CRITERIA_DB[field_label] # Clean up empty list
    return jsonify({"message": "ATS criterion deleted."}), 200

@app.route('/api/admin/ats_criteria_count', methods=['GET'])
@admin_required
def api_get_ats_criteria_count():
    active_count = 0
    for field_label, criteria_list in ATS_VALIDATION_CRITERIA_DB.items():
        for criterion in criteria_list:
            if criterion.get("is_active", False):
                active_count += 1
    return jsonify({"active_count": active_count, "total_count": sum(len(v) for v in ATS_VALIDATION_CRITERIA_DB.values())})


# --- Main Execution ---
if __name__ == '__main__':
    print("-" * 60)
    print("Flask App Starting...")
    print(f"SECRET_KEY: {'Set by ENV or Default' if app.secret_key else 'Using Default Temporary'}")
    print(f"OCR_SPACE_API_KEY: {'Set by ENV' if OCR_SPACE_API_KEY != 'K87955728688957' else 'Using Placeholder K87...'}")
    print("Available User Tabs:", list(AVAILABLE_TABS.keys()))
    print("WARNING: All data (Users, PO DB, ATS Criteria, Resumes) stored IN-MEMORY and will be lost on restart.")
    print("-" * 60)
    app.run(debug=True, host='0.0.0.0', port=5000)