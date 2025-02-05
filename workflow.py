import sqlite3
import uuid
import json
import re
import spacy
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

with open('config/config.json') as config_file:
    config = json.load(config_file)

OPENAI_API_KEY = config['api_keys']['openai']
client = OpenAI(api_key=OPENAI_API_KEY)
fs_user_id = config['user_profile']['fs_user_id']
nlp = spacy.load("en_core_web_sm")

# Lazy imports for detect_intent and extract_ids
def detect_intent(*args, **kwargs):
    from app import detect_intent as summarize_detect_intent
    return summarize_detect_intent(*args, **kwargs)

def extract_ids(doc):
    from app import extract_ids as summarize_extract_ids
    return summarize_extract_ids(doc)

# Lazy import for fetch_ticket_conversations
def fetch_ticket_conversations(ticket_id):
    from app import fetch_ticket_conversations as summarize_fetch_ticket_conversations
    return summarize_fetch_ticket_conversations(ticket_id)

# Lazy import for get_customer_friendly_response
def get_customer_friendly_response(private_messages):
    from app import get_customer_friendly_response as summarize_get_customer_friendly_response
    return summarize_get_customer_friendly_response(private_messages)

# Database Setup
def initialize_database():
    """Initialize the SQLite database to store conversation states."""
    conn = sqlite3.connect("conversations.db")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        context TEXT
    )''')
    conn.commit()
    conn.close()

# Generate Unique Conversation ID
def generate_conversation_id():
    """Generate a unique conversation ID."""
    return str(uuid.uuid4())

# Save Context to Database
def save_context(conversation_id, context):
    """Save or update the context for a given conversation ID."""
    try:
        conn = sqlite3.connect("conversations.db")
        conn.execute("PRAGMA journal_mode=WAL;")  # Enable WAL mode for better concurrency
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO conversations (conversation_id, context)
                          VALUES (?, ?)
                          ON CONFLICT(conversation_id) 
                          DO UPDATE SET context = excluded.context''',
                       (conversation_id, json.dumps(context)))
        conn.commit()
        print(f"DEBUG: Successfully saved context for {conversation_id}: {json.dumps(context, indent=2)}")
    except sqlite3.Error as e:
        print(f"ERROR: SQLite error occurred while saving context for {conversation_id}: {e}")
    except Exception as e:
        print(f"ERROR: Unexpected error while saving context for {conversation_id}: {e}")
    finally:
        conn.close()

