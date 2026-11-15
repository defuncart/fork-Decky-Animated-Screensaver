import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from das.core.catalog import ScreensaverError
from das.library import _read_binary_vdf, shortcut_games
from das.studio.service import StudioService


def _s(key, value): return b"\x01" + key.encode() + b"\0" + value.encode() + b"\0"
def _i(key, value): return b"\x02" + key.encode() + b"\0" + struct.pack("<i", value)
def _d(key, body): return b"\x00" + key.encode() + b"\0" + body + b"\x08"


def shortcuts_blob(*entries):
    body = b"".join(_d(str(n), e) for n, e in enumerate(entries))
    return _d("shortcuts", body) + b"\x08"


class ShortcutParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "steam"
        (self.root / "userdata/1234/config").mkdir(parents=True)

    def write(self, blob, user="1234"):
        folder = self.root / "userdata" / user / "config"; folder.mkdir(parents=True, exist_ok=True)
        (folder / "shortcuts.vdf").write_bytes(blob)

    def games(self):
        import os
        os.environ["DAS_STEAM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "DAS_STEAM_ROOT", None)
        return shortcut_games(self.tmp.name)

    def test_parses_both_key_casings_and_unsigned_appid(self):
        self.write(shortcuts_blob(
            _i("appid", -1000000) + _s("AppName", "Heroic") + _i("LastPlayTime", 77),
            _s("appname", "Lower") + _s("exe", "/bin/x")))
        rows = {r["name"]: r for r in self.games()}
        self.assertEqual(rows["Heroic"]["shortcut_id"], -1000000 & 0xFFFFFFFF)
        self.assertEqual(rows["Heroic"]["last_played"], 77)
        expected = (zlib.crc32(b"/bin/xLower") & 0xFFFFFFFF) | 0x80000000
        self.assertEqual(rows["Lower"]["shortcut_id"], expected)
        self.assertNotIn("appid", rows["Heroic"])

    def test_malformed_and_oversized_input_is_ignored(self):
        for blob in (b"", b"\x00shortcuts\0\x00" , b"\x09zz\0", shortcuts_blob(_s("AppName", "x"))[:-3]):
            self.assertIsNone(_read_binary_vdf(blob)) if blob else None
        self.write(b"\x00shortcuts\0garbage")
        self.assertEqual(self.games(), [])

    def test_skips_nameless_anonymous_user_and_dedupes_across_users(self):
        entry = _i("appid", 5) + _s("AppName", "Same")
        self.write(shortcuts_blob(entry, _i("appid", 6)))
        self.write(shortcuts_blob(entry), user="5678")
        self.write(shortcuts_blob(_i("appid", 9) + _s("AppName", "Anon")), user="0")
        self.assertEqual([r["name"] for r in self.games()], ["Same"])

    def test_no_userdata_is_empty(self):
        import shutil, os
        shutil.rmtree(self.root / "userdata")
        os.environ["DAS_STEAM_ROOT"] = str(self.root); self.addCleanup(os.environ.pop, "DAS_STEAM_ROOT", None)
        self.assertEqual(shortcut_games(self.tmp.name), [])


class _Provider:
    def __init__(self, table): self.table = table; self.calls = []
    def _json_request(self, url):
        self.calls.append(url)
        name = url.rsplit("/", 1)[1].replace("%20", " ")
        return {"data": [{"id": self.table[name], "name": name}] if name in self.table else []}
    def status(self): return {"configured": True}


class ShortcutMergeTests(unittest.TestCase):
    def make(self, shortcuts, table):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.provider = _Provider(table)
        service = StudioService(Path(self.tmp.name) / "lib", self.provider,
                                games_provider=lambda: [{"appid": 620, "name": "Portal 2"}],
                                shortcuts_provider=lambda: shortcuts)
        service._has_key = lambda: True
        service._api_at = 0
        self.addCleanup(lambda: getattr(service, "close", lambda: None)())
        return service

    def test_shortcut_stored_as_negative_sgdb_id_with_metadata(self):
        service = self.make([{"shortcut_id": 3000000001, "name": "Heroic", "last_played": 0}], {"Heroic": 4242})
        service.refresh_games()
        entry = service.catalog.get(-4242)
        self.assertEqual((entry["origin"], entry["shortcut_id"], entry["sgdb_id"], entry["installed"]), ("shortcut", 3000000001, 4242, True))
        self.assertTrue(entry["excluded"])
        self.assertEqual(service.catalog.get(620)["name"], "Portal 2")

    def test_existing_match_is_reused_without_new_lookup(self):
        shortcuts = [{"shortcut_id": 3000000001, "name": "Heroic", "last_played": 0}]
        service = self.make(shortcuts, {"Heroic": 4242})
        service.refresh_games(); first = len(self.provider.calls)
        service.refresh_games()
        self.assertEqual(len(self.provider.calls), first)

    def test_user_inclusion_survives_rescan(self):
        service = self.make([{"shortcut_id": 3000000001, "name": "Heroic", "last_played": 0}], {"Heroic": 4242})
        service.refresh_games(); service.catalog.update(-4242, {"excluded": False}); service.refresh_games()
        self.assertFalse(service.catalog.get(-4242)["excluded"])

    def test_unmatched_shortcut_is_skipped_and_not_retried_this_session(self):
        service = self.make([{"shortcut_id": 3000000002, "name": "Nope", "last_played": 0}], {})
        service.refresh_games(); service.refresh_games()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([g["appid"] for g in service.catalog.games()], [620])

    def test_api_failure_does_not_break_the_steam_scan(self):
        service = self.make([{"shortcut_id": 3000000003, "name": "Heroic", "last_played": 0}], {"Heroic": 1})
        def boom(callback): raise ScreensaverError("down", "steamgriddb_network")
        service._request = boom
        service.refresh_games()
        self.assertEqual([g["appid"] for g in service.catalog.games()], [620])

    def test_two_shortcuts_matching_one_game_keep_the_first(self):
        service = self.make([{"shortcut_id": 3000000004, "name": "Alpha", "last_played": 0},
                             {"shortcut_id": 3000000005, "name": "Bravo", "last_played": 0}], {"Alpha": 7, "Bravo": 7})
        service.refresh_games()
        self.assertEqual(service.catalog.get(-7)["shortcut_id"], 3000000004)


if __name__ == "__main__":
    unittest.main()


class ShortcutRobustnessTests(unittest.TestCase):
    def test_missing_final_end_marker_still_parses(self):
        blob = shortcuts_blob(_i("appid", 5) + _s("AppName", "Heroic"))[:-1]
        self.assertEqual(_read_binary_vdf(blob)["shortcuts"]["0"]["appname"], "Heroic")

    def test_real_steam_layout_with_tags_and_flags(self):
        entry = (_i("appid", -1234567) + _s("AppName", "Heroic") + _s("Exe", '"/usr/bin/heroic"') + _s("StartDir", "/usr/bin")
                 + _s("icon", "") + _s("ShortcutPath", "") + _s("LaunchOptions", "") + _i("IsHidden", 0)
                 + _i("AllowDesktopConfig", 1) + _i("OpenVR", 0) + _i("Devkit", 0) + _s("DevkitGameID", "")
                 + _i("LastPlayTime", 1700000000) + _d("tags", _s("0", "favorite")))
        self.assertEqual(_read_binary_vdf(shortcuts_blob(entry))["shortcuts"]["0"]["tags"]["0"], "favorite")


class ShortcutScanSummaryTests(ShortcutMergeTests):
    def test_summary_counts_and_missing_key_reason(self):
        service = self.make([{"shortcut_id": 3000000001, "name": "Heroic", "last_played": 0},
                             {"shortcut_id": 3000000002, "name": "Nope", "last_played": 0}], {"Heroic": 4242})
        service.refresh_games()
        self.assertEqual(service.status()["shortcut_scan"], dict(found=2, matched=1, unmatched=1, stopped=""))
        service._request = lambda cb: (_ for _ in ()).throw(ScreensaverError("no key", "key_missing"))
        service._shortcut_misses.clear(); service.catalog.data["games"].pop("-4242")
        service.refresh_games()
        self.assertEqual(service.shortcut_scan["stopped"], "key_missing")
        self.assertEqual(service.shortcut_scan["matched"], 0)
