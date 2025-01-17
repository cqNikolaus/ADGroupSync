import os
import requests
import gitlab
from msal import ConfidentialClientApplication  # für Microsoft Graph
# ggf. pip install msal python-gitlab requests
# ------------------------------------------------------------------------------
# 1. Konfiguration: Azure / Microsoft Entra ID
# ------------------------------------------------------------------------------
TENANT_ID = os.getenv("AZURE_TENANT_ID", "<your-tenant-id>")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "<your-client-id>")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "<your-client-secret>")

# Die ID der Azure-Gruppe, die verglichen werden soll
AZURE_GROUP_ID = "<azure-group-id>"

# Microsoft Graph Endpunkt und Scope
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"]

# ------------------------------------------------------------------------------
# 2. Konfiguration: GitLab
# ------------------------------------------------------------------------------
GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com")  # oder self-hosted URL
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "<your-gitlab-personal-access-token>")

# Beispiel: Top-Level-Gruppe /namespace1/subgruppe-x
# Du brauchst entweder die numeric ID oder den vollständigen Pfad
GITLAB_GROUP_ID = 12345678  # numeric Group ID ODER eben "namespace1/subgruppe-x"

# Gast-Zugriffslevel laut GitLab-API (10 = Gast, 20 = Reporter, 30 = Developer, ...)
GITLAB_GUEST_ACCESS_LEVEL = 10

# ------------------------------------------------------------------------------
# 3. Token für die Microsoft Graph API holen
# ------------------------------------------------------------------------------
def get_azure_token():
    app = ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY
    )
    result = app.acquire_token_silent(SCOPE, account=None)

    if not result:
        result = app.acquire_token_for_client(scopes=SCOPE)

    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception("Could not obtain Azure access token")

# ------------------------------------------------------------------------------
# 4. Mitglieder aus Azure-Gruppe ziehen
# ------------------------------------------------------------------------------
def get_azure_group_members(group_id, token):
    """
    Ruft die Mitglieder einer bestimmten Azure-Gruppe (Microsoft Entra ID)
    über Microsoft Graph ab und gibt eine Liste mit Dictionaries zurück.
    """
    url = f"https://graph.microsoft.com/v1.0/groups/{group_id}/members?$select=id,displayName,userPrincipalName,mail"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    members = []
    while url:
        resp = requests.get(url, headers=headers)
        data = resp.json()

        # Falls Fehler
        if resp.status_code != 200:
            raise Exception(
                f"Fehler beim Abruf der Azure-Gruppe: {resp.status_code}, {data}"
            )

        value = data.get("value", [])
        for user in value:
            # Hier könnte man je nach Bedarf auf user["userPrincipalName"] oder user["mail"] zugreifen
            members.append({
                "id": user.get("id"),
                "displayName": user.get("displayName"),
                "mail": user.get("mail") or user.get("userPrincipalName")
            })

        # Paginierung (wenn @odata.nextLink vorhanden, weiter abfragen)
        url = data.get("@odata.nextLink")

    return members

# ------------------------------------------------------------------------------
# 5. Mitglieder einer GitLab-Gruppe ziehen (über python-gitlab)
# ------------------------------------------------------------------------------
def get_gitlab_group_members(gl, group_id):
    """
    Liefert ein Dictionary {email: user_object} aller Mitglieder einer GitLab-Gruppe.
    Achtung: GitLab liefert ggf. nur eingeschränkte Infos, hier vereinfachte Darstellung.
    """
    group = gl.groups.get(group_id)
    members = group.members.list(all=True)  # alle Mitglieder, nicht nur 20

    member_dict = {}
    for m in members:
        # Achtung: m.email kann hier fehlen, je nach API-Konfiguration
        # python-gitlab ab v3.10+ unterstützt group.members.all(...) -> kann man anpassen
        # Manchmal muss man user_details aufrufen:
        user = gl.users.get(m.id)  # um E-Mail zu bekommen (falls nicht direkt vorhanden)
        if hasattr(user, "email") and user.email:
            member_dict[user.email.lower()] = m
        else:
            # evtl. fallback auf username
            pass

    return member_dict

# ------------------------------------------------------------------------------
# 6. User vergleichen und fehlende Azure-User zur GitLab-Gruppe hinzufügen
# ------------------------------------------------------------------------------
def sync_users_to_gitlab(azure_members, gitlab_members, gl, group_id):
    """
    - azure_members: Liste von Dicts mit mindestens 'mail'
    - gitlab_members: Dict {email: gitlab_member_obj}
    """
    group = gl.groups.get(group_id)

    for azure_user in azure_members:
        azure_mail = azure_user["mail"]
        if not azure_mail:
            # Wenn keine Mail vorhanden, kann man ggf. überspringen oder anders handeln
            continue

        azure_mail_lower = azure_mail.lower()

        # Check, ob User bereits in GitLab-Gruppe ist
        if azure_mail_lower not in gitlab_members:
            # User noch nicht drin -> hinzufügen
            # Achtung: user muss existieren in GitLab (d.h. registriert sein)
            # Falls nicht existierend, könnte man z.B. group.invite(...) verwenden
            # Hier gehen wir davon aus, dass user existiert und wir nur hinzufügen.
            try:
                group.members.create({
                    "user_id": find_gitlab_user_by_email(gl, azure_mail_lower),
                    "access_level": GITLAB_GUEST_ACCESS_LEVEL
                })
                print(f"User {azure_mail} als Gast hinzugefügt.")
            except Exception as e:
                print(f"Fehler beim Hinzufügen von {azure_mail}: {e}")

def find_gitlab_user_by_email(gl, email):
    """
    GitLab-User anhand der E-Mail-Adresse finden.
    Gibt die GitLab User-ID zurück. Falls nicht existiert, wirft es eine Exception.
    """
    users = gl.users.list(search=email)
    # Falls man exakten Match braucht, muss man genauer filtern
    for u in users:
        if u.email.lower() == email.lower():
            return u.id
    # Wenn kein User gefunden -> Exception
    raise Exception(f"User mit E-Mail {email} existiert nicht in GitLab.")

# ------------------------------------------------------------------------------
# 7. Hauptablauf
# ------------------------------------------------------------------------------
def main():
    # 1) Token für Azure
    azure_token = get_azure_token()

    # 2) Azure-Gruppe abfragen
    azure_members = get_azure_group_members(AZURE_GROUP_ID, azure_token)
    print(f"[Azure] Es wurden {len(azure_members)} Mitglieder gefunden.")

    # 3) GitLab-Anbindung
    gl = gitlab.Gitlab(url=GITLAB_URL, private_token=GITLAB_TOKEN)

    # 4) GitLab-Gruppe abfragen
    gitlab_members_dict = get_gitlab_group_members(gl, GITLAB_GROUP_ID)
    print(f"[GitLab] Es wurden {len(gitlab_members_dict)} Mitglieder gefunden.")

    # 5) User abgleichen und hinzufügen
    sync_users_to_gitlab(azure_members, gitlab_members_dict, gl, GITLAB_GROUP_ID)

if __name__ == "__main__":
    main()
