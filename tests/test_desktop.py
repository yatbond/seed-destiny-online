"""Exercise the real GUI controller with a real local room service, no emulator."""
import asyncio
import os
import time
import unittest

from seedrooms.client import RoomClient


@unittest.skipUnless(os.name == "nt", "Windows desktop integration")
class DesktopTests(unittest.TestCase):
    def test_four_players_render_and_leave(self):
        from seedrooms.desktop import Launcher
        app = Launcher()
        app.withdraw()
        guests = []

        def until(predicate):
            deadline = time.monotonic() + 10
            while not predicate() and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            self.assertTrue(predicate(), app.status.get())

        try:
            app.local_server()
            until(lambda: app.server is not None and app.values["port"].get() != "7443")
            app.passcode.set("test-passcode-only")
            app.values["name"].set("Test host")
            app.connect_room(True)
            until(lambda: app.connected)

            async def join_guests():
                for number in range(2, 5):
                    guest = await RoomClient.connect("127.0.0.1", app.server.port)
                    guests.append(guest)
                    await guest.join(app.client.room_code, "test-passcode-only", f"Test player {number}")
                    await guest.ready()

            asyncio.run_coroutine_threadsafe(join_guests(), app.loop).result(timeout=10)
            until(lambda: "Test player 4" in app.seats[3].cget("text"))
            self.assertEqual(app.passcode.get(), "")
            self.assertIn("Test host", app.seats[0].cget("text"))
            app.ready()
            until(lambda: "READY" in app.seats[0].cget("text"))
            app.start_match()
            until(lambda: app.room_state.get("started", False))
            app.leave_room()
            until(lambda: not app.connected)
            self.assertEqual(str(app.launch_button.cget("state")), "disabled")
        finally:
            async def close_guests():
                for guest in guests:
                    await guest.close()
            asyncio.run_coroutine_threadsafe(close_guests(), app.loop).result(timeout=5)
            app.close_app()


if __name__ == "__main__":
    unittest.main()
