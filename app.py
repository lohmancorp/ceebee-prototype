from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import os
import base64
import sys
import requests
import json
import re
from openai import OpenAI
import spacy
from spacy.matcher import Matcher

# Lazy imports for workflow-specific functions
def initialize_database():
    from workflow import initialize_database as db_init
    db_init()

def handle_intent(*args, **kwargs):
    from workflow import handle_intent as workflow_handle_intent
    return workflow_handle_intent(*args, **kwargs)

def generate_conversation_id():
    from workflow import generate_conversation_id as workflow_generate_id
    return workflow_generate_id()


with open('config/config.json') as config_file:
    config = json.load(config_file)

OPENAI_API_KEY = config['api_keys']['openai']
FRESH_SERVICE_API_KEY = config['api_keys']['freshservice']
FRESH_SERVICE_BASE_URL = config['urls']['freshservice_base']
client = OpenAI(api_key=OPENAI_API_KEY)

# Load spaCy model for entity extraction
nlp = spacy.load("en_core_web_sm")

app = Flask(__name__)
CORS(app) 

initialize_database()

def sanitize_user_input(input_str):
    """
    Sanitize user input to ensure it doesn't contain special characters or whitespaces.
    """
    return input_str.isalnum()

# Initialize spaCy Matcher
matcher = Matcher(nlp.vocab)

# Add custom patterns for ticket, subscription, and order IDs
patterns = [
    [{"LOWER": "ticket"}, {"LOWER": "id"}, {"IS_DIGIT": True}],
    [{"LOWER": "subscription"}, {"LOWER": "id"}, {"IS_DIGIT": True}],
    [{"LOWER": "order"}, {"LOWER": "id"}, {"IS_DIGIT": True}],
    [{"LOWER": "ticket"}, {"LOWER": "number"}, {"IS_DIGIT": True}],
    [{"LOWER": "order"}, {"TEXT": {"REGEX": r"SO\d+"}}],
    [{"TEXT": {"REGEX": r"(INC|SR)-\d+"}}]  # Adjusted to match standalone prefixes like "INC-123456"
]

matcher.add("ID_PATTERNS", patterns)