# Retrieve Context from Database
def retrieve_context(conversation_id):
    """Retrieve the context for a given conversation ID."""
    try:
        conn = sqlite3.connect("conversations.db")
        conn.execute("PRAGMA journal_mode=WAL;")  # Enable WAL mode for better concurrency
        cursor = conn.cursor()
        cursor.execute('SELECT context FROM conversations WHERE conversation_id = ?', (conversation_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            try:
                context = json.loads(row[0])
                #print(f"DEBUG: Retrieved context for {conversation_id}: {json.dumps(context, indent=2)}")
                return context
            except json.JSONDecodeError as e:
                print(f"ERROR: Malformed JSON in database for {conversation_id}: {e}")
                # Return default context in case of JSON error
                return initialize_default_context()
        else:
            # Log that no context was found and return default
            default_context = initialize_default_context()
            print(f"DEBUG: No context found for {conversation_id}. Returning default: {json.dumps(default_context, indent=2)}")
            return default_context

    except sqlite3.Error as e:
        print(f"ERROR: SQLite error occurred while retrieving context for {conversation_id}: {e}")
        return initialize_default_context()
    except Exception as e:
        print(f"ERROR: Unexpected error while retrieving context for {conversation_id}: {e}")
        return initialize_default_context()

# Initialize Default Context
def initialize_default_context(intent=None):
    default_context = {
        #"next_step": "request_ticket_type",
        "intent": intent,
        "email": None,
        "ticket_type": None,
        "environment": None,
        "subject": None,
        "description": None,
        "details": [],  # Ensure this key exists
        "ticket_id": None
    }

    # Adjust default next_step based on intent
    if intent == "closeTicket":
        default_context["next_step"] = "wait_for_ticket_id"
    elif intent == "createTicket":
        default_context["next_step"] = "request_ticket_type"

    return default_context

# Intent Handler Framework
def handle_intent(intent, details, conversation_id):
    """Route intents to the appropriate handler function."""
    # Retrieve the current context for the conversation
    context = retrieve_context(conversation_id)
    print("DEBUG: Retrieving Context Start of handle_intent")
    print(f"DEBUG: {json.dumps(context, indent=2)}")

    # Add the latest prompt to the context
    if "prompt" in request.json:
        context["prompt"] = request.json["prompt"]

    # Use `next_step` for ongoing flows
    next_step = context.get("next_step")
    if next_step:
        print(f"DEBUG: Continuing flow with next_step: {next_step}")
        # Directly invoke the flow based on `next_step`
        if next_step.startswith("await_"):
            return handle_create_ticket(context, details, conversation_id)
        elif next_step.startswith("wait_for_"):
            return handle_ticket_close(context, details, conversation_id)

    # Fallback to intent detection for new interactions
    if not intent or intent == "unknown":
        next_step = context.get("next_step")
        if next_step and next_step.startswith("await_"):
            print("DEBUG: Fallback to next_step flow for ongoing conversation.")
            return handle_create_ticket(context, details, conversation_id)
        elif next_step.startswith("wait_for_"):
            return handle_ticket_close(context, details, conversation_id)

    else:
        # Handle recognized intents
        if intent == "createTicket":
            response = handle_create_ticket(context, details, conversation_id)
        elif intent == "closeTicket":
            response = handle_ticket_close(context, details, conversation_id)
        elif intent == "getTicketUpdate":
            response = handle_get_ticket_update(context, details)
        elif intent == "howToHelp":
            response = handle_how_to_help(context)
        elif intent == "integrationHelp":
            response = handle_integration_help(context)
        elif intent == "orderReports":
            response = handle_order_reports(context)
        elif intent == "getOrderStatus":
            response = handle_get_order_info(context, details)
        elif intent == "pushOrder":
            response = handle_order_resubmission(context, details)
        elif intent == "cancelOrder":
            response = handle_order_cancellation(context, details)
        else:
            response = {
                "reply": "Sorry, we're still working on CeeBee and working on the kinks. \n\n I can help you open a ticket, get an update on a ticket, close a ticket, get help on how to use CloudBlue, show you how to integrate to CloudBlue, or help you find orders and information about your orders.",
                "next_step": "unsupported"
            }

    # Merge the response into the context
    context.update(response)

    # Persist the updated context
    print("DEBUG: Saving Context in handle_intent function")
    print(f"DEBUG: {json.dumps(context, indent=2)}")
    save_context(conversation_id, context)

    # Return the response (usually contains `reply` and `next_step`)
    return response

# Handle getTicketUpdate Intent
def handle_get_ticket_update(context, details):
    """
    Handle the getTicketUpdate intent logic.
    """
    # Lazy import for fetch_ticket_conversations
    def fetch_ticket_conversations(ticket_id):
        from app import fetch_ticket_conversations as summarize_fetch_ticket_conversations
        return summarize_fetch_ticket_conversations(ticket_id)

    # Lazy import for get_customer_friendly_response
    def get_customer_friendly_response(private_messages):
        from app import get_customer_friendly_response as summarize_get_customer_friendly_response
        return summarize_get_customer_friendly_response(private_messages)

    # Extract the ticket_id from details
    ticket_id = None
    for detail in details:
        if "ticket_id" in detail:
            ticket_id = detail["ticket_id"]
            break  # Stop after finding the first valid ticket_id

    # Validate ticket_id
    if not ticket_id:
        return {
            "reply": "Unable to find a valid ticket ID. Please provide a ticket ID, e.g., **765884**.",
            "next_step": "request_ticket_id"
        }

    # Make API call to Freshservice to check if the ticket exists and fetch details
    try:
        # Check if ticket exists and fetch conversations
        conversations = fetch_ticket_conversations(ticket_id)

        # If conversations are empty, ticket does not exist
        if not conversations:
            return {
                "reply": f"The ticket ID **{ticket_id}** does not exist. Please provide a valid ticket ID.",
                "next_step": "request_ticket_id"
            }

        # Extract private messages from conversations
        private_messages = [
            conv.get("body_text", "")
            for conv in conversations
            if conv.get("private")
        ]

        # Generate customer-friendly response
        if private_messages:
            customer_friendly_response = get_customer_friendly_response(private_messages)
            return {
                "reply": customer_friendly_response + "\n\n Is there anything else I can help with?",
                "next_step": "complete"
            }
        else:
            return {
                "reply": f"No major info was found for ticket ID **{ticket_id}**. Please provide further details or a different ticket ID.",
                "next_step": "request_ticket_id"
            }

    except Exception as e:
        return {
            "reply": f"An error occurred while retrieving the ticket details: {e}",
            "next_step": "error"
        }

def handle_create_ticket(context, details, conversation_id):
    """
    Handle the createTicket intent logic.
    """
    # Nested function to create a ticket (used later in the flow)
    def create_ticket(payload):
        from app import generate_auth_header, FRESH_SERVICE_API_KEY, FRESH_SERVICE_BASE_URL
        import requests
        url = f"{FRESH_SERVICE_BASE_URL}/tickets"
        headers = generate_auth_header(FRESH_SERVICE_API_KEY)

        # Submit the request
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()

        # Debug the raw response
        response_data = response.json()
        print(f"DEBUG: Create ticket raw response: {json.dumps(response_data, indent=2)}")
        return response_data

    # Derive the current step
    step = context.get("next_step", "request_ticket_type")
    print("DEBUG: Starting handle_create_ticket")
    print(f"DEBUG: Step derived from context: {step}")
    print(f"DEBUG: Retrieved context: {json.dumps(context, indent=2)}")

    # Process the latest prompt
    prompt = request.json.get("prompt", "").strip().lower()
    # print(f"DEBUG: Received prompt: {prompt}")

    # Handle steps
    if step == "request_ticket_type":
        context["next_step"] = "await_ticket_type"
        context["reply"] = "Before we start, what kind of request would you like to open? \n\n **Service Request** (configuration change or to publish a new product) \n\n or \n\n **Incident** (something is broken or you are experiencing unexpected behavior)?"
        print("DEBUG: Saving Context in request_ticket_type step")
        save_context(conversation_id, context)

    elif step == "await_ticket_type":
        #prompt = request.json.get("prompt", "").strip().lower()
        if prompt not in ["incident", "service request"]:
            context["reply"] = "Please specify if this is an **Incident** or a **Service Request**."
            return {"reply": context["reply"], "next_step": "await_ticket_type"}  # Early return for invalid input

        context["ticket_type"] = prompt

        if prompt == "service request":
            context["next_step"] = "complete"
            context["reply"] = (
                "The most common **Service Requests** can be found in our new [Service Catalog](https://support.cloudblue.com/support/catalog/items?category_id=23000084502). "
                "If you cannot find an option option that fits your needs, please use the **Generic Request** option."
            )
            return {"reply": context["reply"], "next_step": "complete"}
        
        context["next_step"] = "await_email"
        context["intent"] = "createTicket"
        context["reply"] = "To make sure we can get back to you, please provide an email address to proceed."

    elif step == "await_email":
        #prompt = request.json.get("prompt", "").strip().lower()
        if not re.match(r"[^@]+@[^@]+\.[^@]+", prompt):
            context["reply"] = "Please provide a valid email address."
            return {"reply": context["reply"], "next_step": "await_email"}  # Early return for invalid input

        context["email"] = prompt
        context["next_step"] = "await_environment"
        context["intent"] = "createTicket"
        context["reply"] = "Which environment are you experiencing this issue in? \n\n **Production** \n\n **Staging** \n\n **Development**"

    elif step == "await_environment":
        if prompt not in ["production", "staging", "development"]:
            context["reply"] = "Please specify an environment from one of these three options: **Production**, **Staging**, or **Development**."
            return {"reply": context["reply"], "next_step": "await_environment"}  # Early return for invalid input

        context["environment"] = prompt.capitalize()
        context["next_step"] = "await_subject"
        context["intent"] = "createTicket"
        context["reply"] = "To make the request easier to find later, please provide me with a super short description."

    elif step == "await_subject":
        if not prompt:
            context["reply"] = "Please provide a brief description for your problem, e.g., **Order failing on checkout**."
            return {"reply": context["reply"], "next_step": "await_subject"}  # Early return for invalid input

        context["subject"] = prompt
        context["next_step"] = "await_description"
        context["intent"] = "createTicket"
        context["reply"] = "To help our engineers, can you describe the problem in full detail? \n\n Please make sure to provide details such as **Customer IDs**, **Subscription IDs**, **Order IDs**, etc. \n Such additional details help us provide better and faster answers." 

    elif step == "await_description":
        if not prompt:
            context["reply"] = "Please provide a detailed description of the problem."
            return {"reply": context["reply"], "next_step": "await_description"}  # Early return for invalid input

        context["description"] = prompt
        context["next_step"] = "await_reproduction_steps"
        context["intent"] = "createTicket"
        context["reply"] = "What steps can reproduce the problem?"

    elif step == "await_reproduction_steps":
        if not prompt:
            context["reply"] = "To ensure we can find and respolve the exact issue you are experiencing, please provide the steps to reproduce the problem."
            return {"reply": context["reply"], "next_step": "await_reproduction_steps"}  # Early return for invalid input

        context["reproduction_steps"] = prompt
        context["next_step"] = "await_submission"
        context["intent"] = "createTicket"
        context["reply"] = (
            "Here is a summary of your ticket:\n\n"
            f"- **Ticket Type**: {context.get('ticket_type', 'N/A')}\n"
            f"- **Email**: {context.get('email', 'N/A')}\n"
            f"- **Environment**: {context.get('environment', 'N/A')}\n\n"
            f"- **Subject**: {context.get('subject', 'N/A')}\n"
            f"- **Description**: {context.get('description', 'N/A')}\n"
            f"- **Reproduction Steps**: \n {context.get('reproduction_steps', 'N/A')}\n\n"
            "Would you like to submit this ticket? (**Yes** / **No**)"
        )

    elif step == "await_submission":
        if prompt not in ["yes", "no"]:
            context["reply"] = "Please respond with '**Yes**' to submit the ticket or '**No**' to start over."
            return {"reply": context["reply"], "next_step": "await_submission"}

        if prompt == "no":
            context["next_step"] = "request_ticket_type"
            context["reply"] = "Restarting ticket creation process. what kind of request would you like to open? \n\n **Service Request** (configuration change or to publish a new product) \n or \n **Incident** (something is broken or you are experiencing unexpected behavior)?"
            return {"reply": context["reply"], "next_step": "request_ticket_type"}

        # If Yes, proceed to ticket submission
        context["next_step"] = "await_submit_ticket"
        context["intent"] = "createTicket"
        context["ticketType"] = "Incident"
        context["email"] = context.get('email', '')
        context["environment"] = context.get('environment', 'Production')
        context["subject"] = context.get('subject', '')
        context["description"] = context.get('description', '')
        context["reproduction_steps"] = context.get('reproduction_steps', '')
        context["priority"] = 3

        # If Yes, proceed directly to ticket submission
        try:
            payload = {
                "type": "Incident",
                "description": f"{context.get('description', '')}\n\ncontext.get('environment', 'Production')\n\n{context.get('reproduction_steps', '')}",
                "subject": context.get("subject", ""),
                "email": context.get("email", "default@example.com"),
                "priority": context.get("priority", 3),
                "department_id": 32000018050,
                "custom_fields": {
                    "environment": context.get("environment", "Production"),
                    "ticket_type": "Incident or Problem",
                    "incident_type": "Technical issue"
                }
            }
            print(f"DEBUG: Create ticket payload: {json.dumps(payload, indent=2)}")
            ticket_response = create_ticket(payload)

            # Debug the parsed response
            print(f"DEBUG: Ticket response: {json.dumps(ticket_response, indent=2)}")

            # Extract the ticket ID
            ticket_id = ticket_response.get("ticket", {}).get("id", "N/A") # Default to "N/A" if `id` is not found
            if ticket_id == "N/A":
                print("WARNING: Ticket ID not found in the response.")

            context["next_step"] = "complete"
            context["reply"] = f"Your ticket has been created successfully! The ticket ID is **{ticket_id}**.\n\nIs there anything else we can help you with?"

        except Exception as e:
            context["next_step"] = "error"
            context["reply"] = f"An error occurred while creating the ticket: {e}"


    else:
        context["reply"] = "Our cool new CeeBee Assistant is still learning some new tricks, and what you’re trying to do isn’t fully supported yet. \n Please check back soon—we’re working on it!"
        context["next_step"] = "unsupported"

    # Save the updated context after all changes
    print("DEBUG: Saving Context at the end of handle_create_ticket function")
    save_context(conversation_id, context)
    return {"reply": context["reply"], "next_step": context["next_step"]}

def call_openai_api(model="gpt-4", messages=None, max_tokens=2000):
    """
    Standardized method to call the OpenAI API using the OpenAI client library.
    Logs the full JSON response from OpenAI to the console for troubleshooting.
    """
    try:
        if not model:
            model = "gpt-4"
        if not messages:
            messages = []

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.4
        )

        # Convert the response to a serializable format if needed
        if hasattr(response, "to_dict_recursive"):
            response_dict = response.to_dict_recursive()
        elif hasattr(response, "to_dict"):
            response_dict = response.to_dict()
        else:
            raise TypeError("OpenAI API response object is not serializable.")

        # Log the JSON response in a pretty format
        print("DEBUG: CeeBee API response:", json.dumps(response_dict, indent=2))

        return response_dict["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Error calling OpenAI API: {e}")
        return None
    
def clean_reply(reply):
    """
    Clean and format the reply to ensure proper handling of URLs and markdown.
    """
    import re

    # Replace unintended newlines within URLs
    reply = re.sub(r"(https?://\S+)[\n\r]+(\S+)", r"\1\2", reply)
    
    # Additional cleaning logic can be added here if needed
    return reply
    
def handle_how_to_help(context):
    """
    Handle the howToHelp intent logic by leveraging the standardized OpenAI API call structure.
    """
    user_input = context.get("prompt", "")

    # Define the assistant prompt
    system_prompt = (
        "You are CloudBlue Insight, a specialized bot designed to answer questions based on the CloudBlue Commerce documentation. "
        "Always provide answers by referencing the online documentation at: "
        "https://docs.cloudblue.com/cbc/21.0/home.htm"
        "If the information cannot be found in the documentation, inform the user and suggest they contact their Technical Account Manager (TAM) or CloudBlue Support, "
        "https://support.cloudblue.com/"
        "Please ensure that your responses are clear and easy to understand.  Please use extensive markup to make the information more readable."
        "Please make sure that all urls and links are clickable in your response and ensure that there is a space or a period after the link, depending on if it is mid sentance or at the end of a sentance."
        "As the end of your reply insert two return lines in markdown \n\n and then ask them if they have any other questions about CloudBlue or if you can help with something else."
    )

    # Prepare the messages payload
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]

    # Use the existing OpenAI API handler method
    response_data = call_openai_api("gpt-4", messages)

    if not response_data:
        return {
            "reply": "An error occurred while processing your request. Please try again later.",
            "next_step": "error"
        }

    # Extract and return the reply from the assistant
    try:
        assistant_reply = clean_reply(response_data.strip())
        return {
            "reply": assistant_reply,
            "next_step": "complete"
        }
    except (KeyError, IndexError) as e:
        return {
            "reply": f"Unexpected response format from CeeBee: {e}",
            "next_step": "error"
        }

