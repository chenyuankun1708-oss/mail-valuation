import hashlib
import os
import subprocess
import tempfile
import unittest
import zipfile


class RefreshLogRotationTest(unittest.TestCase):
    def test_rotation_verifies_archive_and_recreates_active_log(self):
        powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(powershell):
            self.skipTest("Windows PowerShell is required")
        with tempfile.TemporaryDirectory() as root:
            log_path = os.path.join(root, "refresh.log")
            content = (b"refresh output\n" * 1000)
            with open(log_path, "wb") as handle:
                handle.write(content)
            script = os.path.abspath(os.path.join("scripts", "rotate_refresh_log.ps1"))
            subprocess.check_call([
                powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                "-LogPath", log_path, "-MaxBytes", "1", "-KeepArchives", "2"
            ])
            self.assertEqual(os.path.getsize(log_path), 0)
            archives = [os.path.join(root, name) for name in os.listdir(root)
                        if name.startswith("refresh-") and name.endswith(".zip")]
            self.assertEqual(len(archives), 1)
            with zipfile.ZipFile(archives[0]) as archive:
                restored = archive.read("refresh.log")
            self.assertEqual(hashlib.sha256(restored).digest(), hashlib.sha256(content).digest())


if __name__ == "__main__":
    unittest.main()
