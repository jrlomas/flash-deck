#!/usr/bin/env python3
"""Flash Deck — a small, pleasant front end for STM32_Programmer_CLI."""

import os
import csv
import hashlib
import json
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango


APP_ID = "io.github.jrlomas.FlashDeck"
APP_NAME = "Flash Deck"
GLib.set_prgname(APP_ID)
GLib.set_application_name(APP_NAME)
DEFAULT_CLI = Path.home() / "STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI"
INSTALL_ROOT = DEFAULT_CLI.parent.parent
PROFILE_PATH = Path.home() / ".config/flash-deck/profiles.json"
REPORT_PATH = Path.home() / ".local/share/flash-deck/production-history.csv"


def locate_stlink_updater(cli):
    if not cli:
        return None
    install_root = Path(cli).resolve().parent.parent
    upgrade_dir = install_root / "Drivers/FirmwareUpgrade"
    java = install_root / "bin/jre/bin/java"
    jar = upgrade_dir / "STLinkUpgrade.jar"
    native = upgrade_dir / "native/linux_x64"
    if java.is_file() and os.access(java, os.X_OK) and jar.is_file() and native.is_dir():
        return {"java": str(java), "jar": str(jar), "native": str(native), "cwd": str(upgrade_dir)}
    return None


def locate_cli():
    configured = os.environ.get("STM32_PROGRAMMER_CLI")
    candidates = [Path(configured)] if configured else []
    candidates += [DEFAULT_CLI, Path("/opt/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI")]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("STM32_Programmer_CLI")