def handle_integration_help(context):
    """
    Handle the integrationHelp intent logic by leveraging the standardized OpenAI API call structure.
    """
    user_input = context.get("prompt", "")

    # Define the assistant prompt
    system_prompt = (
        "You are CloudBlue Insight, a specialized bot designed to answer questions based on the CloudBlue Commerce API documentation. "
        "Always provide answers by referencing the online documentation at: "
        "https://docs.cloudblue.com/cbc/sdk/21.0/"
        "If the information cannot be found in the documentation, inform the user and suggest they contact their Technical Account manager or CloudBlue Support, "
        "https://support.cloudblue.com/"
        "Please ensure that your responses are clear and easy to understand.  Please use extensive markup to make the information more readable."
        "Please make sure that all urls and links are clickable in your response and ensure that there is a space or a period after the link, depending on if it is mid sentance or at the end of a sentance."
        "As the end of your reply, please ask them if they have any other questions about integration or if you can help with something else. Make sure you add in a \n\n in your reply before asking this question."
    )

    # Prepare the messages payload
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]

    # Use the existing OpenAI API handler method
    response_data = call_openai_api("gpt-4", messages)

    if not response_data:
        return {
            "reply": "An error occurred while processing your request. Please try again later.",
            "next_step": "error"
        }

    # Extract and return the reply from the assistant
    try:
        assistant_reply = clean_reply(response_data.strip())
        return {
            "reply": assistant_reply,
            "next_step": "complete"
        }
    except (KeyError, IndexError) as e:
        return {
            "reply": f"Unexpected response format from OpenAI: {e}",
            "next_step": "error"
        }

