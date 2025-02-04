# Project Setup Guide

This guide provides step-by-step instructions on how to install, set up, and run the application, including the newly added workflow system and database handling.

---

## **1. Prerequisites**
Ensure you have the following installed:
- **Python 3.8+ to 3.11**
- **pip** (Python package manager)
- **Virtual Environment** (optional but recommended)

---

## **2. Clone the Repository**
If your code is in a repository, clone it using:
```bash
git clone <your-repository-url>
cd <your-repository-folder>
```
If the code is in a local directory, navigate to it:
```bash
cd /path/to/your/project
```

---

## **3. Set Up a Virtual Environment (Recommended)**
It is recommended to create a virtual environment to manage dependencies:
```bash
python3 -m venv venv
source venv/bin/activate  # On macOS/Linux
venv\Scripts\activate    # On Windows
```

---

## **4. Install Dependencies**
Run the following command to install required Python packages:
```bash
pip install --upgrade pip
```
```bash
pip install -r requirements.txt
```
If `requirements.txt` does not exist, install the packages manually:
```bash
pip install flask flask-cors openai thinc spacy requests
```
Additionally, download the **spaCy English model** required for entity extraction:
```bash
python -m spacy download en_core_web_sm
```

---

## **5. Configure Environment Variables and API Keys**
Create a `config.json` file in the project root directory with the following structure:
```json
{
    "api_keys": {
        "openai": "YOUR_OPEN_AI_API_KEY",
        "freshservice": "YOUR_FRESH_SERVICE_OR_TICKETING_SYTEM_KEY"
    },
    "urls": {
        "freshservice_base": "https://your-sandbox.freshservice.com/api/v2/"
    },
    "user_profile": {
        "person_name": "Jane Doe",
        "person_email": "jane.doe@domain.com",
        "fs_user_id": 123456789,
        "fs_company_id": 98765431
    },
    "aps_info": {
        "aps_token": "YOUR_APS_TOKEN",
        "aps_endpoint": "https://yourbrand.domain.com/aps/2/"
    }
}
```
Alternatively, you can set environment variables in your terminal:

For Linux/macOS:
```bash
export OPENAI_API_KEY="your-openai-api-key"
export FRESH_SERVICE_API_KEY="your-freshservice-api-key"
export FRESH_SERVICE_BASE_URL="https://yourcompany.freshservice.com/api/v2"
export FLASK_PORT=5000
```
For Windows (PowerShell):
```powershell
$env:OPENAI_API_KEY="your-openai-api-key"
$env:FRESH_SERVICE_API_KEY="your-freshservice-api-key"
$env:FRESH_SERVICE_BASE_URL="https://yourcompany.freshservice.com/api/v2"
$env:FLASK_PORT=5000
```

---

## **6. Initialize the Database**
The application includes a workflow-related database initialization function. Run:
```bash
python -c "from workflow import initialize_database; initialize_database()"
```
This creates the necessary SQLite database and tables.

---

## **7. Run the Flask Application**
Start the application with:
```bash
python app.py
```
Or specify a different port:
```bash
python app.py --port 8000
```

---

## **8. Verify the API Endpoints**
Once the server is running, test the API using:
```bash
curl -X POST "http://127.0.0.1:5000/api/conversation" \
-H "Content-Type: application/json" \
-d '{"prompt": "Check the status of order ID 12345"}'
```
If everything is set up correctly, the API should return a JSON response with detected intents and extracted IDs.

---

## **9. Running as a Docker Container (Optional)**
If you prefer to run the application in a Docker container, create a `Dockerfile` in the root of your project:
```dockerfile
FROM python:3.8
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "app.py"]
```
To build and run the container:
```bash
docker build -t my-flask-app .
docker run -p 5000:5000 my-flask-app
```

---

## **10. Running as a Systemd Service (Optional, Linux Only)**
For production environments, you may want to run this as a systemd service.

Create a systemd service file:
```bash
sudo nano /etc/systemd/system/my-flask-app.service
```

Add the following content:
```ini
[Unit]
Description=Flask Application
After=network.target

[Service]
User=your-user
WorkingDirectory=/path/to/your/project
ExecStart=/path/to/your/project/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Reload systemd and enable the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable my-flask-app
sudo systemctl start my-flask-app
```
Check status:
```bash
sudo systemctl status my-flask-app
```

---

## **11. Logging and Debugging**
- To view logs while running:
  ```bash
  tail -f flask.log
  ```
- To check for errors in systemd:
  ```bash
  journalctl -u my-flask-app --no-pager
  ```

---

## **12. Troubleshooting**
### Issue: `ModuleNotFoundError`
If you get an error about missing modules, ensure dependencies are installed:
```bash
pip install -r requirements.txt
```
### Issue: `Database Lock Error`
SQLite uses file-based locking. Restart the application and ensure no other processes are accessing the database.
```bash
rm -rf conversations.db
python -c "from workflow import initialize_database; initialize_database()"
```

---

## **13. Future Improvements**
- Add automated tests.
- Enhance API security.
- Optimize database structure for scalability.
- Implement caching for better performance.

---

## **14. Contributing**
Feel free to submit pull requests or open issues for enhancements.

---

## **15. License**
This project is licensed under the MIT License.