class FlashDeck(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.cli = locate_cli()
        self.stlink_updater = locate_stlink_updater(self.cli)
        self.firmware = None
        self.images = []
        self.external_loaders = []
        self.job_valid = False
        self.production_defaults = {}
        self.probes = []
        self.devices = []
        self.connected = False
        self.probe_update_available_serial = None
        self.probe_update_check_generation = 0
        self.running = False
        self.profiles = self.load_profiles()
        self.profile_loading = False
        self.pending_profile_target = None
        self.production_stop = None

    def do_activate(self):
        if self.props.active_window:
            self.props.active_window.present()
            return
        self.build_window()
        self.window.present()

    def build_window(self):
        self.window = Adw.ApplicationWindow(application=self, title=APP_NAME, default_width=1160, default_height=980)
        self.window.set_icon_name(APP_ID)
        self.window.set_size_request(840, 620)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.status_pill = Gtk.Label(label="CLI ready" if self.cli else "CLI not found")
        self.status_pill.add_css_class("status-pill")
        self.status_pill.add_css_class("success" if self.cli else "warning")
        header.pack_end(self.status_pill)
        toolbar.add_top_bar(header)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        page.set_margin_top(28); page.set_margin_bottom(28); page.set_margin_start(36); page.set_margin_end(36)
        page.set_vexpand(True)
        self.main_page = page
        toolbar.set_content(page)
        self.window.set_content(toolbar)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(label="Probes", hexpand=True)
        title.add_css_class("hero-title"); title.set_halign(Gtk.Align.START)
        hero.append(title)
        self.profile_selector = Gtk.DropDown()
        self.profile_selector.set_size_request(220, -1)
        self.profile_selector.connect("notify::selected", self.on_profile_selected)
        self.profile_save_button = Gtk.Button(icon_name="document-save-symbolic")
        self.profile_save_button.set_tooltip_text("Save the current setup as a profile")
        self.profile_save_button.connect("clicked", self.on_save_profile)
        self.profile_delete_button = Gtk.Button(icon_name="edit-delete-symbolic")
        self.profile_delete_button.set_tooltip_text("Delete selected profile")
        self.profile_delete_button.connect("clicked", self.on_delete_profile)
        hero.append(self.profile_selector); hero.append(self.profile_save_button); hero.append(self.profile_delete_button)
        self.refresh_profile_selector(); page.append(hero)

        grid = Gtk.Grid(column_spacing=20, row_spacing=20, hexpand=True, vexpand=True)
        grid.set_column_homogeneous(True)
        page.append(grid)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, hexpand=True)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, hexpand=True)
        left.set_size_request(480, -1)
        right.set_size_request(480, -1)
        self.main_left = left
        left_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_overlay_scrolling(False)
        left_scroll.set_min_content_height(400)
        left_scroll.set_propagate_natural_height(False)
        left_scroll.set_child(left)
        self.main_left_scroll = left_scroll
        grid.attach(left_scroll, 0, 0, 1, 1); grid.attach(right, 1, 0, 1, 1)

        target_content = self.target_content()
        left.append(self.card("1  Target", target_content, trailing=self.target_status_cluster))
        left.append(self.card("2  Firmware", self.firmware_content()))
        right.append(self.card("Activity", self.log_content(), expand=True))

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        GLib.idle_add(self.start_discovery)

    def card(self, title, content, expand=False, trailing=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True, vexpand=expand)
        box.add_css_class("card")
        heading_row = Gtk.Box(spacing=8)
        heading = Gtk.Label(label=title, xalign=0)
        heading.set_hexpand(True)
        heading.add_css_class("card-title")
        heading_row.append(heading)
        if trailing:
            heading_row.append(trailing)
        box.append(heading_row); box.append(content)
        return box

    @staticmethod
    def action_flow(columns=2):
        actions = Gtk.FlowBox(column_spacing=8, row_spacing=8, selection_mode=Gtk.SelectionMode.NONE)
        actions.set_min_children_per_line(1)
        actions.set_max_children_per_line(columns)
        actions.set_homogeneous(True)
        actions.set_halign(Gtk.Align.FILL)
        actions.set_hexpand(True)
        actions.add_css_class("action-flow")
        return actions

    @staticmethod
    def window_content(spacing=16):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        content.set_margin_top(24); content.set_margin_bottom(24)
        content.set_margin_start(24); content.set_margin_end(24)
        return content

    @staticmethod
    def animated_revealer(child):
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(260)
        revealer.set_reveal_child(False)
        revealer.set_child(child)
        return revealer

    @staticmethod
    def string_dropdown_factory(ellipsize=False):
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item):
            label = Gtk.Label(xalign=0)
            label.set_single_line_mode(True)
            label.set_hexpand(True)
            if ellipsize:
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_width_chars(1)
                label.set_max_width_chars(1)
            list_item.set_child(label)

        def bind(_factory, list_item):
            item = list_item.get_item()
            list_item.get_child().set_label(item.get_string() if item else "")

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def target_content(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.target_status_cluster = Gtk.Box(spacing=6)
        self.target_status_cluster.set_valign(Gtk.Align.CENTER)
        self.target_status_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
        self.target_status_icon.add_css_class("target-disconnected")
        self.target_status_icon.set_tooltip_text("Not connected")
        self.probe_update_button = Gtk.Button(icon_name="software-update-available-symbolic")
        self.probe_update_button.add_css_class("circular")
        self.probe_update_button.add_css_class("probe-update-button")
        self.probe_update_button.set_tooltip_text("ST-LINK firmware update available")
        self.probe_update_button.set_visible(False)
        self.probe_update_button.set_sensitive(False)
        self.probe_update_button.connect("clicked", self.on_probe_firmware_update)
        self.disconnect_button = Gtk.Button(icon_name="network-transmit-receive-symbolic")
        self.disconnect_button.add_css_class("circular"); self.disconnect_button.add_css_class("disconnect-button")
        self.disconnect_button.set_tooltip_text("Disconnect target")
        self.disconnect_button.set_opacity(0); self.disconnect_button.set_sensitive(False)
        self.disconnect_button.connect("clicked", self.on_disconnect)
        self.target_status_cluster.append(self.probe_update_button)
        self.target_status_cluster.append(self.disconnect_button); self.target_status_cluster.append(self.target_status_icon)
        self.discovery_status = Gtk.Box(spacing=10)
        self.discovery_status.set_halign(Gtk.Align.START)
        self.discovery_spinner = Gtk.Spinner(spinning=True)
        self.discovery_spinner.add_css_class("discovery-spinner")
        self.discovery_spinner.set_tooltip_text("Finding connected devices")
        self.discovery_status.append(self.discovery_spinner)
        box.append(self.discovery_status)
        self.probe_selector = Gtk.DropDown.new_from_strings(["Finding connected devices…"])
        self.probe_selector.set_sensitive(False)
        self.probe_selector.set_hexpand(True)
        self.probe_selector.set_size_request(0, -1)
        self.probe_selector.set_factory(self.string_dropdown_factory(ellipsize=True))
        self.probe_selector.set_list_factory(self.string_dropdown_factory())
        self.probe_selector.connect("notify::selected", self.on_device_selected)
        self.device_row = Gtk.Box(spacing=8, hexpand=True)
        self.device_row.set_visible(False)
        self.device_row.append(self.probe_selector)
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.refresh_button.set_tooltip_text("Rescan connected devices")
        self.refresh_button.add_css_class("circular")
        self.refresh_button.connect("clicked", self.on_scan)
        self.device_row.append(self.refresh_button)
        box.append(self.device_row)
        self.connection_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.connection_panel.add_css_class("subcard")
        connection_title = Gtk.Label(label="Connection settings", xalign=0)
        connection_title.add_css_class("section-title")
        self.connection_panel.append(connection_title)
        self.connection_panel.append(self.connection_settings_content())
        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.connect("clicked", self.on_connect)
        connection_actions = self.action_flow()
        connection_actions.append(self.connect_button)
        self.connection_panel.append(connection_actions)
        self.connection_revealer = self.animated_revealer(self.connection_panel)
        box.append(self.connection_revealer)
        self.device_info_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.device_info_panel.add_css_class("subcard")
        device_title = Gtk.Label(label="Target device", xalign=0)
        device_title.add_css_class("section-title")
        self.device_info_panel.append(device_title)
        info_grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self.target_info = {}
        for row, (key, label) in enumerate((("interface", "Interface"), ("device", "Device"), ("device_id", "Device ID"), ("core", "Core"), ("flash", "Flash"), ("voltage", "Voltage"))):
            name = Gtk.Label(label=label, xalign=0); value = Gtk.Label(label="—", xalign=0, selectable=True)
            value.add_css_class("target-value")
            info_grid.attach(name, 0, row, 1, 1); info_grid.attach(value, 1, row, 1, 1)
            self.target_info[key] = value
        self.device_info_panel.append(info_grid)
        self.device_info_revealer = self.animated_revealer(self.device_info_panel)
        box.append(self.device_info_revealer)
        self.target_actions = self.action_flow()
        self.target_actions.set_margin_top(8)
        self.target_actions.set_visible(False)
        self.memory_button = Gtk.Button(label="Examine memory…")
        self.memory_button.add_css_class("suggested-action")
        self.memory_button.connect("clicked", self.on_inspect_memory)
        self.option_button = Gtk.Button(label="Option bytes…")
        self.option_button.add_css_class("suggested-action")
        self.option_button.connect("clicked", self.on_option_bytes)
        self.erase_button = Gtk.Button(label="Erase chip")
        self.erase_button.add_css_class("destructive-action")
        self.erase_button.connect("clicked", self.on_erase)
        self.control_button = Gtk.Button(label="Target controls…")
        self.control_button.add_css_class("suggested-action"); self.control_button.connect("clicked", self.on_target_controls)
        self.recovery_button = Gtk.Button(label="Recovery…")
        self.recovery_button.add_css_class("destructive-action"); self.recovery_button.connect("clicked", self.on_recovery)
        self.target_actions.append(self.memory_button); self.target_actions.append(self.option_button)
        self.target_actions.append(self.control_button); self.target_actions.append(self.recovery_button); self.target_actions.append(self.erase_button)
        self.device_info_panel.append(self.target_actions)
        return box

    def firmware_content(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.drop_zone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.drop_zone.add_css_class("drop-zone")
        self.drop_zone.set_halign(Gtk.Align.FILL)
        icon = Gtk.Image.new_from_icon_name("document-open-symbolic")
        icon.set_pixel_size(30)
        self.file_label = Gtk.Label(label="Drop firmware image(s) here", xalign=0.5, wrap=True)
        self.file_label.add_css_class("file-empty")
        hint = Gtk.Label(label=".bin, .hex, .elf, .srec, .s19, or .stm32  ·  click to browse")
        hint.add_css_class("dim-label")
        self.drop_zone.append(icon); self.drop_zone.append(self.file_label); self.drop_zone.append(hint)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_firmware_drop)
        drop.connect("enter", self.on_drop_enter)
        drop.connect("leave", self.on_drop_leave)
        self.drop_zone.add_controller(drop)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_args: self.on_choose_file(None))
        self.drop_zone.add_controller(click)
        box.append(self.drop_zone)
        self.image_list = Gtk.ListBox()
        self.image_list.add_css_class("boxed-list"); self.image_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.firmware_details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.firmware_details.append(self.image_list)
        self.flash_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.flash_panel.add_css_class("subcard")
        flash_title = Gtk.Label(label="Flash settings", xalign=0)
        flash_title.add_css_class("section-title")
        self.flash_panel.append(flash_title)
        self.flash_panel.append(self.flash_settings_content())
        self.loader_label = Gtk.Label(label="No external loader", xalign=0, hexpand=True,
                                      ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.loader_label.add_css_class("dim-label")
        loader_add = Gtk.Button(label="External loader…"); loader_add.connect("clicked", self.on_choose_loader)
        self.loader_clear = Gtk.Button(label="Clear loaders")
        self.loader_clear.set_tooltip_text("Clear external loaders")
        self.loader_clear.set_sensitive(False); self.loader_clear.connect("clicked", self.on_clear_loaders)
        self.flash_panel.append(self.loader_label)
        loader_actions = self.action_flow()
        loader_actions.append(loader_add); loader_actions.append(self.loader_clear)
        self.flash_panel.append(loader_actions)
        flash_actions = self.action_flow()
        self.production_button = Gtk.Button(label="Production mode…")
        self.production_button.connect("clicked", self.on_production_mode)
        self.verify_button = Gtk.Button(label="Verify only")
        self.verify_button.connect("clicked", self.on_verify_job)
        self.flash_button = Gtk.Button(label="Flash & verify")
        self.flash_button.add_css_class("suggested-action"); self.flash_button.add_css_class("flash-button")
        self.flash_button.connect("clicked", self.on_flash)
        flash_actions.append(self.production_button); flash_actions.append(self.verify_button); flash_actions.append(self.flash_button)
        self.flash_panel.append(flash_actions)
        self.firmware_details.append(self.flash_panel)
        self.firmware_details_revealer = self.animated_revealer(self.firmware_details)
        box.append(self.firmware_details_revealer)
        return box

    def on_firmware_drop(self, _target, file_list, _x, _y):
        files = file_list.get_files()
        if not files:
            return False
        self.drop_zone.remove_css_class("drop-active")
        added = False
        for file in files:
            added = self.set_firmware(file.get_path()) or added
        return added

    def on_drop_enter(self, _target, _x, _y):
        self.drop_zone.add_css_class("drop-active")
        return Gdk.DragAction.COPY

    def on_drop_leave(self, _target):
        self.drop_zone.remove_css_class("drop-active")

    def set_firmware(self, path):
        if not path or Path(path).suffix.lower() not in {".bin", ".hex", ".elf", ".srec", ".s19", ".stm32"}:
            self.toast("Unsupported firmware file")
            return False
        resolved = str(Path(path).resolve())
        if any(image["path"] == resolved for image in self.images):
            self.toast("That firmware image is already in the job")
            return False
        suffix = Path(resolved).suffix.lower()
        address = self.address.get_text().strip() if suffix == ".bin" else ""
        try:
            inspection = self.inspect_firmware(resolved, address)
        except (OSError, ValueError, struct.error) as error:
            self.toast(f"Could not inspect firmware: {error}")
            return False
        self.images.append({"path": resolved, "address": address, "inspection": inspection})
        self.firmware = self.images[0]["path"]
        self.file_label.set_label(f"{len(self.images)} image{'s' if len(self.images) != 1 else ''} ready")
        self.file_label.remove_css_class("file-empty"); self.file_label.add_css_class("file-chosen")
        self.firmware_details_revealer.set_reveal_child(True)
        self.refresh_image_list()
        self.update_job_state()
        return True

    def refresh_image_list(self):
        self.clear_box(self.image_list)
        for index, image in enumerate(self.images):
            info = image["inspection"]
            subtitle = f"{info['format']}  ·  {self.format_bytes(info['size'])}  ·  SHA-256 {info['sha256'][:12]}"
            if info.get("range"):
                subtitle += f"  ·  {info['range']}"
            row = Adw.ActionRow(title=Path(image["path"]).name, subtitle=subtitle)
            address = Gtk.Entry(text=image["address"], placeholder_text="Embedded addresses", width_chars=13, valign=Gtk.Align.CENTER)
            address.set_tooltip_text("Required for raw .bin; optional offset for self-addressed formats")
            address.connect("changed", self.on_image_address_changed, index)
            remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat"); remove.connect("clicked", self.on_remove_image, index)
            row.add_suffix(address); row.add_suffix(remove)
            self.image_list.append(row)

    def on_image_address_changed(self, entry, index):
        if index >= len(self.images):
            return
        self.images[index]["address"] = entry.get_text().strip()
        try:
            self.images[index]["inspection"] = self.inspect_firmware(self.images[index]["path"], self.images[index]["address"])
        except (OSError, ValueError, struct.error):
            pass
        self.update_job_state()

    def on_remove_image(self, _button, index):
        if index >= len(self.images):
            return
        del self.images[index]
        self.firmware = self.images[0]["path"] if self.images else None
        self.refresh_image_list()
        self.firmware_details_revealer.set_reveal_child(bool(self.images))
        self.file_label.set_label(f"{len(self.images)} image{'s' if len(self.images) != 1 else ''} ready" if self.images else "Drop firmware image(s) here")
        self.update_job_state()

    @classmethod
    def inspect_firmware(cls, path, address=""):
        data = Path(path).read_bytes()
        suffix = Path(path).suffix.lower()
        segments = []
        if suffix == ".bin":
            start = int(address, 0)
            if data:
                segments = [(start, start + len(data) - 1)]
        elif suffix == ".hex":
            segments = cls.inspect_intel_hex(data.decode("ascii"))
        elif suffix in {".srec", ".s19"}:
            segments = cls.inspect_srecord(data.decode("ascii"))
        elif suffix == ".elf":
            segments = cls.inspect_elf(data)
        merged = cls.merge_ranges(segments)
        if not merged:
            range_text = "No embedded address information"
        elif len(merged) == 1:
            range_text = f"0x{merged[0][0]:08X}–0x{merged[0][1]:08X}"
        else:
            range_text = f"{len(merged)} segments  ·  0x{merged[0][0]:08X}–0x{merged[-1][1]:08X}"
        return {
            "format": {".bin": "Raw binary", ".hex": "Intel HEX", ".elf": "ELF", ".srec": "Motorola S-record",
                       ".s19": "Motorola S-record", ".stm32": "STM32 image"}[suffix],
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "segments": merged, "range": range_text,
        }

    @staticmethod
    def inspect_intel_hex(text):
        ranges = []
        base = 0
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise ValueError(f"Invalid Intel HEX record on line {line_number}")
            record = bytes.fromhex(line[1:])
            if len(record) < 5 or record[0] + 5 != len(record) or sum(record) & 0xFF:
                raise ValueError(f"Invalid Intel HEX checksum on line {line_number}")
            count, offset, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
            payload = record[4:4 + count]
            if kind == 0 and count:
                start = base + offset; ranges.append((start, start + count - 1))
            elif kind == 2 and count == 2:
                base = int.from_bytes(payload, "big") << 4
            elif kind == 4 and count == 2:
                base = int.from_bytes(payload, "big") << 16
        return ranges

    @staticmethod
    def inspect_srecord(text):
        ranges = []
        address_bytes = {"1": 2, "2": 3, "3": 4}
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or len(line) < 4 or not line.startswith("S"):
                continue
            if line[1] not in address_bytes:
                continue
            record = bytes.fromhex(line[2:])
            if not record or record[0] != len(record) - 1 or (sum(record) & 0xFF) != 0xFF:
                raise ValueError(f"Invalid S-record checksum on line {line_number}")
            width = address_bytes[line[1]]
            start = int.from_bytes(record[1:1 + width], "big")
            data_size = record[0] - width - 1
            if data_size:
                ranges.append((start, start + data_size - 1))
        return ranges

    @staticmethod
    def inspect_elf(data):
        if data[:4] != b"\x7fELF" or len(data) < 52:
            raise ValueError("Invalid ELF header")
        elf_class, encoding = data[4], data[5]
        endian = "<" if encoding == 1 else ">" if encoding == 2 else None
        if not endian or elf_class not in (1, 2):
            raise ValueError("Unsupported ELF encoding")
        if elf_class == 1:
            header = struct.unpack_from(endian + "HHIIIIIHHHHHH", data, 16)
            program_offset, entry_size, count = header[4], header[8], header[9]
            fmt = endian + "IIIIIIII"
        else:
            header = struct.unpack_from(endian + "HHIQQQIHHHHHH", data, 16)
            program_offset, entry_size, count = header[4], header[8], header[9]
            fmt = endian + "IIQQQQQQ"
        ranges = []
        for index in range(count):
            values = struct.unpack_from(fmt, data, program_offset + index * entry_size)
            if elf_class == 1:
                kind, _offset, virtual, physical, file_size, memory_size = values[:6]
            else:
                kind, _flags, _offset, virtual, physical, file_size, memory_size = values[:7]
            if kind == 1 and memory_size:
                start = physical or virtual
                ranges.append((start, start + memory_size - 1))
        return ranges

    @staticmethod
    def merge_ranges(ranges):
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    def update_job_state(self):
        issues = []
        segments = []
        for image in self.images:
            if not Path(image["path"]).is_file():
                issues.append(f"Missing file: {image['path']}")
                continue
            if Path(image["path"]).suffix.lower() == ".bin":
                try:
                    int(image["address"], 0)
                except ValueError:
                    issues.append(f"{Path(image['path']).name} needs a valid start address")
            for segment in image["inspection"].get("segments", []):
                for old_start, old_end, old_name in segments:
                    if segment[0] <= old_end and old_start <= segment[1]:
                        issues.append(f"{Path(image['path']).name} overlaps {old_name}")
                segments.append((*segment, Path(image["path"]).name))
        if self.connected and segments:
            regions = self.load_memory_map()
            for start, end, name in segments:
                if not any(region["address"] <= start and end < region["address"] + region["size"] for region in regions):
                    issues.append(f"{name} extends outside the target’s known memory map")
        self.job_valid = bool(self.images) and not issues
        if hasattr(self, "job_warning"):
            self.job_warning.set_label("\n".join(dict.fromkeys(issues)))
            self.job_warning.set_visible(bool(issues))
        if hasattr(self, "flash_button"):
            enabled = self.connected and self.job_valid
            verify_supported = enabled and all(Path(image["path"]).suffix.lower() != ".stm32" for image in self.images)
            self.flash_button.set_sensitive(enabled); self.production_button.set_sensitive(enabled); self.verify_button.set_sensitive(verify_supported)
            self.verify_button.set_tooltip_text(None if verify_supported else "Read-only verification is unavailable for encrypted/proprietary .stm32 containers")

    @staticmethod
    def load_profiles():
        try:
            data = json.loads(PROFILE_PATH.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def write_profiles(self):
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps(self.profiles, indent=2) + "\n")

    def refresh_profile_selector(self, selected_name=None):
        self.profile_loading = True
        names = ["Flashing profiles…", *[profile["name"] for profile in self.profiles]]
        self.profile_selector.set_model(Gtk.StringList.new(names))
        selected = 0
        if selected_name:
            selected = next((index + 1 for index, profile in enumerate(self.profiles) if profile["name"] == selected_name), 0)
        self.profile_selector.set_selected(selected)
        self.profile_delete_button.set_sensitive(selected > 0)
        self.profile_loading = False

    def current_profile(self, name):
        device = self.selected_device() or {}
        return {
            "name": name,
            "target": {"kind": device.get("kind"), "serial": device.get("serial"), "port": device.get("port")},
            "connection": {
                "debug_port": self.debug_port.get_selected(), "frequency": self.frequency.get_selected(), "mode": self.mode.get_selected(),
                "access_port": self.access_port.get_text(), "reset_mode": self.reset_mode.get_selected(),
                "shared_probe": self.shared_probe.get_active(), "low_power_debug": self.low_power_debug.get_active(),
                "target_sel": self.target_sel.get_text(),
                "dfu_pid": self.dfu_pid.get_text(), "dfu_vid": self.dfu_vid.get_text(),
                "uart_baud": self.uart_baud.get_selected(), "uart_parity": self.uart_parity.get_selected(),
                "uart_stop": self.uart_stop.get_selected(),
            },
            "firmware": self.firmware,
            "images": [{"path": image["path"], "address": image["address"]} for image in self.images],
            "external_loaders": list(self.external_loaders),
            "address": self.address.get_text().strip(),
            "incremental": self.incremental.get_active(),
            "skip_erase": self.skip_erase.get_active(),
            "fast_verify": self.fast_verify.get_active(),
            "reset_after": self.reset_after.get_active(),
            "production": {
                "require_serial": self.production_require_serial_check.get_active() if getattr(self, "production_window", None) else self.production_defaults.get("require_serial", False),
                "uid_address": self.production_uid_address.get_text().strip() if getattr(self, "production_window", None) else self.production_defaults.get("uid_address", ""),
            },
        }

    def on_save_profile(self, _button):
        selected = self.profile_selector.get_selected()
        existing = self.profiles[selected - 1]["name"] if 0 < selected <= len(self.profiles) else ""
        entry = Gtk.Entry(text=existing, placeholder_text="Profile name")
        entry.set_activates_default(True)
        dialog = Adw.MessageDialog.new(self.window, "Save flashing profile", "Save the selected target, connection, firmware, and flash settings.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel"); dialog.add_response("save", "Save profile")
        dialog.set_default_response("save"); dialog.set_close_response("cancel")
        dialog.connect("response", self.finish_save_profile, entry)
        dialog.present()

    def finish_save_profile(self, _dialog, response, entry):
        name = entry.get_text().strip()
        if response != "save" or not name:
            return
        profile = self.current_profile(name)
        match = next((index for index, item in enumerate(self.profiles) if item["name"].casefold() == name.casefold()), None)
        if match is None:
            self.profiles.append(profile)
        else:
            self.profiles[match] = profile
        self.profiles.sort(key=lambda item: item["name"].casefold())
        try:
            self.write_profiles()
            self.refresh_profile_selector(name)
            self.toast(f"Saved profile “{name}”", "success")
        except OSError as error:
            self.toast(f"Could not save profile: {error}")

    def on_profile_selected(self, _selector, _detail):
        if self.profile_loading:
            return
        selected = self.profile_selector.get_selected()
        self.profile_delete_button.set_sensitive(selected > 0)
        if not 0 < selected <= len(self.profiles):
            return
        profile = self.profiles[selected - 1]
        settings = profile.get("connection", {})
        for widget, key in ((self.debug_port, "debug_port"), (self.frequency, "frequency"), (self.mode, "mode"),
                            (self.reset_mode, "reset_mode"), (self.uart_baud, "uart_baud"),
                            (self.uart_parity, "uart_parity"), (self.uart_stop, "uart_stop")):
            widget.set_selected(int(settings.get(key, widget.get_selected())))
        self.access_port.set_text(settings.get("access_port", "0"))
        self.shared_probe.set_active(bool(settings.get("shared_probe", False)))
        self.low_power_debug.set_active(bool(settings.get("low_power_debug", True)))
        self.target_sel.set_text(settings.get("target_sel", ""))
        self.dfu_pid.set_text(settings.get("dfu_pid", "0xDF11"))
        self.dfu_vid.set_text(settings.get("dfu_vid", "0x0483"))
        self.address.set_text(profile.get("address") or "0x08000000")
        self.incremental.set_active(bool(profile.get("incremental", False)))
        self.skip_erase.set_active(bool(profile.get("skip_erase", False)))
        self.fast_verify.set_active(bool(profile.get("fast_verify", True)))
        self.reset_after.set_active(bool(profile.get("reset_after", True)))
        self.production_defaults = dict(profile.get("production", {}))
        self.images.clear(); self.firmware = None
        self.external_loaders = [path for path in profile.get("external_loaders", []) if Path(path).is_file()]
        self.update_loader_label()
        saved_images = profile.get("images") or ([{"path": profile["firmware"], "address": profile.get("address", "0x08000000")}] if profile.get("firmware") else [])
        missing = []
        for saved in saved_images:
            path = saved.get("path")
            if not path or not Path(path).is_file():
                missing.append(path or "unnamed image")
                continue
            if self.set_firmware(path):
                image = self.images[-1]
                image["address"] = saved.get("address", image["address"])
                try:
                    image["inspection"] = self.inspect_firmware(image["path"], image["address"])
                except (OSError, ValueError, struct.error):
                    pass
        self.refresh_image_list()
        self.firmware_details_revealer.set_reveal_child(bool(self.images))
        self.file_label.set_label(f"{len(self.images)} image{'s' if len(self.images) != 1 else ''} ready" if self.images else "Drop firmware image(s) here")
        if self.images:
            self.file_label.remove_css_class("file-empty"); self.file_label.add_css_class("file-chosen")
        else:
            self.file_label.remove_css_class("file-chosen"); self.file_label.add_css_class("file-empty")
        self.update_job_state()
        if missing:
            self.toast(f"Profile has {len(missing)} missing image{'s' if len(missing) != 1 else ''}")
        self.pending_profile_target = profile.get("target")
        self.select_profile_target()

    def select_profile_target(self):
        target = self.pending_profile_target
        if not target:
            return
        for index, device in enumerate(self.devices):
            same_id = (target.get("serial") and target.get("serial") == device.get("serial")) or (target.get("port") and target.get("port") == device.get("port"))
            if target.get("kind") == device.get("kind") and same_id:
                self.probe_selector.set_selected(index + 1)
                self.pending_profile_target = None
                return
        self.toast("Profile loaded; its target is not currently present")

    def on_delete_profile(self, _button):
        selected = self.profile_selector.get_selected()
        if not 0 < selected <= len(self.profiles):
            return
        profile = self.profiles[selected - 1]
        dialog = Adw.MessageDialog.new(self.window, f"Delete “{profile['name']}”?", "This removes the saved profile only.")
        dialog.add_response("cancel", "Cancel"); dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", self.finish_delete_profile, selected - 1)
        dialog.present()

    def finish_delete_profile(self, _dialog, response, index):
        if response != "delete":
            return
        del self.profiles[index]
        try:
            self.write_profiles()
        except OSError as error:
            self.toast(f"Could not update profiles: {error}")
        self.refresh_profile_selector()

    def connection_settings_content(self):
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        self.setting_rows = {}
        self.debug_port = Gtk.DropDown.new_from_strings(["SWD", "JTAG"])
        self.frequency = Gtk.DropDown.new_from_strings(["4000 kHz — reliable", "8000 kHz — fast", "24000 kHz — V3 only"])
        self.frequency.set_selected(0)
        self.mode = Gtk.DropDown.new_from_strings(["Normal connection", "Under reset", "Hot plug", "Power down", "Hardware reset pulse"])
        self.access_port = Gtk.Entry(text="0", width_chars=8)
        self.reset_mode = Gtk.DropDown.new_from_strings(["Software reset", "Hardware reset", "Core reset"])
        self.shared_probe = Gtk.CheckButton(label="Allow shared probe access")
        self.low_power_debug = Gtk.CheckButton(label="Debug in low-power mode", active=True)
        self.target_sel = Gtk.Entry(placeholder_text="Optional multidrop TargetSel")
        self.dfu_pid = Gtk.Entry(text="0xDF11")
        self.dfu_vid = Gtk.Entry(text="0x0483")
        self.uart_baud = Gtk.DropDown.new_from_strings(["115200", "57600", "9600", "230400", "460800"])
        self.uart_parity = Gtk.DropDown.new_from_strings(["Even", "None", "Odd"])
        self.uart_stop = Gtk.DropDown.new_from_strings(["1 stop bit", "2 stop bits"])
        self.add_setting_row(grid, "debug_port", "Debug port", self.debug_port, 0)
        self.add_setting_row(grid, "frequency", "Debug speed", self.frequency, 1)
        self.add_setting_row(grid, "mode", "Connect", self.mode, 2)
        self.add_setting_row(grid, "access_port", "Access port", self.access_port, 3)
        self.add_setting_row(grid, "reset_mode", "Reset mode", self.reset_mode, 4)
        self.add_setting_row(grid, "shared_probe", "", self.shared_probe, 5)
        self.add_setting_row(grid, "low_power_debug", "", self.low_power_debug, 6)
        self.add_setting_row(grid, "target_sel", "SWD multidrop", self.target_sel, 7)
        self.add_setting_row(grid, "dfu_pid", "DFU PID", self.dfu_pid, 8)
        self.add_setting_row(grid, "dfu_vid", "DFU VID", self.dfu_vid, 9)
        self.add_setting_row(grid, "uart_baud", "UART baud", self.uart_baud, 10)
        self.add_setting_row(grid, "uart_parity", "UART parity", self.uart_parity, 11)
        self.add_setting_row(grid, "uart_stop", "UART stop bits", self.uart_stop, 12)
        return grid

    def flash_settings_content(self):
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        self.address = Gtk.Entry(text="0x08000000")
        address_label = Gtk.Label(label="Default binary address", xalign=0)
        grid.attach(address_label, 0, 0, 1, 1); grid.attach(self.address, 1, 0, 1, 1)
        self.incremental = Gtk.CheckButton(label="Program only modified sectors", active=False)
        self.skip_erase = Gtk.CheckButton(label="Skip erase before programming", active=False)
        self.fast_verify = Gtk.CheckButton(label="Fast sector verification", active=True)
        self.reset_after = Gtk.CheckButton(label="Reset target after flashing", active=True)
        grid.attach(self.incremental, 0, 1, 2, 1)
        grid.attach(self.skip_erase, 0, 2, 2, 1)
        grid.attach(self.fast_verify, 0, 3, 2, 1)
        grid.attach(self.reset_after, 0, 4, 2, 1)
        self.job_warning = Gtk.Label(xalign=0, wrap=True)
        self.job_warning.add_css_class("warning-text"); self.job_warning.set_visible(False)
        grid.attach(self.job_warning, 0, 5, 2, 1)
        return grid

    def add_setting_row(self, grid, key, text, control, row):
        label = Gtk.Label(label=text, xalign=0)
        grid.attach(label, 0, row, 1, 1); grid.attach(control, 1, row, 1, 1)
        self.setting_rows[key] = (label, control)

    def on_device_selected(self, _selector, _detail):
        self.set_disconnected()
        self.update_settings_for_device()

    def update_settings_for_device(self):
        selected = self.probe_selector.get_selected()
        if selected == 0 or selected - 1 >= len(self.devices):
            self.connection_revealer.set_reveal_child(False)
            self.device_info_revealer.set_reveal_child(False)
            self.target_actions.set_visible(False)
            return
        kind = self.devices[selected - 1]["kind"]
        visible = {
            "stlink": {"debug_port", "frequency", "mode", "access_port", "reset_mode", "shared_probe", "low_power_debug", "target_sel"},
            "dfu": {"dfu_pid", "dfu_vid"},
            "uart": {"uart_baud", "uart_parity", "uart_stop"},
        }[kind]
        for key, widgets in self.setting_rows.items():
            for widget in widgets:
                widget.set_visible(key in visible)
        self.connection_revealer.set_reveal_child(True)
        self.device_info_revealer.set_reveal_child(False)
        self.target_actions.set_visible(False)

    def set_disconnected(self):
        self.connected = False
        self.probe_update_check_generation += 1
        self.probe_update_available_serial = None
        if hasattr(self, "probe_update_button"):
            self.probe_update_button.set_visible(False)
            self.probe_update_button.set_sensitive(False)
        self.target_status_icon.set_from_icon_name("network-offline-symbolic")
        self.target_status_icon.remove_css_class("target-connected")
        self.target_status_icon.add_css_class("target-disconnected")
        self.target_status_icon.set_tooltip_text("Not connected")
        self.disconnect_button.set_opacity(0); self.disconnect_button.set_sensitive(False)
        if hasattr(self, "flash_button"):
            self.flash_button.set_sensitive(False)
            self.production_button.set_sensitive(False)
            self.verify_button.set_sensitive(False)

    def on_connect(self, _button):
        connect = self.connection_string()
        if not connect:
            self.toast("Select a probe first")
            return
        self.connect_button.set_sensitive(False); self.connect_button.set_label("Connecting…")
        self.target_status_icon.set_from_icon_name("network-wired-symbolic")
        self.target_status_icon.set_tooltip_text("Connecting")
        self.run_cli(["-c", connect], "Connecting to target…", self.finish_connect)

    def finish_connect(self, code, output):
        self.connect_button.set_sensitive(True); self.connect_button.set_label("Connect")
        clean = self.clean_cli_output(output)
        failed = code != 0 or re.search(r"(?:DEV_[A-Z_]+|Unable to get core ID|\bError:)", clean, re.IGNORECASE)
        if failed:
            self.set_disconnected(); self.connection_revealer.set_reveal_child(True); self.toast("Connection failed")
            return
        self.connected = True
        self.target_status_icon.set_from_icon_name("network-wired-symbolic")
        self.target_status_icon.remove_css_class("target-disconnected")
        self.target_status_icon.add_css_class("target-connected")
        self.target_status_icon.set_tooltip_text("Connected")
        self.disconnect_button.set_opacity(1); self.disconnect_button.set_sensitive(True)
        self.populate_target_info(clean)
        self.connection_revealer.set_reveal_child(False)
        self.device_info_revealer.set_reveal_child(True); self.target_actions.set_visible(True)
        self.erase_button.set_sensitive(True)
        self.update_job_state(); self.set_status("Connected", "success")
        self.check_probe_firmware_update()

    def stlink_updater_command(self, serial, action):
        updater = self.stlink_updater
        if not updater:
            return None
        return [updater["java"], f'-Djava.library.path={updater["native"]}', "-jar", updater["jar"],
                "-sn", serial, action]

    def check_probe_firmware_update(self):
        device = self.selected_device() or {}
        self.probe_update_available_serial = None
        self.probe_update_button.set_visible(False)
        self.probe_update_button.set_sensitive(False)
        if not self.connected or device.get("kind") != "stlink" or not self.stlink_updater:
            return
        serial = device.get("serial")
        command = self.stlink_updater_command(serial, "-checkVer")
        if not command:
            return
        self.probe_update_check_generation += 1
        generation = self.probe_update_check_generation
        threading.Thread(target=self.check_probe_firmware_update_process,
                         args=(command, serial, generation), daemon=True).start()

    def check_probe_firmware_update_process(self, command, serial, generation):
        try:
            process = subprocess.run(command, cwd=self.stlink_updater["cwd"], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, timeout=30)
            code, output = process.returncode, process.stdout
        except (OSError, subprocess.TimeoutExpired) as error:
            code, output = -1, str(error)
        GLib.idle_add(self.finish_probe_firmware_update_check, code, output, serial, generation)

    def finish_probe_firmware_update_check(self, code, output, serial, generation):
        device = self.selected_device() or {}
        if (generation != self.probe_update_check_generation or not self.connected or
                device.get("kind") != "stlink" or device.get("serial") != serial):
            return False
        clean = self.clean_cli_output(output)
        available = self.probe_firmware_update_is_available(code, clean)
        if available:
            self.probe_update_available_serial = serial
            self.probe_update_button.set_visible(True)
            self.probe_update_button.set_sensitive(True)
            self.probe_update_button.set_tooltip_text("A bundled ST-LINK firmware update is available")
            self.append_log(f"\n⬆ ST-LINK firmware update available for {serial}\n")
        return False

    @staticmethod
    def probe_firmware_update_is_available(code, output):
        return code == 0 and "up to date" not in output.lower() and "Firmware version detected:" in output

    def on_probe_firmware_update(self, _button):
        device = self.selected_device() or {}
        serial = device.get("serial")
        if (not self.connected or device.get("kind") != "stlink" or
                serial != self.probe_update_available_serial):
            self.probe_update_button.set_visible(False)
            self.toast("The selected probe no longer has an available update")
            return
        board = device.get("board", "ST-LINK")
        firmware = device.get("firmware", "unknown")
        dialog = Adw.MessageDialog.new(
            self.window,
            "Update ST-LINK firmware?",
            f"Update {board} to the newest firmware bundled with STM32CubeProgrammer?\n\n"
            f"Serial: {serial}\nCurrent firmware: {firmware}\n\nDo not unplug the probe during the update."
        )
        dialog.add_response("no", "No")
        dialog.add_response("update", "Update")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("no"); dialog.set_close_response("no")
        dialog.connect("response", self.finish_probe_firmware_update_confirmation, serial)
        dialog.present()

    def finish_probe_firmware_update_confirmation(self, _dialog, response, serial):
        if response != "update":
            return
        device = self.selected_device() or {}
        if not self.connected or device.get("kind") != "stlink" or device.get("serial") != serial:
            self.toast("The selected probe changed; update canceled")
            return
        command = self.stlink_updater_command(serial, "-update")
        if not command or self.running:
            self.toast("ST-LINK updater is unavailable or another operation is running")
            return
        self.running = True
        self.probe_update_button.set_sensitive(False)
        self.disconnect_button.set_sensitive(False)
        self.refresh_button.set_sensitive(False)
        self.flash_button.set_sensitive(False); self.verify_button.set_sensitive(False)
        self.production_button.set_sensitive(False); self.erase_button.set_sensitive(False)
        self.set_status("Updating ST-LINK firmware…", "working")
        self.append_log("\n$ Updating ST-LINK firmware for serial " + serial + "\n")
        threading.Thread(target=self.run_probe_firmware_update_process,
                         args=(command, serial), daemon=True).start()

    def run_probe_firmware_update_process(self, command, serial):
        try:
            process = subprocess.Popen(command, cwd=self.stlink_updater["cwd"], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            output = ""
            for line in process.stdout:
                output += line
                clean = self.clean_cli_output(line)
                if clean.strip():
                    GLib.idle_add(self.append_log, clean)
            code = process.wait()
        except OSError as error:
            code, output = -1, str(error)
        GLib.idle_add(self.finish_probe_firmware_update, code, output, serial)

    def finish_probe_firmware_update(self, code, output, serial):
        clean = self.clean_cli_output(output)
        success = code == 0 and "Upgrade is successful" in clean
        self.running = False
        self.refresh_button.set_sensitive(True)
        self.set_status("ST-LINK firmware updated" if success else "ST-LINK update failed",
                        "success" if success else "warning")
        result = "✓ ST-LINK firmware update completed" if success else "✕ ST-LINK firmware update failed"
        self.append_log(f"\n{result} for {serial}\n")
        self.set_disconnected()
        self.device_info_revealer.set_reveal_child(False); self.target_actions.set_visible(False)
        if success:
            GLib.timeout_add(1500, self.start_discovery)
        else:
            self.update_settings_for_device()
        return False

    def on_disconnect(self, _button):
        self.set_disconnected()
        self.device_info_revealer.set_reveal_child(False); self.target_actions.set_visible(False)
        self.update_settings_for_device()
        self.set_status("Disconnected", "neutral")
        self.append_log("Target disconnected.\n")

    def populate_target_info(self, output):
        device = self.selected_device() or {}
        interface = {"stlink": "ST-LINK / SWD", "dfu": "USB DFU", "uart": "UART bootloader"}.get(device.get("kind"), "—")
        def value(*labels):
            for label in labels:
                match = re.search(rf"^\s*{re.escape(label)}\s*:\s*([^\r\n]+)", output, re.MULTILINE | re.IGNORECASE)
                if match and match.group(1).strip() != "-": return match.group(1).strip()
            return "—"
        self.target_info["interface"].set_label(interface)
        self.target_info["device"].set_label(value("Device name", "Device Name", "Board", "Board Name"))
        self.target_info["device_id"].set_label(value("Device ID", "Device Id"))
        self.target_info["core"].set_label(value("Device CPU", "CPU", "Core"))
        self.target_info["flash"].set_label(value("Flash size", "Flash Size", "NVM size"))
        self.target_info["voltage"].set_label(value("Voltage", "Target voltage"))

    def selected_device(self):
        selected = self.probe_selector.get_selected()
        return self.devices[selected - 1] if selected > 0 and selected - 1 < len(self.devices) else None

    def connection_string(self):
        device = self.selected_device()
        if not device:
            return None
        if device["kind"] == "stlink":
            mode = ["NORMAL", "UR", "HOTPLUG", "POWERDOWN", "HWRSTPULSE"][self.mode.get_selected()]
            freq = ["4000", "8000", "24000"][self.frequency.get_selected()]
            port = ["SWD", "JTAG"][self.debug_port.get_selected()]
            reset = ["SWrst", "HWrst", "Crst"][self.reset_mode.get_selected()]
            parts = [f"port={port}", f"freq={freq}", f"mode={mode}", f"reset={reset}", f"ap={self.access_port.get_text().strip() or '0'}", f"sn={device['serial']}"]
            if self.shared_probe.get_active():
                parts.append("shared")
            if not self.low_power_debug.get_active():
                parts.append("dLPM")
            if self.target_sel.get_text().strip():
                parts.append(f"TargetSel={self.target_sel.get_text().strip()}")
            return " ".join(parts)
        if device["kind"] == "dfu":
            return f"port=USB1 sn={device['serial']} PID={self.dfu_pid.get_text().strip()} VID={self.dfu_vid.get_text().strip()}"
        parity = ["EVEN", "NONE", "ODD"][self.uart_parity.get_selected()]
        stop = ["1", "2"][self.uart_stop.get_selected()]
        baud = ["115200", "57600", "9600", "230400", "460800"][self.uart_baud.get_selected()]
        return f"port=/dev/{device['port']} br={baud} P={parity} db=8 sb={stop} fc=OFF"

    def connection_arguments(self, include_loaders=False):
        arguments = ["-c", self.connection_string()]
        if include_loaders:
            loader_flag = "-el" if (self.selected_device() or {}).get("kind") == "stlink" else "-elbl"
            for loader in self.external_loaders:
                arguments.extend([loader_flag, loader])
        return arguments

    def log_content(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True)
        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False, monospace=True, vexpand=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.log_view.set_top_margin(14); self.log_view.set_bottom_margin(14); self.log_view.set_left_margin(16); self.log_view.set_right_margin(16)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True, min_content_height=180)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_overlay_scrolling(False)
        scroll.set_propagate_natural_width(False)
        scroll.set_propagate_natural_height(False)
        scroll.add_css_class("activity-log")
        scroll.set_margin_top(4)
        scroll.set_child(self.log_view); box.append(scroll)
        self.append_log("Flash Deck is ready. Scan a probe, choose firmware, then flash.\n")
        return box

    def on_choose_file(self, _button):
        dialog = Gtk.FileDialog(title="Choose firmware", modal=True)
        firmware_filter = Gtk.FileFilter()
        firmware_filter.set_name("STM32 firmware")
        for pattern in ("*.bin", "*.hex", "*.elf", "*.srec", "*.s19", "*.stm32"):
            firmware_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter); filters.append(firmware_filter)
        dialog.set_filters(filters); dialog.set_default_filter(firmware_filter)
        dialog.open_multiple(self.window, None, self.on_file_response)

    def on_file_response(self, dialog, result):
        try:
            selected = dialog.open_multiple_finish(result)
        except GLib.Error as error:
            if not any(error.matches(Gtk.DialogError.quark(), state) for state in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)):
                self.toast(f"Could not open firmware: {error.message}")
            return
        for index in range(selected.get_n_items()):
            self.set_firmware(selected.get_item(index).get_path())

    def on_choose_loader(self, _button):
        dialog = Gtk.FileDialog(title="Choose external memory loader", modal=True)
        loader_directory = INSTALL_ROOT / "bin" / "ExternalLoader"
        if loader_directory.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(loader_directory)))
        loader_filter = Gtk.FileFilter(); loader_filter.set_name("STM32 external loaders"); loader_filter.add_pattern("*.stldr")
        filters = Gio.ListStore.new(Gtk.FileFilter); filters.append(loader_filter)
        dialog.set_filters(filters); dialog.set_default_filter(loader_filter)
        dialog.open_multiple(self.window, None, self.on_loader_response)

    def on_loader_response(self, dialog, result):
        try:
            selected = dialog.open_multiple_finish(result)
        except GLib.Error as error:
            if not any(error.matches(Gtk.DialogError.quark(), state) for state in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)):
                self.toast(f"Could not open loader: {error.message}")
            return
        for index in range(selected.get_n_items()):
            path = selected.get_item(index).get_path()
            if path and path not in self.external_loaders:
                self.external_loaders.append(path)
        self.update_loader_label()

    def on_clear_loaders(self, _button):
        self.external_loaders.clear()
        self.update_loader_label()

    def update_loader_label(self):
        if not self.external_loaders:
            self.loader_label.set_label("No external loader")
        elif len(self.external_loaders) == 1:
            region = self.inspect_external_loader(self.external_loaders[0])
            detail = f"  ·  0x{region['address']:08X}  ·  {self.format_bytes(region['size'])}" if region else ""
            self.loader_label.set_label(Path(self.external_loaders[0]).name + detail)
        else:
            self.loader_label.set_label(f"{len(self.external_loaders)} external loaders")
        self.loader_clear.set_sensitive(bool(self.external_loaders))
        self.update_job_state()

    @staticmethod
    def inspect_external_loader(path):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        marker = Path(path).stem.encode()[:24]
        offset = data.find(marker)
        if offset < 0 or offset + 120 > len(data):
            return None
        try:
            _kind, address, size, page_size = struct.unpack_from("<IIII", data, offset + 100)
        except struct.error:
            return None
        if address < 0x08000000 or not 0 < size <= 0x100000000 or not page_size:
            return None
        return {"name": Path(path).stem, "address": address, "size": size, "sectors": [], "external": True}

    def on_scan(self, _button):
        self.start_discovery()

    def start_discovery(self):
        if self.running or not self.cli:
            return
        self.set_disconnected()
        self.running = True; self.flash_button.set_sensitive(False); self.erase_button.set_sensitive(False); self.refresh_button.set_sensitive(False)
        self.discovery_status.set_visible(True); self.discovery_spinner.start()
        self.device_row.set_visible(False)
        self.target_actions.set_visible(False)
        self.connection_revealer.set_reveal_child(False); self.device_info_revealer.set_reveal_child(False)
        self.set_status("Discovering connected devices…", "working")
        self.append_log("\n$ Discovering ST-LINK, DFU, and UART devices\n")
        threading.Thread(target=self.discover_process, daemon=True).start()

    def discover_process(self):
        outputs = {}
        for interface in ("stlink", "usb", "uart"):
            process = subprocess.run([self.cli, "-l", interface], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            outputs[interface] = process.stdout
        GLib.idle_add(self.finish_discovery, outputs)

    def finish_discovery(self, outputs):
        self.running = False; self.update_job_state(); self.erase_button.set_sensitive(True); self.refresh_button.set_sensitive(True)
        devices = []
        stlink = re.sub(r"\x1b\[[0-9;]*m", "", outputs["stlink"])
        for _index, body in re.findall(r"ST-Link Probe (\d+)\s*:\s*(.*?)(?=\nST-Link Probe \d+\s*:|\n-+|\Z)", stlink, re.DOTALL):
            serial = self.value_in_probe(body, "ST-LINK SN")
            if serial and serial != "-":
                board = self.value_in_probe(body, "Board Name") or "ST-LINK"
                fw = self.value_in_probe(body, "ST-LINK FW") or "unknown firmware"
                devices.append({"kind": "stlink", "serial": serial, "board": board, "firmware": fw,
                                "label": f"ST-LINK / SWD  ·  {board}  ·  {serial}  ·  {fw}"})
        dfu = re.sub(r"\x1b\[[0-9;]*m", "", outputs["usb"])
        for serial in re.findall(r"Serial Number\s*:\s*([^\r\n]+)", dfu):
            if serial.strip() and serial.strip() != "-":
                devices.append({"kind": "dfu", "serial": serial.strip(), "label": f"USB DFU  ·  {serial.strip()}"})
        uart = re.sub(r"\x1b\[[0-9;]*m", "", outputs["uart"])
        for port in re.findall(r"^Port:\s*(\S+)", uart, re.MULTILINE):
            if not port.startswith("ttyS"):
                devices.append({"kind": "uart", "port": port, "label": f"UART bootloader  ·  /dev/{port}"})
        self.devices = devices
        self.discovery_spinner.stop()
        if devices:
            labels = ["Select a probe below", *[d["label"] for d in devices]]
            self.probe_selector.set_model(Gtk.StringList.new(labels)); self.probe_selector.set_selected(0); self.probe_selector.set_sensitive(True)
            self.discovery_status.set_visible(False); self.device_row.set_visible(True)
            self.update_settings_for_device()
            self.set_status(f"{len(devices)} device{'s' if len(devices) != 1 else ''} found", "success")
            self.append_log(f"✓ Discovery complete — {len(devices)} device{'s' if len(devices) != 1 else ''} found\n")
            for device in devices:
                self.append_log(f"  • {device['label']}\n")
            self.select_profile_target()
        else:
            self.connection_revealer.set_reveal_child(False)
            self.device_info_revealer.set_reveal_child(False)
            self.target_actions.set_visible(False)
            self.discovery_status.set_visible(False)
            self.probe_selector.set_model(Gtk.StringList.new(["No probes found"])); self.probe_selector.set_sensitive(False); self.device_row.set_visible(True)
            self.set_status("No devices found", "warning")
            self.append_log("No supported devices found.\n")

    def finish_scan(self, code, output):
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        if self.transport.get_selected() != 0:
            self.finish_generic_scan(clean)
            return
        found = []
        for index, body in re.findall(r"ST-Link Probe (\d+)\s*:\s*(.*?)(?=\nST-Link Probe \d+\s*:|\n-+|\Z)", clean, re.DOTALL):
            serial = self.value_in_probe(body, "ST-LINK SN")
            if not serial or serial == "-":
                continue
            found.append({"index": index, "serial": serial,
                          "firmware": self.value_in_probe(body, "ST-LINK FW") or "unknown firmware",
                          "board": self.value_in_probe(body, "Board Name") or "ST-LINK"})
        self.probes = found
        if not found:
            self.probe_selector.set_model(Gtk.StringList.new(["No usable ST-LINK probe found"]))
            self.probe_selector.set_sensitive(False)
            self.scan_button.set_label("No ST-LINK probe found — discover again")
            self.toast("No usable probe found")
            return
        labels = [f"{p['board']}  ·  {p['serial']}  ·  {p['firmware']}" for p in found]
        self.probe_selector.set_model(Gtk.StringList.new(labels))
        self.probe_selector.set_selected(0)
        self.probe_selector.set_sensitive(True)
        self.scan_button.set_label("Refresh ST-LINK probes")

    def finish_generic_scan(self, output):
        if self.transport.get_selected() == 1:
            serials = re.findall(r"Serial Number\s*:\s*([^\r\n]+)", output)
            devices = [f"DFU device · {serial.strip()}" for serial in serials if serial.strip() and serial.strip() != "-"]
        else:
            devices = [f"UART · /dev/{port}" for port in re.findall(r"^Port:\s*(\S+)", output, re.MULTILINE) if not port.startswith("ttyS")]
        if not devices:
            devices = ["No bootloader device found"]
            self.probe_selector.set_sensitive(False)
            self.scan_button.set_label("No device found — discover again")
        else:
            self.probe_selector.set_sensitive(True)
            self.scan_button.set_label("Refresh discovered devices")
        self.probe_selector.set_model(Gtk.StringList.new(devices)); self.probe_selector.set_selected(0)

    @staticmethod
    def value_in_probe(body, label):
        match = re.search(rf"{re.escape(label)}\s*:\s*([^\r\n]+)", body)
        return match.group(1).strip() if match else None

    def on_flash(self, _button):
        if not self.images or not getattr(self, "job_valid", False):
            self.toast("Resolve the flashing job issues first")
            return
        if not self.connected:
            self.toast("Connect to the target first")
            return
        connect = self.connection_string()
        if not connect:
            self.toast("Select a probe first")
            return
        self.run_cli(self.flash_arguments(), "Flashing and verifying…")

    def flash_arguments(self):
        command = self.connection_arguments(include_loaders=True)
        if self.skip_erase.get_active():
            command.append("--skipErase")
        for image in self.images:
            command.extend(["-w", image["path"]])
            if image["address"]:
                command.append(image["address"])
            if self.incremental.get_active():
                command.append("incremental")
            command.append("-v")
            if self.fast_verify.get_active():
                command.append("fast")
        if self.reset_after.get_active():
            command.append("-rst")
        return command

    def on_verify_job(self, _button):
        if not self.connected or not self.job_valid:
            return
        try:
            blocks = []
            for image in self.images:
                blocks.extend(self.firmware_blocks(image["path"], image["address"]))
        except (OSError, ValueError, struct.error) as error:
            self.toast(f"Could not prepare verification: {error}")
            return
        if not blocks:
            self.toast("No addressable firmware data to verify")
            return
        self.running = True
        self.flash_button.set_sensitive(False); self.verify_button.set_sensitive(False); self.production_button.set_sensitive(False)
        self.set_status("Reading target for verification…", "working")
        self.append_log(f"\n$ Read-only verification of {len(blocks)} firmware range{'s' if len(blocks) != 1 else ''}\n")
        arguments = self.connection_arguments(include_loaders=True)
        threading.Thread(target=self.verify_job_process, args=(arguments, blocks), daemon=True).start()

    @classmethod
    def firmware_blocks(cls, path, address=""):
        data = Path(path).read_bytes()
        suffix = Path(path).suffix.lower()
        if suffix == ".bin":
            return [(int(address, 0), data)] if data else []
        if suffix == ".hex":
            base = 0; blocks = []
            for line_number, raw in enumerate(data.decode("ascii").splitlines(), 1):
                line = raw.strip()
                if not line:
                    continue
                record = bytes.fromhex(line[1:]) if line.startswith(":") else b""
                if len(record) < 5 or record[0] + 5 != len(record) or sum(record) & 0xFF:
                    raise ValueError(f"Invalid Intel HEX record on line {line_number}")
                count, offset, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
                payload = record[4:4 + count]
                if kind == 0 and payload:
                    blocks.append((base + offset, payload))
                elif kind == 2 and count == 2:
                    base = int.from_bytes(payload, "big") << 4
                elif kind == 4 and count == 2:
                    base = int.from_bytes(payload, "big") << 16
            return cls.merge_data_blocks(blocks)
        if suffix in {".srec", ".s19"}:
            blocks = []; widths = {"1": 2, "2": 3, "3": 4}
            for line_number, raw in enumerate(data.decode("ascii").splitlines(), 1):
                line = raw.strip()
                if not line or len(line) < 4 or line[:1] != "S" or line[1] not in widths:
                    continue
                record = bytes.fromhex(line[2:]); width = widths[line[1]]
                if not record or record[0] != len(record) - 1 or sum(record) & 0xFF != 0xFF:
                    raise ValueError(f"Invalid S-record on line {line_number}")
                start = int.from_bytes(record[1:1 + width], "big")
                payload = record[1 + width:-1]
                if payload:
                    blocks.append((start, payload))
            return cls.merge_data_blocks(blocks)
        if suffix == ".elf":
            if data[:4] != b"\x7fELF":
                raise ValueError("Invalid ELF header")
            elf_class, encoding = data[4], data[5]
            endian = "<" if encoding == 1 else ">" if encoding == 2 else None
            if elf_class == 1:
                header = struct.unpack_from(endian + "HHIIIIIHHHHHH", data, 16)
                program_offset, entry_size, count, fmt = header[4], header[8], header[9], endian + "IIIIIIII"
            else:
                header = struct.unpack_from(endian + "HHIQQQIHHHHHH", data, 16)
                program_offset, entry_size, count, fmt = header[4], header[8], header[9], endian + "IIQQQQQQ"
            blocks = []
            for index in range(count):
                values = struct.unpack_from(fmt, data, program_offset + index * entry_size)
                if elf_class == 1:
                    kind, file_offset, virtual, physical, file_size = values[:5]
                else:
                    kind, _flags, file_offset, virtual, physical, file_size = values[:6]
                if kind == 1 and file_size:
                    blocks.append((physical or virtual, data[file_offset:file_offset + file_size]))
            return cls.merge_data_blocks(blocks)
        raise ValueError("Read-only verification is unavailable for this file format")

    @staticmethod
    def merge_data_blocks(blocks):
        merged = []
        for address, payload in sorted(blocks):
            if merged and address == merged[-1][0] + len(merged[-1][1]):
                merged[-1] = (merged[-1][0], merged[-1][1] + payload)
            else:
                merged.append((address, payload))
        return merged

    def verify_job_process(self, connection_arguments, blocks):
        output = ""
        mismatch = None
        for address, expected in blocks:
            descriptor, path = tempfile.mkstemp(prefix="flash-deck-verify-", suffix=".bin")
            os.close(descriptor); os.unlink(path)
            try:
                command = [self.cli, *connection_arguments, "-u", f"0x{address:08X}", str(len(expected)), path]
                process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                output += process.stdout
                actual = Path(path).read_bytes() if process.returncode == 0 and Path(path).is_file() else b""
                if process.returncode != 0 or len(actual) != len(expected):
                    mismatch = f"Could not read 0x{address:08X}–0x{address + len(expected) - 1:08X}"
                    break
                for offset, (wanted, found) in enumerate(zip(expected, actual)):
                    if wanted != found:
                        mismatch = f"Mismatch at 0x{address + offset:08X}: firmware 0x{wanted:02X}, target 0x{found:02X}"
                        break
                if mismatch:
                    break
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        GLib.idle_add(self.finish_read_only_verify, mismatch is None, output, mismatch)

    def finish_read_only_verify(self, passed, output, mismatch):
        self.finish_process(0 if passed else 1, output, None)
        self.append_log(("✓ Target exactly matches every firmware byte\n" if passed else f"✕ {mismatch}\n"))
        self.set_status("Verified" if passed else "Verification mismatch", "success" if passed else "warning")

    def on_production_mode(self, _button):
        if not self.connected or not self.job_valid:
            self.toast("Connect a target and choose firmware first")
            return
        if getattr(self, "production_window", None):
            self.production_window.present()
            return
        self.production_window = Adw.ApplicationWindow(application=self, title="Production — Flash Deck", default_width=680, default_height=620)
        self.production_window.set_transient_for(self.window)
        self.production_window.connect("close-request", self.on_production_closed)
        toolbar = Adw.ToolbarView(); toolbar.add_top_bar(Adw.HeaderBar())
        content = self.window_content(spacing=16)
        title = Gtk.Label(label="Repeat flashing", xalign=0); title.add_css_class("hero-title")
        subtitle = Gtk.Label(label=f"{len(self.images)} image{'s' if len(self.images) != 1 else ''}  ·  verify enabled", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        subtitle.add_css_class("dim-label")
        state_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        state_card.add_css_class("production-state")
        self.production_spinner = Gtk.Spinner()
        self.production_state = Gtk.Label(label="Ready", wrap=True, justify=Gtk.Justification.CENTER)
        self.production_state.add_css_class("production-title")
        state_card.append(self.production_spinner); state_card.append(self.production_state)
        counters = Gtk.Box(spacing=24, homogeneous=True)
        self.production_pass = self.production_counter(counters, "PASS")
        self.production_fail = self.production_counter(counters, "FAIL")
        self.production_total = self.production_counter(counters, "TOTAL")
        trace = Gtk.Grid(column_spacing=10, row_spacing=8)
        trace.add_css_class("subcard")
        self.production_serial_entry = Gtk.Entry(placeholder_text="Scan or enter unit serial")
        self.production_serial_entry.connect("changed", lambda entry: setattr(self, "production_unit_serial", entry.get_text().strip()))
        self.production_require_serial_check = Gtk.CheckButton(label="Require a new unit serial for every cycle", active=bool(self.production_defaults.get("require_serial", False)))
        self.production_uid_address = Gtk.Entry(text=self.production_defaults.get("uid_address") or self.default_uid_address(), placeholder_text="Optional 12-byte UID address")
        trace.attach(Gtk.Label(label="Unit serial", xalign=0), 0, 0, 1, 1); trace.attach(self.production_serial_entry, 1, 0, 1, 1)
        trace.attach(self.production_require_serial_check, 0, 1, 2, 1)
        trace.attach(Gtk.Label(label="UID address", xalign=0), 0, 2, 1, 1); trace.attach(self.production_uid_address, 1, 2, 1, 1)
        report_label = Gtk.Label(label=str(REPORT_PATH), xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        report_label.add_css_class("dim-label")
        export_csv = Gtk.Button(label="Export CSV…"); export_csv.connect("clicked", self.on_export_production_report, "csv")
        export_json = Gtk.Button(label="Export JSON…"); export_json.connect("clicked", self.on_export_production_report, "json")
        report_actions = self.action_flow()
        report_actions.append(export_csv); report_actions.append(export_json)
        self.production_control = Gtk.Button(label="Start production run")
        self.production_control.add_css_class("suggested-action")
        self.production_control.connect("clicked", self.toggle_production)
        production_actions = self.action_flow()
        production_actions.append(self.production_control)
        note = Gtk.Label(label="Each detected target is programmed and verified once. Remove it before the next cycle begins.", wrap=True, justify=Gtk.Justification.CENTER)
        note.add_css_class("dim-label")
        content.append(title); content.append(subtitle); content.append(state_card); content.append(counters)
        content.append(trace); content.append(report_label); content.append(report_actions)
        content.append(note); content.append(production_actions)
        toolbar.set_content(content); self.production_window.set_content(toolbar); self.production_window.present()

    @staticmethod
    def production_counter(parent, label):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        value = Gtk.Label(label="0"); value.add_css_class("counter-value")
        caption = Gtk.Label(label=label); caption.add_css_class("card-title")
        box.append(value); box.append(caption); parent.append(box)
        return value

    def toggle_production(self, _button):
        if self.production_stop:
            self.production_stop.set()
            self.production_control.set_sensitive(False)
            self.production_state.set_label("Stopping after the current CLI operation…")
            return
        self.production_counts = {"pass": 0, "fail": 0}
        self.production_unit_serial = self.production_serial_entry.get_text().strip()
        self.production_require_serial = self.production_require_serial_check.get_active()
        self.production_uid_location = self.production_uid_address.get_text().strip()
        if self.production_uid_location:
            try:
                int(self.production_uid_location, 0)
            except ValueError:
                self.production_state.set_label("Enter a valid UID address or leave it blank")
                return
        if self.production_require_serial and not self.production_unit_serial:
            self.production_state.set_label("Scan or enter the first unit serial")
            self.production_serial_entry.grab_focus()
            return
        device = dict(self.selected_device() or {})
        selected_profile = self.profile_selector.get_selected()
        profile_name = self.profiles[selected_profile - 1]["name"] if 0 < selected_profile <= len(self.profiles) else ""
        self.production_context = {
            "target": self.target_info["device"].get_label(), "device_id": self.target_info["device_id"].get_label(),
            "probe": device.get("serial") or device.get("port", ""), "profile": profile_name,
            "job_sha256": hashlib.sha256("".join(image["inspection"]["sha256"] for image in self.images).encode()).hexdigest(),
            "images": ";".join(Path(image["path"]).name for image in self.images),
        }
        self.production_stop = threading.Event()
        self.production_control.set_label("Stop production run")
        self.production_control.remove_css_class("suggested-action")
        self.production_control.add_css_class("destructive-action")
        self.production_spinner.start()
        self.update_production_state("Target detected — programming and verifying…")
        arguments = list(self.flash_arguments())
        connection = self.connection_string()
        threading.Thread(target=self.production_loop, args=(arguments, connection), daemon=True).start()

    def production_loop(self, arguments, connection):
        waiting_for_removal = False
        absent_count = 0
        while not self.production_stop.wait(0.2):
            if waiting_for_removal:
                present = self.production_target_present(connection)
                absent_count = 0 if present else absent_count + 1
                if absent_count >= 3:
                    waiting_for_removal = False
                    absent_count = 0
                    GLib.idle_add(self.prepare_next_production_unit)
                else:
                    GLib.idle_add(self.update_production_state, "PASS — remove the programmed target" if self.production_last_pass else "FAIL — remove the target")
                self.production_stop.wait(0.8)
                continue
            if self.production_require_serial and not self.production_unit_serial:
                GLib.idle_add(self.update_production_state, "Scan or enter the next unit serial…")
                self.production_stop.wait(0.4)
                continue
            if not self.production_target_present(connection):
                GLib.idle_add(self.update_production_state, "Waiting for the next target…")
                self.production_stop.wait(0.8)
                continue
            GLib.idle_add(self.update_production_state, "Programming and verifying…")
            command = [self.cli, *arguments]
            unit_serial = self.production_unit_serial
            uid = self.read_target_uid(connection, self.production_uid_location)
            started = time.monotonic()
            process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            duration = time.monotonic() - started
            output = self.clean_cli_output(process.stdout)
            failed = process.returncode != 0 or bool(re.search(r"(?:DEV_[A-Z_]+|\bError:|Verification failed)", output, re.IGNORECASE))
            self.production_last_pass = not failed
            self.production_counts["fail" if failed else "pass"] += 1
            self.append_production_record(not failed, unit_serial, uid, duration)
            GLib.idle_add(self.finish_production_cycle, not failed, output)
            waiting_for_removal = True
            absent_count = 0
        GLib.idle_add(self.finish_production_run)

    def production_target_present(self, connection):
        try:
            process = subprocess.run([self.cli, "-c", connection], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        output = self.clean_cli_output(process.stdout)
        return process.returncode == 0 and not re.search(r"(?:DEV_[A-Z_]+|Unable to get core ID|\bError:)", output, re.IGNORECASE)

    def default_uid_address(self):
        device = self.target_info["device"].get_label().upper()
        known = {
            "STM32H7": "0x1FF1E800",
            "STM32F4": "0x1FFF7A10",
            "STM32F7": "0x1FF0F420",
            "STM32G0": "0x1FFF7590",
            "STM32G4": "0x1FFF7590",
            "STM32L4": "0x1FFF7590",
        }
        return next((address for family, address in known.items() if family in device), "")

    def read_target_uid(self, connection, address):
        if not address:
            return ""
        descriptor, path = tempfile.mkstemp(prefix="flash-deck-uid-", suffix=".bin")
        os.close(descriptor); os.unlink(path)
        try:
            process = subprocess.run([self.cli, "-c", connection, "-u", address, "12", path],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
            if process.returncode == 0 and Path(path).is_file():
                data = Path(path).read_bytes()
                if len(data) >= 12:
                    return data[:12].hex().upper()
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if os.path.exists(path):
                os.unlink(path)
        return ""

    def append_production_record(self, passed, unit_serial, uid, duration):
        fields = ["timestamp_utc", "result", "unit_serial", "device_uid", "target", "device_id", "probe",
                  "profile", "images", "job_sha256", "duration_seconds"]
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "result": "PASS" if passed else "FAIL", "unit_serial": unit_serial, "device_uid": uid,
            **self.production_context, "duration_seconds": f"{duration:.3f}",
        }
        try:
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            new_file = not REPORT_PATH.exists()
            with REPORT_PATH.open("a", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                if new_file:
                    writer.writeheader()
                writer.writerow(record)
        except OSError as error:
            GLib.idle_add(self.append_log, f"Could not write production report: {error}\n")

    def prepare_next_production_unit(self):
        if not getattr(self, "production_window", None):
            return
        self.production_serial_entry.set_text("")
        self.production_unit_serial = ""
        if self.production_require_serial:
            self.production_state.set_label("Scan or enter the next unit serial…")
            self.production_serial_entry.grab_focus()
        else:
            self.production_state.set_label("Waiting for the next target…")

    def on_export_production_report(self, _button, report_format):
        dialog = Gtk.FileDialog(title=f"Export production history as {report_format.upper()}", modal=True,
                                initial_name=f"flash-deck-production.{report_format}")
        dialog.save(self.production_window, None, self.finish_export_production_report, report_format)

    def finish_export_production_report(self, dialog, result, report_format):
        try:
            destination = dialog.save_finish(result).get_path()
        except GLib.Error as error:
            if not any(error.matches(Gtk.DialogError.quark(), state) for state in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)):
                self.toast(f"Could not export report: {error.message}")
            return
        try:
            if report_format == "csv":
                if REPORT_PATH.exists():
                    shutil.copyfile(REPORT_PATH, destination)
                else:
                    Path(destination).write_text("timestamp_utc,result,unit_serial,device_uid,target,device_id,probe,profile,images,job_sha256,duration_seconds\n")
            else:
                rows = []
                if REPORT_PATH.exists():
                    with REPORT_PATH.open(newline="") as stream:
                        rows = list(csv.DictReader(stream))
                Path(destination).write_text(json.dumps(rows, indent=2) + "\n")
            self.toast(f"Exported production history as {report_format.upper()}", "success")
        except OSError as error:
            self.toast(f"Could not export report: {error}")

    def finish_production_cycle(self, passed, output):
        self.append_log("\n$ Production cycle\n" + output)
        self.production_pass.set_label(str(self.production_counts["pass"]))
        self.production_fail.set_label(str(self.production_counts["fail"]))
        self.production_total.set_label(str(sum(self.production_counts.values())))
        self.update_production_state("PASS — remove the programmed target" if passed else "FAIL — remove the target")
        if passed:
            self.production_state.remove_css_class("production-fail"); self.production_state.add_css_class("production-pass")
        else:
            self.production_state.remove_css_class("production-pass"); self.production_state.add_css_class("production-fail")

    def update_production_state(self, text):
        if getattr(self, "production_window", None):
            self.production_state.set_label(text)

    def finish_production_run(self):
        self.production_stop = None
        if not getattr(self, "production_window", None):
            return
        self.production_spinner.stop()
        self.production_state.set_label("Production run stopped")
        self.production_control.set_label("Start production run")
        self.production_control.set_sensitive(True)
        self.production_control.remove_css_class("destructive-action")
        self.production_control.add_css_class("suggested-action")

    def on_production_closed(self, _window):
        if self.production_stop:
            self.production_stop.set()
        self.production_window = None
        return False

    def on_erase(self, _button):
        if not self.connected:
            return
        dialog = Adw.MessageDialog.new(self.window, "Erase all flash?", "This permanently removes the target’s programmable flash contents.")
        dialog.add_response("cancel", "Cancel"); dialog.add_response("erase", "Erase all")
        dialog.set_response_appearance("erase", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: self.run_cli(["-c", self.connection_string(), "-e", "all"], "Erasing chip…") if response == "erase" and self.connection_string() else None)
        dialog.present()

    def on_target_controls(self, _button):
        if getattr(self, "control_window", None):
            self.control_window.present()
            return
        self.control_window = Adw.ApplicationWindow(application=self, title="Target controls — Flash Deck", default_width=620, default_height=390)
        self.control_window.set_transient_for(self.window)
        self.control_window.connect("close-request", lambda _w: setattr(self, "control_window", None) or False)
        toolbar = Adw.ToolbarView(); toolbar.add_top_bar(Adw.HeaderBar())
        content = self.window_content()
        title = Gtk.Label(label="Target execution", xalign=0); title.add_css_class("hero-title")
        self.core_status = Gtk.Label(label="Use Status to query the core", xalign=0); self.core_status.add_css_class("dim-label")
        actions = self.action_flow()
        commands = (("System reset", "-rst"), ("Hard reset", "-hardRst"), ("Halt", "-halt"),
                    ("Run", "-run"), ("Single step", "-step"), ("Status", "-score"))
        debug_only = self.selected_device().get("kind") == "stlink"
        for label, command in commands:
            button = Gtk.Button(label=label)
            button.set_sensitive(debug_only or command in {"-rst", "-hardRst"})
            button.connect("clicked", self.on_target_command, command, label)
            actions.append(button)
        jump = Gtk.Box(spacing=8)
        self.jump_address = Gtk.Entry(text="0x08000000", hexpand=True)
        jump_button = Gtk.Button(label="Run from address"); jump_button.add_css_class("suggested-action")
        jump_button.set_sensitive(debug_only); jump_button.connect("clicked", self.on_jump_to_address)
        jump.append(self.jump_address); jump.append(jump_button)
        content.append(title); content.append(self.core_status); content.append(actions); content.append(jump)
        toolbar.set_content(content); self.control_window.set_content(toolbar); self.control_window.present()

    def on_target_command(self, _button, command, label):
        self.run_cli(["-c", self.connection_string(), command], f"{label}…", self.finish_target_command)

    def finish_target_command(self, code, output):
        if getattr(self, "control_window", None):
            clean = self.clean_cli_output(output)
            status = next((line.strip() for line in reversed(clean.splitlines()) if line.strip() and "-----" not in line), "")
            self.core_status.set_label(status or ("Completed" if code == 0 else "Command failed"))

    def on_jump_to_address(self, _button):
        try:
            address = int(self.jump_address.get_text().strip(), 0)
        except ValueError:
            self.core_status.set_label("Invalid run address")
            return
        self.run_cli(["-c", self.connection_string(), "-g", f"0x{address:08X}"], "Starting target…", self.finish_target_command)

    def on_recovery(self, _button):
        dialog = Adw.MessageDialog.new(self.window, "Target recovery", "Recovery operations erase data or change protection. Choose only the operation you intend.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("unlock_ob", "Unlock bad option bytes")
        dialog.add_response("read_unprotect", "Remove read protection")
        dialog.set_response_appearance("unlock_ob", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("read_unprotect", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", self.confirm_recovery)
        dialog.present()

    def confirm_recovery(self, _dialog, response):
        if response == "cancel":
            return
        if response == "read_unprotect":
            title = "Remove read protection and erase the chip?"
            body = "RDP regression performs a mass erase. Every programmable flash byte will be lost."
            command = ["-c", self.connection_string(), "-rdu", "-y"]
        else:
            title = "Unlock a chip with bad option bytes?"
            body = "The CLI will repair/unlock option-byte state and may erase or reset the target."
            command = ["-c", self.connection_string(), "-ob", "unlockchip", "-y"]
        confirm = Adw.MessageDialog.new(self.window, title, body)
        confirm.add_response("cancel", "Cancel"); confirm.add_response("proceed", "Proceed with recovery")
        confirm.set_response_appearance("proceed", Adw.ResponseAppearance.DESTRUCTIVE)
        confirm.set_default_response("cancel"); confirm.set_close_response("cancel")
        confirm.connect("response", lambda _d, choice: self.run_cli(command, "Recovering target…") if choice == "proceed" else None)
        confirm.present()

    def on_inspect_memory(self, _button):
        if not self.connected or not self.selected_device():
            return
        if getattr(self, "memory_window", None):
            self.memory_window.present()
            return
        self.memory_window = Adw.ApplicationWindow(application=self, title="Memory — Flash Deck", default_width=900, default_height=650)
        self.memory_window.set_transient_for(self.window)
        self.memory_window.connect("close-request", self.on_memory_closed)
        toolbar = Adw.ToolbarView(); toolbar.add_top_bar(Adw.HeaderBar())
        content = self.window_content()
        title = Gtk.Label(label="Memory inspector", xalign=0); title.add_css_class("hero-title")
        self.memory_regions = self.load_memory_map()
        map_row = Gtk.Box(spacing=10)
        region_labels = [f"{region['name']}  ·  0x{region['address']:08X}  ·  {self.format_bytes(region['size'])}" for region in self.memory_regions]
        self.memory_region = Gtk.DropDown.new_from_strings(region_labels or ["Memory map unavailable"])
        self.memory_region.set_hexpand(True); self.memory_region.set_sensitive(bool(region_labels))
        self.memory_region.connect("notify::selected", self.on_memory_region_selected)
        self.memory_sector = Gtk.DropDown.new_from_strings(["No erasable sectors"])
        self.memory_sector.set_size_request(210, -1)
        self.memory_sector.connect("notify::selected", self.on_memory_sector_selected)
        map_row.append(self.memory_region); map_row.append(self.memory_sector)
        controls = Gtk.Box(spacing=10)
        self.memory_address = Gtk.Entry(text=self.address.get_text(), hexpand=True)
        self.memory_address.set_placeholder_text("Address")
        self.memory_size = Gtk.DropDown.new_from_strings(["256 bytes", "512 bytes", "1 KiB", "2 KiB", "4 KiB"])
        self.memory_size.set_selected(2)
        previous = Gtk.Button(icon_name="go-previous-symbolic"); previous.set_tooltip_text("Previous page"); previous.connect("clicked", lambda _b: self.move_memory_page(-1))
        following = Gtk.Button(icon_name="go-next-symbolic"); following.set_tooltip_text("Next page"); following.connect("clicked", lambda _b: self.move_memory_page(1))
        self.memory_read_button = Gtk.Button(label="Read page"); self.memory_read_button.add_css_class("suggested-action"); self.memory_read_button.connect("clicked", self.read_memory_page)
        controls.append(previous); controls.append(self.memory_address); controls.append(self.memory_size); controls.append(following); controls.append(self.memory_read_button)
        operations = self.action_flow(columns=3)
        export = Gtk.Button(label="Export…"); export.connect("clicked", self.on_export_memory)
        import_button = Gtk.Button(label="Import…"); import_button.connect("clicked", self.on_import_memory)
        checksum = Gtk.Button(label="Checksum"); checksum.connect("clicked", self.on_memory_checksum)
        blank = Gtk.Button(label="Blank check"); blank.connect("clicked", self.on_blank_check)
        fill = Gtk.Button(label="Fill…"); fill.connect("clicked", self.on_fill_memory)
        self.memory_write_button = Gtk.Button(label="Write edits…")
        self.memory_write_button.add_css_class("destructive-action"); self.memory_write_button.set_sensitive(False)
        self.memory_write_button.connect("clicked", self.on_write_memory_edits)
        self.sector_erase_button = Gtk.Button(label="Erase sector…")
        self.sector_erase_button.add_css_class("destructive-action"); self.sector_erase_button.connect("clicked", self.on_erase_sector)
        operations.append(export); operations.append(import_button); operations.append(checksum); operations.append(blank)
        operations.append(fill); operations.append(self.memory_write_button); operations.append(self.sector_erase_button)
        direct = Gtk.Box(spacing=8)
        self.direct_width = Gtk.DropDown.new_from_strings(["8-bit", "16-bit", "32-bit"])
        self.direct_values = Gtk.Entry(placeholder_text="Values, e.g. 0x12 0x34", hexpand=True)
        direct_write = Gtk.Button(label="Direct write…"); direct_write.connect("clicked", self.on_direct_memory_write)
        direct.append(self.direct_width); direct.append(self.direct_values); direct.append(direct_write)
        self.memory_info = Gtk.Label(label="Choose an address and page size", xalign=0); self.memory_info.add_css_class("dim-label")
        self.memory_buffer = Gtk.TextBuffer()
        self.memory_buffer.connect("changed", self.on_memory_buffer_changed)
        self.memory_rendering = False
        self.memory_loaded_data = b""
        self.memory_loaded_address = 0
        view = Gtk.TextView(buffer=self.memory_buffer, editable=True, cursor_visible=True, monospace=True, vexpand=True)
        view.set_left_margin(16); view.set_right_margin(16); view.set_top_margin(14); view.set_bottom_margin(14)
        scroll = Gtk.ScrolledWindow(vexpand=True); scroll.add_css_class("hex-view"); scroll.set_child(view)
        content.append(title); content.append(map_row); content.append(controls); content.append(operations)
        content.append(direct); content.append(self.memory_info); content.append(scroll)
        toolbar.set_content(content); self.memory_window.set_content(toolbar); self.memory_window.present()
        self.on_memory_region_selected()
        self.read_memory_page()

    def on_option_bytes(self, _button):
        if not self.connected or not self.selected_device():
            return
        if getattr(self, "option_window", None):
            self.option_window.present()
            return
        self.option_window = Adw.ApplicationWindow(application=self, title="Option bytes — Flash Deck", default_width=760, default_height=620)
        self.option_window.set_transient_for(self.window)
        self.option_window.connect("close-request", self.on_option_window_closed)
        toolbar = Adw.ToolbarView(); toolbar.add_top_bar(Adw.HeaderBar())
        content = self.window_content()
        intro = Gtk.Label(label="Device option bytes", xalign=0)
        intro.add_css_class("hero-title")
        self.option_groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.option_loading = Gtk.Label(label="Reading option bytes…", xalign=0)
        self.option_loading.add_css_class("dim-label"); self.option_groups_box.append(self.option_loading)
        scroll = Gtk.ScrolledWindow(vexpand=True); scroll.set_child(self.option_groups_box)
        action = self.action_flow()
        self.option_save_button = Gtk.Button(label="Save changes")
        self.option_save_button.add_css_class("destructive-action"); self.option_save_button.set_sensitive(False)
        self.option_save_button.connect("clicked", self.on_save_option_bytes); action.append(self.option_save_button)
        warning = Gtk.Label(label="Option bytes can disable debug access, enable read protection, or make a target unrecoverable. Review the device reference manual before applying.", xalign=0, wrap=True)
        warning.add_css_class("warning-text")
        content.append(intro); content.append(scroll); content.append(warning); content.append(action)
        toolbar.set_content(content); self.option_window.set_content(toolbar); self.option_window.present()
        self.read_option_bytes()

    def on_option_window_closed(self, _window):
        self.option_window = None
        return False

    def read_option_bytes(self):
        connect = self.connection_string()
        if connect:
            self.clear_box(self.option_groups_box)
            loading = Gtk.Label(label="Reading option bytes…", xalign=0); loading.add_css_class("dim-label"); self.option_groups_box.append(loading)
            self.option_save_button.set_sensitive(False)
            self.run_cli(["-c", connect, "-ob", "displ"], "Reading option bytes…", self.finish_option_read)

    def finish_option_read(self, code, output):
        if not getattr(self, "option_window", None):
            return
        groups = self.parse_option_bytes(self.clean_cli_output(output)) if code == 0 else []
        self.clear_box(self.option_groups_box)
        self.option_controls = {}
        if not groups:
            label = Gtk.Label(label="Unable to parse option-byte data. See Activity for details.", xalign=0, wrap=True)
            label.add_css_class("dim-label"); self.option_groups_box.append(label)
            return
        for group_name, options in groups:
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            title = Gtk.Label(label=group_name, xalign=0); title.add_css_class("section-title")
            rows = Gtk.ListBox(); rows.add_css_class("boxed-list"); rows.set_selection_mode(Gtk.SelectionMode.NONE)
            for name, raw_value, description in options:
                row = Adw.ActionRow(title=name, subtitle=description or None)
                if self.is_binary_option(name) and raw_value.lower() in {"0x0", "0x1"}:
                    control = Gtk.Switch(active=int(raw_value, 0) == 1, valign=Gtk.Align.CENTER)
                    control.connect("notify::active", self.on_option_control_changed)
                    editable = True
                elif re.fullmatch(r"0x[0-9A-Fa-f]+", raw_value):
                    control = Gtk.Entry(text=raw_value, width_chars=12, valign=Gtk.Align.CENTER)
                    control.connect("changed", self.on_option_control_changed)
                    editable = True
                else:
                    control = Gtk.Label(label=raw_value, valign=Gtk.Align.CENTER, selectable=True)
                    control.add_css_class("option-symbolic-value")
                    editable = False
                row.add_suffix(control); rows.append(row)
                self.option_controls[name] = {"original": raw_value.lower(), "control": control, "editable": editable}
            group.append(title); group.append(rows); self.option_groups_box.append(group)

    @staticmethod
    def parse_option_bytes(output):
        groups = []
        current = None
        in_options = False
        for line in output.splitlines():
            if "OPTION BYTES BANK:" in line:
                in_options = True
                continue
            if not in_options:
                continue
            heading = re.match(r"^\s{2,}([^:]+):\s*$", line)
            if heading:
                current = (heading.group(1).strip(), [])
                groups.append(current)
                continue
            option = re.match(r"^\s+([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if option and current:
                value_text = option.group(2).strip()
                described = re.match(r"^(.*?)\s+\((.*)\)\s*$", value_text)
                raw_value = (described.group(1) if described else value_text).strip()
                description = (described.group(2) if described else "").strip()
                if raw_value:
                    current[1].append((option.group(1), raw_value, description))
        return [(name, values) for name, values in groups if values]

    @staticmethod
    def is_binary_option(name):
        return bool(re.match(r"^(?:I?WDG\d*_SW|NRST_|IO_HSLV|FZ_|DMEP$|WRPS?\d*$|CPU_FREQ_BOOST$)", name, re.IGNORECASE))

    @staticmethod
    def clear_box(box):
        child = box.get_first_child()
        while child:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    def option_control_value(self, item):
        control = item["control"]
        return ("0x1" if control.get_active() else "0x0") if isinstance(control, Gtk.Switch) else control.get_text().strip().lower()

    def on_option_control_changed(self, *_args):
        editable = [item for item in self.option_controls.values() if item["editable"]]
        changed = any(self.option_control_value(item) != item["original"] for item in editable)
        valid = all(re.fullmatch(r"0x[0-9a-f]+", self.option_control_value(item)) for item in editable)
        self.option_save_button.set_sensitive(changed and valid)

    def on_save_option_bytes(self, _button):
        changes = {name: self.option_control_value(item) for name, item in self.option_controls.items()
                   if item["editable"] and self.option_control_value(item) != item["original"]}
        if not changes:
            return
        summary = "\n".join(f"{name}: {self.option_controls[name]['original']} → {value}" for name, value in changes.items())
        dialog = Adw.MessageDialog.new(self.option_window, f"Save {len(changes)} option-byte change{'s' if len(changes) != 1 else ''}?", f"{summary}\n\nThis may reset, lock, or permanently restrict the target.")
        dialog.add_response("cancel", "Cancel"); dialog.add_response("apply", "Save changes")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", self.finish_option_confirmation, changes)
        dialog.present()

    def finish_option_confirmation(self, _dialog, response, changes):
        if response != "apply":
            return
        connect = self.connection_string()
        if connect:
            assignments = [f"{name}={value}" for name, value in changes.items()]
            self.run_cli(["-c", connect, "-ob", *assignments], "Saving option bytes…", self.finish_option_write)

    def finish_option_write(self, code, _output):
        if code == 0 and getattr(self, "option_window", None):
            self.read_option_bytes()

    def load_memory_map(self):
        external = [region for path in self.external_loaders if (region := self.inspect_external_loader(path))]
        device_id = self.target_info.get("device_id").get_label().strip() if self.target_info.get("device_id") else ""
        if not re.fullmatch(r"0x[0-9A-Fa-f]+", device_id):
            return [{"name": "Internal flash", "address": 0x08000000, "size": 1024 * 1024, "sectors": []}, *external]
        database = INSTALL_ROOT / "Data_Base" / f"STM32_Prog_DB_{device_id}.xml"
        try:
            root = ET.parse(database).getroot()
        except (OSError, ET.ParseError):
            return [{"name": "Internal flash", "address": 0x08000000, "size": 1024 * 1024, "sectors": []}, *external]
        flash_size = self.parse_size_label(self.target_info["flash"].get_label())
        regions = []
        for peripheral in root.findall(".//Peripherals/Peripheral"):
            if (peripheral.findtext("Type") or "").strip() != "Storage":
                continue
            name = (peripheral.findtext("Name") or "Memory").strip()
            configurations = []
            for configuration in peripheral.findall("Configuration"):
                parameters = configuration.find("Parameters")
                if parameters is None:
                    continue
                try:
                    address = int(parameters.get("address"), 0)
                    size = int(parameters.get("size"), 0)
                except (TypeError, ValueError):
                    continue
                configurations.append((configuration, address, size, parameters.get("name") or name))
            if not configurations:
                continue
            if "Flash" in name and flash_size:
                configuration, address, size, map_name = min(configurations, key=lambda item: abs(item[2] - flash_size))
            else:
                configuration, address, size, map_name = max(configurations, key=lambda item: item[2])
            sectors = []
            if "Flash" in name:
                sector_index = 0
                for field in configuration.findall(".//Field/Parameters"):
                    try:
                        field_address = int(field.get("address"), 0)
                        field_size = int(field.get("size"), 0)
                        occurrence = int(field.get("occurrence", "1"), 0)
                    except (TypeError, ValueError):
                        continue
                    for offset in range(occurrence):
                        sectors.append({"index": sector_index, "address": field_address + offset * field_size, "size": field_size})
                        sector_index += 1
            regions.append({"name": map_name, "address": address, "size": size, "sectors": sectors})
        return sorted([*regions, *external], key=lambda region: region["address"])

    @staticmethod
    def parse_size_label(label):
        match = re.search(r"([0-9.]+)\s*(K|M)?(?:i?Bytes?|B)", label, re.IGNORECASE)
        if not match:
            return None
        scale = {"K": 1024, "M": 1024 * 1024}.get((match.group(2) or "").upper(), 1)
        return int(float(match.group(1)) * scale)

    @staticmethod
    def format_bytes(size):
        if size >= 1024 * 1024 and size % (1024 * 1024) == 0:
            return f"{size // (1024 * 1024)} MiB"
        if size >= 1024 and size % 1024 == 0:
            return f"{size // 1024} KiB"
        return f"{size} bytes"

    def on_memory_region_selected(self, *_args):
        if not self.memory_regions:
            return
        index = min(self.memory_region.get_selected(), len(self.memory_regions) - 1)
        region = self.memory_regions[index]
        self.memory_address.set_text(f"0x{region['address']:08X}")
        sectors = region["sectors"]
        labels = [f"Sector {sector['index']}  ·  0x{sector['address']:08X}  ·  {self.format_bytes(sector['size'])}" for sector in sectors]
        self.memory_sector.set_model(Gtk.StringList.new(labels or ["No erasable sectors"]))
        self.memory_sector.set_selected(0)
        self.memory_sector.set_sensitive(bool(sectors))
        self.sector_erase_button.set_sensitive(bool(sectors))

    def on_memory_sector_selected(self, *_args):
        if not self.memory_regions:
            return
        region_index = min(self.memory_region.get_selected(), len(self.memory_regions) - 1)
        sectors = self.memory_regions[region_index]["sectors"]
        selected = self.memory_sector.get_selected()
        if sectors and selected < len(sectors):
            self.memory_address.set_text(f"0x{sectors[selected]['address']:08X}")

    def current_memory_range(self):
        try:
            return int(self.memory_address.get_text().strip(), 0), self.memory_page_bytes()
        except ValueError:
            self.memory_info.set_label("Invalid address")
            return None

    def on_export_memory(self, _button):
        if not self.current_memory_range():
            return
        dialog = Gtk.FileDialog(title="Export memory", modal=True, initial_name="memory.bin")
        dialog.save(self.memory_window, None, self.finish_export_memory)

    def finish_export_memory(self, dialog, result):
        try:
            destination = dialog.save_finish(result)
        except GLib.Error as error:
            if not any(error.matches(Gtk.DialogError.quark(), state) for state in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)):
                self.toast(f"Could not export memory: {error.message}")
            return
        memory_range = self.current_memory_range()
        if memory_range:
            address, size = memory_range
            self.run_cli([*self.connection_arguments(True), "-u", f"0x{address:08X}", str(size), destination.get_path()], "Exporting memory…")

    def on_memory_checksum(self, _button):
        memory_range = self.current_memory_range()
        if memory_range:
            address, size = memory_range
            self.run_cli([*self.connection_arguments(True), "-checksum", f"0x{address:08X}", str(size)], "Calculating checksum…")

    def on_blank_check(self, _button):
        self.run_cli(["-c", self.connection_string(), "-blankcheck"], "Checking internal flash…")

    def on_memory_buffer_changed(self, _buffer):
        if not self.memory_rendering and hasattr(self, "memory_write_button"):
            self.memory_write_button.set_sensitive(bool(self.memory_loaded_data))

    def edited_memory_bytes(self):
        text = self.memory_buffer.get_text(self.memory_buffer.get_start_iter(), self.memory_buffer.get_end_iter(), True)
        data = bytearray()
        for line in text.splitlines()[2:]:
            if not re.match(r"^[0-9A-Fa-f]{8}\s{2}", line):
                continue
            for token in line[10:57].split():
                if re.fullmatch(r"[0-9A-Fa-f]{2}", token):
                    data.append(int(token, 16))
        return bytes(data)

    def on_write_memory_edits(self, _button):
        edited = self.edited_memory_bytes()
        if len(edited) != len(self.memory_loaded_data):
            self.memory_info.set_label("Keep the same number of hex bytes when editing")
            return
        changes = [(index, value) for index, value in enumerate(edited) if value != self.memory_loaded_data[index]]
        if not changes:
            self.memory_info.set_label("No byte changes to write")
            return
        dialog = Adw.MessageDialog.new(self.memory_window, f"Write {len(changes)} changed byte{'s' if len(changes) != 1 else ''}?", f"Modify target memory beginning at 0x{self.memory_loaded_address:08X}. Flash writes may require erased bits.")
        dialog.add_response("cancel", "Cancel"); dialog.add_response("write", "Write changes")
        dialog.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", self.finish_write_memory_edits, edited)
        dialog.present()

    def finish_write_memory_edits(self, _dialog, response, edited):
        if response != "write":
            return
        command = self.connection_arguments(True)
        index = 0
        while index < len(edited):
            if edited[index] == self.memory_loaded_data[index]:
                index += 1
                continue
            start = index; values = []
            while index < len(edited) and edited[index] != self.memory_loaded_data[index]:
                values.append(f"0x{edited[index]:02X}"); index += 1
            command.extend(["-w8", f"0x{self.memory_loaded_address + start:08X}", *values])
        self.run_cli(command, "Writing edited memory…", lambda code, _out: self.read_memory_page() if code == 0 else None)

    def on_direct_memory_write(self, _button):
        values = self.direct_values.get_text().split()
        width = [8, 16, 32][self.direct_width.get_selected()]
        try:
            address = int(self.memory_address.get_text().strip(), 0)
            parsed = [int(value, 0) for value in values]
            if not parsed or any(value < 0 or value >= 1 << width for value in parsed):
                raise ValueError
        except ValueError:
            self.memory_info.set_label(f"Enter a valid address and one or more {width}-bit values")
            return
        rendered = [f"0x{value:0{width // 4}X}" for value in parsed]
        dialog = Adw.MessageDialog.new(self.memory_window, f"Write {len(parsed)} × {width}-bit value{'s' if len(parsed) != 1 else ''}?", f"Write at 0x{address:08X}: {' '.join(rendered)}")
        dialog.add_response("cancel", "Cancel"); dialog.add_response("write", "Write values")
        dialog.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: self.run_cli([*self.connection_arguments(True), f"-w{width}", f"0x{address:08X}", *rendered], "Writing memory…", lambda code, _out: self.read_memory_page() if code == 0 else None) if response == "write" else None)
        dialog.present()

    def on_import_memory(self, _button):
        dialog = Gtk.FileDialog(title="Import file into memory", modal=True)
        dialog.open(self.memory_window, None, self.finish_import_memory)

    def finish_import_memory(self, dialog, result):
        try:
            source = dialog.open_finish(result)
        except GLib.Error as error:
            if not any(error.matches(Gtk.DialogError.quark(), state) for state in (Gtk.DialogError.CANCELLED, Gtk.DialogError.DISMISSED)):
                self.toast(f"Could not open import file: {error.message}")
            return
        try:
            address = int(self.memory_address.get_text().strip(), 0)
        except ValueError:
            self.memory_info.set_label("Invalid import address")
            return
        confirm = Adw.MessageDialog.new(self.memory_window, f"Import {source.get_basename()}?", f"Program this file at 0x{address:08X} and verify it.")
        confirm.add_response("cancel", "Cancel"); confirm.add_response("write", "Import and verify")
        confirm.set_response_appearance("write", Adw.ResponseAppearance.DESTRUCTIVE)
        confirm.set_default_response("cancel"); confirm.set_close_response("cancel")
        confirm.connect("response", lambda _d, response: self.run_cli([*self.connection_arguments(True), "-w", source.get_path(), f"0x{address:08X}", "-v"], "Importing memory…") if response == "write" else None)
        confirm.present()

    def on_fill_memory(self, _button):
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        size = Gtk.Entry(text=str(self.memory_page_bytes())); pattern = Gtk.Entry(text="0xFF")
        width = Gtk.DropDown.new_from_strings(["8-bit", "16-bit", "32-bit"])
        for row, (label, widget) in enumerate((("Size in bytes", size), ("Pattern", pattern), ("Width", width))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1); grid.attach(widget, 1, row, 1, 1)
        dialog = Adw.MessageDialog.new(self.memory_window, "Fill target memory?", "Repeatedly write a pattern into the selected memory range.")
        dialog.set_extra_child(grid)
        dialog.add_response("cancel", "Cancel"); dialog.add_response("fill", "Fill memory")
        dialog.set_response_appearance("fill", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", self.finish_fill_memory, size, pattern, width)
        dialog.present()

    def finish_fill_memory(self, _dialog, response, size, pattern, width):
        if response != "fill":
            return
        try:
            address = int(self.memory_address.get_text().strip(), 0)
            count = int(size.get_text().strip(), 0)
            value = int(pattern.get_text().strip(), 0)
            bits = [8, 16, 32][width.get_selected()]
            if count <= 0 or value < 0 or value >= 1 << bits:
                raise ValueError
        except ValueError:
            self.memory_info.set_label("Invalid fill size or pattern")
            return
        self.run_cli([*self.connection_arguments(True), "-fillmemory", f"0x{address:08X}", f"size={count}", f"pattern=0x{value:X}", f"dataWidth={bits}"], "Filling memory…", lambda code, _out: self.read_memory_page() if code == 0 else None)

    def on_erase_sector(self, _button):
        region = self.memory_regions[min(self.memory_region.get_selected(), len(self.memory_regions) - 1)]
        selected = self.memory_sector.get_selected()
        if selected >= len(region["sectors"]):
            return
        sector = region["sectors"][selected]
        dialog = Adw.MessageDialog.new(self.memory_window, f"Erase sector {sector['index']}?", f"Erase {self.format_bytes(sector['size'])} at 0x{sector['address']:08X}. This cannot be undone.")
        dialog.add_response("cancel", "Cancel"); dialog.add_response("erase", "Erase sector")
        dialog.set_response_appearance("erase", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel"); dialog.set_close_response("cancel")
        dialog.connect("response", self.finish_erase_sector, sector["index"])
        dialog.present()

    def finish_erase_sector(self, _dialog, response, sector_index):
        if response == "erase":
            self.run_cli([*self.connection_arguments(True), "-e", str(sector_index)], f"Erasing sector {sector_index}…")

    def on_memory_closed(self, _window):
        self.memory_window = None
        return False

    def memory_page_bytes(self):
        return [256, 512, 1024, 2048, 4096][self.memory_size.get_selected()]

    def move_memory_page(self, direction):
        try:
            address = int(self.memory_address.get_text().strip(), 0) + direction * self.memory_page_bytes()
        except ValueError:
            address = 0x08000000
        self.memory_address.set_text(f"0x{max(0, address):08X}")
        self.read_memory_page()

    def read_memory_page(self, _button=None):
        try:
            address = int(self.memory_address.get_text().strip(), 0)
        except ValueError:
            self.memory_info.set_label("Invalid address")
            return
        connect = self.connection_string()
        if not connect:
            return
        size = self.memory_page_bytes()
        descriptor, path = tempfile.mkstemp(prefix="flash-deck-memory-", suffix=".bin")
        os.close(descriptor); os.unlink(path)
        self.memory_read_button.set_sensitive(False); self.memory_info.set_label("Reading memory…")
        command = [self.cli, *self.connection_arguments(True), "-u", f"0x{address:08X}", str(size), path]
        threading.Thread(target=self.memory_read_process, args=(command, path, address, size), daemon=True).start()

    def memory_read_process(self, command, path, address, size):
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        data = b""
        if process.returncode == 0 and os.path.exists(path):
            data = Path(path).read_bytes()
        if os.path.exists(path): os.unlink(path)
        GLib.idle_add(self.finish_memory_read, process.returncode, process.stdout, data, address, size)

    def finish_memory_read(self, code, output, data, address, size):
        self.memory_read_button.set_sensitive(True)
        if code != 0 or not data:
            self.memory_info.set_label("Read failed — see Activity for details")
            self.append_log(output)
            return
        lines = ["ADDRESS     00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F  ASCII", "─" * 78]
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            hexes = " ".join(f"{byte:02X}" for byte in chunk).ljust(47)
            ascii_text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
            lines.append(f"{address + offset:08X}  {hexes}  {ascii_text}")
        self.memory_loaded_data = data
        self.memory_loaded_address = address
        self.memory_rendering = True
        self.memory_buffer.set_text("\n".join(lines))
        self.memory_rendering = False
        self.memory_write_button.set_sensitive(False)
        page = (address - 0x08000000) // size if address >= 0x08000000 else 0
        self.memory_info.set_label(f"Page {page}  ·  {len(data)} bytes  ·  0x{address:08X}–0x{address + len(data) - 1:08X}")

    def run_cli(self, arguments, label, callback=None):
        if self.running: return
        if not self.cli:
            self.append_log("\nCLI not found. Set STM32_PROGRAMMER_CLI to its executable path.\n")
            self.toast("STM32_Programmer_CLI was not found")
            return
        self.running = True; self.flash_button.set_sensitive(False); self.verify_button.set_sensitive(False); self.production_button.set_sensitive(False); self.erase_button.set_sensitive(False)
        self.set_status(label, "working")
        command = [self.cli, *arguments]
        self.append_log("\n$ " + shlex.join(command) + "\n")
        threading.Thread(target=self.run_process, args=(command, callback), daemon=True).start()

    def run_process(self, command, callback):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output = ""
        for line in process.stdout:
            output += line
            clean = self.clean_cli_output(line)
            if clean.strip():
                GLib.idle_add(self.append_log, clean)
        code = process.wait()
        GLib.idle_add(self.finish_process, code, output, callback)

    def finish_process(self, code, output, callback):
        self.running = False; self.update_job_state(); self.erase_button.set_sensitive(self.connected)
        self.set_status("Done" if code == 0 else f"Failed (exit {code})",
                        "success" if code == 0 else "warning")
        self.append_log(f"\n{'✓ Completed' if code == 0 else '✕ Failed'}\n")
        if callback: callback(code, output)

    def append_log(self, text):
        text = self.clean_cli_output(text)
        if not text:
            return
        self.log_buffer.insert(self.log_buffer.get_end_iter(), text)
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)

    @staticmethod
    def clean_cli_output(text):
        text = text.replace("␛", "\x1b")
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    def toast(self, text, state="warning"):
        self.set_status(text, state)

    def set_status(self, text, state):
        self.status_pill.set_label(text)
        for css_class in ("neutral", "success", "warning", "working"):
            self.status_pill.remove_css_class(css_class)
        if state:
            self.status_pill.add_css_class(state)


CSS = """
window { background: @window_bg_color; color: @window_fg_color; }
.hero-title { font-size: 30px; font-weight: 800; letter-spacing: -0.6px; color: @window_fg_color; }
.dim-label { color: @window_fg_color; opacity: .62; font-size: 15px; }
.card { background: @card_bg_color; color: @window_fg_color; border: 1px solid @borders; border-radius: 16px; padding: 18px; box-shadow: 0 2px 8px alpha(black, .08); }
.card-title { color: @window_fg_color; opacity: .72; font-size: 12px; font-weight: 800; letter-spacing: .8px; }
.section-title { color: @window_fg_color; opacity: .72; font-size: 12px; font-weight: 800; margin-top: 6px; }
.subcard { background: @view_bg_color; border: 1px solid @borders; border-radius: 12px; padding: 14px; }
.card dropdown { min-height: 42px; }
.discovery-spinner { min-width: 30px; min-height: 30px; color: #2584d8; }
image.target-connected { color: #24e66f; -gtk-icon-palette: success #24e66f; }
image.target-disconnected { color: @window_fg_color; opacity: .45; }
.production-state { background: @card_bg_color; border: 1px solid @borders; border-radius: 16px; padding: 28px; }
.production-title { font-size: 22px; font-weight: 750; }
.production-pass { color: #24c76a; }
.production-fail { color: #e54b4b; }
.counter-value { font-size: 32px; font-weight: 800; font-feature-settings: "tnum"; }
.disconnect-button { min-width: 30px; min-height: 30px; padding: 4px; color: @window_fg_color; }
.disconnect-button image { color: @window_fg_color; -gtk-icon-palette: success @window_fg_color; }
.probe-update-button { min-width: 30px; min-height: 30px; padding: 4px; color: @accent_color; }
.probe-update-button image { color: @accent_color; -gtk-icon-palette: success @accent_color; }
.target-value { font-weight: 700; }
.target-name, .file-chosen { font-weight: 700; color: @accent_color; }
.file-empty { color: @window_fg_color; font-weight: 700; }
.drop-zone { border: 2px dashed @borders; border-radius: 14px; padding: 24px; background: @view_bg_color; }
.drop-zone:hover, .drop-zone.drop-active { border-color: @accent_color; background: alpha(@accent_color, .10); }
.status-pill { border-radius: 999px; padding: 5px 11px; font-weight: 700; font-size: 12px; }
.neutral { color: alpha(@window_fg_color, .72); background: alpha(@window_fg_color, .10); }
.success { color: @success_color; background: alpha(@success_color, .15); }
.warning { color: @warning_color; background: alpha(@warning_color, .15); }
.working { color: @accent_color; background: alpha(@accent_color, .15); }
.warning-text { color: @warning_color; background: alpha(@warning_color, .10); border-radius: 8px; padding: 8px; }
.option-symbolic-value { color: alpha(@window_fg_color, .68); font-weight: 700; }
.action-flow button { min-height: 38px; }
scrolledwindow.activity-log { padding: 6px; border: 1px solid @borders; border-radius: 12px; background: @view_bg_color; }
textview { background: @view_bg_color; color: @view_fg_color; font-size: 12px; }
.hex-view { border: 1px solid @borders; border-radius: 12px; background: @view_bg_color; }
.flash-button { min-width: 160px; font-weight: 800; }
"""


if __name__ == "__main__":
    FlashDeck().run(None)
