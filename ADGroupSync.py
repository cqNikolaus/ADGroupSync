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

        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"

        self.scope = ["https://graph.microsoft.com/.default"]

        self.gl = gitlab.Gitlab(url=self.gitlab_url, private_token=self.gitlab_token)
        self.added_count = 0

    def get_azure_token(self) -> str:
        app = ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=self.authority,
        )
        result = app.acquire_token_silent(self.scope, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=self.scope)

        if not result or "access_token" not in result:
            error_code = result.get("error", "")
            error_desc = result.get("error_description", "")
            if "expired" in error_desc.lower() or "invalid_client" in error_desc.lower():
                logger.warning(
                    "Es könnte sein, dass das Azure App Secret abgelaufen oder ungültig ist. "
                    f"(Fehlercode: {error_code}, Beschreibung: {error_desc})"
                )
            raise Exception(
                f"Das Azure-Zugriffstoken konnte nicht abgerufen werden. Fehler: {error_code}, "
                f"Beschreibung: {error_desc}"
            )

        return result["access_token"]

    def get_azure_group_members(self, token: str) -> list:
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
            url = data.get("@odata.nextLink")

        return members

    def get_gitlab_direct_members(self) -> set[int]:
        headers = {"Private-Token": self.gitlab_token}
        group_id = self.gitlab_group_id
        url = f"{self.gitlab_url}/api/v4/groups/{group_id}/members?per_page=100"

        user_ids = set()
        while url:
            resp = requests.get(url, headers=headers)

            if resp.status_code == 401 or resp.status_code == 403:
                logger.error("Der Zugriff auf GitLab wurde verweigert. "
                             "Möglicherweise ist das GitLab Personal Access Token abgelaufen.")
                raise Exception("Das GitLab Personal Access Token könnte abgelaufen sein.")

            if resp.status_code != 200:
                raise Exception(
                    f"Fehler beim Abruf der GitLab-Gruppe: "
                    f"{resp.status_code}, {resp.text}"
                )
            data = resp.json()
            for member in data:
                user_ids.add(member['id'])

            if 'next' in resp.links:
                url = resp.links['next']['url']
            else:
                url = None

        return user_ids

    def get_gitlab_all_members(self) -> set[int]:
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

            if 'next' in resp.links:
                url = resp.links['next']['url']
            else:
                url = None

        return user_ids

    def get_top_level_azure_map(self) -> dict[str, int]:
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
        try:
            group.members.create({
                "user_id": user_id,
                "access_level": self.guest_access_level,
            })
            self.added_count += 1
            logger.info(
                f"Benutzer '{azure_user['displayName']}' (OID: {azure_user['id']}) "
                f"als Gast hinzugefügt."
            )
        except gitlab.exceptions.GitlabCreateError as exc:
            logger.warning(
                f"Fehler beim Hinzufügen von '{azure_user['displayName']}' (OID: {azure_user['id']}): "
                f"{exc.error_message}"
            )

    def sync(self):
        logger.info("Starte Synchronisation ...")

        azure_token = self.get_azure_token()

        azure_members = self.get_azure_group_members(token=azure_token)
        azure_count = len(azure_members)
        logger.info(f"[Azure] Gruppe: {azure_count} Mitglieder gefunden.")

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

        top_level_azure_map = self.get_top_level_azure_map()

        missing_in_gitlab = []
        existing_in_gitlab = []

        for azure_user in azure_members:
            azure_oid = azure_user["id"].lower()
            if azure_oid in top_level_azure_map:
                existing_in_gitlab.append(azure_user)
            else:
                missing_in_gitlab.append(azure_user)

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
                f" Versuche {to_add_count} fehlende Mitglieder der GitLab-Subgruppe hinzuzufügen."
                if to_add_count > 0 else ""
            )
        )

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

        if missing_in_gitlab:
            logger.info(
                f"Zusammenfassung: {self.added_count} Benutzer hinzugefügt, "
                f"{len(missing_in_gitlab)} Benutzer konnten nicht synchronisiert werden "
                f"(siehe Warnungen oben)."
            )

        logger.info("Synchronisation abgeschlossen.")


def main():
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    azure_group_id = os.getenv("AZURE_GROUP_ID")

    gitlab_url = os.getenv("GITLAB_URL", "https://gitlab.com")
    gitlab_token = os.getenv("GITLAB_TOKEN")

    gitlab_group_id = os.getenv("GITLAB_GROUP_ID")
    top_level_group_id = os.getenv("TOP_LEVEL_GROUP_ID")

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