def handle_ticket_close(context, details, conversation_id):
    """
    Handle the closeTicket intent logic with multi-step workflow.
    """
    FS_USER_ID = config['user_profile']['fs_user_id']
    FS_USER_EMAIL = config['user_profile']['person_email']
    
    # Extract the ticket_id from details if available
    ticket_id = context.get("ticket_id") or None
    for detail in details:
        if "ticket_id" in detail:
            ticket_id = detail["ticket_id"]
            if ticket_id:
                context["ticket_id"] = ticket_id
            break  # Stop after finding the first valid ticket_id 

    # Derive the current step
    step = context.get("next_step", "wait_for_request_ticket_id" if not ticket_id else "wait_for_ask_for_closing_message")
    print(f"DEBUG: Starting handle_ticket_close with step: {step}")
    print(f"DEBUG: Current context: {json.dumps(context, indent=2)}")

    # Process the latest prompt
    prompt = request.json.get("prompt", "").strip().lower()
    print(f"DEBUG: Received prompt: {prompt}")

    # Handle steps
    if step == "wait_for_request_ticket_id" and not ticket_id:
        # Ask the user for the ticket ID
        context["next_step"] = "wait_for_ticket_id"
        context["intent"] = "closeTicket"
        context["reply"] = "Clould you please provide the ticket ID you want to close?"
        context["ticket_id"] = ticket_id
        save_context(conversation_id, context)

    elif step == "wait_for_ticket_id":
        # Use `extract_ids` to find a ticket ID in the user input
        doc = nlp(prompt)
        extracted_details = extract_ids(doc)
        for detail in extracted_details:
            if "ticket_id" in detail:
                ticket_id = detail["ticket_id"]
                break

        if not ticket_id:
            context["reply"] = "Invalid ticket ID. Please provide a valid ticket ID."
            return {"reply": context["reply"], "next_step": "wait_for_ticket_id"}  # Early return

        # Save the valid ticket ID to the context
        context["ticket_id"] = ticket_id
        context["details"] =details

        # Validate ticket existence
        if not validate_ticket(ticket_id):
            context["reply"] = f"The ticket ID **{ticket_id}** does not exist. Please provide a valid ticket ID."
            return {"reply": context["reply"], "next_step": "wait_for_ticket_id"}  # Early return

        context["next_step"] = "wait_for_ask_for_closing_message"
        context["intent"] = "closeTicket"
        context["ticket_id"] = ticket_id
        context["details"] =details
        context["reply"] = "Would you like to add a message to the ticket before closing it? (Yes/No)"
        print(f"DEBUG: Starting handle_ticket_close with step: {step}")
        print(f"DEBUG: Current context: {json.dumps(context, indent=2)}")
        save_context(conversation_id, context)

    elif step == "wait_for_ask_for_closing_message":
        context["ticket_id"] = ticket_id
        context["details"] =details
        if prompt not in ["yes", "no"]:
            context["ticket_id"] = ticket_id
            context["details"] =details
            context["reply"] = "Please respond with 'Yes' to add a message or 'No' to close now without a message."
            return {"reply": context["reply"], "next_step": "wait_for_ask_for_closing_message"}  # Early return

        if prompt == "no":
            # Request closure directly with a default message
            payload = {
                "body": "The issue has been resolved. Please close the ticket.",
                "user_id": FS_USER_ID
            }
            try:
                reply_ticket(payload, context["ticket_id"])
                context["next_step"] = "complete"
                context["reply"] = f"The ticket ID **{context['ticket_id']}** has been updated with a closure request."
            except Exception as e:
                context["next_step"] = "error"
                context["reply"] = f"Failed to request ticket closure: {str(e)}"
            save_context(conversation_id, context)
            return {"reply": context["reply"], "next_step": context["next_step"]}

        # If 'yes', proceed to collect the user's message
        context["next_step"] = "wait_for_closing_message"
        context["intent"] = "closeTicket"
        context["ticket_id"] = ticket_id
        context["details"] =details
        context["reply"] = "Please provide the message you'd like to add to the ticket before closing it."
        print(f"DEBUG: Starting handle_ticket_close after YES is provided.")
        print(f"DEBUG: About to be saved : {json.dumps(context, indent=2)}")
        save_context(conversation_id, context)

    elif step == "wait_for_closing_message":
        # Ensure a valid message is provided
        context["ticket_id"] = ticket_id
        context["details"] =details
        if not prompt:
            context["reply"] = "Please provide a valid message to add to the ticket."
            return {"reply": context["reply"], "next_step": "wait_for_closing_message"}  # Early return

        user_message = prompt
        payload = {
            "body": f"The issue has been resolved. Please close the ticket.\n\n{user_message}",
            "user_id": FS_USER_ID
        }
        try:
            reply_ticket(payload, context["ticket_id"])
            context["next_step"] = "complete"
            context["intent"] = "closeTicket"
            context["reply"] = (
                f"The ticket ID **{context['ticket_id']}** has been updated with your message and permission to close the ticket."
            )
        except Exception as e:
            context["next_step"] = "error"
            context["reply"] = f"Failed to update the ticket with your message: {str(e)}"

    else:
        # Handle unsupported steps
        context["next_step"] = "unsupported"
        context["reply"] = "Our cool new CeeBee Assistant is still learning some new tricks, and what you’re trying to do isn’t fully supported yet. \n Please check back soon, we’re working on it!"

    # Save updated context after processing
    print(f"DEBUG: Saving Context in handle_ticket_close, step: {step}")
    context["ticket_id"] = ticket_id
    context["details"] =details
    save_context(conversation_id, context)
    return {"reply": context["reply"], "next_step": context["next_step"]}

