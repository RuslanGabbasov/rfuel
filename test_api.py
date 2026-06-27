import json
import sys
import urllib.request

url = "https://gdebenz.ru/api/nearby?lat=54.72&lon=55.99&radius_km=10"
r = urllib.request.urlopen(url)
data = r.read()
print("bytes:", len(data), file=sys.stderr)
# Write raw to file
with open("/tmp/api_response.json", "wb") as f:
    f.write(data)
print("saved to /tmp/api_response.json", file=sys.stderr)
# Try parse
try:
    d = json.loads(data)
    print("stations:", len(d.get("stations", [])), file=sys.stderr)
    print("summary:", d.get("summary"), file=sys.stderr)
except json.JSONDecodeError as e:
    print("JSON parse error:", e, file=sys.stderr)
    print("last 200 bytes:", data[-200:], file=sys.stderr)
