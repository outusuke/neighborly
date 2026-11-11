# Neighborly

> Samba shares made simple.

[![License: GPL v3 or later](https://img.shields.io/badge/License-GPL%20v3%2B-blue.svg)](LICENSE)

A small GTK4 + libadwaita app to manage and use Samba without touching `smb.conf` by hand.

- **Overview** – start/stop/restart Samba, start at boot, your server address, workgroup
- **Shared folders** – add, edit, remove shares (read/write, guests, allowed users, hidden)
- **Users** – add Samba users, change passwords, remove them
- **Connections** – see who is connected (`smbstatus`)
- **Connect** – open other computers' shares or browse the network in your file manager

Your comments and other settings in `/etc/samba/smb.conf` are preserved. Every save is
validated with `testparm`, the previous file is kept as `smb.conf.bak`, and Samba reloads
its config automatically. Anything that needs root asks for your password via polkit (`pkexec`).

## Dependencies

| Distro | Install |
|---|---|
| Debian / Ubuntu (24.04+) | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 samba samba-common-bin policykit-1` |
| Fedora | `sudo dnf install python3-gobject gtk4 libadwaita samba polkit` |
| Arch | `sudo pacman -S python-gobject gtk4 libadwaita samba polkit` |

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
  - on SELinux systems offers to label just that folder `samba_share_t`
    (permanent with `semanage`, otherwise a temporary `chcon`).

## License

Neighborly is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version (`GPL-3.0-or-later`).
