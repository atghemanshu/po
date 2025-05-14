import os
import re
import secrets
import string
import random
from functools import wraps
import json
import uuid # Still needed for ATS criteria IDs if you generate them in Python

from dotenv import load_dotenv # For loading .env file

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response
)
# REMOVE: from flask_sqlalchemy import SQLAlchemy
# REMOVE: from sqlalchemy_serializer import SerializerMixin
from supabase import create_client, Client # ADDED for Supabase

from werkzeug.security import generate_password_hash, check_password_hash # Still needed
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument
import requests
# REMOVE: import datetime (unless used elsewhere, Supabase handles timestamps)

load_dotenv() # Call this very early

# --- App Setup ---
TEMP_FOLDER = os.path.join(os.path.dirname(__file__), 'temp')
os.makedirs(TEMP_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("SECRET_KEY environment variable not set!")
app.config['SESSION_TYPE'] = 'filesystem'

# --- Supabase Client Initialization ---
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY") # This should be the service_role key

if not supabase_url or not supabase_key:
    raise RuntimeError("SUPABASE_URL or SUPABASE_KEY environment variables not set!")
supabase: Client = create_client(supabase_url, supabase_key)
# --- End Supabase Client Initialization ---

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
        
         # app.py

def get_po_db_record(po_number_value_param):
    if not po_number_value_param:
        return None
    print(f"DEBUG: get_po_db_record querying for: '{po_number_value_param}'") # DEBUG
    try:
        # Fetch from Supabase, columns are po_number, vendor, phone, total, order_date, vendor_name
        response = supabase.table('admin_po_database_entries').select(
            "po_number, vendor, phone, total, order_date, vendor_name" # Select specific dedicated columns
        ).eq('po_number', po_number_value_param).single().execute()
        
        if response.data:
            db_entry_row = response.data # e.g., {'po_number': '81100', 'vendor': 'S101334', ...}
            
            # Map DB column names back to the "Display Label" keys
            frontend_formatted_record = {
                "PO Number": db_entry_row.get("po_number"),
                "Vendor": db_entry_row.get("vendor"),
                "Phone": db_entry_row.get("phone"),
                "Total": db_entry_row.get("total"),
                # Convert date object to string if it's not already. Supabase might return string.
                "Order Date": str(db_entry_row.get("order_date")) if db_entry_row.get("order_date") else None,
                "Vendor Name": db_entry_row.get("vendor_name")
            }
            return frontend_formatted_record
        return None # PO not found
    except Exception as e:
        if "No rows found" in str(e) or (hasattr(e, 'code') and e.code == 'PGRST116'): # PostgREST code for single() not finding row
            app.logger.info(f"PO record {po_number_value_param} not found in Supabase.")
        else:
            app.logger.error(f"Error fetching PO {po_number_value_param} from Supabase: {e}")
        return None

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
         
         # app.py

def validate_ats_data(extracted_data): # No longer takes criteria_db as argument
    criteria_from_db_grouped = {}
    try:
        response = supabase.table('admin_ats_criteria').select("*").eq('is_active', True).execute()
        if response.data:
            for criterion_db_obj in response.data:
                field_label = criterion_db_obj.get('field_label')
                if field_label not in criteria_from_db_grouped:
                    criteria_from_db_grouped[field_label] = []
                
                # The object from DB directly contains field_label, condition_type, is_active, condition_values
                # This is what the rest of the original validate_ats_data logic can work with.
                # We need to make sure 'keywords', 'value1', etc. are accessible if the original logic expected them flat.
                # The previous version of validate_ats_data already expected to get these from criterion.get("value1") etc.
                # The criterion from DB now has these nested in 'condition_values'.
                
                # Let's make a dictionary for this criterion that mirrors the old structure
                # where condition-specific values were at the top level of the criterion dict.
                # OR, modify the validation logic below to look inside `criterion['condition_values']`.
                # For now, let's try to adapt the criterion object.
                
                current_criterion_for_validation = {
                    "id": criterion_db_obj.get("id"),
                    "field_label": field_label,
                    "condition_type": criterion_db_obj.get("condition_type"),
                    "is_active": criterion_db_obj.get("is_active") # Should always be true here
                }
                if criterion_db_obj.get("condition_values"): # If it's not None
                    current_criterion_for_validation.update(criterion_db_obj.get("condition_values"))
                
                criteria_from_db_grouped[field_label].append(current_criterion_for_validation)
                
    except Exception as e:
        app.logger.error(f"Error fetching ATS criteria for validation: {e}")
        # If criteria can't be fetched, perhaps treat as if no criteria are active or raise error
        return 100.0, {}, f"Error fetching validation criteria: {str(e)}"

    if not criteria_from_db_grouped:
        return 100.0, {}, "No active ATS criteria defined by admin."

    total_active_criteria = 0
    passed_criteria_count = 0
    failed_criteria_details = {}

    for field_label, criteria_list_for_field in criteria_from_db_grouped.items():
        extracted_value_str = str(extracted_data.get(field_label, "")).strip()
        extracted_value_lower = extracted_value_str.lower()

        for criterion in criteria_list_for_field: # criterion is now the adapted dict
            # No need to check is_active again as we filtered in query, but doesn't hurt
            if not criterion.get("is_active", False): continue 
            
            total_active_criteria += 1
            passed_this_criterion = False
            condition_type = criterion.get("condition_type")
            reason = "Condition not met." # Default reason

            try:
                # The rest of your validation logic using criterion.get("value1"), criterion.get("keywords") etc.
                # from the previous version of validate_ats_data should now work because we've
                # merged `condition_values` into the `current_criterion_for_validation` dict.
                if condition_type == "range_numeric":
                    min_val = float(criterion.get("value1", 0))
                    max_val = float(criterion.get("value2", float('inf')))
                    num_ext_val = float(extracted_value_str) if extracted_value_str and extracted_value_str.replace('.', '', 1).isdigit() else None
                    if num_ext_val is not None and min_val <= num_ext_val <= max_val: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' not in range [{min_val}-{max_val}]"
                
                elif condition_type == "contains_any":
                    keywords = [str(kw).strip().lower() for kw in criterion.get("keywords", []) if str(kw).strip()]
                    if any(kw in extracted_value_lower for kw in keywords): passed_this_criterion = True
                    else: reason = f"Did not contain any of: {', '.join(criterion.get('keywords',[]))}"
                
                elif condition_type == "equals_string":
                    target_str = str(criterion.get("value1", "")).strip().lower()
                    if extracted_value_lower == target_str: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' not equal to '{criterion.get('value1', '')}'"
                
                elif condition_type == "min_numeric":
                    min_val = float(criterion.get("value1", 0))
                    num_ext_val = float(extracted_value_str) if extracted_value_str and extracted_value_str.replace('.', '', 1).isdigit() else None
                    if num_ext_val is not None and num_ext_val >= min_val: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' below minimum of {min_val}"

                elif condition_type == "max_numeric":
                    max_val = float(criterion.get("value1", float('inf')))
                    num_ext_val = float(extracted_value_str) if extracted_value_str and extracted_value_str.replace('.', '', 1).isdigit() else None
                    if num_ext_val is not None and num_ext_val <= max_val: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' above maximum of {max_val}"

                elif condition_type == "is_one_of":
                    options = [str(opt).strip().lower() for opt in criterion.get("options", []) if str(opt).strip()]
                    if extracted_value_lower in options: passed_this_criterion = True
                    else: reason = f"Value '{extracted_value_str}' not one of: {', '.join(criterion.get('options',[]))}"
                
                else: 
                    reason = f"Unknown or unhandled condition type '{condition_type}'"
            
            except ValueError: 
                reason = f"Extracted value '{extracted_value_str}' for field '{field_label}' is not a valid number for numeric comparison with rule '{condition_type}'."
            except Exception as e_val:
                app.logger.error(f"Error during specific ATS criterion validation: {e_val} for criterion {criterion}")
                reason = f"Error during validation rule: {e_val}"

            if passed_this_criterion:
                passed_criteria_count += 1
            else:
                failed_criteria_details[f"{field_label} (Rule: {condition_type})"] = {
                    "reason": reason,
                    "extracted_value": extracted_value_str
                }
    
    if total_active_criteria == 0 and not criteria_from_db_grouped : # No active criteria were even loaded
         return 100.0, {}, "No active ATS criteria defined by admin."
    elif total_active_criteria == 0 and criteria_from_db_grouped: # Criteria objects exist but none were active (should not happen if query filters by is_active=True)
        return 100.0, {}, "No criteria were active to validate against (though some may be defined)."


    accuracy = (passed_criteria_count / total_active_criteria) * 100 if total_active_criteria > 0 else 100.0
    return accuracy, failed_criteria_details, None # None for error message if processing occurred

# --- Routes ---
@app.route('/', methods=['GET'])
def landing_page():
    if 'logged_in' in session and session.get('logged_in'):
        role = session.get('role')
        if role == 'admin': return redirect(url_for('admin_dashboard'))
        if session.get('accessible_tabs_info'): return redirect(url_for('app_dashboard'))
    return render_template('Template1.html') # The new Protomatic landing page

# app.py

def _load_user_session_data(user_email_from_db, user_data_from_db):
    """
    Helper to load user data into session after successful login for non-admin users.
    user_data_from_db is a dictionary like:
    {'email': 'user@example.com', 'username': 'testuser', 'role': 'po_verifier', ... (no hashed_password)}
    """
    session['logged_in'] = True
    session['user_email'] = user_email_from_db # or user_data_from_db.get('email')
    session['username'] = user_data_from_db.get("username", user_email_from_db) # Use username from DB
    session['role'] = user_data_from_db.get('role') # Role from DB

    if not session['role'] or session['role'] == 'admin':
        # This function should not be called for admins or users without a valid role
        app.logger.warning(f"Attempted to load session for invalid role or admin: {session['role']}")
        session.clear() # Clear potentially problematic session
        return

    accessible_tabs_info = {}
    user_role = session['role']

    # Logic based on roles to populate accessible_tabs_info
    if user_role == "po_verifier" or user_role == "sub_admin":
        if "po" in AVAILABLE_TABS: # Check if tab is defined
            tab_config = AVAILABLE_TABS["po"]
            accessible_tabs_info["po"] = {
                "id": "po", 
                "name": tab_config["name"], 
                "icon": tab_config["icon"]
            }
    if user_role == "ats_verifier" or user_role == "sub_admin":
        if "ats" in AVAILABLE_TABS: # Check if tab is defined
            tab_config = AVAILABLE_TABS["ats"]
            accessible_tabs_info["ats"] = {
                "id": "ats", 
                "name": tab_config["name"], 
                "icon": tab_config["icon"]
            }
    
    if not accessible_tabs_info:
        app.logger.info(f"User {user_email_from_db} with role {user_role} has no accessible tabs configured.")
        # This case is also handled in login_page after calling this function.
        
    session['accessible_tabs_info'] = accessible_tabs_info
    
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'logged_in' in session and session.get('logged_in'):
        if session.get('role') == 'admin':
            return redirect(url_for('admin_dashboard'))
        if session.get('accessible_tabs_info'): # Check if user already has session with tabs
            return redirect(url_for('app_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            # Query Supabase 'users' table
            response = supabase.table('users').select("email, username, hashed_password, role").eq('email', email).execute()
            
            if response.data:
                user_data_from_db = response.data[0] # .execute() returns a list, get the first item
                
                if user_data_from_db.get('role') != 'admin' and \
                   check_password_hash(user_data_from_db.get('hashed_password'), password):
                    
                    # Pass the fetched user data to _load_user_session_data
                    # Note: _load_user_session_data will need to handle this dictionary format
                    _load_user_session_data(user_data_from_db.get('email'), user_data_from_db) 
                                        
                    if not session.get('accessible_tabs_info'): # Check after _load_user_session_data
                        flash('Login successful, but no modules assigned for your role.', 'warning')
                        # Potentially clear session if no access, or redirect differently
                        # For now, redirecting to login might be confusing, maybe logout or a dedicated no-access page
                        session.clear() 
                        return redirect(url_for('login_page'))
                    
                    flash(f'Login successful! Welcome {session.get("username", "User")}.', 'success')
                    return redirect(url_for('app_dashboard'))
                else:
                    flash('Invalid credentials or not an authorized user account.', 'danger')
            else: # No user found with that email
                flash('Invalid credentials.', 'danger')
        except Exception as e:
            app.logger.error(f"Error during user login for {email}: {e}")
            flash('An error occurred during login. Please try again.', 'danger')
            
    return render_template('login.html')

# app.py

@app.route('/admin', methods=['GET', 'POST']) # Admin login
def admin_login_page():
    if 'logged_in' in session and session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            # Query Supabase 'users' table
            # This is CORRECT for Supabase
            response = supabase.table('users').select("email, username, hashed_password, role").eq('email', email).eq('role', 'admin').execute()
            
            if response.data:
                admin_data_from_db = response.data[0]
                
                if check_password_hash(admin_data_from_db.get('hashed_password'), password):
                    session['logged_in'] = True
                    session['user_email'] = admin_data_from_db.get('email')
                    session['username'] = admin_data_from_db.get('username', 'Admin') 
                    session['role'] = 'admin'
                    session.pop('accessible_tabs_info', None) 
                    flash('Admin login successful!', 'success')
                    return redirect(url_for('admin_dashboard'))
                else:
                    flash('Invalid admin credentials (password mismatch).', 'danger') # More specific
            else: # No admin user found with that email and role 'admin'
                flash('Invalid admin credentials (user not found or not admin).', 'danger') # More specific
        except Exception as e:
            app.logger.error(f"Error during admin login for {email}: {e}") # Log the actual error
            # For connection errors like getaddrinfo, this exception block will be hit.
            if "[Errno 11001]" in str(e): # Check if it's the getaddrinfo error
                 flash('Network error: Could not connect to the authentication service. Please check your internet connection and Supabase URL.', 'danger')
            else:
                 flash('An error occurred during admin login. Please try again later.', 'danger')
            
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

                                db_record_data_for_display = None # Initialize
                                accuracy_val = 0 # Initialize
                                mismatched_data = {} # Initialize
                                comparison_fields_list_for_template = [] # Initialize
                                comp_error_msg = "PO Number not extracted from document." # Default error

                                if po_number_val:
                                    po_number_val = po_number_val.strip() 
                                    print(f"DEBUG: Extracted PO Number for DB lookup: '{po_number_val}' (Type: {type(po_number_val)})") # DEBUG
                                    # get_po_db_record should now fetch from Supabase and return a dict with "Label" keys
                                    po_data_from_db = get_po_db_record(po_number_val) 
                                    
                                    if po_data_from_db:
                                        # Perform comparison using ONLY the PO_KEY_COMPARISON_FIELDS
                                        # compare_po_data expects extracted_data with "Label" keys and db_record with "Label" keys
                                        accuracy_val, mismatched_data, comp_error_from_compare = compare_po_data(
                                            structured_data,       # Extracted data with "Label" keys
                                            po_data_from_db,       # DB data already formatted with "Label" keys by get_po_db_record
                                            PO_KEY_COMPARISON_FIELDS # List of "Label" keys to compare
                                        )
                                        
                                        # For display, show only the key comparison fields from the DB record data
                                        db_record_data_for_display = {
                                            k: po_data_from_db.get(k) for k in PO_KEY_COMPARISON_FIELDS if k in po_data_from_db
                                        }
                                        comparison_fields_list_for_template = PO_KEY_COMPARISON_FIELDS
                                        comp_error_msg = comp_error_from_compare # Overwrite default if comparison happened
                                    else:
                                        comp_error_msg = f"PO Number '{po_number_val}' not found in database."
                                
                                # Populate results for the template
                                file_results_for_template["accuracy"] = accuracy_val
                                file_results_for_template["mismatched_fields"] = mismatched_data
                                file_results_for_template["db_record_for_display"] = db_record_data_for_display
                                file_results_for_template["compared_fields_list"] = comparison_fields_list_for_template
                                
                                # Only set comparison_error if there was truly an error preventing comparison
                                # If po_number_val was extracted but not found in DB, that's a valid state for comp_error_msg
                                # If po_number_val was not extracted, that's also a valid state for comp_error_msg
                                if comp_error_msg and (not po_number_val or (po_number_val and not po_data_from_db and "not found in database" in comp_error_msg)):
                                    file_results_for_template["comparison_error"] = comp_error_msg
                                elif comp_error_msg and comp_error_msg != "PO Number not extracted from document.": # If error came from compare_po_data
                                    file_results_for_template["comparison_error"] = comp_error_msg
                                else: # No significant error, or PO Number just wasn't extracted (already handled by comp_error_msg default)
                                    file_results_for_template["comparison_error"] = None if accuracy_val > 0 or not po_number_val else comp_error_msg

                            elif upload_type == 'ats':
                                structured_data = extract_structured_data(extracted_text, ATS_FIELDS_FOR_USER_EXTRACTION, upload_type='ats')
                                file_results_for_template["structured_data"] = structured_data
                                
                                # --- Save extracted resume data to Supabase ---
                                try:
                                    resume_entry_payload = {
                                       "original_filename": filename,
                                        "sr_no": structured_data.get("Sr no."),
                                        "name": structured_data.get("Name"),
                                        "gender": structured_data.get("Gender"),
                                        "phone": structured_data.get("Phone"),
                                        "city": structured_data.get("City"),
                                        "age": structured_data.get("Age"),
                                        "country": structured_data.get("Country"),
                                        "address": structured_data.get("Address"),
                                        "email": structured_data.get("Email"),
                                        "skills": structured_data.get("Skills"),
                                        "salary": structured_data.get("Salary"),
                                        "percentage": structured_data.get("Percentage")
                                        # user_id: session.get('user_db_id') # If you implement user linking
                                    }
                                    # Ensure keys in structured_data are valid for the dedicated columns if you chose that schema
                                    # If using dedicated columns for extracted_resume_data:
                                    # resume_entry_payload = {
                                    #     "original_filename": filename,
                                    #     "sr_no": structured_data.get("Sr no."),
                                    #     "name": structured_data.get("Name"),
                                    #     # ... map all other ATS_FIELDS_FOR_USER_EXTRACTION to their DB column names
                                    # }

                                    insert_response = supabase.table('extracted_resume_data').insert(resume_entry_payload).execute()
                                    if insert_response.data:
                                        app.logger.info(f"Successfully saved extracted data for resume: {filename}")
                                    # else: # Handle error if insert_response.error exists
                                        # app.logger.error(f"Error saving resume data for {filename} to Supabase: {getattr(insert_response, 'error', 'Unknown error')}")
                                        # file_results_for_template["error"] = "Could not save extracted resume data to database." 
                                        # results[filename] = file_results_for_template
                                        # continue # Skip to next file if saving failed
                                except Exception as db_save_error:
                                    app.logger.error(f"Exception saving resume data for {filename} to Supabase: {db_save_error}")
                                    file_results_for_template["error"] = "Database error while saving resume data."
                                    results[filename] = file_results_for_template
                                    continue # Skip to next file

                                # --- Corrected call to validate_ats_data ---
                                # It now fetches criteria from Supabase itself
                                accuracy, failed_details, validation_error_msg = validate_ats_data(structured_data) 
                                
                                file_results_for_template["accuracy"] = accuracy
                                file_results_for_template["mismatched_fields"] = failed_details # These are failed criteria
                                file_results_for_template["comparison_error"] = validation_error_msg # Overall message like "No active criteria"
                                # --- Prepare data for the accuracy chart ---
                                acc_calc_val = accuracy if accuracy is not None else 0.0
                                file_results_for_template["acc_calc_val"] = acc_calc_val # Raw accuracy for JS
                                file_results_for_template["acc_display_val"] = f"{acc_calc_val:.1f}" # Formatted for display

                                chart_radius = 40 # SVG units
                                chart_stroke_width = 10 # SVG units
                                chart_circumference = 2 * 3.1415926535 * chart_radius
                                chart_offset = chart_circumference * (1 - (acc_calc_val / 100))

                                file_results_for_template["chart_radius"] = chart_radius
                                file_results_for_template["chart_stroke_width"] = chart_stroke_width
                                file_results_for_template["chart_circumference"] = chart_circumference
                                file_results_for_template["chart_offset"] = chart_offset

                                chart_color = "#dc3545" # Default bad (red)
                                chart_text_class = "accuracy-bad"
                                chart_description = "Low"
                                if acc_calc_val >= 99.9:
                                    chart_color = "#198754" # Good (green)
                                    chart_text_class = "accuracy-good"
                                    chart_description = "Excellent"
                                elif acc_calc_val >= 80:
                                    chart_color = "#198754" # Good (green)
                                    chart_text_class = "accuracy-good"
                                    chart_description = "Good"
                                elif acc_calc_val >= 60:
                                    chart_color = "#ffc107" # Moderate (yellow)
                                    chart_text_class = "accuracy-moderate"
                                    chart_description = "Moderate"

                                file_results_for_template["chart_color"] = chart_color
                                file_results_for_template["chart_text_class"] = chart_text_class
                                file_results_for_template["chart_description"] = chart_description
                                # --- End chart data preparation ---
                                
                                 

                                # Get list of fields for which active criteria exist, for template display
                                active_criteria_fields = []
                                try:
                                    criteria_response = supabase.table('admin_ats_criteria').select("field_label").eq('is_active', True).execute()
                                    if criteria_response.data:
                                        active_criteria_fields = list(set([c['field_label'] for c in criteria_response.data])) # Unique field labels
                                except Exception as crit_e:
                                    app.logger.error(f"Could not fetch active criteria field list for display: {crit_e}")

                                file_results_for_template["compared_fields_list"] = active_criteria_fields
                            
                        else:
                            file_results_for_template["error"] = extracted_text or "Text extraction failed."
                        print(f"DEBUG: file_results_for_template for {filename}: {json.dumps(file_results_for_template, indent=2)}")

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
    try:
        # Select all columns needed for display, excluding admins
        # The 'users' table in Supabase has 'email', 'username', 'role'
        response = supabase.table('users').select("email, username, role").neq('role', 'admin').execute()
        
        if response.data:
            user_list = response.data # response.data is already a list of dictionaries
            return jsonify(user_list)
        else:
            # Handle case where there are no non-admin users or an error occurred
            # if response.error: # Supabase client might populate an error attribute
            #     app.logger.error(f"Supabase error fetching users: {response.error.message}")
            #     return jsonify({"error": f"Database error: {response.error.message}"}), 500
            return jsonify([]) # Return empty list if no non-admin users
            
    except Exception as e:
        app.logger.error(f"Exception in api_manage_get_users: {e}")
        return jsonify({"error": "An unexpected error occurred while fetching users."}), 500

     # app.py
# Ensure generate_password_hash is imported: from werkzeug.security import generate_password_hash

@app.route('/api/admin/manage_users', methods=['POST'])
@admin_required
def api_manage_add_user():
    data = request.json
    email = data.get('email')
    username = data.get('username')
    password = data.get('password')
    role = data.get('role') 

    # --- Basic Validation ---
    if not all([email, username, password, role]):
        return jsonify({"error": "All fields (email, username, password, role) are required"}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email): # Basic email format check
        return jsonify({"error": "Invalid email format"}), 400
    valid_roles = ["sub_admin", "po_verifier", "ats_verifier"]
    if role not in valid_roles:
        return jsonify({"error": f"Invalid role specified. Must be one of: {', '.join(valid_roles)}"}), 400
    if role == 'admin': # Prevent creating another admin this way
        return jsonify({"error": "Cannot create admin users through this API."}), 400
    # --- End Validation ---

    try:
        # Check if user already exists by email
        existing_user_response = supabase.table('users').select("id").eq('email', email).execute()
        if existing_user_response.data:
            return jsonify({"error": "User with this email already exists"}), 409

        hashed_pwd = generate_password_hash(password)
        user_to_insert = {
            "email": email, 
            "username": username, 
            "hashed_password": hashed_pwd, 
            "role": role
        }
        insert_response = supabase.table('users').insert(user_to_insert).execute()

        if insert_response.data:
            created_user_data = insert_response.data[0]
            # Return only non-sensitive info
            user_for_response = {
                "email": created_user_data.get('email'),
                "username": created_user_data.get('username'),
                "role": created_user_data.get('role')
            }
            return jsonify({"message": "User created successfully", "user": user_for_response}), 201
        else:
            # This 'else' might indicate an issue not caught by an exception, e.g., RLS preventing insert
            # but with service_role key, RLS should be bypassed.
            # More likely, if there's an error, execute() would raise it or response.error would be set.
            # error_msg = "Failed to create user in database."
            # if hasattr(insert_response, 'error') and insert_response.error:
            #     error_msg += f" DB Error: {insert_response.error.message}"
            # app.logger.error(f"User creation failed for {email} with response: {insert_response}")
            return jsonify({"error": "Failed to create user due to a database issue."}), 500
            
    except Exception as e:
        app.logger.error(f"Exception in api_manage_add_user for {email}: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500

@app.route('/api/admin/manage_users/<string:user_email_param>', methods=['PUT'])
@admin_required
def api_manage_update_user(user_email_param):
    # user_email_param is the original email from the URL, used to identify the user.
    data = request.json # Get the update data from the request body

    try:
        # Step 1: Fetch the user by their email to get their ID and current role.
        # It's safer to perform updates using the immutable primary key (id) if possible.
        user_check_response = supabase.table('users').select("id, role, email, username").eq('email', user_email_param).single().execute()
        # .single() will return one record or raise an error if not exactly one is found.
        # If no user is found, response.data will be None or an error might be in response.error
        # However, the supabase-py client often raises an exception directly if .single() finds no match.

        if not user_check_response.data: # Should be caught by exception from .single() if no user
            return jsonify({"error": "User not found"}), 404
        
        user_to_update = user_check_response.data 
        user_to_update_id = user_to_update['id']
        
        if user_to_update['role'] == 'admin':
            return jsonify({"error": "Cannot modify the main admin account through this API"}), 403

        updates_payload = {} # Dictionary to hold only the fields that are actually being changed

        # Check for username update
        if 'username' in data and data['username'] is not None:
            new_username = data['username'].strip()
            if new_username and new_username != user_to_update.get('username'):
                updates_payload['username'] = new_username

        # Check for role update
        if 'role' in data and data['role'] is not None:
            new_role = data['role'].strip()
            valid_roles = ["sub_admin", "po_verifier", "ats_verifier"]
            if new_role in valid_roles:
                if new_role != user_to_update.get('role'):
                    updates_payload['role'] = new_role
            else:
                return jsonify({"error": f"Invalid role specified. Must be one of: {', '.join(valid_roles)}"}), 400
        
        # Check for password update (admin can change/reset password)
        if 'password' in data and data['password']: # If a new password is provided
            updates_payload['hashed_password'] = generate_password_hash(data['password'])
            app.logger.info(f"Admin is updating password for user {user_email_param}")

        if not updates_payload:
            return jsonify({"message": "No valid changes provided for the user."}), 200 # Or 304 Not Modified

        # Perform the update against the user's ID
        update_response = supabase.table('users').update(updates_payload).eq('id', user_to_update_id).execute()

        if update_response.data:
            updated_user_info = update_response.data[0]
            # Prepare a clean response object, excluding sensitive info like password hash
            user_for_response = {
                "email": updated_user_info.get('email'), 
                "username": updated_user_info.get('username'),
                "role": updated_user_info.get('role')
            }
            return jsonify({"message": "User updated successfully", "user": user_for_response}), 200
        else:
            # This path might be taken if the update affected 0 rows but didn't error,
            # or if there was a PostgREST error not caught as an exception.
            error_detail = "Unknown database error during update."
            if hasattr(update_response, 'error') and update_response.error:
                error_detail = update_response.error.message
                app.logger.error(f"Supabase error updating user {user_email_param}: {error_detail} | Details: {update_response.error.details}")

            return jsonify({"error": f"Failed to update user: {error_detail}"}), 500

    except Exception as e:
        # This will catch errors from .single() if user not found, or other unexpected errors.
        # Example: if .single() finds no user, it might raise a PostgrestAPIError or similar.
        # We can check the type of e if needed for more specific error messages.
        if "No rows found" in str(e) or (hasattr(e, 'code') and e.code == 'PGRST116'): # PGRST116 is PostgREST code for "requested range not satisfiable"
            app.logger.warning(f"Attempt to update non-existent user: {user_email_param}")
            return jsonify({"error": "User not found"}), 404
        
        app.logger.error(f"Exception in api_manage_update_user for {user_email_param}: {type(e).__name__} - {e}")
        return jsonify({"error": f"An unexpected server error occurred."}), 500   
@app.route('/api/admin/manage_users/<string:user_email_param>', methods=['DELETE'])
@admin_required
def api_manage_delete_user(user_email_param):
    try:
        # Fetch the user to ensure they exist and are not admin before deleting
        user_check_response = supabase.table('users').select("id, role").eq('email', user_email_param).execute()

        if not user_check_response.data:
            return jsonify({"error": "User not found"}), 404
        
        user_to_delete_role = user_check_response.data[0]['role']
        user_to_delete_id = user_check_response.data[0]['id']

        if user_to_delete_role == 'admin':
            return jsonify({"error": "Cannot delete admin account"}), 403

        # Perform delete against the user's ID for safety
        delete_response = supabase.table('users').delete().eq('id', user_to_delete_id).execute()

        if delete_response.data: # Successful delete usually returns the deleted record(s)
            print(f"Admin deleted user {user_email_param}")
            return jsonify({"message": "User deleted successfully"}), 200
        else:
            # error_msg = "Failed to delete user."
            # if hasattr(delete_response, 'error') and delete_response.error:
            #     error_msg += f" DB Error: {delete_response.error.message}"
            # app.logger.error(f"User deletion failed for {user_email_param} with response: {delete_response}")
            return jsonify({"error": "Failed to delete user due to a database issue."}), 500

    except Exception as e:
        app.logger.error(f"Exception in api_manage_delete_user for {user_email_param}: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500
# --- Admin API for PO Data Entry & Count ---
            # app.py

@app.route('/api/admin/po_database_entry/<string:po_number_param>', methods=['DELETE'])
@admin_required
def api_delete_po_database_entry(po_number_param):
    if not po_number_param:
        return jsonify({"error": "PO Number is required for deletion."}), 400
        
    try:
        # Check if the entry exists before attempting to delete (optional but good for specific error message)
        # check_response = supabase.table('admin_po_database_entries').select("po_number").eq('po_number', po_number_param).execute()
        # if not check_response.data:
        #     return jsonify({"error": f"PO entry '{po_number_param}' not found."}), 404

        # Perform delete
        delete_response = supabase.table('admin_po_database_entries').delete().eq('po_number', po_number_param).execute()

        # delete() typically returns the deleted records in .data if successful and rows were affected.
        # If no rows were affected (e.g., po_number didn't exist), .data might be empty.
        if delete_response.data: 
            return jsonify({"message": f"PO entry '{po_number_param}' deleted successfully."}), 200
        else:
            # This could mean the PO number didn't exist, or another issue occurred.
            # Check if there was an error reported by PostgREST
            # if hasattr(delete_response, 'error') and delete_response.error:
            #     app.logger.error(f"Supabase error deleting PO {po_number_param}: {delete_response.error.message}")
            #     return jsonify({"error": f"Database error: {delete_response.error.message}"}), 500
            # If no error, but no data, it means the po_number was not found.
            return jsonify({"error": f"PO entry '{po_number_param}' not found or already deleted."}), 404
            
    except Exception as e:
        app.logger.error(f"Exception in api_delete_po_database_entry for {po_number_param}: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500

@app.route('/api/admin/po_database_entries', methods=['GET'])
@admin_required
def api_get_all_po_database_entries():
    try:
        # Select all relevant columns.
        # The column names in the DB are 'po_number', 'vendor', 'phone', etc.
        # The frontend JS (admin_dashboard.html) expects keys like "PO Number", "Vendor".
        # So we need to map them back or select them with aliases if Supabase client supports it easily,
        # or do the mapping in Python. Let's do mapping in Python for clarity.
        
        response = supabase.table('admin_po_database_entries').select("*").order('po_number').execute() # Fetch all POs

        if response.data:
            po_entries_for_frontend = []
            for entry_from_db in response.data:
                # Map database column names back to the "Label" names expected by JavaScript/frontend
                # This assumes your MASTER_FIELD_DEFINITIONS["po"] labels are the keys your JS uses for table headers.
                frontend_entry = {
                    "PO Number": entry_from_db.get("po_number"), # Primary key, always include
                    "Vendor": entry_from_db.get("vendor"),
                    "Phone": entry_from_db.get("phone"),
                    "Total": entry_from_db.get("total"),
                    "Order Date": str(entry_from_db.get("order_date")) if entry_from_db.get("order_date") else None, # Convert date to string
                    "Vendor Name": entry_from_db.get("vendor_name")
                    # Add any other fields that your admin_dashboard.html existing PO table might try to display
                }
                # Filter out None values if you don't want them in the JSON response for fields not set
                # frontend_entry = {k: v for k, v in frontend_entry.items() if v is not None}
                po_entries_for_frontend.append(frontend_entry)
            return jsonify(po_entries_for_frontend)
        else:
            # if response.error:
            #     app.logger.error(f"Supabase error fetching PO entries: {response.error.message}")
            #     return jsonify({"error": f"Database error: {response.error.message}"}), 500
            return jsonify([]) # No PO entries found
            
    except Exception as e:
        app.logger.error(f"Exception in api_get_all_po_database_entries: {e}")
        return jsonify({"error": "An unexpected error occurred while fetching PO entries."}), 500
    
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
    form_data = request.json  # e.g., {"PO Number": "value", "Vendor": "value", ... }
    
    po_number_val = form_data.get("PO Number")
    if not po_number_val or not po_number_val.strip():
        return jsonify({"error": "PO Number is required and cannot be empty."}), 400

    # Map frontend labels to database column names and prepare data for upsert
    # Only include fields that are actually provided in the form_data
    db_payload = {"po_number": po_number_val.strip()}

    # Map from MASTER_FIELD_DEFINITIONS labels to DB columns if they exist in form_data
    # Assumes DB columns are lowercase_with_underscore versions of labels or a direct mapping
    # This mapping needs to be robust.
    # For simplicity here, let's assume direct mapping for the known fields.
    # You might want a more structured mapping if labels and DB columns differ significantly.
    
    # Fields from MASTER_FIELD_DEFINITIONS["po"] that the admin can configure for entry:
    # {"id": "po_doc_number", "label": "PO Number"} -> po_number (PK, handled)
    # {"id": "po_doc_vendor_id", "label": "Vendor"} -> vendor
    # {"id": "po_doc_phone", "label": "Phone"}    -> phone
    # {"id": "po_doc_total", "label": "Total"}      -> total
    # {"id": "po_doc_order_date", "label": "Order Date"} -> order_date
    # Let's add "Vendor Name" as it's in PO_FIELDS_FOR_USER_EXTRACTION and useful
    
    if "Vendor" in form_data and form_data["Vendor"].strip():
        db_payload["vendor"] = form_data["Vendor"].strip()
    if "Vendor Name" in form_data and form_data["Vendor Name"].strip(): # Assuming admin can configure "Vendor Name"
        db_payload["vendor_name"] = form_data["Vendor Name"].strip()
    if "Phone" in form_data and form_data["Phone"].strip():
        db_payload["phone"] = form_data["Phone"].strip()
    if "Total" in form_data and form_data["Total"].strip():
        db_payload["total"] = form_data["Total"].strip()
    if "Order Date" in form_data and form_data["Order Date"].strip():
        # Ensure date is in YYYY-MM-DD for DATE column or handle conversion
        # For simplicity, assuming frontend sends it correctly or it's text.
        # If it's M/D/YYYY, you'd need to parse and reformat:
        # try:
        #     date_obj = datetime.strptime(form_data["Order Date"].strip(), '%m/%d/%Y')
        #     db_payload["order_date"] = date_obj.strftime('%Y-%m-%d')
        # except ValueError:
        #     return jsonify({"error": "Invalid Order Date format. Please use MM/DD/YYYY or ensure it's correctly formatted for the database."}), 400
        db_payload["order_date"] = form_data["Order Date"].strip() # Assuming it's YYYY-MM-DD or TEXT column handles it


    # If it's a new entry, `created_at` and `updated_at` will be set by DB defaults.
    # If it's an update, the trigger will handle `updated_at`.
    # If you need to explicitly set them for an insert if no default:
    # from datetime import datetime, timezone
    # if not editing: # simplified logic for new entry
    #    db_payload["created_at"] = datetime.now(timezone.utc).isoformat()
    # db_payload["updated_at"] = datetime.now(timezone.utc).isoformat()


    if len(db_payload) <= 1 and "po_number" in db_payload: # Only po_number was provided
        return jsonify({"error": "No data provided to save besides PO Number."}), 400

    try:
        # Upsert: inserts if po_number doesn't exist, updates if it does.
        # The primary key 'po_number' is used by Supabase to determine if it's an insert or update.
        response = supabase.table('admin_po_database_entries').upsert(db_payload).execute()

        if response.data:
            return jsonify({"message": f"PO data for '{po_number_val}' saved successfully."}), 200 # Or 201 if new
        else:
            # error_msg = f"Failed to save PO data for '{po_number_val}'."
            # if hasattr(response, 'error') and response.error:
            #     error_msg += f" DB Error: {response.error.message}"
            # app.logger.error(f"PO upsert failed for {po_number_val} with response: {response}")
            return jsonify({"error": "Failed to save PO data due to a database issue."}), 500
            
    except Exception as e:
        app.logger.error(f"Exception in api_add_po_data_entry for {po_number_val}: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500

# app.py

@app.route('/api/admin/po_database_count', methods=['GET'])
@admin_required
def api_get_po_database_count():
    try:
        # To get an exact count, PostgREST requires a specific header or function call.
        # The `select` with `count='exact'` is the standard way.
        response = supabase.table('admin_po_database_entries').select("po_number", count='exact').execute()
        
        # The count is available in response.count
        if response.count is not None:
            return jsonify({"count": response.count})
        else:
            # if response.error:
            #     app.logger.error(f"Supabase error counting PO entries: {response.error.message}")
            #     return jsonify({"error": f"Database error: {response.error.message}"}), 500
            app.logger.warning(f"Supabase PO count returned None, response: {response}")
            return jsonify({"count": 0}) # Default to 0 if count is None but no explicit error
            
    except Exception as e:
        app.logger.error(f"Exception in api_get_po_database_count: {e}")
        return jsonify({"error": "An unexpected error occurred while counting PO entries."}), 500

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
    is_active = data.get("is_active", True) # Default to True if not provided

    # --- Basic Validation ---
    if not field_label or not condition_type:
        return jsonify({"error": "Field label and condition type are required."}), 400
    if not any(f["label"] == field_label for f in MASTER_FIELD_DEFINITIONS.get("ats",[])):
        return jsonify({"error": f"Invalid ATS field label: {field_label}"}), 400
    # --- End Basic Validation ---

    condition_values_payload = {} # To store specific values for the JSONB column
    
    # --- Validation and Payload Preparation for condition_values ---
    if condition_type == "range_numeric":
        if data.get("value1") is None or data.get("value2") is None:
            return jsonify({"error": "value1 and value2 are required for numeric range."}), 400
        try:
            condition_values_payload["value1"] = float(data["value1"])
            condition_values_payload["value2"] = float(data["value2"])
        except ValueError:
            return jsonify({"error": "value1 and value2 must be numbers for numeric range."}), 400
    elif condition_type in ["min_numeric", "max_numeric", "equals_string"]:
        if data.get("value1") is None:
            return jsonify({"error": "value1 is required for this condition type."}), 400
        if condition_type in ["min_numeric", "max_numeric"]:
            try: condition_values_payload["value1"] = float(data["value1"])
            except ValueError: return jsonify({"error": "value1 must be a number."}), 400
        else: # equals_string
            condition_values_payload["value1"] = str(data["value1"])
    elif condition_type == "contains_any":
        keywords = data.get("keywords")
        if not keywords or not isinstance(keywords, list) or not all(isinstance(kw, str) for kw in keywords):
            return jsonify({"error": "Keywords (a list of strings) are required for 'contains_any'."}), 400
        condition_values_payload["keywords"] = keywords
    elif condition_type == "is_one_of":
        options = data.get("options")
        if not options or not isinstance(options, list) or not all(isinstance(opt, str) for opt in options):
            return jsonify({"error": "Options (a list of strings) are required for 'is_one_of'."}), 400
        condition_values_payload["options"] = options
    # --- End Validation and Payload Preparation ---

    # Data to insert into the Supabase table
    criterion_to_insert = {
        "id": str(uuid.uuid4()), # Generate new UUID
        "field_label": field_label,
        "condition_type": condition_type,
        "is_active": is_active,
        "condition_values": condition_values_payload if condition_values_payload else None # Store as JSONB
    }

    try:
        response = supabase.table('admin_ats_criteria').insert(criterion_to_insert).execute()

        if response.data:
            # Return the created criterion (it will include DB defaults like created_at)
            return jsonify({"message": "ATS criterion added successfully.", "criterion": response.data[0]}), 201
        else:
            # error_msg = "Failed to add ATS criterion."
            # if hasattr(response, 'error') and response.error:
            #     error_msg += f" DB Error: {response.error.message}"
            # app.logger.error(f"ATS criterion add failed with response: {response}")
            return jsonify({"error": "Failed to add ATS criterion due to a database issue."}), 500
            
    except Exception as e:
        app.logger.error(f"Exception in api_add_ats_criterion: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500 
    

@app.route('/api/admin/ats_criteria/<string:criterion_id_param>', methods=['PUT']) # field_label no longer needed in URL for update if ID is unique
@admin_required
def api_update_ats_criterion(criterion_id_param):
    data_from_request = request.json # This contains the FULL updated criterion from the form

    # field_label_from_payload = data_from_request.get("field_label") # This will be the original field_label (JS disables changing it)
    # condition_type_from_payload = data_from_request.get("condition_type")
    # is_active_from_payload = data_from_request.get("is_active", True)
    
    # --- Basic Validation (similar to add, but less strict on presence of all fields if only some are updated) ---
    # Ensure the criterion_id is what we expect
    if not criterion_id_param:
        return jsonify({"error": "Criterion ID is required for an update."}), 400
    
    # It's good practice to fetch the existing criterion to ensure it exists
    # try:
    #     existing_check = supabase.table('admin_ats_criteria').select("id").eq('id', criterion_id_param).single().execute()
    #     if not existing_check.data:
    #         return jsonify({"error": "Criterion not found for update."}), 404
    # except Exception: # Catches error if .single() finds no record
    #     return jsonify({"error": "Criterion not found for update (or multiple with same ID - should not happen)."}), 404
    
    # --- End Basic Validation ---

    updates_payload = {} # Build the payload for Supabase .update()
    
    # Fields that can be directly updated
    if "field_label" in data_from_request: updates_payload["field_label"] = data_from_request["field_label"] # Should not change as per JS
    if "condition_type" in data_from_request: updates_payload["condition_type"] = data_from_request["condition_type"]
    if "is_active" in data_from_request: updates_payload["is_active"] = data_from_request["is_active"]

    # Rebuild condition_values from the submitted form data
    condition_values_for_db = {}
    condition_type = data_from_request.get("condition_type", updates_payload.get("condition_type")) # Use new or existing type

    if condition_type == "range_numeric":
        if data_from_request.get("value1") is not None and data_from_request.get("value2") is not None: # Ensure values are present
            try:
                condition_values_for_db["value1"] = float(data_from_request["value1"])
                condition_values_for_db["value2"] = float(data_from_request["value2"])
            except ValueError: return jsonify({"error": "value1 and value2 must be numbers."}), 400
    elif condition_type in ["min_numeric", "max_numeric", "equals_string"]:
        if data_from_request.get("value1") is not None:
            if condition_type != "equals_string":
                try: condition_values_for_db["value1"] = float(data_from_request["value1"])
                except ValueError: return jsonify({"error": "value1 must be a number."}), 400
            else:
                condition_values_for_db["value1"] = str(data_from_request["value1"])
    elif condition_type == "contains_any":
        keywords = data_from_request.get("keywords")
        if keywords is not None and isinstance(keywords, list): condition_values_for_db["keywords"] = keywords
        elif keywords is not None: return jsonify({"error": "Keywords must be a list."}), 400
    elif condition_type == "is_one_of":
        options = data_from_request.get("options")
        if options is not None and isinstance(options, list): condition_values_for_db["options"] = options
        elif options is not None: return jsonify({"error": "Options must be a list."}), 400
    
    if condition_values_for_db: # Only add to payload if there's something to update/set
        updates_payload["condition_values"] = condition_values_for_db
    elif condition_type and not condition_values_for_db : # If type implies values but none provided, set to null or empty
        updates_payload["condition_values"] = None


    if not updates_payload:
        return jsonify({"message": "No valid changes provided for criterion."}), 200

    try:
        response = supabase.table('admin_ats_criteria').update(updates_payload).eq('id', criterion_id_param).execute()

        if response.data:
            return jsonify({"message": "ATS criterion updated successfully.", "criterion": response.data[0]}), 200
        else:
            # This could mean the ID wasn't found, or another issue.
            # error_msg = "Failed to update ATS criterion. It might not exist or an error occurred."
            # if hasattr(response, 'error') and response.error:
            #     error_msg = f"DB Error: {response.error.message}"
            # app.logger.error(f"ATS criterion update failed for ID {criterion_id_param} with response: {response}")
            return jsonify({"error": "Failed to update ATS criterion (possibly not found or no changes made)."}), 404 # 404 if not found
            
    except Exception as e:
        app.logger.error(f"Exception in api_update_ats_criterion for ID {criterion_id_param}: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500

@app.route('/api/admin/ats_criteria/<string:criterion_id_param>', methods=['DELETE']) # field_label no longer needed in URL for delete if ID is unique
@admin_required
def api_delete_ats_criterion(criterion_id_param):
    if not criterion_id_param:
         return jsonify({"error": "Criterion ID is required for deletion."}), 400
    try:
        # Optional: Check if it exists first for a more specific "not found"
        # check_response = supabase.table('admin_ats_criteria').select("id").eq('id', criterion_id_param).execute()
        # if not check_response.data:
        #     return jsonify({"error": f"Criterion with ID '{criterion_id_param}' not found."}), 404

        response = supabase.table('admin_ats_criteria').delete().eq('id', criterion_id_param).execute()

        if response.data: # Successful delete usually returns the deleted record(s)
            return jsonify({"message": "ATS criterion deleted successfully."}), 200
        else:
            # error_msg = f"Failed to delete criterion ID '{criterion_id_param}'. It might not exist."
            # if hasattr(response, 'error') and response.error:
            #     error_msg = f"DB Error: {response.error.message}"
            # app.logger.error(f"ATS criterion delete failed for ID {criterion_id_param} with response: {response}")
            return jsonify({"error": f"Failed to delete criterion (ID: {criterion_id_param}). It may not exist or an error occurred."}), 404 # 404 if not found
            
    except Exception as e:
        app.logger.error(f"Exception in api_delete_ats_criterion for ID {criterion_id_param}: {e}")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500
   
     # app.py

@app.route('/api/admin/ats_criteria_count', methods=['GET'])
@admin_required
def api_get_ats_criteria_count():
    try:
        # Count active criteria
        active_response = supabase.table('admin_ats_criteria').select("id", count='exact').eq('is_active', True).execute()
        active_count = active_response.count if active_response.count is not None else 0

        # Count total criteria (optional, if needed by frontend)
        # total_response = supabase.table('admin_ats_criteria').select("id", count='exact').execute()
        # total_count = total_response.count if total_response.count is not None else 0
        
        return jsonify({"active_count": active_count}) #, "total_count": total_count})
            
    except Exception as e:
        app.logger.error(f"Exception in api_get_ats_criteria_count: {e}")
        return jsonify({"error": "An unexpected error occurred while counting ATS criteria."}), 500
      

if __name__ == '__main__':
    # With Supabase, table creation is usually managed via the Supabase dashboard (SQL Editor / Table Editor)
    # or Supabase CLI migrations. We don't need db.create_all() from SQLAlchemy.
    # However, we can still seed the initial admin user here if it doesn't exist.

    print("Checking for initial admin user in Supabase...")
    try:
        admin_check_response = supabase.table('users').select("id").eq('email', 'admin@example.com').execute()
        
        if not admin_check_response.data: # If list is empty, user not found
            admin_default_password = os.environ.get('ADMIN_INITIAL_PASSWORD', 'admin@a123')
            admin_user_data = {
                "email": 'admin@example.com',
                "username": 'admin_user',
                "hashed_password": generate_password_hash(admin_default_password), # Make sure generate_password_hash is imported
                "role": 'admin'
            }
            insert_response = supabase.table('users').insert(admin_user_data).execute()
            if insert_response.data:
                print(f"Initial admin user 'admin@example.com' created successfully.")
            else:
                # Attempt to access error from PostgrestAPIResponse if available
                error_message = "Unknown error during admin user creation."
                if hasattr(insert_response, 'error') and insert_response.error:
                    error_message = insert_response.error.message
                elif hasattr(insert_response, 'status_code') and insert_response.status_code != 201:
                    error_message = f"Status: {insert_response.status_code}, Body: {getattr(insert_response, 'data', '')}"

                print(f"Failed to create initial admin user: {error_message}")
                # You might want to raise an error here or handle it more gracefully depending on strictness
        else:
            print("Admin user 'admin@example.com' already exists.")
    except Exception as e:
        print(f"CRITICAL ERROR during initial admin user check/creation with Supabase: {e}")
        print("Please ensure your Supabase connection is correct and the 'users' table exists with the correct schema.")
    
    print(f"Starting Flask app on http://0.0.0.0:5000")
    print(f"Attempting to connect to Supabase at URL: {supabase_url[:20]}... (URL partially hidden for brevity)") # Show part of URL
    app.run(debug=True, host='0.0.0.0', port=5000)