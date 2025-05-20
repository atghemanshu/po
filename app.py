import os
import re
import secrets
import string
import random
import platform 
import json
import uuid # Still needed for ATS criteria IDs if you generate them in Python

from functools import wraps
from dotenv import load_dotenv # For loading .env file

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response
)
# REMOVE: from flask_sqlalchemy import SQLAlchemy
# REMOVE: from sqlalchemy_serializer import SerializerMixin
from supabase import create_client, Client # ADDED for Supabase
from docx2pdf import convert # Ensure this is imported
from werkzeug.security import generate_password_hash, check_password_hash # Still needed
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument
import requests

from io import BytesIO
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.graphics.shapes import Drawing, Circle, Wedge, String # Using Wedge
from reportlab.graphics.charts.textlabels import Label
import math

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

# BLOCK to conditionally import pythoncom
if platform.system() == "Windows":
    try:
        import pythoncom
    except ImportError:
        # You can use app.logger here if your app is already configured
        print("WARNING: pywin32 (pythoncom) is not installed. DOCX to PDF conversion using MS Word COM automation will likely fail on Windows.")
        pythoncom = None # Define as None if import fails
else:
    pythoncom = None # Not needed on non-Windows systems
# END OF ADDED BLOCK

load_dotenv() # Call this very early

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
        # {"id": "ats_experience_years", "label": "Experience (Years)", "description": "Total years of professional experience"},
    ]
}

# --- Fields for User-Side Extraction (Fixed Sets) ---
PO_FIELDS_FOR_USER_EXTRACTION = ["PO Number", "Vendor", "Phone", "Total", "Order Date"]
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

# This function should ONLY do direct DOCX text extraction
def original_extract_text_from_docx(file_path): # Renamed to avoid confusion during refactor
    try:
        doc = DocxDocument(file_path) # Needs from docx import Document as DocxDocument
        full_text = [p.text for p in doc.paragraphs if p.text]
        return '\n'.join(full_text).strip() if full_text else "No text extracted from DOCX (direct method)."
    except Exception as e:
        print(f"Error during direct DOCX text extraction: {e}") # Use print or app.logger
        return f"Error extracting text directly from DOCX: {e}"
    
def extract_text_from_file(temp_file_path, filename): # temp_file_path is the path to the saved uploaded file
    _, file_extension = os.path.splitext(filename)
    file_extension = file_extension.lower()

    if file_extension in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff']:
        return ocr_image_via_api(temp_file_path)
    elif file_extension == '.pdf':
        return extract_text_from_pdf(temp_file_path)
    elif file_extension == '.docx':
        print(f"INFO: Processing DOCX file: {filename}")
        pdf_output_path = temp_file_path + ".pdf" # Create a new path for the PDF
        
        # --- MODIFICATION STARTS HERE ---
        com_initialized_this_call = False # Flag to ensure we only uninitialize if this specific call initialized COM
        try:
            if pythoncom and platform.system() == "Windows":
                try:
                    # COINIT_APARTMENTTHREADED is typically required for Office automation / UI components.
                    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
                    com_initialized_this_call = True
                    print(f"INFO: CoInitializeEx(COINIT_APARTMENTTHREADED) called for DOCX conversion of '{filename}'.")
                except pythoncom.com_error as e:
                    # This can happen if COM is already initialized, possibly with a different concurrency model.
                    # RPC_E_CHANGED_MODE (0x80010106) means it's already initialized differently.
                    # S_FALSE (1) means it's already initialized with the same mode (not an error pythoncom raises).
                    # If an exception occurs, we assume this call didn't establish initialization,
                    # so com_initialized_this_call remains False.
                    print(f"WARNING: CoInitializeEx for '{filename}' reported an issue (HRESULT: {e.hresult}, Details: {e.strerror}). Conversion will proceed; existing COM state might be incompatible or already suitable.")
                    # com_initialized_this_call remains False, so CoUninitialize won't be called by this scope's finally.

            print(f"INFO: Attempting to convert '{filename}' to PDF at '{pdf_output_path}'...")
            convert(temp_file_path, pdf_output_path) # temp_file_path is the input DOCX
            
            if os.path.exists(pdf_output_path):
                print(f"INFO: DOCX successfully converted to PDF. Extracting text from PDF: {pdf_output_path}")
                text_from_pdf = extract_text_from_pdf(pdf_output_path)
                try:
                    os.remove(pdf_output_path) # Clean up the temporary PDF
                    print(f"INFO: Temporary PDF '{pdf_output_path}' removed.")
                except OSError as e_os:
                    print(f"WARNING: Could not remove temporary PDF '{pdf_output_path}': {e_os}")
                return text_from_pdf
            else:
                print(f"WARNING: DOCX to PDF conversion for '{filename}' did not create output file. Falling back to direct DOCX extraction.")
                return original_extract_text_from_docx(temp_file_path) # Use original direct method

        except Exception as e_convert:
            print(f"ERROR: Failed to convert DOCX '{filename}' to PDF or process it: {e_convert}")
            print(f"INFO: Falling back to direct text extraction from DOCX for '{filename}'.")
            return original_extract_text_from_docx(temp_file_path) 
        finally:
            if com_initialized_this_call and pythoncom and platform.system() == "Windows":
                try:
                    pythoncom.CoUninitialize()
                    print(f"INFO: CoUninitialize called for DOCX conversion of '{filename}'.")
                except Exception as e_uninit:
                    print(f"ERROR: CoUninitialize failed for '{filename}': {e_uninit}")
        # --- MODIFICATION ENDS HERE ---
            
    return f"Error: Unsupported file format '{file_extension}'."

