# Neighborly

> Samba shares made simple.

[![License: GPL v3 or later](https://img.shields.io/badge/License-GPL%20v3%2B-blue.svg)](LICENSE)

A small GTK4 + libadwaita app to manage and use Samba without touching `smb.conf` by hand.

- **Overview** – start/stop/restart Samba, start at boot, your server address, workgroup
- **Shared folders** – add, edit, remove shares (read/write, guests, allowed users, hidden)
- **Users** – add Samba users, change passwords, remove them
- **Connections** – see who is connected (`smbstatus`)
- **Connect** – open other computers' shares or browse the network in your file manager;
  test your own shares natively (`systemctl is-active`, `smbclient -L localhost -N`,
  `smbclient //localhost/share -N -c ls`) without opening a file manager

Your comments and other settings in `/etc/samba/smb.conf` are preserved. Every save is
validated with `testparm`, the previous file is kept as `smb.conf.bak`, and Samba reloads
its config automatically. Anything that needs root asks for your password via polkit (`pkexec`).

## Dependencies

| Distro | Install |
|---|---|
| Debian / Ubuntu (24.04+) | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 samba samba-common-bin smbclient policykit-1` |
| Fedora | `sudo dnf install python3-gobject gtk4 libadwaita samba samba-client polkit` |
| Arch | `sudo pacman -S python-gobject gtk4 libadwaita samba smbclient polkit` |

Needs libadwaita 1.4 or newer.

## Run

```sh
python3 neighborly.py
```

## Install (optional)

```sh
sudo install -m 755 neighborly.py /usr/local/bin/neighborly
sudo install -m 644 io.neighborly.Neighborly.desktop /usr/share/applications/
```

## Notes

Things you used to do in a terminal are handled in the app, each with a confirmation:

- **Users → Add user:** if the system account doesn't exist, the app offers to create it
  (no home folder, no login shell) and sets the Samba password in a single admin prompt.
- **Overview → Firewall:** detects firewalld or ufw and opens/closes only the `samba` service.
  Opening it shows a warning about untrusted networks.
- **Shared folders → Add/Edit:** before saving, the app
  - refuses system folders and whole-`/home` style paths,
  - checks Unix permissions for the people who'll use the share (including parent folders) and
    explains problems in plain language; it never changes permissions or runs `chmod 777`,
  - if a parent folder (like your home folder) stops guests or the listed users from entering, offers
    to let just those users pass through it with an ACL entry (`setfacl -m u:USER:x`, enter only: they
    can't list or read it). Neighborly remembers what it added in
    `~/.config/neighborly/acl-grants.json` and removes it again when you stop sharing or move the
    share elsewhere. ACL entries that were already there are never touched. Needs the `acl` package,
  - on SELinux systems offers to label just that folder `samba_share_t`
    (permanent with `semanage`, otherwise a temporary `chcon`).

## Right-click "Share on local network…"

Like the old `nautilus-share`, but not tied to one file manager. In the ☰ menu choose
**Add “Share” to file managers' right-click menu**; Neighborly adds the entry to every
supported file manager it finds, with no root needed:

| File manager | Result |
|---|---|
| Nautilus (Files) | Top-level entry if `nautilus-python` is installed (`python3-nautilus` / `nautilus-python`), otherwise under right-click › Scripts. Restart with `nautilus -q`. |
| Nemo | Right-click entry |
| Dolphin | Right-click entry |
| Thunar | Custom action (your existing actions are kept, `uca.xml.bak` is saved). Restart with `thunar -q`. |
| PCManFM / PCManFM-Qt | Right-click entry |

The entry opens Neighborly's share dialog on that folder (or edits the share if the folder is
already shared). You can also run it directly: `python3 neighborly.py /path/to/folder`.
Use **Remove “Share” from right-click menus** in the same menu to undo everything.

## License

Neighborly is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version (`GPL-3.0-or-later`).
