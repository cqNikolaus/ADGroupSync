import os
import logging
import requests
import gitlab
from msal import ConfidentialClientApplication

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


class ADGroupSync:
    def __init__(
            self,
            tenant_id: str,
            client_id: str,
            client_secret: str,
            azure_group_id: str,
            gitlab_url: str,
            gitlab_token: str,
            gitlab_group_id: str,
            top_level_group_id: str = None,
            guest_access_level: int = 10,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.azure_group_id = azure_group_id
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token
        self.gitlab_group_id = gitlab_group_id
        self.top_level_group_id = top_level_group_id
        self.guest_access_level = guest_access_level

        # Microsoft Graph Konfiguration
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.scope = ["https://graph.microsoft.com/.default"]

        # GitLab Konfiguration
        self.gl = gitlab.Gitlab(url=self.gitlab_url, private_token=self.gitlab_token)
        self.added_count = 0

    def get_azure_token(self) -> str:
        """Holt ein Access Token für die Microsoft Graph API via MSAL."""
        app = ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=self.authority,
        )
        result = app.acquire_token_silent(self.scope, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=self.scope)

        if "access_token" in result:
            return result["access_token"]
        else:
            raise Exception("Could not obtain Azure access token.")

    def get_azure_group_members(self, token: str) -> list:
        """
        Ruft alle Mitglieder der angegebenen Azure-Gruppe ab.
        Gibt eine Liste von Dictionaries zurück: {"id", "displayName", "mail"}.
        """
        url = (
            f"https://graph.microsoft.com/v1.0/groups/"
            f"{self.azure_group_id}/members"
            f"?$select=id,displayName,userPrincipalName,mail"
        )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        members = []
        while url:
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                raise Exception(
                    f"Fehler beim Abruf der Azure-Gruppe {self.azure_group_id}: "
                    f"{resp.status_code}, {resp.text}"
                )

            data = resp.json()
            value = data.get("value", [])
            for user in value:
                azure_id = user.get("id")
                display_name = user.get("displayName")
                primary_mail = user.get("mail") or user.get("userPrincipalName")

                members.append(
                    {
                        "id": azure_id,
                        "displayName": display_name,
                        "mail": primary_mail,
                    }
                )
            # Paginierung
            url = data.get("@odata.nextLink")

        return members

    def get_gitlab_direct_members(self) -> set[int]:
        """
        Holt nur die direkten Mitglieder der GitLab-Subgruppe (ohne vererbte Mitglieder).
        """
        headers = {"Private-Token": self.gitlab_token}
        group_id = self.gitlab_group_id
        url = f"{self.gitlab_url}/api/v4/groups/{group_id}/members?per_page=100"

        user_ids = set()
        while url:
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                raise Exception(
                    f"Fehler beim Abruf der GitLab-Gruppe: "
                    f"{resp.status_code}, {resp.text}"
                )
            data = resp.json()
            for member in data:
                user_ids.add(member['id'])

            # Check for pagination
            if 'next' in resp.links:
                url = resp.links['next']['url']
            else:
                url = None

        return user_ids

    def get_gitlab_all_members(self) -> set[int]:
        """
        Holt alle Mitglieder der GitLab-Subgruppe (inkl. vererbte Mitglieder).
        """
        headers = {"Private-Token": self.gitlab_token}
        group_id = self.gitlab_group_id
        url = f"{self.gitlab_url}/api/v4/groups/{group_id}/members/all?per_page=100&include_inherited=true"

        user_ids = set()
        while url:
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                raise Exception(
                    f"Fehler beim Abruf der GitLab-Gruppe: "
                    f"{resp.status_code}, {resp.text}"
                )
            data = resp.json()
            for member in data:
                user_ids.add(member['id'])

            # Check for pagination
            if 'next' in resp.links:
                url = resp.links['next']['url']
            else:
                url = None

        return user_ids

    def get_top_level_azure_map(self) -> dict[str, int]:
        """
        Lädt die Mitglieder der Top-Level-Gruppe (falls konfiguriert) und baut
        eine Zuordnung AzureOID.lower() -> GitLab-User-ID.
        So erkennen wir anhand der Azure OID, welche GitLab-User-ID dahintersteckt.
        """
        if not self.top_level_group_id:
            return {}

        grp = self.gl.groups.get(self.top_level_group_id)
        members = grp.members.list(all=True, include_inherited=True)

        azure_map = {}
        for m in members:
            gsi = getattr(m, "group_saml_identity", None)
            if gsi:
                oid = gsi.get("extern_uid", "").lower()
                if oid:
                    azure_map[oid] = m.id

        return azure_map

    def _add_user_to_gitlab_group(self, group, user_id: int, azure_user: dict):
        """Fügt einen (bereits vorhandenen GitLab-)User der Subgruppe hinzu (z.B. als Gast)."""
        try:
            group.members.create({
                "user_id": user_id,
                "access_level": self.guest_access_level,
            })
            self.added_count += 1
            logger.info(
                f"User '{azure_user['displayName']}' (OID: {azure_user['id']}) "
                f"als Gast hinzugefügt."
            )
        except gitlab.exceptions.GitlabCreateError as exc:
            logger.warning(
                f"Fehler beim Hinzufügen von '{azure_user['displayName']}' (OID: {azure_user['id']}): "
                f"{exc.error_message}"
            )


    def sync(self):
        logger.info("Starte Synchronisation ...")

        # 1) Azure-Token
        azure_token = self.get_azure_token()

        # 2) Azure-Gruppe: Mitglieder
        azure_members = self.get_azure_group_members(token=azure_token)
        azure_count = len(azure_members)
        logger.info(f"[Azure] Gruppe: {azure_count} Mitglieder gefunden.")

        # 3) GitLab-Subgruppe: Mitglieder (direkt und vererbt)
        gitlab_direct_user_ids = self.get_gitlab_direct_members()
        gitlab_all_user_ids = self.get_gitlab_all_members()
        gitlab_direct_count = len(gitlab_direct_user_ids)
        gitlab_total_count = len(gitlab_all_user_ids)
        gitlab_inherited_count = gitlab_total_count - gitlab_direct_count

        logger.debug(
            f"[GitLab] Sub-Gruppe: insgesamt {gitlab_total_count} Mitglieder, "
            f"davon {gitlab_direct_count} direkte Mitglieder und "
            f"{gitlab_inherited_count} vererbte."
        )

        # 4) Identifizieren, welche Azure-User bereits in GitLab sind
        top_level_azure_map = self.get_top_level_azure_map()

        # Liste der Benutzer, die nicht in GitLab gefunden wurden
        missing_in_gitlab = []
        # Liste der Benutzer, die in GitLab existieren
        existing_in_gitlab = []

        for azure_user in azure_members:
            azure_oid = azure_user["id"].lower()
            if azure_oid in top_level_azure_map:
                existing_in_gitlab.append(azure_user)
            else:
                missing_in_gitlab.append(azure_user)

        # Log users not found in GitLab
        if missing_in_gitlab:
            logger.warning(
                "Folgende Benutzer aus der Azure-Gruppe scheinen in GitLab noch nicht zu existieren:"
            )
            for user in missing_in_gitlab:
                logger.warning(
                    f"- {user['displayName']} (Mail: {user['mail']}, OID: {user['id']})"
                )
            logger.info(
                "Diese Benutzer werden entweder im nächsten SCIM-Provisionierungszyklus erstellt "
                "oder existieren bereits, aber wurden nicht per SCIM erstellt."
            )

        # 5) Berechne, welche User hinzugefügt werden müssen
        azure_user_gitlab_ids = {
            top_level_azure_map[u["id"].lower()]
            for u in existing_in_gitlab
        }
        users_to_add = azure_user_gitlab_ids - gitlab_direct_user_ids
        to_add_count = len(users_to_add)

        direct_members_from_azure = len(gitlab_direct_user_ids.intersection(azure_user_gitlab_ids))

        logger.info(
            f"Von den {len(existing_in_gitlab)} in GitLab gefundenen Azure-Mitgliedern sind bereits "
            f"{direct_members_from_azure} direkte Mitglieder der GitLab-Subgruppe."
            + (
                f" Versuche {to_add_count} fehlende Mitglieder der GitLab-Subgruppe hinzuzufügen." if to_add_count > 0 else "")
        )

        # 6) Fehlende in GitLab hinzufügen
        if to_add_count > 0:
            sub_group = self.gl.groups.get(self.gitlab_group_id)
            for azure_user in existing_in_gitlab:
                azure_oid = azure_user["id"].lower()
                gitlab_user_id = top_level_azure_map[azure_oid]
                if gitlab_user_id in users_to_add:
                    self._add_user_to_gitlab_group(sub_group, gitlab_user_id, azure_user)

            logger.info(f"{self.added_count} Benutzer wurden erfolgreich der Subgruppe hinzugefügt.")
        else:
            logger.info("Keine neuen Mitglieder hinzugefügt. Sub-Gruppe ist bereits synchron.")

        # Zusammenfassung
        if missing_in_gitlab:
            logger.info(
                f"Zusammenfassung: {self.added_count} Benutzer hinzugefügt, "
                f"{len(missing_in_gitlab)} Benutzer konnten nicht synchronisiert werden "
                f"(siehe Warnungen oben)."
            )
        else:
            logger.info(
                f"Zusammenfassung: {self.added_count} Benutzer hinzugefügt, "
                f"alle Azure-Benutzer wurden in GitLab gefunden."
            )

        logger.info("Synchronisation abgeschlossen.")



def main():
    tenant_id = os.getenv("AZURE_TENANT_ID", "<your-tenant-id>")
    client_id = os.getenv("AZURE_CLIENT_ID", "<your-client-id>")
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "<your-client-secret>")
    azure_group_id = os.getenv("AZURE_GROUP_ID", "<azure-group-id>")

    gitlab_url = os.getenv("GITLAB_URL", "https://gitlab.com")
    gitlab_token = os.getenv("GITLAB_TOKEN", "<your-gitlab-personal-access-token>")

    gitlab_group_id = os.getenv("GITLAB_GROUP_ID", "12345678")
    top_level_group_id = os.getenv("TOP_LEVEL_GROUP_ID", "87654321")

    syncer = ADGroupSync(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        azure_group_id=azure_group_id,
        gitlab_url=gitlab_url,
        gitlab_token=gitlab_token,
        gitlab_group_id=gitlab_group_id,
        top_level_group_id=top_level_group_id,
    )
    syncer.sync()


if __name__ == "__main__":
    main()