# --- Structured Data Extraction ---
def extract_structured_data(text, fields_to_extract_labels, upload_type=None):
    if not text or not isinstance(text, str) or not fields_to_extract_labels:
        # app.logger.warning(f"extract_structured_data called with invalid text or no fields. Text type: {type(text)}")
        print(f"Warning: extract_structured_data called with invalid text or no fields. Text type: {type(text)}")
        return {}
   
    data = {label: None for label in fields_to_extract_labels}
    lines = text.strip().split('\n') # <<< DEFINE 'lines' HERE, EARLY ON
    # We will fill `data` using specific logic first, then consider generic as a last pass if needed.
     
    for i, line_text in enumerate(lines): # Use the 'lines' defined above
        line_strip = line_text.strip()
        for field_label in fields_to_extract_labels:
            # ... (rest of your initial generic key-value logic using 'lines' and 'line_strip') ...
            if data[field_label] is not None: continue 

            pattern_label = re.escape(field_label)
            match = re.match(r"^\s*" + pattern_label + r"\s*[:\-]?\s*(.+)", line_strip, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if value: 
                    data[field_label] = value
                    break # Found for this field_label for this line
            
            # Simple check: if label is in line, try next line as value
            if field_label.lower() in line_strip.lower() and i + 1 < len(lines): # check 'lines' length
                next_line_strip = lines[i+1].strip() # use 'lines'
                is_next_line_a_label = False
                for other_label in fields_to_extract_labels:
                    if next_line_strip.lower().startswith(other_label.lower() + ":") or \
                       next_line_strip.lower().startswith(other_label.lower() + " "):
                        is_next_line_a_label = True
                        break
                if next_line_strip and not is_next_line_a_label:
                    if not data[field_label]: 
                        data[field_label] = next_line_strip

    if upload_type == 'po':
        # --- PO Specific Extraction - Prioritize these over generic ---
           # --- Apply type-specific extraction first ---
        normalized_text = re.sub(r'[ \t]+', ' ', text) 
        normalized_text = re.sub(r'\r\n?', '\n', normalized_text)
        print("---- NORMALIZED PO TEXT (first 500 chars) ----") # DEBUG LINE
        print(normalized_text[:500])                          # DEBUG LINE
        print("---------------------------------------------") # DEBUG LINE

        # PO Number: "PO Number: 81100"
        if "PO Number" in fields_to_extract_labels:
            m = re.search(r"\bPO Number\s*:\s*([A-Z0-9\-]+)", text, re.IGNORECASE)
            if m: data["PO Number"] = m.group(1).strip()

        # Order Date: "Order Date: 8/8/2024"
        if "Order Date" in fields_to_extract_labels:
            m = re.search(r"\bOrder Date\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
            if m: data["Order Date"] = m.group(1).strip()
        
        vendor_details_text = None
        # Regex: Start with "Vendor:", capture everything non-greedily until "Ship To:"
        # or until another major section that's clearly not part of vendor details.
        # We are looking for the block that contains the Vendor ID and Vendor Phone.
         # Vendor details block isolation
        vendor_details_search_text = normalized_text 
        # Look for the vendor block that contains "Vendor: S..." pattern
        # This tries to find the block specifically around the vendor ID and associated phone.
        # It looks for "Vendor: S<digits>" and captures text around it.
        vendor_id_block_match = re.search(
            # Looking for a block starting with "Vendor: S<digits>" and capturing until a common separator
            # Using non-greedy match .*?
            # The terminators are common section headers. Added \b to avoid partial word matches for terminators.
            r"(Vendor\s*:\s*S\d+.*?)(?:\bContact\s*:|\bShip Via\s*:|\bTerms\s*:|\bF\.O\.B\s*:|\bEmail\s*:|\Z)",
            normalized_text, 
            re.IGNORECASE | re.DOTALL # DOTALL allows . to match newlines, crucial for multi-line blocks
        )
        if vendor_id_block_match:
            vendor_details_search_text = vendor_id_block_match.group(1).strip() # Use the captured group and strip whitespace
            print(f"DEBUG PO: Isolated vendor ID block: ---{vendor_details_search_text[:150]}---")
        else:
            print("DEBUG PO: Could not isolate specific vendor ID block. Searching full text for Vendor ID/Phone.")
        
        # Vendor (ID)
        if "Vendor" in fields_to_extract_labels:
            m_vendor_id = re.search(r"\bVendor\s*:\s*(S\d+)\b", vendor_details_search_text, re.IGNORECASE)
            if m_vendor_id:
                data["Vendor"] = m_vendor_id.group(1).strip()
            else:
                print(f"DEBUG PO: Vendor ID (Sxxxxx) not found in designated search text: ---{vendor_details_search_text[:150]}---")


        # Phone (Vendor's Phone)
        if "Phone" in fields_to_extract_labels:
            m_phone = re.search(r"\bPhone\s*:\s*(\(?\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}(?:\s*x\d+)?)", vendor_details_search_text, re.IGNORECASE)
            if m_phone:
                phone_candidate = m_phone.group(1).strip()
                # Simple check: if the company phone is hardcoded or known, avoid it.
                # Your sample vendor phone is 734-426-3655
                if phone_candidate != "952-345-2244": # Avoid the company phone if it's specifically that
                    data["Phone"] = phone_candidate
                elif "734-426-3655" in vendor_details_search_text: # If company phone was matched, but vendor phone is also in block
                    data["Phone"] = "734-426-3655" 
                else: # If only company phone was in block, or ambiguity remains
                    print(f"DEBUG PO: Phone found in vendor block ('{phone_candidate}') might be ambiguous or company phone. Consider refining logic if incorrect.")
                    data["Phone"] = phone_candidate # Take what was found in the block for now
            else:
                print(f"DEBUG PO: Vendor Phone not found in designated search text: ---{vendor_details_search_text[:150]}---")
        
        # Total (Grand Total) - search in full normalized text
        if "Total" in fields_to_extract_labels:
            # Strictest first: "Total:" alone on a line with $ amount
            m_total = re.search(r"^\s*Total\s*:\s*(\$\s*\d{1,3}(?:,\d{3})*\.\d{2})\s*$", normalized_text, re.MULTILINE | re.IGNORECASE)
            if m_total:
                data["Total"] = m_total.group(1).strip()
            else:
                # Fallback: "Total: $amount" anywhere, but not followed by "Subtotal" or "Tax" on the same line.
                # (?!.*\b(?:Subtotal|Tax)\b) is a negative lookahead.
                m_total_fallback = re.search(r"\bTotal\s*:\s*(\$\s*\d{1,3}(?:,\d{3})*\.\d{2})\b(?!.*\b(?:Subtotal|Tax)\b)", normalized_text, re.IGNORECASE | re.MULTILINE)
                if m_total_fallback:
                     data["Total"] = m_total_fallback.group(1).strip()
                else:
                    print(f"DEBUG PO: Grand Total amount not found with specific regexes in normalized text.")
        
        print(f"DEBUG PO Extracted data (after PO specific): {data}")

    elif upload_type == 'ats':
       
    # Sr no.: S009
        if "Sr no." in fields_to_extract_labels and data["Sr no."] is None:
            m = re.search(r"Sr\s*no\.\s*:\s*(\S+)", text, re.IGNORECASE)
            if m: data["Sr no."] = m.group(1).strip()
        
        # Name: Olivia Miller
        if "Name" in fields_to_extract_labels and data["Name"] is None:
            m = re.search(r"Name\s*:\s*(.+)", text, re.IGNORECASE) # Captures rest of the line
            if m: data["Name"] = m.group(1).strip()
        
        # Gender: F
        if "Gender" in fields_to_extract_labels and data["Gender"] is None:
            m = re.search(r"Gender\s*:\s*([A-Za-z]+)", text, re.IGNORECASE)
            if m: data["Gender"] = m.group(1).strip().upper() # Standardize case

        # Phone: 8788019869
        if "Phone" in fields_to_extract_labels and data["Phone"] is None:
            # Look for "Phone:" followed by digits, allowing spaces, hyphens, parentheses
            m = re.search(r"Phone\s*:\s*([\d\s\-\(\)]+)", text, re.IGNORECASE)
            if m:
                phone_str = m.group(1).strip()
                data["Phone"] = re.sub(r"[^\d]", "", phone_str) # Clean to just digits

        # City: Sydney
        if "City" in fields_to_extract_labels and data["City"] is None:
            m = re.search(r"City\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["City"] = m.group(1).strip()

        # Age: 28
        if "Age" in fields_to_extract_labels and data["Age"] is None:
            m = re.search(r"Age\s*:\s*(\d+)", text, re.IGNORECASE)
            if m: data["Age"] = m.group(1).strip()

        # Country: Australia
        if "Country" in fields_to_extract_labels and data["Country"] is None:
            m = re.search(r"Country\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Country"] = m.group(1).strip()

        # Address: 42 Bondi Beach Road
        if "Address" in fields_to_extract_labels and data["Address"] is None:
            m = re.search(r"Address\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Address"] = m.group(1).strip()
        
        # Email: olivia.m@example.net
        if "Email" in fields_to_extract_labels and data["Email"] is None:
            m = re.search(r"Email\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text, re.IGNORECASE)
            if m: data["Email"] = m.group(1).strip()

        # Skills: Shopify, Java, React, Camunda
        if "Skills" in fields_to_extract_labels and data["Skills"] is None:
            m = re.search(r"Skills\s*:\s*(.+)", text, re.IGNORECASE)
            if m: data["Skills"] = m.group(1).strip()
            
        if "Salary" in fields_to_extract_labels and data["Salary"] is None:
            print("-" * 20)
            print("DEBUG: Attempting Salary Extraction")
            print(f"DEBUG: Relevant text for salary (first 500 chars of a relevant section if identifiable, or whole text if short):\n{text[:500]}") # Print relevant part of text
            
            salary_text_candidate = None
            # Try common keywords first
            m_salary_keyword = re.search(
                r"(?:salary|ctc|compensation|expected\s*salary|remuneration)\s*[:\-]?\s*([\$€£₹]?\s*\d[\d,\.]*\s*(?:k|lpa|lakhs|lacs)?)",
                text, re.IGNORECASE
            )
            if m_salary_keyword:
                salary_text_candidate = m_salary_keyword.group(1)
                print(f"DEBUG: Salary Keyword Match found: '{m_salary_keyword.group(0)}', captured: '{salary_text_candidate}'")
            else:
                print("DEBUG: Salary Keyword Match NOT found.")
                # ... (your fallback logic with prints) ...

            if salary_text_candidate:
                # ... (your cleaning logic with prints) ...
                print(f"DEBUG: Final extracted Salary: {data['Salary']}")
            else:
                print("DEBUG: No salary candidate found by any regex.")
            print("-" * 20)

        if "Percentage" in fields_to_extract_labels and data["Percentage"] is None:
            print("-" * 20)
            print("DEBUG: Attempting Percentage Extraction")
            print(f"DEBUG: Relevant text for percentage (first 500 chars of a relevant section if identifiable, or whole text if short):\n{text[:500]}") # Print relevant part of text

            percentage_val_str = None
            # Pattern 1: Keyword followed by number
            m_keyword_percent = re.search(
                r"(?:percentage|score|marks|grade|cgpa|academic.*?score)\s*[:\-]?\s*(\d+(?:\.\d+)?)(?:\s*(?:%|percent|percentage)|(?:\s*/\s*\d+))?",
                text, re.IGNORECASE
            )
            if m_keyword_percent:
                percentage_val_str = m_keyword_percent.group(1).strip()
                print(f"DEBUG: Percentage Keyword Match found: '{m_keyword_percent.group(0)}', captured: '{percentage_val_str}'")
            else:
                print("DEBUG: Percentage Keyword Match NOT found.")
            # ... (your fallback logic with prints) ...

            if percentage_val_str:
                # ... (your cleaning logic with prints) ...
                print(f"DEBUG: Final extracted Percentage: {data['Percentage']}")
            else:
                print("DEBUG: No percentage candidate found by any regex.")
            print("-" * 20)

        
        print(f"DEBUG ATS Extracted data: {data}")
        return data
    
    # --- Fallback Generic Key-Value Extraction (apply this only if specific type logic didn't fill everything) ---
    # This part should run *after* the PO/ATS specific logic if you want to fill remaining Nones
    # Be cautious as it can be overly greedy.
    lines_generic = text.strip().split('\n') # Re-split if text was modified or just use original lines
    for i, line_text_generic in enumerate(lines_generic):
        line_strip_generic = line_text_generic.strip()
        for field_label_generic in fields_to_extract_labels:
            if data[field_label_generic] is not None: continue # Skip if already filled by specific or previous generic

            pattern_label_gen = re.escape(field_label_generic)
            match_gen = re.match(r"^\s*" + pattern_label_gen + r"\s*[:\-]?\s*(.+)", line_strip_generic, re.IGNORECASE)
            if match_gen:
                value_gen = match_gen.group(1).strip()
                if value_gen: 
                    data[field_label_generic] = value_gen
                    break 
            
            if field_label_generic.lower() in line_strip_generic.lower() and i + 1 < len(lines_generic):
                next_line_strip_gen = lines_generic[i+1].strip()
                is_next_line_a_label_gen = False
                for other_label_gen in fields_to_extract_labels:
                    if next_line_strip_gen.lower().startswith(other_label_gen.lower() + ":") or \
                       next_line_strip_gen.lower().startswith(other_label_gen.lower() + " "):
                        is_next_line_a_label_gen = True
                        break
                if next_line_strip_gen and not is_next_line_a_label_gen:
                    if not data[field_label_generic]: 
                        data[field_label_generic] = next_line_strip_gen
    
    # app.logger.debug(f"Final Extracted data after all passes: {data}")
    print(f"DEBUG Final Extracted data after all passes: {data}")
    return data       
       

def get_po_db_record(po_number_value_param):
    
    if not po_number_value_param:
        return None
    try:
        # Fetch from Supabase. Assuming Supabase table `admin_po_database_entries`
        # has columns: po_number (PK), vendor, phone, total, order_date
        # These column names should match what api_add_po_data_entry saves.
        response = supabase.table('admin_po_database_entries').select(
            "po_number, vendor, phone, total, order_date" # Select specific dedicated columns
        ).eq('po_number', str(po_number_value_param).strip()).single().execute() # Ensure po_number_value_param is string and stripped
        
        if response.data:
            db_entry_row = response.data
            order_date_from_db = db_entry_row.get("order_date") # This will be 'YYYY-MM-DD' string from Supabase
            display_order_date = None
            if order_date_from_db:
                try:
                    # Assuming date_from_db is 'YYYY-MM-DD'
                    year, month, day = map(int, order_date_from_db.split('-'))
                    display_order_date = f"{month}/{day}/{year}"
                except ValueError:
                    display_order_date = order_date_from_db # Fallback to raw string if parsing fails

            frontend_formatted_record = {
                "PO Number": db_entry_row.get("po_number"),
                "Vendor": db_entry_row.get("vendor"),
                "Phone": db_entry_row.get("phone"),
                "Total": db_entry_row.get("total"),
                "Order Date": display_order_date, # Use formatted date
            }
            return frontend_formatted_record
        return None 
    except Exception as e:
        if "No rows found" in str(e) or (hasattr(e, 'code') and e.code == 'PGRST116'):
            app.logger.info(f"PO record {po_number_value_param} not found in Supabase.")
        else:
            app.logger.error(f"Error fetching PO {po_number_value_param} from Supabase: {e}")
        return None
    

def normalize_date_for_comparison(date_string):
    """
    Attempts to normalize various date string formats to 'YYYY-MM-DD'.
    Returns the original string if parsing fails, to allow string comparison as a fallback.
    """
    if not date_string or not isinstance(date_string, str):
        return date_string # Or None if you prefer to treat non-strings as not comparable

    date_str_stripped = date_string.strip()

    # Try M/D/YYYY (e.g., 8/8/2024, 08/08/2024)
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date_str_stripped):
        try:
            month, day, year = map(int, date_str_stripped.split('/'))
            return f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            return date_str_stripped # Fallback to original if parsing parts fails

    # Try YYYY-MM-DD (e.g., 2024-08-08, 2024-8-8)
    elif re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", date_str_stripped):
        try:
            year, month_str, day_str = date_str_stripped.split('-')
            return f"{int(year):04d}-{int(month_str):02d}-{int(day_str):02d}"
        except ValueError:
            return date_str_stripped # Fallback

    # Add other common formats if needed, e.g., DD-MM-YYYY, YYYY/MM/DD

    return date_str_stripped # Return original if no known format matched for robust string comparison

def compare_po_data(extracted_data, db_record, comparison_field_labels):
    if not db_record:
        return 0, {}, "PO Record not found in database for comparison."
    if not comparison_field_labels:
        return 0, {}, "No PO fields specified for comparison."

    matched_fields = 0
    mismatched = {}  # To store {label: {"db_value": X, "extracted_value": Y}}

    # Fields that are in comparison_field_labels AND actually present in the db_record
    # This is what we can meaningfully compare against.
    actual_comparable_fields_in_db = [label for label in comparison_field_labels if label in db_record]

    if not actual_comparable_fields_in_db:
        # If none of the fields we *want* to compare are even in the DB record,
        # accuracy is 0, and we can't list mismatches for these specific fields.
        return 0, {}, "None of the designated comparison fields were found in the database record."

    total_fields_to_compare_against_db = len(actual_comparable_fields_in_db)
    
    # Iterate through all fields designated for comparison by the system
    for label in comparison_field_labels:
        db_value_original = db_record.get(label) # Might be None if label not in actual_comparable_fields_in_db
        extracted_value_original = extracted_data.get(label)

        # --- Normalization ---
        db_str_normalized = None
        ext_str_normalized = None

        if label == "Order Date":
            # get_po_db_record might format DB date to M/D/YYYY for display.
            # Extracted date can also be M/D/YYYY. Normalize both to YYYY-MM-DD.
            if db_value_original is not None:
                db_str_normalized = normalize_date_for_comparison(str(db_value_original))
            if extracted_value_original is not None:
                ext_str_normalized = normalize_date_for_comparison(str(extracted_value_original))
        else: # For non-date fields
            if db_value_original is not None:
                db_str_normalized = str(db_value_original).strip().lower().replace('$', '').replace(',', '').replace(' ', '')
            if extracted_value_original is not None:
                ext_str_normalized = str(extracted_value_original).strip().lower().replace('$', '').replace(',', '').replace(' ', '')
        
        # --- Comparison Logic ---
        # We only formally compare if the field is one that the DB actually provided a value for from our comparison list
        if label in actual_comparable_fields_in_db:
            # Scenario 1: Both have non-empty values after normalization and they match
            if ext_str_normalized and db_str_normalized and ext_str_normalized == db_str_normalized:
                matched_fields += 1
            # Scenario 2: Both are effectively empty (None or normalized to empty string)
            elif (ext_str_normalized is None or ext_str_normalized == "") and \
                 (db_str_normalized is None or db_str_normalized == ""):
                matched_fields += 1 # Treat as a match if both are empty for a compared field
            # Scenario 3: They are different (and at least one is not effectively empty, or both non-empty but different)
            else:
                mismatched[label] = {
                    "db_value": db_value_original if db_value_original is not None else "(Not in DB / Empty)",
                    "extracted_value": extracted_value_original if extracted_value_original is not None else "(Not Extracted / Empty)"
                }
        elif extracted_value_original is not None:
            # Field was extracted, but it's not in `actual_comparable_fields_in_db` (meaning the DB didn't have this comparison field for this record).
            # This isn't a "mismatch" against the DB for accuracy calculation based on comparison_field_labels.
            # If you want to list these as "extracted but not in DB for comparison", you'd handle it differently.
            # For now, it just means it doesn't contribute to a match or mismatch for these keys.
            pass

    accuracy = (matched_fields / total_fields_to_compare_against_db) * 100 if total_fields_to_compare_against_db > 0 else 0
    
    # --- Determine Comparison Error Message ---
    comparison_error_message = None
    if not mismatched and accuracy < 99.9 and total_fields_to_compare_against_db > 0: 
        # If no specific mismatches were listed, but accuracy isn't perfect,
        # it implies some fields were considered "matched" because both were empty,
        # or one was empty and the other had data but wasn't listed as a mismatch by the logic above.
        # The refined mismatch logic should reduce this ambiguity.
        # A more precise message for this case might be:
        comparison_error_message = "Accuracy affected by fields where one source is empty/missing and the other has data, or data normalization differences."
    elif not actual_comparable_fields_in_db and comparison_field_labels: # Should be caught earlier
        comparison_error_message = "None of the designated comparison fields were present in the database record provided."

    return accuracy, mismatched, comparison_error_message

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
# app.py

@app.route('/app', methods=['GET', 'POST'])
@login_required
def app_dashboard():
    if 'processed_results_for_report' not in session:
        session['processed_results_for_report'] = {}

    results = {} 
    accessible_tabs_info = session.get('accessible_tabs_info', {})
    
    default_tab_id = next(iter(accessible_tabs_info)) if accessible_tabs_info else None
    active_tab_id = request.form.get('active_tab_id', request.args.get('active_tab_id', default_tab_id))
    if active_tab_id not in accessible_tabs_info and default_tab_id:
        active_tab_id = default_tab_id
    elif not active_tab_id and not default_tab_id:
        flash("Error: No accessible tabs and no default.", "danger")
        return redirect(url_for('logout'))

    if request.method == 'POST':
        upload_type = request.form.get('upload_type')
        active_tab_id = upload_type 

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
                    
                    temp_filename_base = secrets.token_hex(8) + "_" + filename 
                    temp_file_path = os.path.join(TEMP_FOLDER, temp_filename_base)
                    file_results_for_template = {"filename": filename} # Start with filename

                    try:
                        doc_file.save(temp_file_path)
                        extracted_text = extract_text_from_file(temp_file_path, filename)
                        file_results_for_template["extracted_text"] = extracted_text

                        if not extracted_text or extracted_text.lower().startswith("error"):
                            file_results_for_template["error"] = extracted_text or "Text extraction failed."
                            app.logger.warning(f"Text extraction failed for {filename}: {extracted_text}")
                            results[filename] = file_results_for_template
                            session['processed_results_for_report'][filename] = file_results_for_template
                            session.modified = True
                            continue # Move to the next file

                        # Initialize structured_data to an empty dict to avoid NoneType errors
                        structured_data = {}

                        if upload_type == 'po':
                            structured_data_result = extract_structured_data(extracted_text, PO_FIELDS_FOR_USER_EXTRACTION, upload_type='po')
                            if not isinstance(structured_data_result, dict):
                                app.logger.error(f"extract_structured_data (PO) did not return dict for {filename}, got: {type(structured_data_result)}. Using empty dict.")
                                structured_data = {}
                                file_results_for_template["error"] = "Internal error: Failed to structure PO data."
                            else:
                                structured_data = structured_data_result
                            
                            file_results_for_template["structured_data"] = structured_data
                            po_number_val = structured_data.get("PO Number")

                            db_record_data_for_display = None
                            accuracy_val = 0
                            mismatched_data = {}
                            comparison_fields_list_for_template = []
                            comp_error_msg = "PO Number not extracted from document."

                            if po_number_val:
                                po_number_val = po_number_val.strip()
                                app.logger.debug(f"Extracted PO Number for DB lookup: '{po_number_val}'")
                                po_data_from_db = get_po_db_record(po_number_val)

                                if po_data_from_db:
                                    app.logger.debug(f"Data found in DB for PO '{po_number_val}': {po_data_from_db}")
                                    accuracy_val, mismatched_data, comp_err_compare = compare_po_data(
                                        structured_data, po_data_from_db, PO_KEY_COMPARISON_FIELDS
                                    )
                                    db_record_data_for_display = {
                                        k: po_data_from_db.get(k) for k in PO_KEY_COMPARISON_FIELDS if k in po_data_from_db
                                    }
                                    comparison_fields_list_for_template = PO_KEY_COMPARISON_FIELDS
                                    comp_error_msg = comp_err_compare
                                    if comp_error_msg is None and accuracy_val < 100 and not mismatched_data:
                                        comp_error_msg = "Some compared fields might be empty in either extracted or DB data, affecting accuracy."
                                    elif comp_error_msg is None and accuracy_val >= 99.9: # Changed to >= 99.9 for float precision
                                        comp_error_msg = None
                                else:
                                    comp_error_msg = f"PO Number '{po_number_val}' not found in database."
                            
                            file_results_for_template["accuracy"] = accuracy_val
                            file_results_for_template["mismatched_fields"] = mismatched_data
                            file_results_for_template["db_record_for_display"] = db_record_data_for_display
                            file_results_for_template["compared_fields_list"] = comparison_fields_list_for_template

                            if comp_error_msg:
                                file_results_for_template["comparison_error"] = comp_error_msg
                            
                            # PO Chart Data Preparation
                            acc_calc_val_po = accuracy_val if accuracy_val is not None else 0.0
                            file_results_for_template["acc_calc_val"] = acc_calc_val_po
                            file_results_for_template["acc_display_val"] = f"{acc_calc_val_po:.1f}"
                            # ... (rest of your chart_... variable assignments for PO) ...
                            chart_radius = 40; chart_stroke_width = 10
                            chart_circumference = 2 * 3.1415926535 * chart_radius
                            chart_offset = chart_circumference * (1 - (acc_calc_val_po / 100))
                            file_results_for_template["chart_radius"] = chart_radius
                            file_results_for_template["chart_stroke_width"] = chart_stroke_width
                            file_results_for_template["chart_circumference"] = chart_circumference
                            file_results_for_template["chart_offset"] = chart_offset
                            chart_color_po = "#dc3545"; chart_text_class_po = "accuracy-bad"; chart_description_po = "Low"
                            if acc_calc_val_po >= 99.9: chart_color_po = "#198754"; chart_text_class_po = "accuracy-good"; chart_description_po = "Excellent"
                            elif acc_calc_val_po >= 80: chart_color_po = "#198754"; chart_text_class_po = "accuracy-good"; chart_description_po = "Good"
                            elif acc_calc_val_po >= 60: chart_color_po = "#ffc107"; chart_text_class_po = "accuracy-moderate"; chart_description_po = "Moderate"
                            file_results_for_template["chart_color"] = chart_color_po
                            file_results_for_template["chart_text_class"] = chart_text_class_po
                            file_results_for_template["chart_description"] = chart_description_po
                            
                            if not file_results_for_template.get("error"): # Only increment if no major error so far
                                processed_count += 1

                        elif upload_type == 'ats':
                            structured_data_result = extract_structured_data(extracted_text, ATS_FIELDS_FOR_USER_EXTRACTION, upload_type='ats')
                            if not isinstance(structured_data_result, dict):
                                app.logger.error(f"extract_structured_data (ATS) did not return dict for {filename}, got: {type(structured_data_result)}. Using empty dict.")
                                structured_data = {}
                                file_results_for_template["error"] = "Internal error: Failed to structure ATS data."
                            else:
                                structured_data = structured_data_result

                            file_results_for_template["structured_data"] = structured_data
                            
                            try: # Save to DB
                                resume_payload = {"original_filename": filename}
                                # Map labels to DB column names for extracted_resume_data table
                                for label in ATS_FIELDS_FOR_USER_EXTRACTION:
                                    db_col_name = label.lower().replace(' ', '_').replace('.', '') # e.g. "Sr no." -> "sr_no"
                                    if label in structured_data:
                                        resume_payload[db_col_name] = structured_data[label]
                                
                                insert_response = supabase.table('extracted_resume_data').insert(resume_payload).execute()
                                if hasattr(insert_response, 'error') and insert_response.error:
                                     app.logger.error(f"Error saving resume data for {filename} to Supabase: {insert_response.error.message}")
                                     # Decide if this is a fatal error for this file's processing
                                     file_results_for_template["error"] = f"DB save error: {insert_response.error.message}"
                                     # continue or allow validation with unsaved data? For now, let's log and continue.
                                else:
                                     app.logger.info(f"Saved extracted data for resume: {filename}")
                            except Exception as db_e:
                                app.logger.error(f"Exception saving resume {filename} to Supabase: {db_e}", exc_info=True)
                                file_results_for_template["error"] = "Database error saving resume data."
                                # continue if this is critical

                            if not file_results_for_template.get("error"): # Proceed to validation only if no major errors so far
                                accuracy, failed_details, validation_error_msg = validate_ats_data(structured_data)
                                file_results_for_template["accuracy"] = accuracy
                                file_results_for_template["mismatched_fields"] = failed_details
                                file_results_for_template["comparison_error"] = validation_error_msg
                                
                                acc_calc_val_ats = accuracy if accuracy is not None else 0.0
                                file_results_for_template["acc_calc_val"] = acc_calc_val_ats
                                file_results_for_template["acc_display_val"] = f"{acc_calc_val_ats:.1f}"
                                # ... (rest of your chart_... variable assignments for ATS, similar to PO) ...
                                chart_radius = 40; chart_stroke_width = 10
                                chart_circumference = 2 * 3.1415926535 * chart_radius
                                chart_offset = chart_circumference * (1 - (acc_calc_val_ats / 100))
                                file_results_for_template["chart_radius"] = chart_radius
                                file_results_for_template["chart_stroke_width"] = chart_stroke_width
                                file_results_for_template["chart_circumference"] = chart_circumference
                                file_results_for_template["chart_offset"] = chart_offset
                                chart_color_ats = "#dc3545"; chart_text_class_ats = "accuracy-bad"; chart_description_ats = "Low"
                                if acc_calc_val_ats >= 99.9: chart_color_ats = "#198754"; chart_text_class_ats = "accuracy-good"; chart_description_ats = "Excellent"
                                elif acc_calc_val_ats >= 80: chart_color_ats = "#198754"; chart_text_class_ats = "accuracy-good"; chart_description_ats = "Good"
                                elif acc_calc_val_ats >= 60: chart_color_ats = "#ffc107"; chart_text_class_ats = "accuracy-moderate"; chart_description_ats = "Moderate"
                                file_results_for_template["chart_color"] = chart_color_ats
                                file_results_for_template["chart_text_class"] = chart_text_class_ats
                                file_results_for_template["chart_description"] = chart_description_ats

                                active_criteria_fields = []
                                try:
                                    criteria_response = supabase.table('admin_ats_criteria').select("field_label").eq('is_active', True).execute()
                                    if criteria_response.data:
                                        active_criteria_fields = list(set([c['field_label'] for c in criteria_response.data]))
                                except Exception as crit_e:
                                    app.logger.error(f"Could not fetch active ATS criteria field list: {crit_e}")
                                file_results_for_template["compared_fields_list"] = active_criteria_fields
                                
                                processed_count += 1 # Increment only if validation part is reached
                        
                        results[filename] = file_results_for_template
                        session['processed_results_for_report'][filename] = file_results_for_template
                        session.modified = True 

                    except Exception as e:
                        app.logger.error(f"Outer error processing {filename}: {e}", exc_info=True)
                        results[filename] = {"error": f"Server error during processing file: {str(e)}"}
                    finally:
                        if os.path.exists(temp_file_path):
                            try: os.remove(temp_file_path)
                            except OSError as e_os: app.logger.error(f"Error removing temp file {temp_file_path}: {e_os}")
                
                if processed_count == 0 and any(not results[fn].get("error") for fn in results):
                     # If no errors but processed_count is 0, it means some logic path was missed for incrementing
                     app.logger.warning("processed_count is 0 but some files might have been processed without error. Review logic.")
                     if doc_files: flash('Some files might have been processed but results are incomplete.', 'warning')
                elif processed_count == 0 and doc_files: # All files resulted in an error before count increment
                    flash('Could not process any of the selected files due to errors.', 'danger')
                elif processed_count > 0 and processed_count < len(doc_files):
                     flash(f'Successfully processed {processed_count} out of {len(doc_files)} file(s). Some had errors.', 'warning')
                elif processed_count > 0:
                    flash(f'Successfully processed {processed_count} file(s).', 'info')


    current_tab_display_name = accessible_tabs_info.get(active_tab_id, {}).get("name", "Dashboard")
    
    return render_template('app_dashboard.html',
                           results=results,
                           accessible_tabs_info=accessible_tabs_info,
                           active_tab_id=active_tab_id,
                           current_tab_display_name=current_tab_display_name,
                           PO_FIELDS_FOR_USER_EXTRACTION=PO_FIELDS_FOR_USER_EXTRACTION,
                           ATS_FIELDS_FOR_USER_EXTRACTION=ATS_FIELDS_FOR_USER_EXTRACTION
                           )
        

@app.route('/download_report/<doc_type>/<filename_key>')
@login_required
def download_report(doc_type, filename_key):
    processed_results = session.get('processed_results_for_report', {}).get(filename_key)

    if not processed_results:
        flash(f"Report data for '{filename_key}' not found or expired.", "warning")
        return redirect(url_for('app_dashboard'))

    if 'error' in processed_results:
        # You might want to offer a text file with the error, or just an HTML page
        return Response(f"Cannot generate report for '{filename_key}' due to processing error: {processed_results['error']}", mimetype='text/plain')

    try:
        pdf_data = generate_pdf_report(processed_results, filename_key, doc_type, app.logger)

        safe_filename = "".join(c if c.isalnum() else "_" for c in filename_key) # Sanitize filename
        pdf_filename = f"report_{doc_type}_{safe_filename}.pdf"

        return Response(
            pdf_data,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment;filename={pdf_filename}'}
        )
    except Exception as e:
        app.logger.error(f"Error generating PDF report for {filename_key}: {e}", exc_info=True)
        flash(f"Could not generate PDF report for '{filename_key}'. An internal error occurred.", "danger")
        # Fallback to text report or redirect
        # For simplicity, redirecting here. You could offer a text fallback.
        return redirect(url_for('app_dashboard', active_tab_id=doc_type))

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
        # Select relevant columns, excluding admins
        response = supabase.table('users').select("email, username, role").neq('role', 'admin').execute()
        
        if response.data:
            # response.data is already a list of dictionaries with the selected columns
            return jsonify(response.data)
        elif hasattr(response, 'error') and response.error:
            app.logger.error(f"Supabase error fetching users: {response.error.message}")
            return jsonify({"error": f"Database error: {response.error.message}"}), 500
        else:
            # No error, but no non-admin users found
            return jsonify([]) 
            
    except Exception as e:
        app.logger.error(f"Exception in api_manage_get_users: {type(e).__name__} - {e}", exc_info=True)
        return jsonify({"error": "An unexpected server error occurred while fetching users."}), 500
    

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

# app.py

@app.route('/api/admin/manage_users/<string:user_email_param>', methods=['PUT']) # Renamed param for clarity
@admin_required
def api_manage_update_user(user_email_param):
    try:
        # Fetch the user to ensure they exist and are not an admin
        user_response = supabase.table('users').select("*").eq('email', user_email_param).single().execute()
        
        if not user_response.data:
            return jsonify({"error": "User not found"}), 404
        
        user_in_db = user_response.data
        if user_in_db.get("role") == 'admin':
            return jsonify({"error": "Cannot modify admin account via this API"}), 403

        data_to_update = {}
        request_payload = request.json

        if 'username' in request_payload and request_payload['username'].strip():
            if request_payload['username'].strip() != user_in_db.get('username'):
                data_to_update['username'] = request_payload['username'].strip()
        
        valid_roles = ["sub_admin", "po_verifier", "ats_verifier"]
        if 'role' in request_payload and request_payload['role'] in valid_roles:
            if request_payload['role'] != user_in_db.get('role'):
                data_to_update['role'] = request_payload['role']
        elif 'role' in request_payload: # Role provided but invalid
            return jsonify({"error": f"Invalid role specified. Must be one of: {', '.join(valid_roles)}"}), 400

        if 'password' in request_payload and request_payload['password']: # If new password is provided
            data_to_update['hashed_password'] = generate_password_hash(request_payload['password'])
            app.logger.info(f"Admin is updating password for user {user_email_param}")

        if not data_to_update:
            return jsonify({"message": "No changes provided for the user."}), 200

        # Perform the update
        update_response = supabase.table('users').update(data_to_update).eq('email', user_email_param).execute()

        if update_response.data:
            updated_user_data = update_response.data[0]
            user_info_to_return = {
                "email": updated_user_data.get('email'),
                "username": updated_user_data.get('username'),
                "role": updated_user_data.get('role')
            }
            return jsonify({"message": "User updated successfully.", "user": user_info_to_return}), 200
        elif hasattr(update_response, 'error') and update_response.error:
            app.logger.error(f"Supabase error updating user {user_email_param}: {update_response.error.message}")
            return jsonify({"error": f"Database error: {update_response.error.message}"}), 500
        else:
            app.logger.error(f"Supabase user update for {user_email_param} returned no data and no error.")
            return jsonify({"error": "Failed to update user (Supabase - unexpected response)."}), 500

    except Exception as e:
        app.logger.error(f"Exception updating user {user_email_param}: {type(e).__name__} - {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}"}), 500

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
    form_data = request.json 
    po_number_val = form_data.get("PO Number")
    if not po_number_val or not po_number_val.strip():
        return jsonify({"error": "PO Number is required."}), 400

    po_number_val = po_number_val.strip()
    db_payload_for_supabase = {"po_number": po_number_val}

    for field_def in MASTER_FIELD_DEFINITIONS.get("po", []):
        label = field_def["label"] 
        db_column_name = None
        if label == "PO Number":
            continue 
        elif label == "Vendor":
            db_column_name = "vendor"
        elif label == "Phone":
            db_column_name = "phone"
        elif label == "Total":
            db_column_name = "total"
        elif label == "Order Date":
            db_column_name = "order_date"
        # Add other mappings here if you have more, e.g.:
        # elif label == "Vendor Name": 
        #     db_column_name = "vendor_name" # Assuming you have a 'vendor_name' column

        # Ensure you have a db_column_name before proceeding for this label
        if not db_column_name:
            continue # Skip if this label from MASTER_FIELD_DEFINITIONS isn't mapped to a DB column

        # Check if the label (form field name) is present in the submitted form_data
        if label in form_data:
            submitted_value = form_data[label]

            if submitted_value is not None:
                value_to_store_str = str(submitted_value).strip()

                if db_column_name == "order_date":
                    if not value_to_store_str: # If admin submitted an empty string for date
                        db_payload_for_supabase[db_column_name] = None # Store as NULL
                    else:
                        try:
                            parsed_date_for_db = None
                            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", value_to_store_str):
                                month, day, year = map(int, value_to_store_str.split('/'))
                                parsed_date_for_db = f"{year:04d}-{month:02d}-{day:02d}"
                            elif re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", value_to_store_str):
                                year, month_str, day_str = value_to_store_str.split('-')
                                parsed_date_for_db = f"{int(year):04d}-{int(month_str):02d}-{int(day_str):02d}"
                            
                            if parsed_date_for_db:
                                db_payload_for_supabase[db_column_name] = parsed_date_for_db
                            else:
                                app.logger.warning(f"Order Date '{value_to_store_str}' for PO {po_number_val} has unrecognized format.")
                                return jsonify({"error": f"Invalid Order Date format: '{value_to_store_str}'. Use MM/DD/YYYY or YYYY-MM-DD."}), 400
                        except ValueError:
                             app.logger.error(f"Invalid date value for Order Date: {value_to_store_str}")
                             return jsonify({"error": f"Invalid date format for Order Date: '{value_to_store_str}'. Use MM/DD/YYYY or YYYY-MM-DD."}), 400
                else: # For non-date fields (Vendor, Phone, Total, Vendor Name etc.)
                    if value_to_store_str == "":
                        db_payload_for_supabase[db_column_name] = None # Store empty string as NULL
                    else:
                        db_payload_for_supabase[db_column_name] = value_to_store_str
            else: # If form_data[label] is explicitly null (e.g., from JSON "Vendor": null)
                db_payload_for_supabase[db_column_name] = None # Store as NULL
        # else:
            # If label is NOT in form_data (meaning admin didn't configure this field for entry, so it wasn't on the form),
            # we simply don't add this key to db_payload_for_supabase.
            # Supabase upsert will then leave the existing value in that DB column unchanged, or set its DB default if it's a new row.
            # Since your other columns (vendor, phone, total) default to NULL in the DB, this behavior is fine.

    


    if len(db_payload_for_supabase) <= 1 and "po_number" in db_payload_for_supabase:
        return jsonify({"error": "No data provided to save besides PO Number."}), 400

    try:
        app.logger.info(f"Upserting PO data to Supabase: {db_payload_for_supabase}")
        response = supabase.table('admin_po_database_entries').upsert(db_payload_for_supabase).execute()
        
        if response.data:
            app.logger.info(f"Supabase PO upsert successful for {po_number_val}")
            return jsonify({"message": f"PO data for '{po_number_val}' saved successfully."}), 200
        elif hasattr(response, 'error') and response.error:
            app.logger.error(f"Supabase error saving PO {po_number_val}: code={response.error.code}, message={response.error.message}, details={response.error.details}, hint={response.error.hint}")
            return jsonify({"error": f"Database error: {response.error.message} (Code: {response.error.code})"}), 500
        else:
            app.logger.error(f"Supabase PO upsert for {po_number_val} returned no data and no explicit error. Status: {getattr(response, 'status_code', 'N/A')}")
            return jsonify({"error": "Failed to save PO data (Supabase - unexpected response)."}), 500
            
    except Exception as e:
        app.logger.error(f"Exception saving PO {po_number_val} to Supabase: {type(e).__name__} - {e}", exc_info=True)
        return jsonify({"error": f"Server error saving PO: {str(e)}"}), 500 


@app.route('/api/admin/po_database_count', methods=['GET'])
@admin_required
def api_get_po_database_count():
    try:
        # The count can be retrieved efficiently by asking for just 'id' or 'po_number'
        # and setting head=True to only get count, or by letting Supabase client handle it.
        # Supabase client with `count='exact'` is efficient.
        response = supabase.table('admin_po_database_entries').select("po_number", count='exact').execute()
        
        if hasattr(response, 'count') and response.count is not None:
            return jsonify({"count": response.count})
        elif hasattr(response, 'error') and response.error:
            app.logger.error(f"Supabase error counting PO entries: {response.error.message}")
            return jsonify({"error": f"Database error: {response.error.message}"}), 500
        else:
            app.logger.error("Supabase PO count returned no count and no error.")
            return jsonify({"count": 0}) # Or handle as error
            
    except Exception as e:
        app.logger.error(f"Exception counting PO entries: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}"}), 500
    
# --- Admin APIs for ATS Criteria Management & Count ---
# app.py - Corrected /api/admin/ats_criteria (GET) route

@app.route('/api/admin/ats_criteria', methods=['GET'])
@admin_required
def api_get_ats_criteria():
    try:
        # Fetch all criteria from the Supabase table
        # Order by field_label and then perhaps by another field like created_at for consistent ordering
        response = supabase.table('admin_ats_criteria').select("*").order('field_label').order('created_at').execute()

        if response.data:
            criteria_by_field = {}
            for criterion_row in response.data:
                field_label = criterion_row.get('field_label')
                if field_label not in criteria_by_field:
                    criteria_by_field[field_label] = []
                
                # Reconstruct the criterion dictionary.
                # The `condition_values` from the DB is already a dict (JSONB).
                # We need to merge its keys into the main criterion dictionary
                # for consistency if your frontend JavaScript expects flat keys like 'value1', 'keywords'.
                criterion_detail = {
                    "id": criterion_row.get('id'),
                    "field_label": field_label,
                    "condition_type": criterion_row.get('condition_type'),
                    "is_active": criterion_row.get('is_active')
                    # Add other top-level fields from the row if any, e.g., created_at
                }
                # Spread the condition_values dict into the criterion_detail dict
                if criterion_row.get('condition_values'): # Check if it's not None
                    criterion_detail.update(criterion_row.get('condition_values'))
                
                criteria_by_field[field_label].append(criterion_detail)
            
            app.logger.info(f"Fetched ATS criteria from Supabase: {len(response.data)} items grouped into {len(criteria_by_field)} fields.")
            return jsonify(criteria_by_field)
            
        elif hasattr(response, 'error') and response.error:
            app.logger.error(f"Supabase error fetching ATS criteria: code={response.error.code}, message={response.error.message}")
            return jsonify({"error": f"Database error: {response.error.message}"}), 500
        else:
            # No error, but no data either
            app.logger.info("No ATS criteria found in Supabase.")
            return jsonify({}) # Return empty object if no criteria found
            
    except Exception as e:
        app.logger.error(f"Exception fetching ATS criteria: {type(e).__name__} - {e}", exc_info=True)
        return jsonify({"error": f"Server error while fetching ATS criteria: {str(e)}"}), 500

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

# --- Helper Function to Generate the Donut Chart Drawing ---
def create_accuracy_donut_chart(accuracy_percent_numeric,
                                size_pts=60, stroke_width_pts=8,
                                progress_color_hex="#28a745",
                                track_color_hex="#e9ecef",
                                text_color_hex_inside_chart="#000000",
                                font_name='Helvetica-Bold',
                                font_size_pts=10):
    drawing = Drawing(width=size_pts, height=size_pts)
    
    # Use different variable names for center coordinates to avoid keyword clashes
    center_x_coord = size_pts / 2.0
    center_y_coord = size_pts / 2.0
    radius_val = (size_pts - stroke_width_pts) / 2.0

    # Background Track
    track = Circle(
        cx=center_x_coord, 
        cy=center_y_coord, 
        r=radius_val,
        fillColor=None, 
        strokeColor=colors.HexColor(track_color_hex),
        strokeWidth=stroke_width_pts
    )
    drawing.add(track)

    progress_shape = None 

    if accuracy_percent_numeric > 0.01: # Only draw if visually significant
        sweep_angle_degrees = (accuracy_percent_numeric / 100.0) * 360.0
        
        # ReportLab angles: 0 is East (3 o'clock), 90 is North (12 o'clock)
        start_angle_degrees_val = 90  # Start at the top of the circle
        
        # Calculate endangledegrees for clockwise sweep
        # Wedge draws from startangledegrees to endangledegrees.
        # For clockwise, end angle = start angle - sweep.
        end_angle_degrees_val = start_angle_degrees_val - sweep_angle_degrees

        if abs(sweep_angle_degrees) >= 359.99: # If it's a full circle
            progress_shape = Circle( 
                cx=center_x_coord, 
                cy=center_y_coord, 
                r=radius_val,
                fillColor=None, 
                strokeColor=colors.HexColor(progress_color_hex),
                strokeWidth=stroke_width_pts, 
                strokeLineCap=1 # For a rounded look if the stroke is thick
            )
        else:
            progress_shape = Wedge(
                centerx=center_x_coord,             # CORRECTED: Use 'centerx'
                centery=center_y_coord,             # CORRECTED: Use 'centery'
                radius=radius_val,
                startangledegrees=start_angle_degrees_val,
                endangledegrees=end_angle_degrees_val, # CORRECTED: Use 'endangledegrees'
                fillColor=None,                      # Donut chart, so no fill for the wedge
                strokeColor=colors.HexColor(progress_color_hex),
                strokeWidth=stroke_width_pts,
                strokeLineCap=1                      # 0=butt, 1=round, 2=square (round looks good for donuts)
            )
        
        if progress_shape:
            drawing.add(progress_shape)

    # Text in the center
    display_text_in_chart = f"{float(accuracy_percent_numeric):.0f}%" # Display as integer percentage
    text_label = Label()
    text_label.setOrigin(center_x_coord, center_y_coord)
    text_label.boxAnchor = 'c' # Anchor at the center of the text box
    text_label.text = display_text_in_chart
    text_label.fontName = font_name
    text_label.fontSize = font_size_pts
    text_label.fillColor = colors.HexColor(text_color_hex_inside_chart)
    drawing.add(text_label)
    
    return drawing

# --- Main PDF Generation Function ---
def generate_pdf_report(processed_results, filename_key, doc_type, app_logger):
    # global PO_KEY_COMPARISON_FIELDS # This line can be removed, not needed
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            leftMargin=0.5*inch, rightMargin=0.5*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()
    
    # --- Custom Styles (keep these as they are used by other elements) ---
    styles.add(ParagraphStyle(name='MainReportTitle', parent=styles['Normal'], fontSize=14, alignment=0, spaceBottom=10, leading=16, fontName='Helvetica-Bold'))
    styles.add(ParagraphStyle(name='SectionTitle', parent=styles['Normal'], fontSize=11, fontName='Helvetica-Bold', spaceBefore=10, spaceBottom=6, leading=14, alignment=0))
    styles.add(ParagraphStyle(name='ComparisonSectionTitle', parent=styles['Normal'], fontSize=12, fontName='Helvetica-Bold', spaceBefore=12, spaceBottom=6, leading=14))
    styles.add(ParagraphStyle(name='SubSectionTitle', parent=styles['Normal'], fontSize=10, fontName='Helvetica-Bold', spaceBefore=8, spaceBottom=4, leading=12))
    styles.add(ParagraphStyle(name='AccuracyText', parent=styles['Normal'], fontSize=9, leading=12, alignment=0)) # Still used for accuracy text
    styles.add(ParagraphStyle(name='SmallNote', parent=styles['Normal'], fontSize=8, textColor=colors.dimgrey, leading=10))
    styles.add(ParagraphStyle(name='SuccessMessage', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor("#155724"), fontName='Helvetica-Bold', leading=12))
    styles.add(ParagraphStyle(name='DataItemKey', parent=styles['Normal'], fontSize=9, fontName='Helvetica-Bold', leading=11))
    styles.add(ParagraphStyle(name='DataItemValue', parent=styles['Normal'], fontSize=9, leading=11, wordWrap='CJK', spaceLeft=5))
    styles.add(ParagraphStyle(name='TableHeader', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', alignment=1, textColor=colors.black))
    styles.add(ParagraphStyle(name='TableCellText', parent=styles['Normal'], fontSize=8, leading=10, wordWrap='CJK'))
    styles.add(ParagraphStyle(name='MismatchTableHeader', parent=styles['Normal'], fontSize=9, fontName='Helvetica-Bold',textColor=colors.HexColor("#721c24")))
    styles.add(ParagraphStyle(name='FailedCriteriaTableHeader', parent=styles['Normal'], fontSize=9, fontName='Helvetica-Bold', textColor=colors.HexColor("#856404")))

    story = []
    if filename_key:
        story.append(Paragraph(f"Results for: {filename_key}", styles['MainReportTitle']))
        story.append(Spacer(1, 0.1*inch))

    if doc_type == 'po':
        # --- PO Extracted Data and DB Data (Side-by-Side Table) ---
        # This part remains the same as your last correct version for PO data display
        po_field_order_for_cards = PO_FIELDS_FOR_USER_EXTRACTION # Use defined list
        left_card_flowables = [Paragraph("Extracted Data:", styles['SectionTitle'])]
        structured_data = processed_results.get('structured_data', {})
        if structured_data:
            for field_name in po_field_order_for_cards:
                if field_name in structured_data: # Display only if field exists in extracted data
                    v = str(structured_data.get(field_name, "N/A"))
                    item_data = [[Paragraph(f"{field_name}:", styles['DataItemKey']), Paragraph(v, styles['DataItemValue'])]]
                    item_table = Table(item_data, colWidths=[0.9*inch, None])
                    item_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('LEFTPADDING',(0,0),(-1,-1),0), ('BOTTOMPADDING',(0,0),(-1,-1),2)]))
                    left_card_flowables.append(item_table)
        else:
            left_card_flowables.append(Paragraph("No structured data extracted.", styles['SmallNote']))

        right_card_flowables = [Paragraph("Database Data (For Comparison):", styles['SectionTitle'])]
        db_record = processed_results.get('db_record_for_display', {})
        if db_record:
            for field_name in PO_KEY_COMPARISON_FIELDS: # Show only key comparison fields from DB
                if field_name in db_record:
                    v = str(db_record.get(field_name, "N/A"))
                    item_data = [[Paragraph(f"{field_name}:", styles['DataItemKey']), Paragraph(v, styles['DataItemValue'])]]
                    item_table = Table(item_data, colWidths=[0.9*inch, None])
                    item_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('LEFTPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
                    right_card_flowables.append(item_table)
        else:
            right_card_flowables.append(Paragraph("No database record found or comparison not applicable.", styles['SmallNote']))

        master_data_table = Table([[left_card_flowables, right_card_flowables]], colWidths=[3.5*inch, 3.5*inch], spaceBefore=0)
        master_data_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('RIGHTPADDING', (0,0), (0,0), 0.15*inch), ('LEFTPADDING', (1,0), (1,0), 0.15*inch)]))
        story.append(master_data_table)
        story.append(Spacer(1, 0.15*inch))

        # --- PO Comparison & Accuracy (Text Only) ---
        po_comparison_elements = []
        po_comparison_elements.append(Paragraph("PO Comparison & Accuracy:", styles['ComparisonSectionTitle']))
        
        acc_display_val_str = processed_results.get('acc_display_val', "0.0")
        chart_desc = processed_results.get('chart_description', 'N/A')
        chart_color = processed_results.get('chart_color', '#28a745') # Still useful for text color
        compared_fields_str = ", ".join(PO_KEY_COMPARISON_FIELDS) if PO_KEY_COMPARISON_FIELDS else "configured fields"

        # REMOVED Donut Chart Call and Table for Chart
        acc_line_html = f"Accuracy (based on: {compared_fields_str}): <font color='{chart_color}'><b>{chart_desc} ({acc_display_val_str}%)</b></font>"
        acc_para = Paragraph(acc_line_html, styles['AccuracyText'])
        po_comparison_elements.append(acc_para) # Add accuracy text directly
        po_comparison_elements.append(Spacer(1, 0.05*inch))

        # PO Mismatch Table (remains the same)
        mismatched = processed_results.get('mismatched_fields', {})
        comp_error = processed_results.get('comparison_error')
        acc_val_logic = processed_results.get('acc_calc_val', 0.0) # Get the numeric accuracy
        if not mismatched and acc_val_logic >= 99.9:
            success_html = "<font color='#155724'><b>✔ All compared fields matched!</b></font>"
            po_comparison_elements.append(Paragraph(success_html, styles['SuccessMessage']))
        elif not mismatched and comp_error:
            po_comparison_elements.append(Paragraph(f"Note: {comp_error}", styles['SmallNote']))
        elif not mismatched and acc_val_logic < 99.9:
             po_comparison_elements.append(Paragraph("Note: Accuracy affected by empty/missing or normalized fields.", styles['SmallNote']))
        
        if mismatched:
            mismatch_data = [[Paragraph(h, styles['TableHeader']) for h in ["Field", "Database Value", "Extracted Value"]]]
            for f, vals in mismatched.items():
                mismatch_data.append([Paragraph(str(f), styles['TableCellText']), Paragraph(str(vals.get("db_value","N/A")), styles['TableCellText']), Paragraph(str(vals.get("extracted_value","N/A")), styles['TableCellText'])])
            t_mismatch = Table(mismatch_data, colWidths=[1.8*inch, 2.6*inch, 2.6*inch], repeatRows=1)
            t_mismatch.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f8d7da")),
                ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor("#721c24")), 
                ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('LEFTPADDING', (0,0), (-1,-1), 4), ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ]))
            po_comparison_elements.append(t_mismatch)
            if comp_error and not (not mismatched and acc_val_logic < 99.9): # Only show comp_error if not already covered by the "empty fields" note
                po_comparison_elements.append(Spacer(1,0.05*inch))
                po_comparison_elements.append(Paragraph(f"Note: {comp_error}", styles['SmallNote']))
        story.append(KeepTogether(po_comparison_elements))

    elif doc_type == 'ats':
        # --- ATS Extracted Data ---
        story.append(Paragraph("Extracted Data:", styles['SectionTitle']))
        structured_data_ats = processed_results.get('structured_data', {})
        if structured_data_ats:
            ats_field_order = ATS_FIELDS_FOR_USER_EXTRACTION
            data_items_for_ats = []
            for field_name in ats_field_order:
                if field_name in structured_data_ats:
                    value_text = str(structured_data_ats.get(field_name, "N/A"))
                    item_para_key = Paragraph(f"{field_name}:", styles['DataItemKey'])
                    item_para_val = Paragraph(value_text, styles['DataItemValue'])
                    data_items_for_ats.append([item_para_key, item_para_val])
            
            if data_items_for_ats:
                extracted_data_table = Table(data_items_for_ats, colWidths=[1.5*inch, None])
                extracted_data_table.setStyle(TableStyle([
                    ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('LEFTPADDING',(0,0),(-1,-1),0),
                    ('BOTTOMPADDING',(0,0),(-1,-1),3)
                ]))
                story.append(extracted_data_table)
            else:
                story.append(Paragraph("No relevant structured data extracted for display.", styles['SmallNote']))
        else:
            story.append(Paragraph("No structured data extracted.", styles['SmallNote']))
        
        story.append(Spacer(1, 0.2*inch))

        # --- ATS Criteria Validation (Text Only) ---
        validation_elements = []
        validation_elements.append(Paragraph("ATS Criteria Validation:", styles['ComparisonSectionTitle']))
        
        acc_display_val_str_ats = processed_results.get('acc_display_val', "0.0")
        chart_desc_ats = processed_results.get('chart_description', 'N/A') # Description like "Low", "Good"
        chart_color_ats = processed_results.get('chart_color', '#dc3545') # Color for text

        # REMOVED Donut Chart Call and Table for Chart
        ats_acc_text_html = f"Validation Accuracy (Criteria Met): <font color='{chart_color_ats}'><b>{chart_desc_ats} ({acc_display_val_str_ats}%)</b></font>"
        ats_acc_para_style = ParagraphStyle('AtsAccuracyText', parent=styles['AccuracyText'], alignment=0) # Left align
        ats_acc_para = Paragraph(ats_acc_text_html, ats_acc_para_style)
        validation_elements.append(ats_acc_para)
        validation_elements.append(Spacer(1, 0.1*inch))

        # Failed Criteria Table (remains the same)
        failed_criteria = processed_results.get('mismatched_fields', {})
        validation_note = processed_results.get('comparison_error') 
        accuracy_numeric_for_chart_ats = 0.0 # Recalculate or get from processed_results if needed for logic
        try: accuracy_numeric_for_chart_ats = float(acc_display_val_str_ats)
        except: pass


        if failed_criteria:
            validation_elements.append(Paragraph("Failed Criteria:", styles['SubSectionTitle']))
            # ... (Failed criteria table definition remains the same as your last version) ...
            failed_criteria_header = [Paragraph(h, styles['TableHeader']) for h in ["Criterion (Field & Rule)", "Your Document's Value", "Issue / Details"]]
            failed_criteria_data_rows = [failed_criteria_header]
            for criterion_key, details in failed_criteria.items():
                failed_criteria_data_rows.append([
                    Paragraph(str(criterion_key), styles['TableCellText']),
                    Paragraph(str(details.get("extracted_value", "N/A")), styles['TableCellText']),
                    Paragraph(str(details.get("reason", "N/A")), styles['TableCellText'])
                ])
            if len(failed_criteria_data_rows) > 1:
                failed_criteria_table = Table(failed_criteria_data_rows, colWidths=[2.5*inch, 2.0*inch, None], repeatRows=1)
                failed_criteria_table.setStyle(TableStyle([
                    ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#fff3cd")), 
                    ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor("#856404")),
                    ('GRID', (0,0), (-1,-1), 0.5, colors.grey), 
                    ('VALIGN', (0,0), (-1,-1), 'TOP'), 
                    ('LEFTPADDING', (0,0), (-1,-1), 4), ('RIGHTPADDING', (0,0), (-1,-1), 4),
                    ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                ]))
                validation_elements.append(failed_criteria_table)
        elif validation_note:
             validation_elements.append(Paragraph(f"Note: {validation_note}", styles['SmallNote']))
        elif accuracy_numeric_for_chart_ats >= 99.9 :
             validation_elements.append(Paragraph("✔ All active criteria passed!", styles['SuccessMessage']))
        else:
            validation_elements.append(Paragraph("No specific criteria failed, but accuracy is not 100%. Check for unvalidated fields or criteria configuration.", styles['SmallNote']))
        
        story.append(KeepTogether(validation_elements))

    # --- Build PDF Document ---
    try:
        doc.build(story)
        pdf_data = buffer.getvalue()
    except Exception as e_build:
        app_logger.error(f"Error during PDF doc.build: {e_build}", exc_info=True)
        # Return a simple text error if PDF build fails catastrophically
        return f"Failed to build PDF: {e_build}".encode('utf-8') # Must return bytes for Response
    finally:
        buffer.close()
        
    return pdf_data

