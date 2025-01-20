import os
import logging
import requests
import gitlab
from msal import ConfidentialClientApplication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


class ADGroupSync:
    """
    Synchronisiert Mitglieder aus einer bestimmten Azure AD / Microsoft Entra ID Gruppe
    mit den Mitgliedern einer bestimmten Subgruppe in GitLab.
    Nutzt dabei optional eine 'Top-Level'-Gruppe, um existierende GitLab-Benutzer zu finden.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        azure_group_id: str,
        gitlab_url: str,
        gitlab_token: str,
        gitlab_group_id: str,
        top_level_group_id: str = None,    # <-- Neu: optionale Top-Level-Gruppe
        guest_access_level: int = 10,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.azure_group_id = azure_group_id
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token
        self.gitlab_group_id = gitlab_group_id
        self.top_level_group_id = top_level_group_id  # Neu
        self.guest_access_level = guest_access_level

        # Microsoft Graph
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.scope = ["https://graph.microsoft.com/.default"]

        # GitLab
        self.gl = gitlab.Gitlab(url=self.gitlab_url, private_token=self.gitlab_token)
        self.group = None

    def get_azure_token(self) -> str:
        logger.debug("Starte Token-Abruf von Azure AD.")
        app = ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=self.authority,
        )
        result = app.acquire_token_silent(self.scope, account=None)

        if not result:
            result = app.acquire_token_for_client(scopes=self.scope)

        if "access_token" in result:
            logger.debug("Azure AD Token erfolgreich abgerufen.")
            return result["access_token"]
        else:
            raise Exception("Could not obtain Azure access token.")

    def get_azure_group_members(self, token: str) -> list:
        """
        Ruft alle Mitglieder der angegebenen Azure-Gruppe ab (id, displayName, mail).
        """
        logger.debug(f"Rufe Mitglieder der Azure-Gruppe {self.azure_group_id} ab.")
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

        logger.info(f"[Azure] Es wurden {len(members)} Mitglieder gefunden.")
        return members

    def get_gitlab_group(self):
        """
        Ruft die Ziel-Gruppe (Sub-Gruppe) ab.
        """
        if not self.group:
            logger.debug(f"Rufe GitLab-Gruppe {self.gitlab_group_id} ab.")
            self.group = self.gl.groups.get(self.gitlab_group_id)
        return self.group

    def get_group_members_with_saml_identity(self, group_id: str) -> dict:
        """
        Lädt alle Mitglieder einer GitLab-Gruppe und mappt:
        { azure_object_id (lowercase): <MemberObj> }

        Nutz dafür das Feld 'group_saml_identity', das bei Gruppen-SAML/SCIM gesetzt wird.
        """
        grp = self.gl.groups.get(group_id)
        members = grp.members.list(all=True)

        member_dict = {}
        for m in members:
            gsi = getattr(m, "group_saml_identity", None)
            if gsi:
                extern_uid = gsi.get("extern_uid")
                if extern_uid:
                    member_dict[extern_uid.lower()] = m
        return member_dict

    def sync(self):
        """
        Ablauf:
        1. Azure-Gruppe abfragen
        2. Sub-Gruppe in GitLab abfragen (alle Mitglieder, ID=gitlab_group_id)
        3. Optionale Top-Level-Gruppe abfragen (alle Mitglieder, ID=top_level_group_id)
        4. Für jeden Azure-Nutzer: falls er in der Sub-Gruppe fehlt,
           prüfe, ob er schon in der Top-Level-Gruppe existiert.
           - Ja → hole user_id und füge zur Sub-Gruppe hinzu
           - Nein → log info "User existiert noch nicht in GitLab"
        """
        logger.info("Starte Synchronisation ...")

        # 1) Azure Token + AAD Gruppe
        azure_token = self.get_azure_token()
        azure_members = self.get_azure_group_members(token=azure_token)

        # 2) GitLab Sub-Gruppe abrufen
        sub_group_members = self.get_group_members_with_saml_identity(self.gitlab_group_id)
        logger.info(f"[GitLab] Sub-Gruppe hat {len(sub_group_members)} Mitglieder mit Azure OID.")

        # 3) Optional: Top-Level-Gruppe abrufen, falls konfiguriert
        if self.top_level_group_id:
            top_group_members = self.get_group_members_with_saml_identity(self.top_level_group_id)
            logger.info(f"[GitLab] Top-Level-Gruppe hat {len(top_group_members)} Mitglieder mit Azure OID.")
        else:
            top_group_members = {}

        # 4) Differenz bilden
        sub_group = self.get_gitlab_group()  # das Subgruppen-Objekt
        for azure_user in azure_members:
            azure_oid = azure_user["id"]
            if not azure_oid:
                logger.warning(
                    f"Nutzer {azure_user.get('displayName')} hat keine Azure Object ID? Überspringe ..."
                )
                continue

            oid_lower = azure_oid.lower()

            # Ist der User schon in der Sub-Gruppe?
            if oid_lower in sub_group_members:
                # Dann müssen wir nichts tun
                continue

            # User ist in der Azure-Gruppe, aber (noch) nicht in der Sub-Gruppe.
            # Prüfen, ob er in GitLab überhaupt existiert (sprich: in top_group).
            if oid_lower in top_group_members:
                # => User existiert in GitLab
                user_id = top_group_members[oid_lower].id
                self._add_user_to_gitlab_group(sub_group, user_id, azure_user)
            else:
                # User existiert noch gar nicht in GitLab => SCIM muss ihn erst anlegen
                logger.info(
                    f"User {azure_user['displayName']} (OID: {azure_oid}) "
                    f"existiert noch nicht in GitLab. Überspringe ..."
                )

        logger.info("Synchronisation abgeschlossen.")

    def _add_user_to_gitlab_group(self, group, user_id: int, azure_user: dict):
        """
        Fügt einen bereits existierenden GitLab-User als Gast zur Sub-Gruppe hinzu.
        """
        try:
            group.members.create({
                "user_id": user_id,
                "access_level": self.guest_access_level,
            })
            logger.info(
                f"User {azure_user['displayName']} (OID: {azure_user['id']}) "
                f"als Gast hinzugefügt."
            )
        except gitlab.exceptions.GitlabCreateError as exc:
            logger.warning(
                f"Fehler beim Hinzufügen von {azure_user['displayName']} (OID: {azure_user['id']}): "
                f"{exc.error_message}"
            )


def main():
    """
    Hauptfunktion: Liest Konfiguration, erzeugt ADGroupSync-Instanz und führt den Sync durch.
    """
    # --------------------------------------------------------------------------
    # 1. Umgebungsvariablen / Konfiguration
    # --------------------------------------------------------------------------
    tenant_id = os.getenv("AZURE_TENANT_ID", "<your-tenant-id>")
    client_id = os.getenv("AZURE_CLIENT_ID", "<your-client-id>")
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "<your-client-secret>")
    azure_group_id = os.getenv("AZURE_GROUP_ID", "<azure-group-id>")

    gitlab_url = os.getenv("GITLAB_URL", "https://gitlab.com")
    gitlab_token = os.getenv("GITLAB_TOKEN", "<your-gitlab-personal-access-token>")
    gitlab_group_id = os.getenv("GITLAB_GROUP_ID", "12345678")   # oder namespace/pfad
    top_level_group_id = os.getenv("TOP_LEVEL_GROUP_ID")         # <--- optionaler Env
    guest_access_level = 10  # Gast

    # --------------------------------------------------------------------------
    # 2. Sync-Objekt erzeugen und ausführen
    # --------------------------------------------------------------------------
    syncer = ADGroupSync(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        azure_group_id=azure_group_id,
        gitlab_url=gitlab_url,
        gitlab_token=gitlab_token,
        gitlab_group_id=gitlab_group_id,
        top_level_group_id=top_level_group_id,
        guest_access_level=guest_access_level,
    )
    syncer.sync()


if __name__ == "__main__":
    main()
