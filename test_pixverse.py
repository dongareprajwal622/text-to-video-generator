import requests

KEY = "sk-73297ea6895352cf90a561b778a8bcda"

# Test v1 with Token header
print("Testing v1 + Token header...")
r = requests.post(
    "https://app-api.pixverse.ai/openapi/v1/video/text/generate",
    json={"prompt": "a cat", "aspect_ratio": "16:9", "duration": 5, "quality": "720p"},
    headers={"Token": KEY, "Content-Type": "application/json"},
    timeout=30
)
print(f"Status: {r.status_code}")
print(f"Response: {r.text}")

# Test v2 with API-KEY header
print("\nTesting v2 + API-KEY header...")
import uuid
r2 = requests.post(
    "https://app-api.pixverse.ai/openapi/v2/video/text/generate",
    json={"prompt": "a cat", "aspect_ratio": "16:9", "duration": 5},
    headers={"API-KEY": KEY, "Ai-trace-id": str(uuid.uuid4()), "Content-Type": "application/json"},
    timeout=30
)
print(f"Status: {r2.status_code}")
print(f"Response: {r2.text}")