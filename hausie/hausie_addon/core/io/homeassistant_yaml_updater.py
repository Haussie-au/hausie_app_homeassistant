from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Callable

from .pi_file_sender import PiFileSender
from ..creators.automation_creator import AutomationCreator
from ..creators.cover_creator import CoverCreator
from ..creators.group_creator import GroupCreator
from ..creators.input_creator import InputCreator
from ..creators.script_creator import ScriptCreator
from ..creators.switch_creator import SwitchCreator
from ..utils.naming import build_filename, prefix_filename, slugify


class HomeAssistantYamlUpdater:
    """Update Home Assistant YAML artifacts with a remote-first flow."""

    def __init__(
        self,
        *,
        automation_creator: AutomationCreator | None = None,
        cover_creator: CoverCreator | None = None,
        group_creator: GroupCreator | None = None,
        input_creator: InputCreator | None = None,
        script_creator: ScriptCreator | None = None,
        switch_creator: SwitchCreator | None = None,
        pi_sender: PiFileSender | None = None,
        remote_root: str | None = None,
        backup_suffix: str = ".bak",
        require_remote: bool = True,
    ) -> None:
        self.automation_creator = automation_creator or AutomationCreator()
        self.cover_creator = cover_creator or CoverCreator()
        self.group_creator = group_creator or GroupCreator()
        self.input_creator = input_creator or InputCreator()
        self.script_creator = script_creator or ScriptCreator()
        self.switch_creator = switch_creator or SwitchCreator()
        self.pi_sender = pi_sender
        self.remote_root = remote_root
        self.backup_suffix = backup_suffix
        self.require_remote = require_remote

    def _ensure_remote(self) -> None:
        if self.require_remote and (not self.pi_sender or not self.remote_root):
            raise RuntimeError("PI sender and remote_root are required for YAML updates.")

    def _remote_path(self, *parts: str) -> str:
        root = (self.remote_root or "").rstrip("/")
        clean = [p.strip("/") for p in parts if p]
        return "/".join([root] + clean) if root else "/".join(clean)

    def _read_remote_text(self, remote_path: str) -> str | None:
        if not self.pi_sender:
            return None
        try:
            return self.pi_sender.read_remote_text(remote_path)
        except Exception:
            return None

    def _sync_remote_to_local(self, local_path: Path, remote_path: str) -> str | None:
        text = self._read_remote_text(remote_path)
        if text is None:
            return None
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(text, encoding="utf-8")
        return text

    def _backup_remote_text(self, remote_path: str, text: str | None) -> None:
        if not self.pi_sender or text is None:
            return
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        self.pi_sender.send_file(tmp_path, f"{remote_path}{self.backup_suffix}")
        Path(tmp_path).unlink(missing_ok=True)

    def _send_local(self, local_path: Path, remote_path: str) -> None:
        if not self.pi_sender:
            return
        self.pi_sender.send_file(local_path, remote_path)

    def update_light_group(
        self,
        area_name: str,
        group_id: str,
        group_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Update a light group entry in its YAML file."""
        self._ensure_remote()
        area_slug = slugify(area_name)
        filename = output_filename or (build_filename("group", area_slug) if area_slug else f"{group_id}.yaml")
        filename = prefix_filename(filename)
        local_path = (self.group_creator.groups_dir / filename).resolve()
        remote_path = self._remote_path("groups", filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.group_creator.create_light_group(
            area_slug,
            group_id=group_id,
            group_name=group_name,
            lights=entities,
            output_filename=filename,
        )
        self._send_local(local_path, remote_path)

    def update_cover_group(
        self,
        area_name: str,
        unique_id: str,
        group_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Update a cover-group YAML file."""
        self._ensure_remote()
        area_slug = slugify(area_name)
        filename = output_filename or (build_filename("cover", area_slug) if area_slug else f"{unique_id}.yaml")
        filename = prefix_filename(filename)
        local_path = (self.cover_creator.covers_dir / filename).resolve()
        remote_path = self._remote_path("covers", filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.cover_creator.create_cover_group(
            area_slug,
            group_name=group_name,
            unique_id=unique_id,
            entities=entities,
            output_filename=filename,
        )
        self._send_local(local_path, remote_path)

    def update_boolean_group(
        self,
        area_name: str,
        group_id: str,
        group_name: str,
        entities: list[str],
        *,
        all_state: bool = True,
        output_filename: str | None = None,
    ) -> None:
        """Update a boolean group entry in its YAML file."""
        self._ensure_remote()
        area_slug = slugify(area_name)
        filename = output_filename or (build_filename("group", area_slug) if area_slug else f"{group_id}.yaml")
        filename = prefix_filename(filename)
        local_path = (self.group_creator.groups_dir / filename).resolve()
        remote_path = self._remote_path("groups", filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.group_creator.create_boolean_group(
            area_slug,
            group_id=group_id,
            group_name=group_name,
            entities=entities,
            all_state=all_state,
            output_filename=filename,
        )
        self._send_local(local_path, remote_path)

    def update_toggle_script(
        self,
        area_name: str | None,
        script_id: str,
        script_name: str,
        entities: list[str],
        *,
        output_filename: str | None = None,
    ) -> None:
        """Update a toggle script entry in its YAML file."""
        safe_area = slugify(area_name) if area_name else "general"
        filename = prefix_filename(output_filename or f"{safe_area}_scripts.yaml")
        self.update_script_entry(
            filename,
            lambda: self.script_creator.create_toggle_script(
                safe_area,
                script_id,
                script_name,
                entities,
                output_filename=filename,
            ),
        )

    def update_input(
        self,
        area_name: str,
        input_entry: dict,
        *,
        context: dict,
        template_file: str,
        output_filename: str | None = None,
    ) -> None:
        """Update a helper entry using the matching template."""
        self._ensure_remote()
        input_type = input_entry["type"]
        area_slug = slugify(area_name)
        filename = prefix_filename(output_filename or build_filename(input_type, area_slug))
        local_path = (self.input_creator.inputs_dir / input_type / filename).resolve()
        remote_path = self._remote_path("helpers", input_type, filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.input_creator._create_input(
            area=area_name,
            input_type=input_type,
            input_name=f"{input_entry['id']}.yaml",
            template_file=template_file,
            context=context,
            output_filename=output_filename,
        )
        self._send_local(local_path, remote_path)

    def update_automation(self, automation_entry: dict, *, context: dict, template_file: str) -> None:
        """Update an automation YAML file using the matching template."""
        self._ensure_remote()
        filename = build_filename("automation", automation_entry["id"])
        local_path = (self.automation_creator.automations_dir / filename).resolve()
        remote_path = self._remote_path("automations", filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        self.automation_creator._create_from_template(
            automation_filename=filename,
            template_file=template_file,
            context=context,
        )
        self._send_local(local_path, remote_path)

    def update_switch_entry(
        self,
        area_name: str,
        *,
        template_file: str,
        context: dict,
        output_filename: str | None = None,
    ) -> None:
        """Update a switch YAML file using the matching template."""
        self._ensure_remote()
        area_slug = slugify(area_name)
        filename = output_filename or build_filename("switch", area_slug) if area_slug else "switches.yaml"
        filename = prefix_filename(filename)
        local_path = (self.switch_creator.switches_dir / filename).resolve()
        remote_path = self._remote_path("switches", filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        dest_file = self.switch_creator.create_from_template(
            area_slug,
            template_file=template_file,
            context=context,
            output_filename=output_filename,
        )
        self._send_local(dest_file, remote_path)

    def update_script_entry(self, filename: str, update_fn: Callable[[], None]) -> None:
        """Update a scripts YAML file using a caller-provided update function."""
        self._ensure_remote()
        filename = prefix_filename(filename)
        local_path = (self.script_creator.scripts_dir / filename).resolve()
        remote_path = self._remote_path("scripts", filename)
        original = self._sync_remote_to_local(local_path, remote_path)
        self._backup_remote_text(remote_path, original)
        update_fn()
        self._send_local(local_path, remote_path)
