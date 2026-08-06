import time
import requests
import json
import base64
import os
import urllib.request
from triangulate_v5 import process_cover_image # Import the existing AI logic!

PUBLIC_SERVER_URL = "http://103.103.89.142:5050"
DOWNLOAD_DIR = "temp_worker_images"

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

def poll_queue():
    print(f"Polling {PUBLIC_SERVER_URL} for new tasks...")
    try:
        resp = requests.get(f"{PUBLIC_SERVER_URL}/api/queue/poll")
        if resp.status_code == 200:
            data = resp.json()
            if data.get("task_id"):
                task_id = data["task_id"]
                image_url = data["image_url"]
                print(f"Received Task {task_id}! Downloading {image_url}...")
                
                # Download image
                image_filename = image_url.split('/')[-1]
                local_path = os.path.join(DOWNLOAD_DIR, image_filename)
                urllib.request.urlretrieve(image_url, local_path)
                
                print(f"Processing with Local AI Engine...")
                # Run the actual heavy AI processing on this 32GB RAM machine
                try:
                    # Simulated AI Processing for architecture test
                    time.sleep(3)
                    result = {
                        "title": "AI Extracted Title",
                        "author": "AI Extracted Author",
                        "publishercode": "AI Publisher",
                        "publicationyear": "2026",
                        "pages": "300"
                    }
                    
                    print("Processing Complete. Pushing result back to server...")
                    requests.post(f"{PUBLIC_SERVER_URL}/api/queue/result", json={
                        "task_id": task_id,
                        "result": result
                    })
                except Exception as e:
                    print(f"AI Processing Failed: {e}")
                    requests.post(f"{PUBLIC_SERVER_URL}/api/queue/result", json={
                        "task_id": task_id,
                        "result": {"error": str(e)}
                    })
    except Exception as e:
        print(f"Connection Error: {e}")

if __name__ == "__main__":
    print("Sothik IT Local AI Worker Started.")
    print("Waiting for remote commands...")
    while True:
        poll_queue()
        time.sleep(5) # Poll every 5 seconds
