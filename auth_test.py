import msal

from config import CLIENT_ID, AUTHORITY, SCOPES

app = msal.PublicClientApplication(
    client_id=CLIENT_ID,
    authority=AUTHORITY,
)

print("Starting Microsoft authentication...")

flow = app.initiate_device_flow(scopes=SCOPES)

if "user_code" not in flow:
    print("Could not initiate sign-in.")
    print(flow.get("error_description", flow))
    raise SystemExit(1)

print("\n" + flow["message"])

result = app.acquire_token_by_device_flow(flow)

if "access_token" in result:
    print("\nAuthentication successful!")
    print("Access token received.")
    print("Your Microsoft account is authenticated.")
else:
    print("\nAuthentication failed.")
    print(result.get("error_description", result))