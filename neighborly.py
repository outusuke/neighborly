#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Neighborly
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
"""Neighborly - a friendly GTK4 / libadwaita front-end for Samba.

Features
  * Start / stop / restart the Samba service, enable it at boot
  * Create, edit and remove shared folders (edits /etc/samba/smb.conf,
    keeps your comments, validates with testparm, keeps a .bak copy)
  * Add Samba users, change passwords, remove users
  * See who is connected right now (smbstatus)
  * Open other computers' shares in your file manager

Anything that needs root goes through pkexec (a normal polkit password prompt).
"""
import copy
import glob
import grp
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.neighborly.Neighborly"
CONF = "/etc/samba/smb.conf"
SPECIAL = {"global", "printers", "print$"}
YES = {"yes", "true", "1", "on"}

# Runs as root: backup old config, install the new one, tell smbd to reload.
INSTALL_SCRIPT = (
    '[ -f "$2" ] && cp -p "$2" "$2.bak"; '
    'install -m 644 "$1" "$2" && '
    '(smbcontrol all reload-config >/dev/null 2>&1 || true)'
)


# --------------------------------------------------------------------------
# smb.conf model (preserves comments and unknown settings)
# --------------------------------------------------------------------------
def _norm(key):
    return " ".join(key.split()).lower()


def _kv(line):
    s = line.strip()
    if not s or s[0] in "#;" or "=" not in s:
        return None, None
    k, v = s.split("=", 1)
    return _norm(k), v.strip()


def is_yes(value):
    return value is not None and value.strip().lower() in YES


class Section:
    def __init__(self, name):
        self.name = name
        self.lines = []

    def get(self, key, default=None):
        key = _norm(key)
        for line in self.lines:
            k, v = _kv(line)
            if k == key:
                return v
        return default

    def set(self, key, value):
        """Set a key (value=None removes it)."""
        key_n = _norm(key)
        last = -1
        for i, line in enumerate(self.lines):
            k, _ = _kv(line)
            if k is None:
                continue
            last = i
            if k == key_n:
                if value is None:
                    del self.lines[i]
                else:
                    self.lines[i] = f"\t{key} = {value}"
                return
        if value is not None:
            self.lines.insert(last + 1, f"\t{key} = {value}")

    def writable(self):
        ro = self.get("read only")
        if ro is not None:
            return not is_yes(ro)
        for k in ("writable", "writeable", "write ok"):
            v = self.get(k)
            if v is not None:
                return is_yes(v)
        return False

    def guest(self):
        return is_yes(self.get("guest ok")) or is_yes(self.get("public"))

    def browseable(self):
        v = self.get("browseable", self.get("browsable"))
        return True if v is None else is_yes(v)


class SmbConf:
    def __init__(self):
        self.preamble = []
        self.sections = []

    @classmethod
    def parse(cls, text):
        conf, cur = cls(), None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[") and "]" in s:
                cur = Section(s[1:s.index("]")].strip())
                conf.sections.append(cur)
            elif cur is None:
                conf.preamble.append(line)
            else:
                cur.lines.append(line)
        return conf

    def find(self, name):
        for s in self.sections:
            if s.name.lower() == name.lower():
                return s
        return None

    def ensure(self, name):
        s = self.find(name)
        if s is None:
            s = Section(name)
            self.add(s)
        return s

    def add(self, section):
        if self.sections and self.sections[-1].lines and self.sections[-1].lines[-1].strip():
            self.sections[-1].lines.append("")
        self.sections.append(section)
        if section.lines and section.lines[-1].strip():
            section.lines.append("")

    def remove(self, name):
        self.sections = [s for s in self.sections if s.name.lower() != name.lower()]

    def shares(self):
        return [s for s in self.sections if s.name.lower() not in SPECIAL]

    def dump(self):
        out = list(self.preamble)
        for s in self.sections:
            out.append(f"[{s.name}]")
            out.extend(s.lines)
        return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def run_async(cmd, callback, stdin_text=None):
    """Run a command in a thread; callback(rc, stdout, stderr) runs on the UI thread."""
    def work():
        try:
            r = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=180)
            res = (r.returncode, r.stdout, r.stderr)
        except FileNotFoundError:
            res = (127, "", f"'{cmd[0]}' was not found. Is it installed?")
        except Exception as e:  # noqa: BLE001
            res = (1, "", str(e))
        GLib.idle_add(lambda: callback(*res) and False)

    threading.Thread(target=work, daemon=True).start()


def detect_service():
    for name in ("smbd", "smb"):
        try:
            r = subprocess.run(["systemctl", "cat", f"{name}.service"], capture_output=True)
            if r.returncode == 0:
                return name
        except FileNotFoundError:
            break
    return "smbd"


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


DESKTOP_ID = APP_ID + ".desktop"


def desktop_file():
    return os.path.join(GLib.get_user_data_dir(), "applications", DESKTOP_ID)


def desktop_installed():
    return os.path.exists(desktop_file())


def installed_script():
    """Fixed home for the program once it's added to the menu."""
    return os.path.join(GLib.get_user_data_dir(), "neighborly", "neighborly.py")


def ensure_installed_copy():
    """Copy the program to ~/.local/share/neighborly so launchers keep working
    if the download folder is moved or deleted. Returns the path of the copy."""
    src = os.path.abspath(__file__)
    script = installed_script()
    os.makedirs(os.path.dirname(script), exist_ok=True)
    if not (os.path.exists(script) and os.path.samefile(src, script)):
        shutil.copy2(src, script)
    os.chmod(script, 0o755)
    return script


def _cleanup_copy():
    """Delete the installed copy once neither the launcher nor a right-click entry needs it."""
    if desktop_installed() or context_installed():
        return
    try:
        os.unlink(installed_script())
    except FileNotFoundError:
        pass
    try:
        os.rmdir(os.path.dirname(installed_script()))  # only if empty
    except OSError:
        pass


