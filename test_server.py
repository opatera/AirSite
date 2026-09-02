import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class LibrarySettingsTests(unittest.TestCase):
    def setUp(self):
        self.original_config = server.CONFIG_FILE
        self.original_music = server.MUSIC_ROOT
        self.original_gba = server.GBA_ROOT
        self.original_save = server.GBA_SAVE_ROOT
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        server.CONFIG_FILE = self.root / "settings.json"

    def tearDown(self):
        server.CONFIG_FILE = self.original_config
        server.MUSIC_ROOT = self.original_music
        server.GBA_ROOT = self.original_gba
        server.GBA_SAVE_ROOT = self.original_save
        self.temp_dir.cleanup()

    def test_save_library_settings_persists_valid_directories(self):
        music = self.root / "music"
        gba = self.root / "roms" / "gba"
        music.mkdir()
        gba.mkdir(parents=True)

        settings = server.save_library_settings(str(music), str(gba))

        self.assertTrue(settings["ready"])
        self.assertEqual(settings["musicDirectory"], str(music.resolve()))
        self.assertEqual(settings["gbaDirectory"], str(gba.resolve()))
        self.assertEqual(json.loads(server.CONFIG_FILE.read_text())["musicDirectory"], str(music.resolve()))
        self.assertEqual(server.GBA_SAVE_ROOT, gba.resolve() / ".airretro-saves")

    def test_save_library_settings_rejects_missing_directory(self):
        music = self.root / "music"
        music.mkdir()

        with self.assertRaisesRegex(ValueError, "GBA ROM directory"):
            server.save_library_settings(str(music), str(self.root / "missing"))

    def test_clean_relative_path_rejects_traversal(self):
        with self.assertRaisesRegex(ValueError, "traversal"):
            server.clean_relative_path("albums/../../outside")

    def test_local_app_url_uses_reserved_localhost_domain(self):
        self.assertEqual(server.local_app_url(8080), "http://airretro.localhost:8080")

    @patch("server.subprocess.Popen")
    @patch("server.shutil.which")
    def test_browser_launcher_prefers_firefox(self, mock_which, mock_popen):
        mock_which.side_effect = lambda browser: "/usr/bin/firefox" if browser == "firefox" else None

        server.open_airretro_browser("http://airretro.localhost:8080")

        mock_popen.assert_called_once_with(
            ["/usr/bin/firefox", "http://airretro.localhost:8080"], start_new_session=True
        )

    @patch("server.ThreadingHTTPServer")
    def test_server_uses_next_port_when_preferred_port_is_busy(self, mock_http_server):
        mock_http_server.side_effect = [OSError(98, "Address already in use"), "server"]

        web_server, port = server.create_local_server("127.0.0.1", 8080)

        self.assertEqual(web_server, "server")
        self.assertEqual(port, 8081)

    @patch("server.threading.Thread")
    @patch("server.platform.system", return_value="Windows")
    def test_system_sampler_is_disabled_outside_linux(self, _mock_platform, mock_thread):
        server.start_system_history_sampler()

        mock_thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
