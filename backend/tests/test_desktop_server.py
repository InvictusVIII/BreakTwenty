import unittest

from app.desktop_server import bind_loopback_socket


class DesktopServerTests(unittest.TestCase):
    def test_operating_system_assigns_a_private_loopback_port(self) -> None:
        listener = bind_loopback_socket("127.0.0.1", 0)
        try:
            host, port = listener.getsockname()[:2]
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
            self.assertLessEqual(port, 65535)
        finally:
            listener.close()

    def test_non_loopback_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact loopback"):
            bind_loopback_socket("0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