def extract_ids(doc):
    """
    Extract ticket, subscription, and order IDs from the provided spaCy doc object.

    Args:
        doc: spaCy document object.

    Returns:
        list: A list of extracted IDs with their types.
    """
    matches = matcher(doc)
    details = []
    seen_ids = {"ticket_id": set(), "subscription_id": set(), "order_id": set()}  # Track IDs to avoid duplicates

    # Define regex patterns
    order_id_pattern = r"\b(SO|CF|CH|CL|DG|UG|RN|TA|TS)\d{6,10}\b"  # Matches prefixed order IDs like SO000099
    ticket_id_pattern = r"\b(INC|SR)-\d+\b"  # Matches prefixed ticket IDs like INC-34 or SR-34
    numeric_ticket_pattern = r"(ticket|sr|incident|number).*?\b\d+\b"  # Matches phrases like "Ticket ID 34"

    for match_id, start, end in matches:
        span = doc[start:end]
        id_value = span.text.strip()  # Preserve the original text

        # Extract ticket IDs (e.g., "INC-123456", "SR-34")
        if re.match(ticket_id_pattern, span.text, re.IGNORECASE):
            ticket_id = span.text.strip()
            if ticket_id not in seen_ids["ticket_id"]:
                details.append({"ticket_id": ticket_id})
                seen_ids["ticket_id"].add(ticket_id)

        # Extract subscription IDs
        elif re.match(r"^SUB-\d+$", span.text, re.IGNORECASE) or "subscription" in span.text.lower():
            subscription_id = span.text.strip()
            if subscription_id not in seen_ids["subscription_id"]:
                details.append({"subscription_id": subscription_id})
                seen_ids["subscription_id"].add(subscription_id)

        # Extract order IDs (e.g., "SO000099")
        elif re.match(order_id_pattern, span.text, re.IGNORECASE):
            order_id = span.text.strip()
            if order_id not in seen_ids["order_id"]:
                details.append({"order_id": order_id})
                seen_ids["order_id"].add(order_id)

    # Fallback: Handle phrases like "Ticket ID 34" and "Order SO000099"
    text = doc.text.lower()  # Case-insensitive matching for fallback logic
    fallback_matches = re.findall(r"(order|subscription|ticket|sr|incident|number)\s+(with\s+)?(id|number)?\s+(\S+)", text)
    for match in fallback_matches:
        id_type, _, _, id_value = match
        if id_type == "order" and re.match(order_id_pattern, id_value, re.IGNORECASE) and id_value not in seen_ids["order_id"]:
            details.append({"order_id": id_value})
            seen_ids["order_id"].add(id_value)
        elif id_type in ["ticket", "sr", "incident", "number"] and id_value not in seen_ids["ticket_id"]:
            details.append({"ticket_id": id_value})
            seen_ids["ticket_id"].add(id_value)

    # Fallback: Match standalone numeric ticket IDs
    numeric_ticket_matches = re.findall(r"\b\d+\b", text)
    for match in numeric_ticket_matches:
        if match not in seen_ids["ticket_id"]:
            details.append({"ticket_id": match})
            seen_ids["ticket_id"].add(match)

    # Remove overlapping IDs to ensure a single ID is not classified as multiple types
    order_ids = seen_ids["order_id"]
    subscription_ids = seen_ids["subscription_id"]
    ticket_ids = seen_ids["ticket_id"]

    # Remove IDs from order if they exist in subscription
    seen_ids["order_id"] -= subscription_ids
    # Remove IDs from ticket if they exist in order or subscription
    seen_ids["ticket_id"] -= (order_ids | subscription_ids)

    # Rebuild the details list to ensure no overlap
    details = (
        [{"ticket_id": ticket_id} for ticket_id in seen_ids["ticket_id"]] +
        [{"subscription_id": subscription_id} for subscription_id in seen_ids["subscription_id"]] +
        [{"order_id": order_id} for order_id in seen_ids["order_id"]]
    )

    return details

def generate_auth_header(api_key):
    """
    Generate the authorization header for FreshService API requests.
    """
    if sanitize_user_input(api_key):
        encoded_credentials = base64.b64encode(f"{api_key}:X".encode('utf-8')).decode('utf-8')
        return {
            "Content-Type": "application/json",
            "Authorization": f"Basic {encoded_credentials}"
        }
    else:
        sys.exit("Special characters or whitespaces are not allowed in the API key. Authentication failed.")

def fetch_ticket_conversations(ticket_id):
    """
    Fetch conversations and ticket details for a specific ticket from FreshService.

    Args:
    - ticket_id (int): The ID of the ticket.

    Returns:
    - list: Combined list of conversations and ticket details.
    """
    headers = generate_auth_header(FRESH_SERVICE_API_KEY)

    # Fetch conversations from FreshService
    try:
        conversations_url = f"{FRESH_SERVICE_BASE_URL}/tickets/{ticket_id}/conversations"
        conversations_response = requests.get(conversations_url, headers=headers)
        conversations_response.raise_for_status()

        conversations_data = conversations_response.json()
        if "conversations" not in conversations_data:
            raise Exception("Invalid response format: 'conversations' key not found.")

        conversations = conversations_data["conversations"]
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to fetch conversations: {e}")

    # Fetch ticket details from FreshService
    try:
        ticket_details_url = f"{FRESH_SERVICE_BASE_URL}/tickets/{ticket_id}"
        ticket_details_response = requests.get(ticket_details_url, headers=headers)
        ticket_details_response.raise_for_status()

        ticket_details_data = ticket_details_response.json()
        if "ticket" not in ticket_details_data:
            raise Exception("Invalid response format: 'ticket' key not found.")

        ticket = ticket_details_data["ticket"]
        subject = ticket.get("subject", "N/A")
        description_text = ticket.get("description_text", "N/A")

        # Add ticket details as a new conversation entry
        conversations.append({
            "body_text": f"Subject: {subject}\nDescription: {description_text}",
            "private": False  # Marking it public for inclusion in summaries
        })
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to fetch ticket details: {e}")

    return conversations