def validate_ticket(ticket_id):
    from app import generate_auth_header, FRESH_SERVICE_API_KEY, FRESH_SERVICE_BASE_URL
    import requests
    url = f"{FRESH_SERVICE_BASE_URL}/tickets/{ticket_id}"
    headers = generate_auth_header(FRESH_SERVICE_API_KEY)

    # Submit the request
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    # Debug the raw response
    response_data = response.json()
    print(f"DEBUG: Validate ticket raw response: {json.dumps(response_data, indent=2)}")
    return response_data

def reply_ticket(payload, ticket_id):
    """
    Submit a reply to a ticket in FreshService and log detailed responses for debugging.
    """
    from app import generate_auth_header, FRESH_SERVICE_API_KEY, FRESH_SERVICE_BASE_URL
    import requests

    url = f"{FRESH_SERVICE_BASE_URL}tickets/{ticket_id}/reply"
    headers = generate_auth_header(FRESH_SERVICE_API_KEY)

    try:
        # Submit the request
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()  # Raise HTTPError for bad HTTP responses (4xx and 5xx)

        # Parse and debug the raw response
        response_data = response.json()
        print(f"DEBUG: Successful ticket reply response: {json.dumps(response_data, indent=2)}")
        return response_data

    except requests.exceptions.HTTPError as e:
        # Log detailed error information
        print(f"ERROR: HTTPError occurred: {e}")
        if e.response is not None:
            try:
                error_response = e.response.json()  # Parse response if JSON
                print(f"ERROR: FreshService API Error Response: {json.dumps(error_response, indent=2)}")
            except ValueError:
                print(f"ERROR: Non-JSON Error Response: {e.response.text}")
        raise  # Re-raise the exception for upstream handling

    except Exception as e:
        # Catch all other exceptions
        print(f"ERROR: Unexpected error during ticket reply: {str(e)}")
        raise