def install_desktop():
    """Add a launcher to the applications menu (no root needed)."""
    script = ensure_installed_copy()
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Neighborly\n"
        "GenericName=Samba Share Manager\n"
        "Comment=Manage Samba shares, users and connections\n"
        f'Exec=python3 "{script}"\n'
        "Icon=network-server\n"
        "Terminal=false\n"
        "Categories=GNOME;GTK;System;Settings;Network;\n"
        "Keywords=samba;smb;share;windows;network;\n"
    )
    path = desktop_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)
    _update_desktop_db(os.path.dirname(path))


def remove_desktop():
    """Remove the launcher (and the installed copy if nothing else uses it)."""
    try:
        os.unlink(desktop_file())
    except FileNotFoundError:
        pass
    _cleanup_copy()
    _update_desktop_db(os.path.dirname(desktop_file()))


def _update_desktop_db(directory):
    try:
        subprocess.run(["update-desktop-database", directory], capture_output=True)
    except FileNotFoundError:
        pass


# ---- right-click "Share" entry for file managers --------------------------
# Every entry just runs: python3 <installed copy> <folder>. Neighborly then opens
# its share dialog on that folder (and reuses the running window if there is one).
SHARE_LABEL = "Share on local network…"
SHARE_COMMENT = "Share this folder with Samba (Neighborly)"
THUNAR_ID = "neighborly-share-1"

# nautilus-python extension. @SCRIPT@ is replaced with the installed program's path.
NAUTILUS_EXTENSION = '''# SPDX-License-Identifier: GPL-3.0-or-later
# Installed by Neighborly: adds "Share on local network…" to the Files right-click menu.
import subprocess

from gi.repository import GObject, Nautilus

SCRIPT = @SCRIPT@


class NeighborlyShare(GObject.GObject, Nautilus.MenuProvider):
    @staticmethod
    def _folder(info):
        if info is None or info.get_uri_scheme() != "file" or not info.is_directory():
            return None
        return info.get_location().get_path()

    def _items(self, kind, folder):
        if not folder:
            return []
        item = Nautilus.MenuItem(name="NeighborlyShare::" + kind,
                                 label="Share on local network…",
                                 tip="Share this folder with Samba (Neighborly)",
                                 icon="folder-publicshare-symbolic")
        item.connect("activate", self._share, folder)
        return [item]

    # Nautilus 4 passes (files); older versions pass (window, files).
    def get_file_items(self, *args):
        files = args[-1]
        return self._items("file", self._folder(files[0])) if len(files) == 1 else []

    def get_background_items(self, *args):
        return self._items("background", self._folder(args[-1]))

    @staticmethod
    def _share(_item, folder):
        subprocess.Popen(["python3", SCRIPT, folder], start_new_session=True)
'''


def nautilus_script(script):
    """Fallback for Nautilus without nautilus-python: shows up under right-click > Scripts."""
    return f'''#!/bin/sh
# Installed by Neighborly: right-click > Scripts > Share on local network
dir=$(printf '%s\\n' "$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS" | head -n 1)
if [ -z "$dir" ]; then
    dir=$(python3 -c 'import sys, urllib.parse as u; print(u.unquote(u.urlparse(sys.argv[1]).path))' "$NAUTILUS_SCRIPT_CURRENT_URI")
fi
exec python3 "{script}" "$dir"
'''


def _data(*parts):
    return os.path.join(GLib.get_user_data_dir(), *parts)


def _entry_paths():
    return {
        "nautilus-ext": _data("nautilus-python", "extensions", "neighborly_share.py"),
        "nautilus-script": _data("nautilus", "scripts", "Share on local network"),
        "nemo": _data("nemo", "actions", "neighborly-share.nemo_action"),
        "dolphin": _data("kio", "servicemenus", "neighborly-share.desktop"),
        "libfm": _data("file-manager", "actions", "neighborly-share.desktop"),  # PCManFM
    }


def thunar_uca():
    return os.path.join(GLib.get_user_config_dir(), "Thunar", "uca.xml")


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def has_nautilus_python():
    pats = ("/usr/lib*/nautilus/extensions-*/libnautilus-python.so",
            "/usr/lib/*/nautilus/extensions-*/libnautilus-python.so",
            "/usr/local/lib*/nautilus/extensions-*/libnautilus-python.so")
    return any(glob.glob(p) for p in pats)


def _thunar_has():
    try:
        root = ET.parse(thunar_uca()).getroot()
    except (OSError, ET.ParseError):
        return False
    return any(a.findtext("unique-id") == THUNAR_ID for a in root.findall("action"))


def _thunar_update(add, command=None):
    """Add/remove our custom action in Thunar's uca.xml (original is backed up once)."""
    path = thunar_uca()
    if not add and not _thunar_has():
        return
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        base = "/etc/xdg/Thunar/uca.xml"  # keep Thunar's default actions
        if os.path.exists(base):
            shutil.copy(base, path)
        else:
            _write(path, '<?xml version="1.0" encoding="UTF-8"?>\n<actions>\n</actions>\n')
    elif not os.path.exists(path + ".bak"):
        shutil.copy2(path, path + ".bak")
    tree = ET.parse(path)
    root = tree.getroot()
    for a in root.findall("action"):
        if a.findtext("unique-id") == THUNAR_ID:
            root.remove(a)
    if add:
        a = ET.SubElement(root, "action")
        for tag, text in (("icon", "folder-publicshare"), ("name", SHARE_LABEL),
                          ("unique-id", THUNAR_ID), ("command", command),
                          ("description", SHARE_COMMENT), ("patterns", "*")):
            ET.SubElement(a, tag).text = text
        ET.SubElement(a, "directories")
    if hasattr(ET, "indent"):
        ET.indent(tree)
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def context_installed():
    return any(os.path.exists(p) for p in _entry_paths().values()) or _thunar_has()


