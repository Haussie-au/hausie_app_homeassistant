import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ADDON_ROOT = Path(__file__).resolve().parents[1] / "hausie"
sys.path.insert(0, str(ADDON_ROOT))

from hausie_addon import addon_server  # noqa: E402
from hausie_addon.core.clients.ha_client import HAClient  # noqa: E402


class CredentialPasswordResetTests(unittest.TestCase):
    def test_password_change_uses_home_assistant_admin_websocket_command(self) -> None:
        ha = HAClient.__new__(HAClient)
        ha._auth_ws_call = Mock()

        ha.change_auth_user_password("existing-user-id", "new-password")

        ha._auth_ws_call.assert_called_once_with(
            "config/auth_provider/homeassistant/admin_change_password",
            {"user_id": "existing-user-id", "password": "new-password"},
        )

    def test_existing_hausie_users_are_updated_without_deletion(self) -> None:
        ha = Mock()
        ha.fetch_users.return_value = [
            {"id": "admin-user-id", "username": "hausie_admin", "isOwner": True, "isAdmin": True},
            {"id": "support-user-id", "username": "hausie_support_user", "isOwner": False, "isAdmin": True},
        ]
        validation = {"credentials_valid": True, "validation_error": ""}

        with (
            patch.object(
                addon_server,
                "resolve_ha_runtime_credentials",
                return_value=("existing-token", "hausie_support_user", "existing-password"),
            ),
            patch.object(addon_server, "load_device_state", return_value={}),
            patch.object(addon_server, "save_device_state"),
            patch.object(addon_server, "_resolve_ha_client", return_value=ha),
            patch.object(addon_server, "_supervisor_request") as supervisor_request,
            patch.object(addon_server, "persist_ha_runtime_credentials"),
            patch.object(addon_server, "_validate_ha_credentials", return_value=validation),
            patch.object(addon_server, "_sync_local_config"),
            patch.object(addon_server, "_MQTT_LISTENER", object()),
            patch.object(addon_server, "_SUPPORT_MANAGER", object()),
            patch.object(addon_server, "_HEARTBEAT", object()),
            patch.object(addon_server, "_start_license_monitor"),
            patch.object(addon_server, "_start_inventory_monitor"),
        ):
            result = addon_server._save_ha_credentials(
                {
                    "ha_token": "new-token",
                    "admin_password": "new-admin-password",
                    "support_password": "new-support-password",
                }
            )

        self.assertEqual(result, validation)
        ha.change_auth_user_password.assert_has_calls(
            [
                call("admin-user-id", "new-admin-password"),
                call("support-user-id", "new-support-password"),
            ]
        )
        self.assertEqual(ha.change_auth_user_password.call_count, 2)
        ha.delete_auth_user_by_username.assert_not_called()
        ha.create_auth_user.assert_not_called()
        for supervisor_call in supervisor_request.call_args_list:
            self.assertNotEqual(supervisor_call.args[:2], ("POST", "/auth/reset"))


if __name__ == "__main__":
    unittest.main()
