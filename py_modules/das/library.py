"""Read-only discovery of installed Steam applications.

Decky Animated Screensaver uses this small parser instead of asking Steam or Decky to build a
library view.  Only local ``appmanifest_*.acf`` files are inspected.
"""

from __future__ import annotations

import os
from pathlib import Path
import re


_PAIR = re.compile(r'^\s*"([^"]+)"\s+"([^"]*)"\s*$')


def _steam_home(user_home=None):
    home = Path(user_home or os.environ.get("DECKY_USER_HOME") or Path.home())
    override = os.environ.get("DAS_STEAM_ROOT")
    candidates = [
        Path(override) if override else None,
        home / ".steam/steam",
        home / ".steam/root",
        home / ".local/share/Steam",
    ]
    # Decky runs as root on SteamOS. If its user-home environment is absent,
    # Path.home() points at root, not at the user owning the Steam session.
    # Only fall back for implicit discovery; never override a caller's home.
    if user_home is None and hasattr(os,"geteuid") and os.geteuid() == 0:
        try:
            import pwd
            homes = [Path(pwd.getpwuid(int(uid)).pw_dir) for uid in os.listdir('/run/user') if uid.isdigit() and int(uid)>0]
        except (OSError,KeyError,ValueError): homes = []
        for candidate in [*homes, Path('/home/deck')]:
            candidates.extend((candidate/'.steam/steam',candidate/'.steam/root',candidate/'.local/share/Steam'))
    return next((path for path in candidates if path and path.is_dir()), None)


def _pairs(path):
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [match.groups() for line in lines if (match := _PAIR.match(line))]


def steam_library_roots(user_home=None):
    root = _steam_home(user_home)
    if root is None:
        return []
    roots = [root]
    config = root / "steamapps/libraryfolders.vdf"
    for key, value in _pairs(config):
        if key.lower() != "path":
            continue
        candidate = Path(value.replace("\\\\", "\\"))
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def installed_games(user_home=None, limit=2000):
    """Return unique installed Steam games sorted by display name."""
    games = {}
    for root in steam_library_roots(user_home):
        for manifest in (root / "steamapps").glob("appmanifest_*.acf"):
            values = {key.lower(): value for key, value in _pairs(manifest)}
            try:
                appid = int(values.get("appid", "0"))
            except ValueError:
                continue
            name = values.get("name", "").strip()
            if appid > 0 and name:
                games[appid] = {"appid": appid, "name": name[:160]}
            if len(games) >= max(1, int(limit)):
                break
    return sorted(games.values(), key=lambda item: (item["name"].casefold(), item["appid"]))


def last_played_game(games, user_home=None):
    """Use Steam's LastPlayed, not download/update time or alphabetical order."""
    installed={game['appid']:game for game in games}
    newest=(0,0)
    for root in steam_library_roots(user_home):
        for manifest in (root/'steamapps').glob('appmanifest_*.acf'):
            values={key.lower():value for key,value in _pairs(manifest)}
            try: appid=int(values.get('appid','0')); played=int(values.get('lastplayed','0'))
            except ValueError: continue
            if appid in installed and played>newest[0]: newest=(played,appid)
    game=installed.get(newest[1])
    return {**game,'last_played':newest[0]} if game else None


_MAX_SHORTCUTS_BYTES = 4 * 1024 * 1024
_MAX_VDF_DEPTH = 8


def _read_binary_vdf(data):
    """Parse Valve's binary KeyValues (used by ``shortcuts.vdf``) into nested dicts.

    Keys are lower-cased because Steam has written both ``AppName`` and ``appname``.
    Returns ``None`` for malformed input; never raises.
    """
    import struct
    size = len(data)

    def cstring(pos):
        end = data.find(b"\x00", pos)
        if end < 0:
            raise ValueError("unterminated string")
        return data[pos:end].decode("utf-8", errors="replace"), end + 1

    def section(pos, depth):
        if depth > _MAX_VDF_DEPTH:
            raise ValueError("too deep")
        result = {}
        while True:
            if pos >= size:
                raise ValueError("truncated")
            kind = data[pos]
            pos += 1
            if kind == 0x08:
                return result, pos
            key, pos = cstring(pos)
            key = key.lower()
            if kind == 0x00:
                result[key], pos = section(pos, depth + 1)
            elif kind == 0x01:
                result[key], pos = cstring(pos)
            elif kind in (0x02, 0x03, 0x04, 0x06):
                if pos + 4 > size:
                    raise ValueError("truncated")
                fmt = "<f" if kind == 0x03 else "<i"
                result[key] = struct.unpack_from(fmt, data, pos)[0]
                pos += 4
            elif kind in (0x07, 0x0A):
                if pos + 8 > size:
                    raise ValueError("truncated")
                result[key] = struct.unpack_from("<Q" if kind == 0x07 else "<q", data, pos)[0]
                pos += 8
            else:
                raise ValueError("unknown type")

    try:
        parsed, _ = section(0, 0)
    except (ValueError, struct.error):
        return None
    return parsed


def _shortcut_appid(entry):
    """Unsigned 32-bit id Steam uses for a non-Steam shortcut."""
    value = entry.get("appid")
    if isinstance(value, int) and not isinstance(value, bool) and value:
        return value & 0xFFFFFFFF
    import zlib
    seed = (str(entry.get("exe", "")) + str(entry.get("appname", ""))).encode("utf-8", errors="replace")
    return (zlib.crc32(seed) & 0xFFFFFFFF) | 0x80000000


def shortcut_games(user_home=None, limit=2000):
    """Return non-Steam shortcuts from every ``userdata/*/config/shortcuts.vdf``.

    Rows are ``{"shortcut_id", "name", "last_played"}`` where ``shortcut_id`` is the
    unsigned 32-bit id Steam assigns to the shortcut. They deliberately carry no
    ``appid`` key: that field means "Steam AppID" elsewhere in the plugin.
    """
    root = _steam_home(user_home)
    if root is None:
        return []
    games = {}
    try:
        users = sorted((root / "userdata").iterdir())
    except OSError:
        return []
    for user in users:
        if not user.name.isdigit() or int(user.name) == 0:
            continue
        path = user / "config" / "shortcuts.vdf"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_SHORTCUTS_BYTES:
                continue
            parsed = _read_binary_vdf(path.read_bytes())
        except OSError:
            continue
        entries = (parsed or {}).get("shortcuts")
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("appname", "")).strip()
            if not name:
                continue
            number = _shortcut_appid(entry)
            played = entry.get("lastplaytime")
            row = {"shortcut_id": number, "name": name[:160],
                   "last_played": played if isinstance(played, int) and played > 0 else 0}
            previous = games.get(number)
            if previous is None or row["last_played"] > previous["last_played"]:
                games[number] = row
            if len(games) >= max(1, int(limit)):
                break
    return sorted(games.values(), key=lambda item: (item["name"].casefold(), item["shortcut_id"]))