def install_context_menu():
    """Add the right-click entry to every supported file manager found on this computer.
    Returns a list of (file manager, note)."""
    paths = _entry_paths()
    found = []
    have = {n: shutil.which(n) for n in ("nautilus", "nemo", "dolphin", "thunar", "pcmanfm", "pcmanfm-qt")}
    if not any(have.values()):
        return found
    script = ensure_installed_copy()
    exe = f'python3 "{script}"'
    if have["nautilus"]:
        if has_nautilus_python():
            _write(paths["nautilus-ext"], NAUTILUS_EXTENSION.replace("@SCRIPT@", repr(script)))
            _remove(paths["nautilus-script"])
            found.append(("Nautilus", "restart it with “nautilus -q” to see the entry"))
        else:
            _write(paths["nautilus-script"], nautilus_script(script), 0o755)
            _remove(paths["nautilus-ext"])
            found.append(("Nautilus", "find it under right-click › Scripts (install nautilus-python "
                                      "for a top-level entry)"))
    if have["nemo"]:
        _write(paths["nemo"], f"[Nemo Action]\nName={SHARE_LABEL}\nComment={SHARE_COMMENT}\n"
                              f"Exec={exe} %F\nIcon-Name=folder-publicshare-symbolic\n"
                              "Selection=s\nExtensions=dir;\nQuote=double\n")
        found.append(("Nemo", "appears right away"))
    if have["dolphin"]:
        _write(paths["dolphin"], "[Desktop Entry]\nType=Service\nServiceTypes=KonqPopupMenu/Plugin\n"
                                 "MimeType=inode/directory;\nActions=neighborlyShare;\n"
                                 "X-KDE-Priority=TopLevel\n\n[Desktop Action neighborlyShare]\n"
                                 f"Name={SHARE_LABEL}\nIcon=folder-publicshare\nExec={exe} %f\n", 0o755)
        found.append(("Dolphin", "appears right away"))
    if have["pcmanfm"] or have["pcmanfm-qt"]:
        _write(paths["libfm"], f"[Desktop Entry]\nType=Action\nName={SHARE_LABEL}\n"
                               "Icon=folder-publicshare\nProfiles=profile-zero;\n\n"
                               "[X-Action-Profile profile-zero]\nMimeTypes=inode/directory;\n"
                               f"Exec={exe} %f\n")
        found.append(("PCManFM", "appears right away"))
    if have["thunar"]:
        _thunar_update(True, f"{exe} %f")
        found.append(("Thunar", "restart it with “thunar -q” to see the entry"))
    return found


def remove_context_menu():
    for p in _entry_paths().values():
        _remove(p)
    _thunar_update(False)
    _cleanup_copy()


# ---- safety helpers: folders, permissions, SELinux, accounts, firewall ----
BLOCKED_EXACT = {"/", "/home", "/opt", "/run", "/srv", "/tmp", "/var", "/media", "/mnt"}
BLOCKED_TREES = ("/etc", "/proc", "/sys", "/dev", "/boot", "/root",
                 "/usr", "/bin", "/sbin", "/lib", "/lib64")
OK_SELINUX_TYPES = {"samba_share_t", "public_content_t", "public_content_rw_t"}
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def is_blocked_path(path):
    """Folders that must never be shared (would expose the system or every user)."""
    p = os.path.realpath(path)
    return p in BLOCKED_EXACT or any(p == t or p.startswith(t + "/") for t in BLOCKED_TREES)


def which_sbin(name):
    return shutil.which(name, path=os.environ.get("PATH", "") + ":/usr/sbin:/sbin")


def nologin_shell():
    for p in ("/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/nologin"):
        if os.path.exists(p):
            return p
    return "/bin/false"


def _may(st, uid, gids, bit):
    if uid == 0:
        return True
    if st.st_uid == uid:
        return bool(st.st_mode & (bit << 6))
    if st.st_gid in gids:
        return bool(st.st_mode & (bit << 3))
    return bool(st.st_mode & bit)


def unix_problems(path, users, writable, guest):
    """Plain-language list of Unix-permission problems for the people who'll use the share."""
    names = [u for u in users if not u.startswith(("@", "+", "&"))]
    guests_only = not users and guest
    if guests_only:
        names = ["nobody"]
    problems = []
    for u in names:
        label = "Guests" if guests_only else f"“{u}”"
        try:
            pw = pwd.getpwnam(u)
        except KeyError:
            problems.append(f"{label} is not a user on this computer, so they can't sign in.")
            continue
        gids = {pw.pw_gid} | {g.gr_gid for g in grp.getgrall() if u in g.gr_mem}
        blocked, d = None, path
        while d != "/":
            d = os.path.dirname(d)
            try:
                if not _may(os.stat(d), pw.pw_uid, gids, 1):
                    blocked = d
                    break
            except OSError:
                break
        if blocked:
            problems.append(f"{label} can't enter {blocked} (no execute permission), "
                            "so the share would be unreachable.")
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        mode = oct(st.st_mode & 0o777)[2:]
        if not (_may(st, pw.pw_uid, gids, 4) and _may(st, pw.pw_uid, gids, 1)):
            problems.append(f"{label} can't open this folder (owner {owner}, permissions {mode}).")
        elif writable and not _may(st, pw.pw_uid, gids, 2):
            problems.append(f"{label} can't write in this folder (owner {owner}, permissions {mode}), "
                            "so “Allow changes” would fail.")
    return problems


def selinux_needs_label(path):
    ge = which_sbin("getenforce")
    if not ge:
        return False
    try:
        if subprocess.run([ge], capture_output=True, text=True).stdout.strip() != "Enforcing":
            return False
        out = subprocess.run(["ls", "-dZ", "--", path], capture_output=True, text=True).stdout.split()
        parts = out[0].split(":") if out else []
        return len(parts) >= 3 and parts[2] not in OK_SELINUX_TYPES
    except OSError:
        return False


def selinux_fix_cmd(path):
    """Label just this folder for Samba (permanent with semanage, else a temporary chcon)."""
    rx = re.sub(r"([.^$*+?{}\[\]|()\\])", r"\\\1", path)
    if which_sbin("semanage"):
        script = ('( semanage fcontext -a -t samba_share_t "$2(/.*)?" || '
                  'semanage fcontext -m -t samba_share_t "$2(/.*)?" ) && restorecon -R "$1"')
    else:
        script = 'chcon -R -t samba_share_t "$1"'
    return ["pkexec", "sh", "-c", script, "sh", path, rx]