@app.route('/api/admin/ats_criteria_count', methods=['GET'])
@admin_required
def api_get_ats_criteria_count():
    try:
        # Count active criteria
        active_response = supabase.table('admin_ats_criteria').select(
            "id", count='exact'
        ).eq('is_active', True).execute()
        
        active_count = 0
        if hasattr(active_response, 'count') and active_response.count is not None:
            active_count = active_response.count
        elif hasattr(active_response, 'error') and active_response.error:
            app.logger.error(f"Supabase error counting active ATS criteria: {active_response.error.message}")
            # Decide if you want to return error or just 0 for counts
            return jsonify({"error": f"Database error counting active criteria: {active_response.error.message}"}), 500

        # Count total criteria
        total_response = supabase.table('admin_ats_criteria').select(
            "id", count='exact'
        ).execute()
        
        total_count = 0
        if hasattr(total_response, 'count') and total_response.count is not None:
            total_count = total_response.count
        elif hasattr(total_response, 'error') and total_response.error:
            app.logger.error(f"Supabase error counting total ATS criteria: {total_response.error.message}")
            return jsonify({"error": f"Database error counting total criteria: {total_response.error.message}"}), 500
            
        return jsonify({"active_count": active_count, "total_count": total_count})
        
    except Exception as e:
        app.logger.error(f"Exception counting ATS criteria: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}"}), 500
    
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