def handle_order_reports(context):
    """
    Handle the orderReports intent logic by querying the commerce API and filtering the order data.
    """
    user_input = context.get("prompt", "")

    # Fetch orders from the API
    try:
        # Define the API endpoint for fetching orders
        orders_endpoint = (
            "resources/88a64097-6581-4b50-9745-26843f37461c/orders/"
            "?in(type,(SO,CF,CH,CL,DG,UG,RN,CH,TA,TS)),sort(-orderDate),limit(0,40)"
        )

        # Call the commerce API to fetch orders
        orders = call_commerce_api(orders_endpoint, method="GET")

        if not isinstance(orders, list):
            raise ValueError("The orders data is not in the expected format (list).")
    except Exception as e:
        return {
            "reply": f"An error occurred while fetching orders from the API: {e}",
            "next_step": "error"
        }

    # Filter orders by orderDate (2024 and after)
    filtered_orders = [
        order for order in orders
        if "orderDate" in order and order["orderDate"] >= "2024-11-19" and order.get("total", {}).get("value", 0) > 0
    ]

    # Sort the filtered orders by orderDate (newest first) and limit to 100 orders
    filtered_orders = sorted(filtered_orders, key=lambda x: x["orderDate"], reverse=True)[:100]

    # Transform the orders to a smaller payload
    transformed_orders = [
        {
            "orderId": order["orderId"],
            "internalId": order["internalId"],
            "orderNumber": order["orderNumber"],
            "total": order["total"],
            "status": order["status"],
            "paymentStatus": order["paymentStatus"],
            "provisioningStatus": order["provisioningStatus"],
            "orderDate": order["orderDate"],
            "endCustomerName": order["endCustomerName"],
            "sourceSystem": order.get("sourceSystem", "N/A")
        }
        for order in filtered_orders
    ]

    # Echo how many orders were selected
    print(f"DEBUG: Number of orders selected: {len(filtered_orders)}")

    # Define the assistant prompt
    system_prompt = (
        "You are CloudBlue Insight, a specialized bot designed to answer questions based on order data. "
        "You will receive filtered order details in JSON format along with a user's query. "
        "Your task is to analyze the order data and respond to the user's query based on the provided data. "
        "If the query cannot be answered with the provided data, inform the user and suggest alternative steps. "
        "Keep your response concise and directly address the user's query."
        "For the table, please do not print the value orderId. Instead, make the value in the table for orderNumber a clickable URL that MUST OPEN the link in a new browser tab. Here is how the links are formed: "
        "https://hpeinc.demos.cloudblue.com/ccp/v/pa/ux1-ui/order-details?orderId=32542b3c-6f91-4037-abe5-ecf5f0752eef "
        "See where it has string orderId=32542b3c-6f91-4037-abe5-ecf5f0752eef replace the value with the orderId value from the users' order data."
    )

    # Prepare the messages payload
    compact_orders = json.dumps(transformed_orders, separators=(",", ":"))
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Here is the filtered order data: {compact_orders}"},
        {"role": "user", "content": user_input}
    ]

    # Use the OpenAI API handler method with a smaller max_tokens value
    response_data = call_openai_api("gpt-4", messages, max_tokens=2000)

    if not response_data:
        return {
            "reply": "An error occurred while processing your request. Please try again later.",
            "next_step": "error"
        }

    # Extract and return the reply from the assistant
    try:
        assistant_reply = clean_reply(response_data.strip())
        return {
            "reply": assistant_reply,
            "next_step": "complete"
        }
    except (KeyError, IndexError) as e:
        return {
            "reply": f"Unexpected response format from OpenAI: {e}",
            "next_step": "error"
        }