def get_customer_friendly_response(private_messages):
    """
    Generate a customer-friendly response summarizing the private messages.

    Args:
    - private_messages (list): A list of private messages (strings).

    Returns:
    - str: A customer-friendly response.
    """
    combined_text = "\n\n".join([f"Private Message {i+1}: {message}" for i, message in enumerate(private_messages)])

    prompt = f"""
    The following text contains technical details from private discussions about a customer's issue. 
    These messages are not accessible to the customer and may contain sensitive or internal information.

    Your first task is to:
    1. Summarize the overall issue based on the Subject & description then;
    2. Summarize these private messages into a customer-friendly response.
    3. Combine the ovearll summary and the private message summary into a single summary with a \n\n between the two summaries.
    4. Avoid sharing sensitive or technical details.
    5. Focus on providing a clear, reassuring update to the customer.
    6. Do not sign the reply with Best regards,\n[Your Name] or Sincerely etc.
    7. Do not label each of the summaries with ANYTHING.  Specifically \"Subject & Description Summary:\" or \"Private Messages Summary:\"

    Private Messages:
    {combined_text}

    Customer-Friendly Response:
    """

    try:
        response = client.chat.completions.create(model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=2000,
        temperature=0.4)
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"An error occurred: {e}"

def call_openai_api(model, messages):
    """
    Standardized method to call the OpenAI API using the OpenAI client library.
    """
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=2000,
            temperature=0.4
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error calling OpenAI API: {e}")
        return None
    
@app.route('/')
def index():
    """
    Render the HTML page for ticket input and response display.
    """
    return render_template_string('''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ticket Summarizer</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                margin: 20px;
            }
            label, textarea, input {
                display: block;
                margin: 10px 0;
            }
            textarea {
                width: 100%;
                height: 200px;
                padding: 10px;
                font-family: Arial, sans-serif;
                font-size: 14px;
            }
            input[type="text"], input[type="submit"] {
                padding: 10px;
                font-size: 14px;
            }
        </style>
    </head>
    <body>
        <h1>Ticket Summarizer</h1>
        <form action="/summarize_html" method="post">
            <label for="ticket_id">Enter Ticket ID:</label>
            <input type="text" id="ticket_id" name="ticket_id" required>
            <input type="submit" value="Summarize">
        </form>
        {% if response %}
        <h2>Response:</h2>
        <textarea readonly>{{ response }}</textarea>
        {% endif %}
    </body>
    </html>
    ''', response=None)

@app.route('/summarize_html', methods=['POST'])
def summarize_ticket_html():
    """
    Handle the form submission and display the response as formatted text.
    """
    ticket_id = request.form.get('ticket_id')
    try:
        conversations = fetch_ticket_conversations(ticket_id)

        private_messages = [
            conv.get("body_text", "")
            for conv in conversations
            if conv.get("private") == True
        ]

        if not private_messages:
            response = "No private messages found to summarize."
        else:
            response = get_customer_friendly_response(private_messages)
    except Exception as e:
        response = f"An error occurred: {e}"

    return render_template_string('''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ticket Summarizer</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                margin: 20px;
            }
            label, textarea, input {
                display: block;
                margin: 10px 0;
            }
            textarea {
                width: 100%;
                height: 200px;
                padding: 10px;
                font-family: Arial, sans-serif;
                font-size: 14px;
            }
            input[type="text"], input[type="submit"] {
                padding: 10px;
                font-size: 14px;
            }
        </style>
    </head>
    <body>
        <h1>Ticket Summarizer</h1>
        <form action="/summarize_html" method="post">
            <label for="ticket_id">Enter Ticket ID:</label>
            <input type="text" id="ticket_id" name="ticket_id" required>
            <input type="submit" value="Summarize">
        </form>
        <h2>Response:</h2>
        <textarea readonly>{{ response }}</textarea>
    </body>
    </html>
    ''', response=response)

