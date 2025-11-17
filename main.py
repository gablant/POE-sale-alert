import functions_framework
import requests
import firebase_admin
from firebase_admin import messaging, firestore
import os
import datetime
import json

# --- Global variable for lazy initialization ---
_db_client = None 

# --- FCM Notification Function (no changes) ---
def send_notification(item_name, price, league):
    """Sends a notification via Firebase Cloud Messaging."""
    message = messaging.Message(
        notification=messaging.Notification(
            title="PoE Sale!",
            body=f"Sold {item_name} for {price} in {league}"
        ),
        topic="poe_sales"
    )
    try:
        response = messaging.send(message)
        print(f"Notification sent: {response} for {item_name} for {price} in {league}")
    except Exception as e:
        print(f"Error sending FCM notification: {e}")

# --- Main Cloud Function Entry Point ---
@functions_framework.http
def check_poe_sales_api(request):
    """
    Cloud Function to fetch PoE trade history via API, detect new sales,
    and send FCM notifications.
    """
    global _db_client

    print(f"Function triggered by HTTP at: {datetime.datetime.now()}")

    # --- Firebase setup (LAZY INITIALIZATION) ---
    if _db_client is None:
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        _db_client = firestore.client()
    db = _db_client

    # --- Get Environment Variables (from Secret Manager) ---
    poesessid_cookie = os.environ.get("POE_SESSID_COOKIE")
    cf_clearance_cookie = os.environ.get("POE_CF_CLEARANCE_COOKIE")
    poe_league_name = "Keepers" # HARDCODED: No longer from environment variable

    # --- AGGRESSIVE DEBUGGING - Explicit Checks ---
    print(f"DEBUG: poesessid_cookie (type: {type(poesessid_cookie)}): {poesessid_cookie[:40] if poesessid_cookie else 'None'}... (len: {len(poesessid_cookie) if poesessid_cookie else 0})")
    print(f"DEBUG: cf_clearance_cookie (type: {type(cf_clearance_cookie)}): {cf_clearance_cookie[:40] if cf_clearance_cookie else 'None'}... (len: {len(cf_clearance_cookie) if cf_clearance_cookie else 0})")
    print(f"DEBUG: poe_league_name (type: {type(poe_league_name)}): {poe_league_name if poe_league_name else 'None'}... (len: {len(poe_league_name) if poe_league_name else 0})")


    missing_vars = []
    if not poesessid_cookie or len(poesessid_cookie) < 30: # Check length for POESESSID
        missing_vars.append("POE_SESSID_COOKIE")
    if not cf_clearance_cookie or len(cf_clearance_cookie) < 50: # Check length for CF_CLEARANCE
        missing_vars.append("POE_CF_CLEARANCE_COOKIE")
    # Removed check for poe_league_name as it's hardcoded

    if missing_vars:
        error_message = f"Error: Required environment variables not set or truncated: {', '.join(missing_vars)}"
        print(error_message)
        return (error_message, 500)
    print("DEBUG: All required environment variables are confirmed present and appear to be full length.")


    # --- Session setup for requests (no changes) ---
    session = requests.Session()
    session.cookies.set("POESESSID", poesessid_cookie)
    session.cookies.set("cf_clearance", cf_clearance_cookie)

    session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36', # Updated User-Agent
    'Accept': '*/*', # Changed to * / * for API
    'Accept-Language': 'fi-FI,fi;q=0.9', # From your debug, update if different
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Connection': 'keep-alive',
    'X-Requested-With': 'XMLHttpRequest', # Important for AJAX calls
    'Referer': 'https://www.pathofexile.com/trade/history', # Important for API calls
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
    'DNT': '1', # Do Not Track header
    # *** ADD THESE CLIENT HINTS - COPY LATEST VALUES FROM YOUR BROWSER'S NETWORK TAB ***
    'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    'sec-ch-ua-arch': '"x86"',
    'sec-ch-ua-bitness': '"64"',
    'sec-ch-ua-full-version': '"142.0.7444.163"', # Make sure this is correct for your browser
    'sec-ch-ua-full-version-list': '"Chromium";v="142.0.7444.163", "Google Chrome";v="142.0.7444.163", "Not_A Brand";v="99.0.0.0"', # Make sure this is correct
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-model': '""',
    'sec-ch-ua-platform': '"Windows"',
    'sec-ch-ua-platform-version': '"19.0.0"', # Make sure this is correct for your OS
    # *** END CLIENT HINTS ***
})


    # --- Fetch existing seen sales from Firestore ---
    sales_state_doc_ref = db.collection('poe_api_state').document('sales_history')
    
    try:
        sales_state_doc = sales_state_doc_ref.get()
        firestore_sales_data = sales_state_doc.to_dict() if sales_state_doc.exists else {}
        current_seen_sales_keys = set(firestore_sales_data.keys())
        print(f"Loaded {len(current_seen_sales_keys)} previously seen sales from Firestore.")
    except Exception as e:
        print(f"Error loading sales state from Firestore: {e}")
        current_seen_sales_keys = set()
        firestore_sales_data = {} 

    # --- Fetch current trade history via API ---
    trade_history_api_url = f"https://www.pathofexile.com/api/trade/history/{poe_league_name}"
    
    try:
        resp = session.get(trade_history_api_url, timeout=30)
        resp.raise_for_status()
        trade_data = resp.json()
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching trade history from API {trade_history_api_url}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"API Error Status Code: {e.response.status_code}") # ADD THIS LINE
            print(f"API Error Response Text (first 500 chars): {e.response.text[:500]}...") # Print partial text for context
        return (f"Error fetching trade history: {e}", 500)

    except json.JSONDecodeError as e:
        print(f"Error decoding JSON response from {trade_history_api_url}: {e}")
        print(f"Raw response text: {resp.text[:500]}...")
        return (f"Error parsing API response: {e}", 500)

    newly_seen_sales_for_db = {}
    sales_detected_in_this_run = 0

    if isinstance(trade_data, dict) and 'sales' in trade_data:
        sales_list = trade_data['sales']
    elif isinstance(trade_data, list):
        sales_list = trade_data
    else:
        print(f"Unexpected API response structure: {type(trade_data)}. Raw: {json.dumps(trade_data)[:200]}...")
        return ("Unexpected API response structure.", 500)

    for sale_entry in sales_list:
        try:
            sale_id = sale_entry.get("id")
            item_name = sale_entry.get("item", {}).get("name", "Unknown Item")
            price_amount = sale_entry.get("price", {}).get("amount", "Unknown Amount")
            price_currency = sale_entry.get("price", {}).get("currency", "Unknown Currency")
            price = f"{price_amount} {price_currency}"
            
            if sale_id is None:
                print(f"Skipping sale entry without an 'id': {sale_entry}")
                continue

            sale_key = str(sale_id)
            league = poe_league_name

            if sale_key not in current_seen_sales_keys:
                print(f"New sale detected via API: {item_name} for {price} in {league}")
                send_notification(item_name, price, league)
                
                current_seen_sales_keys.add(sale_key)
                newly_seen_sales_for_db[sale_key] = firestore.SERVER_TIMESTAMP
                sales_detected_in_this_run += 1
        except Exception as e:
            print(f"Error processing sale entry {sale_entry}: {e}")

    if newly_seen_sales_for_db:
        try:
            sales_state_doc_ref.set(newly_seen_sales_for_db, merge=True)
            print(f"Updated Firestore with {sales_detected_in_this_run} new sales API records.")
        except Exception as e:
            print(f"Error updating Firestore with new sales: {e}")
            return (f"Error updating Firestore: {e}", 500)
    else:
        print("No new sales to add to Firestore in this run.")

    print("POE sales API check completed successfully.")
    return ('API scraper ran successfully', 200)