def call_commerce_api(endpoint, method='GET', payload=None, params=None):
    """
    Makes a request to the commerce API using configurations from 'config.json' and logs the request/response.

    :param endpoint: API endpoint (relative to the base URL).
    :param method: HTTP method ('GET' or 'POST').
    :param payload: Payload for POST requests (as a dictionary).
    :param params: Query parameters for GET requests (as a dictionary).
    :return: JSON response or raises an HTTP error.
    """
    try:
        with open('config/config.json') as config_file:
            config = json.load(config_file)

        APS_TOKEN = config['aps_info']['aps_token']
        BASE_URL = config['aps_info']['aps_endpoint'].rstrip('/')

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        headers = {
            'Content-Type': 'application/json',
            'APS-Token': APS_TOKEN
        }

        # Print the request details
        print(f"DEBUG: Making {method.upper()} request to {url}")
        if params:
            print(f"DEBUG: Query Parameters:\n{json.dumps(params, indent=2)}")
        if payload:
            print(f"DEBUG: Payload:\n{json.dumps(payload, indent=2)}")

        # Make the request
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, params=params)
        elif method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=payload)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}. Use 'GET' or 'POST'.")

        # Raise an error for bad HTTP responses
        response.raise_for_status()

        # Parse and print the response
        try:
            response_json = response.json()
            print(f"DEBUG: Response JSON:\n{json.dumps(response_json, indent=2)}")
            return response_json
        except ValueError:
            # Non-JSON response
            print(f"DEBUG: Response Text:\n{response.text}")
            return response.text

    except FileNotFoundError:
        raise Exception("Configuration file 'config.json' not found.")
    except KeyError as e:
        raise Exception(f"Missing required configuration key: {e}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"An error occurred while making the API request: {e}")

def handle_get_order_info(context, details):
    """
    Handles the intent to fetch order information and process it for OpenAI.

    Args:
        context (dict): The current conversation context.
        details (list): List of extracted details including the order ID.

    Returns:
        dict: A structured response containing a reply and next step.
    """
    # Extract the order_id from details
    order_number = None
    for detail in details:
        if "order_id" in detail:
            order_number = detail["order_id"]
            break  # Stop after finding the first valid order_id

    # Validate order_number
    if not order_number:
        return {
            "reply": "Unable to find a valid order ID. Please provide a valid order ID, e.g., **SO000099**.",
            "next_step": "request_order_id"
        }

    try:
        # First API call to find the order by order number
        order_search_endpoint = f"services/order-manager/orders?like(orderNumber,{order_number}),select(orderDetails)"
        order_search_response = call_commerce_api(order_search_endpoint, method="GET")

        if not order_search_response or not isinstance(order_search_response, list):
            raise ValueError("Order search API response is invalid or empty.")

        # Extract the orderId from the first order in the response
        order_data = order_search_response[0]
        order_id = order_data.get("orderId")
        if not order_id:
            raise ValueError("Order ID not found in the order search response.")

        # Second API call to fetch detailed order information
        order_details_endpoint = f"resources/88a64097-6581-4b50-9745-26843f37461c/orders/{order_id}"
        order_details_response = call_commerce_api(order_details_endpoint, method="GET")

        if not order_details_response or not isinstance(order_details_response, dict):
            raise ValueError("Order details API response is invalid or empty.")

        # Extract relevant information
        end_customer_name = order_details_response.get("endCustomerName", "N/A")
        order_id = order_details_response.get("orderId", "N/A")
        internal_id = order_details_response.get("internalId", "N/A")
        type_ = order_details_response.get("type", "N/A")
        description = order_details_response.get("description", "N/A")
        creator = order_details_response.get("creator", "N/A")
        original_user = order_details_response.get("originalUser", "N/A")
        total = order_details_response.get("total", {}).get("value", "N/A")
        total_currency = order_details_response.get("total", {}).get("code", "N/A")
        payment_status = order_details_response.get("paymentStatus", "N/A")
        provisioning_status = order_details_response.get("provisioningStatus", "N/A")
        status = order_details_response.get("status", "N/A")
        error_details = order_details_response.get("errorDetails", {}).get("en_US", "N/A")

        # Extract the reason from errorDetails if available
        error_reason = "N/A"
        match = re.search(r"Reason: (.+?)(?:\.|\\n|$)", error_details)
        if match:
            error_reason = match.group(1).strip()

        possible_push_to_status = order_details_response.get("possiblePushToStatus", {})
        actions = "\n".join(
            [f"o {key}: {value.get('en_US', 'N/A')}" for key, value in possible_push_to_status.items()]
        )

        # Prepare data for OpenAI
        openai_prompt = (
            "The following contains details of an order.\n\n"
            f"End Customer\n• Name: {end_customer_name}\n\n"
            f"General Information\n• Order ID: {order_id}\n• Internal ID: {internal_id}\n"
            f"• Order Number: {order_number}\n• Type: {type_}\n• Description: {description}\n"
            f"• Creator: {creator}\n• Original User: {original_user}\n\n"
            f"Financials\n• Total: ${total} {total_currency}\n• Payment Status: {payment_status}\n\n"
            f"Provisioning Details\n• Status: {status}\n• Provisioning Status: {provisioning_status}\n"
            f"• Error Details:\n  • {error_details}\n  • Reason: {error_reason}\n\n"
            f"Actions\n• Possible statuses for resolution:\n{actions}\n\n"
            "Based on the information provided, provide a meaningful summary for the user. "
            "Focus on errors and their resolution suggestions if applicable."
        )

        # Call OpenAI API
        openai_response = call_openai_api(
            model="gpt-4",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant summarizing order details. "
                        "Please make sure your response is well formatted and uses rich markdown so the answer is clear and "
                        "the user can understand the results easily. Maximum size of text should be no more than standard text size using **bold** for markdown."
                        "If field 'reason' in 'errorDetails' is not null, please summarize that text for the user and put it at the top of your reply followed by two newlines."
                        "please remove RB: Proceed with provisioning, from your response.  Add at the end of the list 'Contact Support'"
                        "if an error, please add \n\n to the end of your response and ask 'Which of these options would you like to proceed with'"
                    )
                },
                {"role": "user", "content": openai_prompt}
            ]
        )

        return {
            "reply": openai_response,
            "next_step": "complete"
        }

    except Exception as e:
        print(f"Error in handle_get_order_info: {e}")
        return {
            "reply": f"An error occurred while processing the order information: {e}",
            "next_step": "error"
        }

    """
    Handles the intent to fetch order information and process it for OpenAI.

    Args:
        context (dict): The current conversation context.
        details (list): List of extracted details including the order ID.

    Returns:
        dict: A structured response containing a reply and next step.
    """
    # Extract the order_id from details
    order_number = None
    for detail in details:
        if "order_id" in detail:
            order_number = detail["order_id"]
            break  # Stop after finding the first valid order_id

    # Validate order_number
    if not order_number:
        return {
            "reply": "Unable to find a valid order ID. Please provide a valid order ID, e.g., **SO000099**.",
            "next_step": "request_order_id"
        }

    try:
        # First API call to find the order by order number
        order_search_endpoint = f"services/order-manager/orders?like(orderNumber,{order_number}),select(orderDetails)"
        order_search_response = call_commerce_api(order_search_endpoint, method="GET")

        if not order_search_response or not isinstance(order_search_response, list):
            raise ValueError("Order search API response is invalid or empty.")

        # Extract the orderId from the first order in the response
        order_data = order_search_response[0]
        order_id = order_data.get("orderId")
        if not order_id:
            raise ValueError("Order ID not found in the order search response.")

        # Second API call to fetch detailed order information
        order_details_endpoint = f"resources/88a64097-6581-4b50-9745-26843f37461c/orders/{order_id}"
        order_details_response = call_commerce_api(order_details_endpoint, method="GET")

        if not order_details_response or not isinstance(order_details_response, dict):
            raise ValueError("Order details API response is invalid or empty.")

        # Extract relevant information
        end_customer_name = order_details_response.get("endCustomerName", "N/A")
        order_id = order_details_response.get("orderId", "N/A")
        internal_id = order_details_response.get("internalId", "N/A")
        type_ = order_details_response.get("type", "N/A")
        description = order_details_response.get("description", "N/A")
        creator = order_details_response.get("creator", "N/A")
        original_user = order_details_response.get("originalUser", "N/A")
        total = order_details_response.get("total", {}).get("value", "N/A")
        total_currency = order_details_response.get("total", {}).get("code", "N/A")
        payment_status = order_details_response.get("paymentStatus", "N/A")
        provisioning_status = order_details_response.get("provisioningStatus", "N/A")
        status = order_details_response.get("status", "N/A")
        error_details = order_details_response.get("errorDetails", {}).get("en_US", "N/A")
        possible_push_to_status = order_details_response.get("possiblePushToStatus", {})
        actions = "\n".join(
            [f"o {key}: {value.get('en_US', 'N/A')}" for key, value in possible_push_to_status.items()]
        )

        # Prepare data for OpenAI
        openai_prompt = (
            "The following contains details of an order.\n\n"
            f"End Customer\n• Name: {end_customer_name}\n\n"
            f"General Information\n• Order ID: {order_id}\n• Internal ID: {internal_id}\n"
            f"• Order Number: {order_number}\n• Type: {type_}\n• Description: {description}\n"
            f"• Creator: {creator}\n• Original User: {original_user}\n\n"
            f"Financials\n• Total: ${total} {total_currency}\n• Payment Status: {payment_status}\n\n"
            f"Provisioning Details\n• Status: {status}\n• Provisioning Status: {provisioning_status}\n"
            f"• Error Details:\n  • Error: {error_details}\n\n"
            f"Actions\n• Possible statuses for resolution:\n{actions}\n\n"
            "Based on the information provided, provide a meaningful summary for the user. "
            "Focus on errors and their resolution suggestions if applicable."
        )

        # Call OpenAI API
        openai_response = call_openai_api(
            model="gpt-4",
            messages=[
                {
                    "role": "system", 
                    "content": 
                        "You are a helpful assistant summarizing order details."  
                        "Please make sure your response is well formatted and using rich markdown so the answer is clear and the user can understand the results easily. Maximum size of text should be no more than standard text size using **bold** for markdown."
                        "If field 'reason' in 'errorDetails' is not null, please summarize that text for the user and put it at the top of your reply followed by \n\n"
                    },
                {"role": "user", "content": openai_prompt}
            ]
        )

        return {
            "reply": openai_response,
            "next_step": "complete"
        }

    except Exception as e:
        print(f"Error in handle_get_order_info: {e}")
        return {
            "reply": f"An error occurred while processing the order information: {e}",
            "next_step": "error"
        }

