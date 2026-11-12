"""Send ONE sample stacked lead to the GoHighLevel webhook (GHL_WEBHOOK_URL), so GHL can show the
fields as its 'Mapping Reference'. Does not mark anything as sent."""
import json
import os
import sys

from stacker.outreach import _post, ghl_payload
from stacker.stack import LABELS

url = os.environ.get("GHL_WEBHOOK_URL", "").strip()
if not url:
    sys.exit("GHL_WEBHOOK_URL secret is not set in this repo (Settings > Secrets and variables > Actions).")
with open("docs/stacked.json", encoding="utf-8") as f:
    stacks = json.load(f)["stacks"]
sample = next((s for s in stacks if s.get("owner") and s.get("mailing")), stacks[0])
payload = ghl_payload(sample, LABELS)
payload["source"] = "ohio-lead-stacker (test sample)"
status = _post(url, payload)
print(f"Sent sample lead ({sample['address']}) - GHL answered HTTP {status}")
print(json.dumps(payload, indent=1))