def detect_firewall():
    if shutil.which("firewall-cmd"):
        return "firewalld"
    if which_sbin("ufw"):
        return "ufw"
    return None


def heads_up(parent, heading, body, buttons, callback):
    """Dialog with custom buttons: buttons = [(id, label, style)], style None/'suggested'/'destructive'.
    One button must have id 'cancel'. callback(response_id) is called for any response."""
    styles = {"suggested": Adw.ResponseAppearance.SUGGESTED,
              "destructive": Adw.ResponseAppearance.DESTRUCTIVE}
    new_style = hasattr(Adw, "AlertDialog")
    d = Adw.AlertDialog.new(heading, body) if new_style else \
        Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
    for rid, label, style in buttons:
        d.add_response(rid, label)
        if style:
            d.set_response_appearance(rid, styles[style])
    d.set_close_response("cancel")
    d.set_default_response("cancel")
    d.connect("response", lambda _d, r: callback(r))
    d.present(parent) if new_style else d.present()


def alert(parent, heading, body):
    if hasattr(Adw, "AlertDialog"):
        d = Adw.AlertDialog.new(heading, body)
        d.add_response("ok", "OK")
        d.present(parent)
    else:
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
        d.add_response("ok", "OK")
        d.present()


def confirm(parent, heading, body, label, callback):
    def on_response(_d, response):
        if response == "ok":
            callback()

    if hasattr(Adw, "AlertDialog"):
        d = Adw.AlertDialog.new(heading, body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", on_response)
        d.present(parent)
    else:
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", on_response)
        d.present()


def icon_button(icon, tooltip, callback, *args):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    b.connect("clicked", lambda _b: callback(*args))
    return b


def esc(text):
    return GLib.markup_escape_text(text or "")


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------
class ShareDialog(Adw.Window):
    def __init__(self, win, section=None, folder=None):
        super().__init__(transient_for=win, modal=True, default_width=500, default_height=660,
                         title="Edit shared folder" if section else "New shared folder")
        self.win = win
        self.orig_name = section.name if section else None

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self.on_save)
        header.pack_start(cancel)
        header.pack_end(self.save_btn)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        general = Adw.PreferencesGroup(title="General")
        page.add(general)
        self.name = Adw.EntryRow(title="Share name")
        self.path = Adw.EntryRow(title="Folder to share")
        pick = icon_button("folder-open-symbolic", "Choose folder…", self.pick_folder)
        self.path.add_suffix(pick)
        self.comment = Adw.EntryRow(title="Description (optional)")
        for r in (self.name, self.path, self.comment):
            general.add(r)

        access = Adw.PreferencesGroup(title="Access")
        page.add(access)
        self.writable = Adw.SwitchRow(title="Allow changes",
                                      subtitle="Let people create, edit and delete files")
        self.browseable = Adw.SwitchRow(title="Show in network list",
                                        subtitle="Otherwise only reachable by typing the name")
        self.guest = Adw.SwitchRow(title="Allow guests",
                                   subtitle="Anyone on the network, no password needed")
        self.users = Adw.EntryRow(title="Only these users (space-separated, empty = everyone)")
        for r in (self.writable, self.browseable, self.guest, self.users):
            access.add(r)

        if section:
            self.name.set_text(section.name)
            self.path.set_text(section.get("path", ""))
            self.comment.set_text(section.get("comment", ""))
            self.writable.set_active(section.writable())
            self.browseable.set_active(section.browseable())
            self.guest.set_active(section.guest())
            self.users.set_text(section.get("valid users", ""))
        else:
            self.browseable.set_active(True)
            if folder:  # opened from a file manager's right-click menu
                self.path.set_text(folder)
                self.name.set_text(os.path.basename(folder.rstrip("/")) or "share")

        view.set_content(page)
        self.set_content(view)

    def pick_folder(self):
        dlg = Gtk.FileDialog(title="Choose a folder to share")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            self.path.set_text(path)
            if not self.name.get_text().strip():
                self.name.set_text(os.path.basename(path.rstrip("/")) or "share")

        dlg.select_folder(self, None, done)

    def on_save(self, _btn):
        name = self.name.get_text().strip()
        path = self.path.get_text().strip()
        if not name or any(c in name for c in "[]/\\"):
            return alert(self, "Invalid name", "Give the share a name without slashes or brackets.")
        if name.lower() in SPECIAL:
            return alert(self, "Reserved name", f"“{name}” is reserved by Samba. Pick another name.")
        if not os.path.isdir(path):
            return alert(self, "Folder not found", "Choose an existing folder to share.")
        real = os.path.realpath(path)
        if is_blocked_path(real):
            return alert(self, "This folder can't be shared",
                         f"Sharing {real} would expose important system files or other people's "
                         "data. Pick a more specific folder inside it.")
        clash = self.win.conf.find(name)
        if clash is not None and clash.name.lower() != (self.orig_name or "").lower():
            return alert(self, "Name already used", f"A share called “{name}” already exists.")
        self.check_then_commit(name, path, real)

    def check_then_commit(self, name, path, real):
        issues = unix_problems(real, self.users.get_text().split(),
                               self.writable.get_active(), self.guest.get_active())
        need_label = selinux_needs_label(real)
        if not issues and not need_label:
            return self._commit(name, path)
        lines = list(issues)
        buttons = [("cancel", "Go back", None), ("save", "Save anyway", None)]
        if need_label:
            lines.append("SELinux is enforcing and this folder isn't labelled for Samba, so other "
                         "computers will probably get “Permission denied”.")
            buttons.append(("fix", "Fix label and save", "suggested"))

        def answer(r):
            if r == "save":
                self._commit(name, path)
            elif r == "fix":
                self.fix_label_then_commit(name, path, real)

        heads_up(self, "Heads-up before saving", "\n".join("• " + ln for ln in lines),
                 buttons, answer)

    def fix_label_then_commit(self, name, path, real):
        self.save_btn.set_sensitive(False)

        def done(rc, out, err):
            self.save_btn.set_sensitive(True)
            if rc == 0:
                self._commit(name, path)
            else:
                alert(self, "Could not fix the label", err.strip() or "Authentication cancelled.")

        run_async(selinux_fix_cmd(real), done)

    def _commit(self, name, path):
        new = copy.deepcopy(self.win.conf)
        sec = new.find(self.orig_name) if self.orig_name else None
        is_new = sec is None
        if is_new:
            sec = Section(name)
        sec.name = name
        sec.set("path", path)
        sec.set("comment", self.comment.get_text().strip() or None)
        sec.set("read only", "no" if self.writable.get_active() else "yes")
        sec.set("browseable", "yes" if self.browseable.get_active() else "no")
        sec.set("guest ok", "yes" if self.guest.get_active() else "no")
        sec.set("valid users", self.users.get_text().strip() or None)
        for alias in ("writable", "writeable", "write ok", "public", "browsable"):
            sec.set(alias, None)  # avoid conflicting synonyms
        if is_new:
            new.add(sec)
        self.save_btn.set_sensitive(False)
        self.win.save_conf(new, done=self.close, failed=lambda: self.save_btn.set_sensitive(True))