def handle_order_resubmission(context, details):
    """
    Handles the resubmission of an order.

    Args:
        context (dict): The current conversation context.
        details (list): List of extracted details including the order ID.

    Returns:
        dict: A structured response indicating the result.
    """
    # Extract the order_number from details
    order_number = None
    for detail in details:
        if "order_id" in detail:
            order_number = detail["order_id"]
            break  # Stop after finding the first valid order_id

    # Validate order_number
    if not order_number:
        return {
            "reply": "Unable to find a valid order ID. Please provide a valid order ID, e.g., **SO000099**.",
            "next_step": "request_order_id"
        }

    try:
        # First API call to find the order by order number
        order_search_endpoint = f"services/order-manager/orders?like(orderNumber,{order_number}),select(orderDetails)"
        order_search_response = call_commerce_api(order_search_endpoint, method="GET")

        if not order_search_response or not isinstance(order_search_response, list):
            raise ValueError("Order search API response is invalid or empty.")

        # Extract the orderId from the first order in the response
        order_data = order_search_response[0]
        order_id = order_data.get("orderId")
        if not order_id:
            raise ValueError("Order ID not found in the order search response.")

        # Resubmit the order using the extracted orderId
        resubmit_endpoint = f"services/order-manager/orders/{order_id}/push"
        payload = {"ofStatus": "PD"}  # Payload required for resubmission
        resubmit_response = call_commerce_api(resubmit_endpoint, method="POST", payload=payload)

        return {
            "reply": f"The order with order number **{order_number}** has been successfully resubmitted. \n\n [Follow the order here](https://hpeinc.demos.cloudblue.com/ccp/v/pa/ux1-ui/order-details?orderId={order_id})",
            "next_step": "complete"
        }

    except ValueError as ve:
        # Handle validation or search errors
        return {
            "reply": f"An error occurred while processing the order: {ve}",
            "next_step": "error"
        }
    except Exception as e:
        # Handle general errors
        return {
            "reply": f"An error occurred while resubmitting the order number **{order_number}**: {e}",
            "next_step": "error"
        }

def handle_order_cancellation(context, details):
    """
    Handles the cancellation of an order that is in process.

    Args:
        context (dict): The current conversation context.
        details (list): List of extracted details including the order ID.

    Returns:
        dict: A structured response indicating the result.
    """
    # Extract the order_number from details
    order_number = None
    for detail in details:
        if "order_id" in detail:
            order_number = detail["order_id"]
            break  # Stop after finding the first valid order_id

    # Validate order_number
    if not order_number:
        return {
            "reply": "Unable to find a valid order ID. Please provide a valid order ID, e.g., **SO000099**.",
            "next_step": "request_order_id"
        }

    try:
        # First API call to find the order by order number
        order_search_endpoint = f"services/order-manager/orders?like(orderNumber,{order_number}),select(orderDetails)"
        order_search_response = call_commerce_api(order_search_endpoint, method="GET")

        if not order_search_response or not isinstance(order_search_response, list):
            raise ValueError("Order search API response is invalid or empty.")

        # Extract the orderId from the first order in the response
        order_data = order_search_response[0]
        order_id = order_data.get("orderId")
        if not order_id:
            raise ValueError("Order ID not found in the order search response.")

        # Resubmit the order using the extracted orderId
        resubmit_endpoint = f"services/order-manager/orders/{order_id}/push"
        payload = {"ofStatus": "CL"}  # Payload required for resubmission
        resubmit_response = call_commerce_api(resubmit_endpoint, method="POST", payload=payload)

        return {
            "reply": f"The order with order number **{order_number}** has been successfully submitted for cancellation.\n\n [Follow the order here](https://hpeinc.demos.cloudblue.com/ccp/v/pa/ux1-ui/order-details?orderId={order_id})",
            "next_step": "complete"
        }

    except ValueError as ve:
        # Handle validation or search errors
        return {
            "reply": f"An error occurred while processing the order: {ve}",
            "next_step": "error"
        }
    except Exception as e:
        # Handle general errors
        return {
            "reply": f"An error occurred while cancelling the order number **{order_number}**: {e}",
            "next_step": "error"
        }
    

if __name__ == "__main__":
    initialize_database()
    print("Database initialized.")
    #print(installation_instructions())