@app.route('/api/summarize', methods=['POST'])
def summarize_text():
    """
    Handle a POST request to summarize input text using the given prompt.
    """
    try:
        # Parse JSON payload
        data = request.get_json()
        if not data or 'prompt' not in data:
            return jsonify({"error": "Missing 'prompt' field in JSON payload"}), 400

        prompt = data['prompt']

        # Call OpenAI GPT
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {
                    "role": "system", 
                    "content": 'You are CloudBlue Insight, a specialized bot designed to answer questions based on the CloudBlue Commerce documentation. Always provide answers by referencing the online documentation at: https://docs.cloudblue.com/cbc/21.0/home.htm . If the information cannot be found in the documentation, inform the user and suggest they contact CloudBlue Support, linking to the support page.'
                    },
                {"role": "user", "content": prompt}
            ],
            max_tokens=2000,
            temperature=0.4
        )

        # Extract the summarized content
        summary = response.choices[0].message.content.strip()

        return jsonify({"summary": summary})

    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

@app.route('/api/intent', methods=['POST'])
def detect_intent():
    data = request.json
    if 'prompt' not in data:
        return jsonify({"error": "Missing 'prompt' field"}), 400

    prompt = data['prompt']

    # Query OpenAI API for intent classification
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant that classifies user intents. "
                        "Focus on identifying the primary intent based on the user's question or request. "
                        "If the user includes information like 'order ID' or 'subscription ID,' use this as additional context, not as the primary intent indicator."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Classify the following prompt into one of the predefined intents: \n{prompt}\n\n"
                        "Predefined intents are:\n"
                        "- Trouble Tickets:\n"
                        "  - createTicket: Used when the user explicitly requests creating a ticket. Examples: \n"
                        "    - 'Can you open a ticket for me?'\n"
                        "    - 'Please create a ticket.'\n"
                        "    - 'I need help, can you log a ticket?'\n"
                        "  - getTicketUpdate: Used when the user asks for an update on a ticket. Examples: \n"
                        "    - 'What is the status of my ticket'\n"
                        "    - 'Can you update me on ticket number'\n"
                        "  - updateTicket: Used when the user wants to modify an existing ticket. Examples: \n"
                        "    - 'I need to update ticket'\n"
                        "    - 'Please make changes to ticket'\n"
                        "  - closeTicket: Used when the user asks to close a ticket. Examples: \n"
                        "    - 'Close ticket ID 12345.'\n"
                        "    - 'Please resolve and close ticket 54321.'\n"
                        "- How to Help:\n"
                        "  - howToHelp: Used when the user requests guidance or troubleshooting help. Examples: \n"
                        "    - 'Where can I find help in the documentation?'\n"
                        "    - 'Can you guide me on this process?'\n"
                        "  - integrationHelp: Used when the user requests help integrating systems. Examples: \n"
                        "    - 'I need help with integrating your API.'\n"
                        "- Order from Catalog:\n"
                        "  - listProducts: Used when the user requests a list of all products in the catalog. Examples: \n"
                        "    - 'Show me all products'\n"
                        "    - 'What products do you have?'\n"
                        "  - filterProducts: Used when the user requests filtering the catalog based on a keyword. Examples: \n"
                        "    - 'Show products related to laptops'\n"
                        "  - orderProduct: Used when the user wants to place an order from the catalog. Examples: \n"
                        "    - 'Order item ID 123'\n"
                        "    - 'I want to buy a laptop.'\n"
                        "- Manage Orders:\n"
                        "  - getOrderStatus: Used when the user asks for the status of a specific order. Examples: \n"
                        "    - 'What is the status of my order ID 98765?'\n"
                        "    - 'Track my order 12345.'\n"
                        "    - 'What is happening with order ID 12345'\n"
                        "  - listOpenOrders: Used when the user asks to see all open orders. Examples: \n"
                        "    - 'Show me all open orders'\n"
                        "  - listFailedOrders: Used when the user requests to see failed orders. Examples: \n"
                        "    - 'Show me failed orders'\n"
                        "  - cancelOrder: Used when the user wants to cancel an existing order. Examples: \n"
                        "    - 'Cancel order ID 12345.'\n"
                        "  - pushOrder: Used for when the user wants to resubmit an order due to an error. Examples: \n"
                        "    - 'Please resubmit order ID 12345'\n"
                        "    - 'Please try order ID 12345 again'\n"
                        "    - 'Please process order ID 12345'\n"
                        "  - orderReports: Used when the user requests order reports for a specific period. Examples: \n"
                        "    - 'Provide a report of all orders for January 2025.'\n"
                        "    - 'Generate a summary of orders placed last week.'\n"
                        "    - 'Give me a list of my last orders.'\n"
                        "    - 'Show me my last orders.'\n"
                        "    - 'Show me the orders from 01-01-2025 to 01-07-2025.'\n"
                        "    - 'Show me a list of orders with that failed payment?'\n"
                        "\nProvide the response ONLY as a JSON object containing 'intent', 'category', and 'certainty'."
                    )
                }
            ],
            max_tokens=300,
            temperature=0
        )

        # Parse OpenAI response for intent and classification
        response_content = response.choices[0].message.content.strip()
        intent_data = json.loads(response_content)
        intent = intent_data.get("intent")
        category = intent_data.get("category")
        certainty = intent_data.get("certainty", "0.2")  # Default to "0.2" as a string if not provided

        # Ensure certainty is a float
        try:
            certainty = round(float(certainty), 2)
        except ValueError:
            certainty = 1.0 if certainty.lower() == "high" else 0.2

    except json.JSONDecodeError:
        return jsonify({"error": "Invalid response format from OpenAI API."}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to classify intent: {str(e)}"}), 500

    # Extract IDs using the extract_ids function
    doc = nlp(prompt)
    details = extract_ids(doc)

    # Return response
    return jsonify({
        "classification": category,
        "intent": intent,
        "certainty": certainty,
        "details": details
    })

from workflow import initialize_database, handle_intent, generate_conversation_id, retrieve_context, save_context
from app import detect_intent  # Import detect_intent directly from summarize.py

@app.route('/api/conversation', methods=['POST'])
def conversation():
    """
    Handle conversation endpoint, ensuring conversation_id persists and details are updated.
    """
    data = request.json

    if 'prompt' not in data:
        return jsonify({"error": "Missing 'prompt' field"}), 400

    # Generate or use existing conversation_id
    conversation_id = data.get("conversation_id") or generate_conversation_id()

    # Retrieve conversation context
    context = retrieve_context(conversation_id)
    print("DEBUG: Retrieving Context Start of Conversation function")
    print(f"DEBUG: {json.dumps(context, indent=2)}")

    # Extract intent and details
    prompt = data['prompt']
    try:
        # Use detect_intent to classify intent and extract details
        intent_response = detect_intent()
        intent_data = intent_response.get_json()
        intent = intent_data.get("intent")
        details = intent_data.get("details", [])
        certainty = intent_data.get("certainty", 0.2)

        # Use extract_ids if no details are populated
        if not details:
            doc = nlp(prompt)
            details = extract_ids(doc)

        # Update context with new details
        print("DEBUG: Retrieving Context Mid of Conversation function before feeding handle intent")
        print(f"DEBUG: {json.dumps(context, indent=2)}")
        context.update({"intent": intent, "details": details})
        # Save updated context
        print("DEBUG: Saving Context in conversation ROUTE function")
        save_context(conversation_id, context)

        # Handle intent logic
        reply_data = handle_intent(intent, details, conversation_id)

        # Construct full response
        return jsonify({
            "conversation_id": conversation_id,
            "classification": intent_data.get("classification"),
            "intent": intent,
            "certainty": certainty,
            "details": details,
            "reply": reply_data.get("reply"),
            "next_step": reply_data.get("next_step")
        })

    except Exception as e:
        return jsonify({"error": f"Failed to process the conversation: {str(e)}"}), 500

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="Run the Flask app with a specified port.")
    parser.add_argument('--port', type=int, default=int(os.getenv("FLASK_PORT", 5000)),
                        help="Port number for the Flask app (default: 5000 or FLASK_PORT environment variable).")
    args = parser.parse_args()

    app.run(debug=True, host="0.0.0.0", port=args.port)
