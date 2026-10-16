"""Exercise the real GUI controller with a real local room service, no emulator."""
import asyncio
import os
import time
import unittest

from seedrooms.client import RoomClient


@unittest.skipUnless(os.name == "nt", "Windows desktop integration")
class DesktopTests(unittest.TestCase):
    def test_join_and_launch_waits_for_authenticated_room(self):
        from seedrooms.desktop import Launcher
        app = Launcher()
        app.withdraw()
        owner = None
        launches = []
        app.launch_game = lambda: launches.append((app.connected, app.client.cabinet_id, app.local_port))
        def until(predicate):
            deadline = time.monotonic() + 10
            while not predicate() and time.monotonic() < deadline:
                app.update()
                time.sleep(.01)
            self.assertTrue(predicate(),app.status.get())
        try:
            app.local_server()
            until(lambda: app.server is not None and app.values['port'].get() != '7443')
            async def create():
                nonlocal owner
                owner = await RoomClient.connect('127.0.0.1',app.server.port)
                await owner.create('join-launch-test')
            asyncio.run_coroutine_threadsafe(create(),app.loop).result(timeout=10)
            app.code.set(owner.room_code)
            app.passcode.set('incorrect-passcode')
            app.join_launch()
            until(lambda:'Could not complete' in app.status.get())
            self.assertEqual(launches,[])
            app.passcode.set('join-launch-test')
            app.join_launch()
            until(lambda:bool(launches))
            self.assertTrue(launches[0][0])
            self.assertEqual(launches[0][1],2)
            self.assertGreater(launches[0][2],0)
        finally:
            if owner:
                asyncio.run_coroutine_threadsafe(owner.close(),app.loop).result(timeout=5)
            app.close_app()

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
            app.team_preference.set("A")
            app.publish_player_status("loading")
            until(lambda: "Preparing / booting" in app.seats[0].cget("text"))
            self.assertIn("Team A", app.seats[0].cget("text"))
            app.publish_player_status("connected")
            until(lambda: "Game link connected" in app.seats[0].cget("text"))
            app.check_room_connection()
            until(lambda: "round trip" in app.connection_status.get())
            self.assertIn("Not a game-latency", app.connection_status.get())
            # Team changes reset readiness for every player; guests ready again.
            async def ready_guests():
                for guest in guests:
                    await guest.ready()
            asyncio.run_coroutine_threadsafe(ready_guests(), app.loop).result(timeout=10)
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
