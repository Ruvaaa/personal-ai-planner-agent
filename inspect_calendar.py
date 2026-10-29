import requests
import msal
from config import CLIENT_ID, AUTHORITY, SCOPES

app = msal.PublicClientApplication(
    client_id=CLIENT_ID,
    authority=AUTHORITY
)

flow = app.initiate_device_flow(scopes=SCOPES)
print(flow["message"])

result = app.acquire_token_by_device_flow(flow)

if "access_token" not in result:
    raise RuntimeError(result.get("error_description"))

headers = {
    "Authorization": f"Bearer {result['access_token']}"
}

base_url = "https://graph.microsoft.com/v1.0"

# Identify the Microsoft account
me = requests.get(
    f"{base_url}/me",
    headers=headers,
    params={"$select": "displayName,mail,userPrincipalName"}
)
me.raise_for_status()

account = me.json()

print("\n--- AUTHENTICATED ACCOUNT ---")
print("Name:", account.get("displayName"))
print("Email:", account.get("mail"))
print("Username:", account.get("userPrincipalName"))

# Inspect events scheduled for 17 October 2026
events = requests.get(
    f"{base_url}/me/calendarView",
    headers=headers,
    params={
        "startDateTime": "2026-10-17T00:00:00+02:00",
        "endDateTime": "2026-10-18T00:00:00+02:00",
        "$select": "subject,start,end,webLink,isCancelled"
    }
)
events.raise_for_status()

print("\n--- EVENTS ON 17 OCTOBER 2026 ---")

for event in events.json().get("value", []):
    print("\nTitle:", event.get("subject"))
    print("Start:", event.get("start"))
    print("End:", event.get("end"))
    print("Cancelled:", event.get("isCancelled"))
    print("Outlook link:", event.get("webLink"))

if not events.json().get("value"):
    print("No events found for this date.")