class UserDialog(Adw.Window):
    def __init__(self, win, user=None):
        super().__init__(transient_for=win, modal=True, default_width=460, default_height=520,
                         title="Change password" if user else "Add Samba user")
        self.win = win
        self.user = user

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self.on_save)
        header.pack_start(cancel)
        header.pack_end(self.save_btn)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            description="The user must already exist on this computer. Samba keeps its own "
                        "password list, separate from the login password.")
        page.add(g)
        self.name = Adw.EntryRow(title="System username")
        if user:
            self.name.set_text(user)
            self.name.set_sensitive(False)
        self.pw = Adw.PasswordEntryRow(title="Samba password")
        self.pw2 = Adw.PasswordEntryRow(title="Confirm password")
        for r in (self.name, self.pw, self.pw2):
            g.add(r)
        view.set_content(page)
        self.set_content(view)

    def on_save(self, _btn):
        name = self.name.get_text().strip()
        pw, pw2 = self.pw.get_text(), self.pw2.get_text()
        if not name:
            return alert(self, "Missing username", "Enter the username.")
        if not pw:
            return alert(self, "Missing password", "Enter a password.")
        if pw != pw2:
            return alert(self, "Passwords differ", "The two passwords do not match.")
        try:
            pwd.getpwnam(name)
            exists = True
        except KeyError:
            exists = False
        if exists:
            cmd = ["pkexec", "smbpasswd"] + ([] if self.user else ["-a"]) + ["-s", name]
            return self._run(cmd, name, pw)
        if not USERNAME_RE.match(name):
            return alert(self, "Invalid username",
                         "Use lowercase letters, digits, “-” and “_”, starting with a letter.")
        # One admin prompt: create the account, then set the Samba password.
        cmd = ["pkexec", "sh", "-c",
               'useradd -M -s "$2" -- "$1" && smbpasswd -a -s "$1"', "sh", name, nologin_shell()]
        heads_up(self, f"Create a system account for “{name}”?",
                 "This user doesn't exist on this computer yet. A new account will be created "
                 "with no home folder and no ability to log in here. It is only used to sign in "
                 "to Samba shares.",
                 [("cancel", "Cancel", None), ("create", "Create account", "suggested")],
                 lambda r: r == "create" and self._run(cmd, name, pw))

    def _run(self, cmd, name, pw):
        self.save_btn.set_sensitive(False)

        def done(rc, out, err):
            if rc == 0:
                self.win.toast("Password saved" if self.user else f"User “{name}” added")
                self.win.refresh_users()
                self.close()
            else:
                self.save_btn.set_sensitive(True)
                alert(self, "Could not save", err.strip() or out.strip() or "Authentication cancelled.")

        run_async(cmd, done, stdin_text=f"{pw}\n{pw}\n")


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------
class Window(Adw.ApplicationWindow):
    PAGES = [
        ("overview", "Overview", "network-server-symbolic"),
        ("shares", "Shared folders", "folder-publicshare-symbolic"),
        ("users", "Users", "system-users-symbolic"),
        ("connections", "Connections", "network-workgroup-symbolic"),
        ("connect", "Connect", "network-wired-symbolic"),
    ]

    def __init__(self, app):
        super().__init__(application=app, title="Neighborly",
                         default_width=1000, default_height=700)
        self.conf = SmbConf()
        self.service = detect_service()
        self.firewall = detect_firewall()
        self._updating = False
        self.share_rows, self.user_rows = [], []

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.split = Adw.NavigationSplitView()
        self.toasts.set_child(self.split)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        # sidebar
        sb = Adw.ToolbarView()
        sb_header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Add to applications menu", "win.add-to-menu")
        menu.append("Remove from applications menu", "win.remove-from-menu")
        menu.append("Add “Share” to file managers' right-click menu", "win.add-context")
        menu.append("Remove “Share” from right-click menus", "win.remove-context")
        menu.append("About Neighborly", "win.about")
        sb_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                          primary=True, tooltip_text="Main menu"))
        sb.add_top_bar(sb_header)
        self.setup_menu_actions()
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        for key, title, icon in self.PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=6, margin_end=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row.set_child(box)
            row.page_key, row.page_title = key, title
            self.sidebar.append(row)
        self.sidebar.connect("row-selected", self.on_nav)
        sb.set_content(self.sidebar)
        self.split.set_sidebar(Adw.NavigationPage.new(sb, "Neighborly"))

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.build_overview(), "overview")
        self.stack.add_named(self.build_shares(), "shares")
        self.stack.add_named(self.build_users(), "users")
        self.stack.add_named(self.build_connections(), "connections")
        self.stack.add_named(self.build_connect(), "connect")
        ct = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(icon_button("view-refresh-symbolic", "Reload everything", self.refresh_all))
        ct.add_top_bar(header)
        ct.set_content(self.stack)
        self.content_page = Adw.NavigationPage.new(ct, "Overview")
        self.split.set_content(self.content_page)

        self.sidebar.select_row(self.sidebar.get_row_at_index(0))
        self.refresh_all()

    # ---- main menu -------------------------------------------------------
    def setup_menu_actions(self):
        for name, cb in (("add-to-menu", self.on_add_to_menu),
                         ("remove-from-menu", self.on_remove_from_menu),
                         ("add-context", self.on_add_context),
                         ("remove-context", self.on_remove_context),
                         ("about", self.on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        self.sync_menu_actions()

    def sync_menu_actions(self):
        installed = desktop_installed()
        self.lookup_action("add-to-menu").set_enabled(not installed)
        self.lookup_action("remove-from-menu").set_enabled(installed)
        ctx = context_installed()
        self.lookup_action("add-context").set_enabled(not ctx)
        self.lookup_action("remove-context").set_enabled(ctx)

    def on_add_context(self, *_):
        try:
            found = install_context_menu()
        except (OSError, ET.ParseError) as e:
            alert(self, "Could not add the right-click entry", str(e))
            return self.sync_menu_actions()
        self.sync_menu_actions()
        if not found:
            return alert(self, "No supported file manager found",
                         "Neighborly can add a “Share on local network…” entry to Nautilus, Nemo, "
                         "Dolphin, Thunar and PCManFM, but none of them was found on this computer.")
        alert(self, "Right-click entry added", "\n".join(f"• {n}: {note}" for n, note in found))

    def on_remove_context(self, *_):
        try:
            remove_context_menu()
            self.toast("Removed from right-click menus")
        except (OSError, ET.ParseError) as e:
            alert(self, "Could not remove the right-click entry", str(e))
        self.sync_menu_actions()

    def on_add_to_menu(self, *_):
        try:
            install_desktop()
            self.toast("Added to your applications menu")
        except OSError as e:
            alert(self, "Could not add to menu", str(e))
        self.sync_menu_actions()

    def on_remove_from_menu(self, *_):
        try:
            remove_desktop()
            self.toast("Removed from your applications menu")
        except OSError as e:
            alert(self, "Could not remove from menu", str(e))
        self.sync_menu_actions()

    def on_about(self, *_):
        if hasattr(Adw, "AboutDialog"):
            d = Adw.AboutDialog(application_name="Neighborly", application_icon="network-server",
                                comments="A friendly way to manage and use Samba.",
                                version="1.0")
            d.present(self)
        else:
            d = Adw.AboutWindow(transient_for=self, application_name="Neighborly",
                                application_icon="network-server",
                                comments="A friendly way to manage and use Samba.", version="1.0")
            d.present()

    def share_folder(self, path):
        """Open the share dialog for a folder (used by the file managers' right-click entry)."""
        path = os.path.realpath(path)
        if not os.path.isdir(path):
            self.toast(f"“{path}” is not a folder")
            return
        for i, (key, _title, _icon) in enumerate(self.PAGES):
            if key == "shares":
                self.sidebar.select_row(self.sidebar.get_row_at_index(i))
        for s in self.conf.shares():
            shared = s.get("path")
            if shared and os.path.realpath(shared) == path:  # already shared: edit it
                return ShareDialog(self, s).present()
        ShareDialog(self, folder=path).present()

    # ---- plumbing --------------------------------------------------------
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    def on_nav(self, _lb, row):
        if row is None:
            return
        self.stack.set_visible_child_name(row.page_key)
        self.content_page.set_title(row.page_title)
        self.split.set_show_content(True)

    def open_uri(self, uri):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            self.toast(f"Could not open {uri}: {e.message}")

    def copy_text(self, text):
        self.get_clipboard().set(text)
        self.toast("Copied to clipboard")

    def refresh_all(self):
        self.load_conf()
        self.refresh_status()
        self.refresh_firewall()

    def load_conf(self):
        try:
            with open(CONF, encoding="utf-8", errors="replace") as f:
                self.conf = SmbConf.parse(f.read())
        except FileNotFoundError:
            self.conf = SmbConf.parse("[global]\n\tworkgroup = WORKGROUP\n")
            self.toast(f"{CONF} not found - is Samba installed? A new file will be created on save.")
        except PermissionError:
            self.toast(f"Not allowed to read {CONF}")
        g = self.conf.find("global")
        self.workgroup.set_text(g.get("workgroup", "") if g else "")
        self.server_string.set_text(g.get("server string", "") if g else "")
        self.refresh_shares()

    def save_conf(self, new_conf, done=None, failed=None):
        fd, tmp = tempfile.mkstemp(prefix="smbconf-", suffix=".conf")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_conf.dump())
        os.chmod(tmp, 0o644)

        def work():
            err = None
            try:
                r = subprocess.run(["testparm", "-s", tmp], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    err = "Samba rejected this configuration:\n\n" + (r.stderr or r.stdout).strip()
            except FileNotFoundError:
                pass  # testparm missing: skip validation
            if err is None:
                try:
                    r = subprocess.run(["pkexec", "sh", "-c", INSTALL_SCRIPT, "sh", tmp, CONF],
                                       capture_output=True, text=True)
                    if r.returncode in (126, 127):
                        err = "Authentication was cancelled or failed."
                    elif r.returncode != 0:
                        err = r.stderr.strip() or "Could not write the configuration file."
                except FileNotFoundError:
                    err = "pkexec (polkit) is not installed."
            try:
                os.unlink(tmp)
            except OSError:
                pass
            GLib.idle_add(lambda: self._saved(new_conf, err, done, failed) and False)

        threading.Thread(target=work, daemon=True).start()

    def _saved(self, new_conf, err, done, failed):
        if err:
            alert(self, "Couldn't save changes", err)
            if failed:
                failed()
            return
        self.conf = new_conf
        self.refresh_shares()
        self.toast("Changes saved and applied")
        if done:
            done()

    # ---- overview --------------------------------------------------------
    def build_overview(self):
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Service")
        page.add(g)
        self.status_row = Adw.ActionRow(title="Samba service", subtitle="Checking…")
        self.status_icon = Gtk.Image(icon_name="content-loading-symbolic")
        self.status_row.add_prefix(self.status_icon)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for label, action in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, a=action: self.service_action(a))
            box.append(b)
        self.status_row.add_suffix(box)
        g.add(self.status_row)
        self.boot_row = Adw.SwitchRow(title="Start when the computer boots")
        self.boot_row.connect("notify::active", self.on_boot_toggled)
        g.add(self.boot_row)

        g2 = Adw.PreferencesGroup(title="This server",
                                  description="How other computers can reach your shares")
        page.add(g2)
        host, ip = socket.gethostname(), local_ip()
        addr = f"smb://{host}/"
        sub = f"Windows: \\\\{host}\\"
        if ip:
            sub += f"      By IP: smb://{ip}/"
        row = Adw.ActionRow(title=esc(addr), subtitle=esc(sub))
        row.add_suffix(icon_button("edit-copy-symbolic", "Copy address", self.copy_text, addr))
        g2.add(row)

        gf = Adw.PreferencesGroup(title="Firewall",
                                  description="Let other computers on your network reach your shares")
        page.add(gf)
        self.fw_row = Adw.ActionRow(title="Samba through the firewall", subtitle="Checking…")
        fw_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.fw_allow = Gtk.Button(label="Allow")
        self.fw_block = Gtk.Button(label="Block")
        self.fw_allow.connect("clicked", lambda _b: self.firewall_action("allow"))
        self.fw_block.connect("clicked", lambda _b: self.firewall_action("block"))
        fw_box.append(self.fw_allow)
        fw_box.append(self.fw_block)
        self.fw_row.add_suffix(fw_box)
        gf.add(self.fw_row)

        g3 = Adw.PreferencesGroup(title="Network identity")
        page.add(g3)
        self.workgroup = Adw.EntryRow(title="Workgroup")
        self.server_string = Adw.EntryRow(title="Server description")
        g3.add(self.workgroup)
        g3.add(self.server_string)
        apply_row = Adw.ActionRow()
        apply_btn = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", lambda _b: self.apply_global())
        apply_row.add_suffix(apply_btn)
        g3.add(apply_row)
        return page

    def refresh_status(self):
        def got_active(rc, out, err):
            active = out.strip() == "active"
            self.status_row.set_subtitle("Running" if active else "Stopped")
            self.status_icon.set_from_icon_name(
                "emblem-ok-symbolic" if active else "process-stop-symbolic")

        def got_enabled(rc, out, err):
            self._updating = True
            self.boot_row.set_active(out.strip() == "enabled")
            self._updating = False

        run_async(["systemctl", "is-active", f"{self.service}.service"], got_active)
        run_async(["systemctl", "is-enabled", f"{self.service}.service"], got_enabled)

    def service_action(self, action):
        def done(rc, out, err):
            if rc == 0:
                self.toast(f"Samba: {action} done")
            else:
                alert(self, "Service command failed", err.strip() or "Authentication cancelled.")
            self.refresh_status()

        run_async(["pkexec", "systemctl", action, f"{self.service}.service"], done)

    def on_boot_toggled(self, row, _pspec):
        if self._updating:
            return
        action = "enable" if row.get_active() else "disable"

        def done(rc, out, err):
            if rc != 0:
                alert(self, "Could not change boot setting", err.strip() or "Authentication cancelled.")
            self.refresh_status()

        run_async(["pkexec", "systemctl", action, f"{self.service}.service"], done)

    def apply_global(self):
        new = copy.deepcopy(self.conf)
        g = new.ensure("global")
        g.set("workgroup", self.workgroup.get_text().strip() or None)
        g.set("server string", self.server_string.get_text().strip() or None)
        self.save_conf(new)

    # ---- firewall --------------------------------------------------------
    def refresh_firewall(self):
        has = self.firewall is not None
        self.fw_allow.set_sensitive(has)
        self.fw_block.set_sensitive(has)
        if self.firewall is None:
            self.fw_row.set_subtitle("No supported firewall found (firewalld or ufw), "
                                     "so nothing is blocking Samba")
            return
        if self.firewall == "ufw":
            self.fw_row.set_subtitle("Using ufw · Allow or Block asks for your admin password")
            return

        def done(rc, out, err):
            words = out.split()
            if not words or words[0] != "running":
                sub = "firewalld is not running, so nothing is blocking Samba"
            elif len(words) >= 3:
                state = "allowed" if words[2] == "yes" else "blocked"
                sub = f"firewalld · zone “{words[1]}” · Samba is {state}"
            else:
                sub = "Could not read the firewall status"
            self.fw_row.set_subtitle(esc(sub))

        run_async(["sh", "-c", "firewall-cmd --state; firewall-cmd --get-default-zone; "
                               "firewall-cmd --query-service=samba"], done)

    def firewall_action(self, action):
        if self.firewall == "firewalld":
            flag = "--add-service=samba" if action == "allow" else "--remove-service=samba"
            cmd = ["pkexec", "sh", "-c", f"firewall-cmd --permanent {flag} && firewall-cmd --reload"]
        elif self.firewall == "ufw":
            cmd = ["pkexec", "ufw", "allow", "samba"] if action == "allow" \
                else ["pkexec", "ufw", "delete", "allow", "samba"]
        else:
            return

        def run():
            def done(rc, out, err):
                if rc == 0:
                    self.toast("Firewall updated")
                else:
                    alert(self, "Firewall command failed",
                          err.strip() or out.strip() or "Authentication cancelled.")
                self.refresh_firewall()

            run_async(cmd, done)

        if action == "allow":
            heads_up(self, "Allow Samba through the firewall?",
                     "Other computers on the network will be able to reach Samba (ports 139 and 445). "
                     "Only do this on networks you trust, like home or office, and not on public "
                     "Wi-Fi or a server exposed to the internet.",
                     [("cancel", "Cancel", None), ("ok", "Allow", "suggested")],
                     lambda r: r == "ok" and run())
        else:
            run()

    # ---- shares ----------------------------------------------------------
    def build_shares(self):
        page = Adw.PreferencesPage()
        self.shares_group = Adw.PreferencesGroup(
            title="Shared folders", description="Folders other computers can open")
        add = Gtk.Button(label="Add folder", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: ShareDialog(self).present())
        self.shares_group.set_header_suffix(add)
        page.add(self.shares_group)
        return page

    def refresh_shares(self):
        for r in self.share_rows:
            self.shares_group.remove(r)
        self.share_rows = []
        shares = self.conf.shares()
        if not shares:
            row = Adw.ActionRow(title="No shared folders yet",
                                subtitle="Press “Add folder” to share one")
            self.shares_group.add(row)
            self.share_rows.append(row)
            return
        host = socket.gethostname()
        for s in shares:
            path = s.get("path", "") if s.name.lower() != "homes" else "Every user's home folder"
            bits = [path, "Read & write" if s.writable() else "Read-only"]
            if s.guest():
                bits.append("Guests allowed")
            if not s.browseable():
                bits.append("Hidden")
            row = Adw.ActionRow(title=esc(s.name), subtitle=esc("  ·  ".join(b for b in bits if b)))
            row.add_prefix(Gtk.Image(icon_name="folder-publicshare-symbolic"))
            row.add_suffix(icon_button("folder-open-symbolic", "Open in Files",
                                       self.open_uri, f"smb://localhost/{s.name}"))
            row.add_suffix(icon_button("edit-copy-symbolic", "Copy network address",
                                       self.copy_text, f"smb://{host}/{s.name}"))
            row.add_suffix(icon_button("document-edit-symbolic", "Edit",
                                       lambda sec=s: ShareDialog(self, sec).present()))
            row.add_suffix(icon_button("user-trash-symbolic", "Stop sharing",
                                       self.delete_share, s.name))
            self.shares_group.add(row)
            self.share_rows.append(row)

    def delete_share(self, name):
        def go():
            new = copy.deepcopy(self.conf)
            new.remove(name)
            self.save_conf(new)

        confirm(self, f"Stop sharing “{name}”?",
                "The folder and its files stay untouched; it just stops being shared.",
                "Stop sharing", go)

    # ---- users -----------------------------------------------------------
    def build_users(self):
        page = Adw.PreferencesPage()
        self.users_group = Adw.PreferencesGroup(
            title="Samba users",
            description="People who can sign in to password-protected shares")
        box = Gtk.Box(spacing=6)
        box.append(icon_button("view-refresh-symbolic", "Reload list", self.refresh_users))
        add = Gtk.Button(label="Add user", valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: UserDialog(self).present())
        box.append(add)
        self.users_group.set_header_suffix(box)
        page.add(self.users_group)
        self._set_user_placeholder("Press reload to list users (asks for your admin password)")
        return page

    def _clear_user_rows(self):
        for r in self.user_rows:
            self.users_group.remove(r)
        self.user_rows = []

    def _set_user_placeholder(self, text):
        self._clear_user_rows()
        row = Adw.ActionRow(title=esc(text))
        self.users_group.add(row)
        self.user_rows.append(row)

    def refresh_users(self):
        def done(rc, out, err):
            if rc != 0:
                return self._set_user_placeholder(err.strip() or "Could not list users")
            self._clear_user_rows()
            users = [ln.split(":") for ln in out.splitlines() if ":" in ln]
            if not users:
                return self._set_user_placeholder("No Samba users yet")
            for parts in users:
                name = parts[0]
                full = parts[2] if len(parts) > 2 and parts[2] else ""
                row = Adw.ActionRow(title=esc(name), subtitle=esc(full or f"UID {parts[1]}"))
                row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
                row.add_suffix(icon_button("dialog-password-symbolic", "Change password",
                                           lambda n=name: UserDialog(self, n).present()))
                row.add_suffix(icon_button("user-trash-symbolic", "Remove from Samba",
                                           self.delete_user, name))
                self.users_group.add(row)
                self.user_rows.append(row)

        run_async(["pkexec", "pdbedit", "-L"], done)

    def delete_user(self, name):
        def go():
            def done(rc, out, err):
                if rc == 0:
                    self.toast(f"“{name}” removed from Samba")
                else:
                    alert(self, "Could not remove user", err.strip() or "Authentication cancelled.")
                self.refresh_users()

            run_async(["pkexec", "smbpasswd", "-x", name], done)

        confirm(self, f"Remove “{name}” from Samba?",
                "The system account is not deleted, only its Samba access.", "Remove", go)

    # ---- connections -----------------------------------------------------
    def build_connections(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Active connections",
                                 description="Who is connected to this server right now")
        g.set_header_suffix(icon_button("view-refresh-symbolic", "Refresh", self.refresh_connections))
        page.add(g)
        self.conn_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                      left_margin=12, right_margin=12, top_margin=12, bottom_margin=12)
        self.conn_view.get_buffer().set_text("Press refresh to look (asks for your admin password).")
        sw = Gtk.ScrolledWindow(min_content_height=360, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        sw.add_css_class("card")
        sw.set_child(self.conn_view)
        g.add(sw)
        return page

    def refresh_connections(self):
        def done(rc, out, err):
            text = out.strip() if rc == 0 else (err.strip() or "Could not run smbstatus.")
            self.conn_view.get_buffer().set_text(text or "Nobody is connected.")

        run_async(["pkexec", "smbstatus"], done)

    # ---- connect ---------------------------------------------------------
    def build_connect(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Use shares from other computers",
                                 description="Opens in your file manager")
        page.add(g)
        self.server_row = Adw.EntryRow(title="Server name or IP address", show_apply_button=True)
        self.server_row.connect("apply", self.on_connect)
        g.add(self.server_row)
        for title, sub, uri in (
            ("Browse the network", "See computers and shares nearby", "network:///"),
            ("This computer's shares", "Check how your shares look to others", "smb://localhost/"),
        ):
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, u=uri: self.open_uri(u))
            g.add(row)
        return page

    def on_connect(self, row):
        server = row.get_text().strip().removeprefix("smb://").strip("/")
        if server:
            self.open_uri(f"smb://{server}/")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def _window(self):
        return self.props.active_window or Window(self)

    def do_activate(self):
        self._window().present()

    def do_open(self, files, *_):
        """`neighborly /some/folder` opens the share dialog for that folder."""
        win = self._window()
        win.present()
        path = files[0].get_path() if files else None
        if path:
            win.share_folder(path)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
